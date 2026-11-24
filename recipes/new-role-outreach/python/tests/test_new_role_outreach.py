import csv
import json

import pytest

from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures
from new_role_outreach.recipe import RECIPE, load_config, months_before, plan

F = fixtures_dir("new-role-outreach")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def people(row):
    return {p["person_id"]: p for p in row["people"]}


def test_happy_path_statuses_follow_rules(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["a1"]["status"] == "eligible_contacts_found" and rows["a1"]["eligible_count"] == 1
    a1 = people(rows["a1"])
    assert a1["fp900000000001"]["status"] == "eligible"
    assert a1["fp900000000002"]["status"] == "not_eligible"
    assert a1["fp900000000003"]["status"] == "needs_review"   # unknown is never eligible
    assert rows["a2"]["status"] == "no_people_found"
    assert rows["a3"]["duplicate_of"] == "a1"
    assert rows["c1"]["status"] == "eligible" and rows["c2"]["status"] == "needs_review"
    assert rows["c3"]["status"] == "invalid_input" and rows["a4"]["status"] == "invalid_input"
    assert rows["a5"]["status"] == "company_not_found"
    assert rows["c4"]["status"] == "eligible" and rows["c5"]["status"] == "eligible"
    assert result["summary"]["unique_eligible_people"] == 4
    assert transport.unused() == []
    assert len(transport.sent) == 30


def test_search_limits_and_skipped_records_are_reported():
    rows = by_id(run("happy")[0])
    search = rows["a1"]["search"]
    assert search["total"] == 6 and search["retrieved"] == 4 and search["not_retrieved"] == 2
    assert search["skipped"][0]["id"] == "f900000000099"
    assert search["request"] == {"companyId": "f900000000001", "selectedCountry": "AU", "from": "0", "size": "4",
                                 "seniority": "4"}
    assert "2 more matching person(s) were not retrieved" in rows["a1"]["status_reason"]


def test_no_match_search_counts_as_zero_people():
    result, _ = run("happy")
    a2 = by_id(result)["a2"]
    assert a2["search"]["no_match_response"] is True and a2["people"] == []
    assert any("success false" in c for c in result["coverage"])
    assert result["errors"] == []


def test_contact_details_only_for_eligible_people():
    result, transport = run("happy")
    fetched = sorted(r.query["id"] for r in transport.sent if r.path == "/people")
    assert fetched == ["fp900000000001", "fp900000000010", "fp900000000011", "fp900000000012"]
    rows = by_id(result)
    assert people(rows["a1"])["fp900000000002"]["contact"] is None
    assert people(rows["c1"])["fp900000000010"]["contact"]["phones"] == [{"value": "+61 400 000 010", "is_dnd": True}]
    assert "no person profile" in people(rows["c4"])["fp900000000011"]["contact_note"]


def test_source_dates_and_conflicts_are_kept_as_returned():
    rows = by_id(run("happy")[0])
    c1 = people(rows["c1"])["fp900000000010"]
    assert [t["started"] for t in c1["triggers"]] == ["2026-09", "2026-10", "2026-09"]
    assert c1["conflicts"][0].startswith("Triggers that answered yes returned different start dates")
    c4 = people(rows["c4"])["fp900000000011"]
    assert "before the window start 2026-05-24" in c4["conflicts"][0]
    c5 = people(rows["c5"])["fp900000000012"]
    assert c5["conflicts"] == ["has_changed_jobs_since answered yes but is_new_to_role answered no. Both answers "
                               "are shown as returned."]


def test_triggers_send_window_inputs():
    _, transport = run("happy")
    questions = {b["tool"]: b["question"] for b in (r.json_body for r in transport.sent if r.path == "/decisions"
                                                     and r.method == "POST")}
    assert questions["is_new_to_role"]["within_months"] == 6
    assert questions["is_new_to_role"]["count_internal_moves"] is True
    assert questions["has_changed_jobs_since"]["since"] == "2026-05-24"


def test_coverage_names_model_tools_and_intent_limit():
    result, _ = run("happy")
    assert any("is_new_exec" in c and "decision model" in c for c in result["coverage"])
    assert any("does not show buying intent" in c for c in result["coverage"])


def test_executive_only_uses_new_exec_alone():
    result, transport = run("exec-only", config="exec-only.config.json")
    rows = by_id(result)
    assert rows["row-1"]["status"] == "not_eligible"
    assert "executive_only counts only New executive" in rows["row-1"]["status_reason"]
    assert rows["row-2"]["status"] == "eligible"
    assert "fetch_contact_details is false" in rows["row-2"]["people"][0]["contact_note"]
    assert result["summary"]["window_start"] == "2026-02-28"
    assert not [r for r in transport.sent if r.path == "/people"]


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    x1 = rows["x1"]["people"][0]["triggers"]
    assert x1[0]["error"]["status"] == 500 and x1[1]["error"]["kind"] == "timeout"
    assert rows["x1"]["status"] == "incomplete"
    assert rows["x2"]["status"] == "error"
    assert rows["x3"]["status"] == "eligible" and "could not be retrieved" in rows["x3"]["people"][0]["contact_note"]
    x4 = rows["x4"]["people"][0]["triggers"]
    assert x4[0]["error"]["kind"] == "invalid_response" and x4[2]["result"] == "not_asked"
    assert rows["x5"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 4
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 11


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=1.2)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "eligible"
    assert rows["row-1"]["people"][0]["contact_note"] == "Not retrieved, because the run stopped."
    assert rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 3


def test_changed_tool_stops_before_paid_calls():
    result, transport = run("bad-tool")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert any("count_internal_moves" in e["message"] for e in result["errors"])
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html and "<b>Alex</b>" not in html and "&lt;b&gt;Alex&lt;/b&gt;" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows][:5] == ["a1", "a1", "a1", "a2", "a3"]
    c1 = next(r for r in rows if r["row_id"] == "c1")
    assert c1["phones"] == "'+61 400 000 010 (do not call flag: yes)"   # leading + neutralised for spreadsheets
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_months_before_clamps_to_month_end():
    assert months_before("2026-11-24", 6) == "2026-05-24"
    assert months_before("2026-03-31", 1) == "2026-02-28"
    assert months_before("2026-01-15", 13) == "2024-12-15"


def test_plan_counts_upper_bounds():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 2
    assert calls["POST /people/search"].requests == 3
    assert calls["POST /decisions"].requests == 48      # (3 accounts x 4 people + 4 contacts) x 3 triggers
    assert calls["GET /people"].requests == 16


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(window_months=0), "window_months must be a whole number from 1 to 60"),
    (lambda c: c.update(window_months=48), "36 or less when new_exec"),
    (lambda c: c.update(triggers=["new_to_role", "promoted"]), "triggers\\[2\\] must be one of"),
    (lambda c: c.update(triggers=["new_to_role"], executive_only=True), "executive_only needs new_exec"),
    (lambda c: c.update(executive_seniorities=["CEO"]), "executive_seniorities must be"),
    (lambda c: c.update(evaluation_date="24/11/2026"), "evaluation_date must be a date"),
    (lambda c: c["search"].update(seniority=4), "search.seniority must be one seniority id"),
    (lambda c: c["search"].update(max_people=500), "search.max_people"),
    (lambda c: c["search"].update(name="Alex"), "Unknown search key"),
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
])
def test_invalid_config(mutate, message):
    cfg = json.loads((F / "config.json").read_text())
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 21.84   # 2 companies + 48 answers + 16 people
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_rows_without_account_or_contact(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name\nAcme\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2


def test_decisions_person_not_found_stops_that_contact():
    result, t = run("person-not-found")
    rows = {r["row_id"]: r for r in result["rows"]}
    assert rows["c1"]["status"] == "person_not_found"
    assert rows["c1"]["people"][0]["status"] == "person_not_found"
    assert result["errors"] == []
    assert sum(1 for r in t.sent if r.json_body and r.json_body["question"].get("work_email") == "nobody@acme.example") == 1
    assert rows["c2"]["status"] in ("not_eligible", "needs_review")
