import csv
import json

import pytest

from contact_health_check.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("contact-health-check")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def checks(row):
    return {c["check_id"]: c for c in row["checks"]}


def test_happy_path_statuses_follow_policy(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["h1"]["status"] == "healthy" and rows["h1"]["channels"] == ["email", "mobile"]
    assert rows["h2"]["status"] == "stale"
    assert rows["h3"]["status"] == "needs_review"            # unknown role is never healthy
    assert rows["h3"]["missing_information"] == ["Still in the expected role", "Mobile on the Australian Do Not Call register"]
    assert rows["h4"]["status"] == "healthy" and rows["h4"]["duplicate_of"] == "h1"
    assert rows["h5"]["status"] == "reach_unconfirmed" and rows["h5"]["phone_channel"] == "held_on_dnc"
    assert rows["h6"]["status"] == "invalid_input" and "12345" in rows["h6"]["status_reason"]
    assert rows["h7"]["status"] == "needs_review"
    assert rows["h8"]["status"] == "needs_review" and "No employment or role check" in rows["h8"]["status_reason"]
    assert rows["h10"]["status"] == "stale"
    assert transport.unused() == []
    # One company lookup per unique domain; the duplicate row sent nothing new.
    assert len([r for r in transport.sent if r.path == "/company"]) == 2
    assert len(transport.sent) == 34


def test_unknown_is_distinct_from_no_and_from_not_checked():
    rows = by_id(run("happy")[0])
    assert checks(rows["h3"])["role"]["answer"] == "unknown" and checks(rows["h3"])["role"]["result"] == "unknown"
    h7 = checks(rows["h7"])
    assert h7["employment"]["result"] == "not_checked" and h7["employment"]["answer"] is None
    assert "no company profile" in rows["h7"]["status_reason"]
    assert checks(rows["h8"])["employment"]["result"] == "not_supplied"


def test_role_check_sends_title_and_resolved_company():
    _, transport = run("happy")
    bodies = [r.json_body for r in transport.sent if r.path == "/decisions" and r.method == "POST"]
    roles = [b["question"] for b in bodies if b["tool"] == "is_contact_current"]
    assert {"person_id": "fp900000000007", "title": "Office Manager"} in roles   # employer had no profile
    works = [b["question"] for b in bodies if b["tool"] == "works_at_company"]
    assert {"work_email": "jordan.example@beta.example", "company_id": "f900000000002"} in works


def test_flags_show_api_facts_without_correcting_them():
    rows = by_id(run("happy")[0])
    assert "Grade returned: Unsure." in rows["h2"]["flags"][0]
    assert "as at 2026-10-15" in rows["h5"]["flags"][0] and "not a live or legal check" in rows["h5"]["flags"][0]
    assert rows["h10"]["flags"][0].startswith("works_at_company answered yes and is_contact_current answered no")
    assert checks(rows["h10"])["role"]["details"]["current_title"] == "Chief Data Officer"


def test_last_refresh_information_is_kept():
    rows = by_id(run("happy")[0])
    email = checks(rows["h1"])["email"]
    assert email["as_at"] == "2026-10-30" and email["last_refreshed"] == "2026-11-01"
    assert "Work email likely to deliver: as at 2026-10-30, last refreshed 2026-11-01" in rows["h1"]["last_refresh"]


def test_model_tools_are_named_in_coverage():
    result, _ = run("happy")
    assert any("is_contact_current" in c and "decision model" in c for c in result["coverage"])
    assert any("not live mailbox or telephone checks" in c for c in result["coverage"])


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    e1 = checks(rows["e1"])
    assert e1["employment"]["error"]["status"] == 502 and e1["role"]["error"]["kind"] == "timeout"
    assert rows["e1"]["status"] == "incomplete"
    assert checks(rows["e2"])["email"]["error"]["kind"] == "invalid_response"
    assert rows["e2"]["status"] == "healthy" and rows["e2"]["channels"] == ["mobile"]
    assert checks(rows["e3"])["email"]["result"] == "not_asked" and rows["e3"]["status"] == "incomplete"
    assert rows["e4"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 11


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=1.35)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "healthy"
    assert rows["row-2"]["status"] == "incomplete"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 5


def test_fetch_person_uses_person_id_and_handles_missing_profile():
    result, transport = run("fetch-person", config="fetch-person.config.json")
    rows = by_id(result)
    assert rows["f1"]["status"] == "healthy" and rows["f1"]["person"]["id"] == "fp900000000001"
    assert rows["f1"]["phone_channel"] == "unknown"
    assert rows["f2"]["status"] == "person_not_found" and rows["f2"]["checks"] == []
    assert transport.unused() == []


def test_reachability_off_gives_current():
    result, _ = run("current-only", config="current-only.config.json")
    assert result["rows"][0]["status"] == "current"
    assert [c["check_id"] for c in result["rows"][0]["checks"]] == ["employment", "role"]


def test_missing_tool_stops_before_paid_calls():
    result, transport = run("bad-tool")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert any("is_mobile_on_dnc" in e["message"] for e in result["errors"])
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html and "<b>Data</b>" not in html and "&lt;b&gt;Data&lt;/b&gt;" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8", "h10"]
    assert rows[0]["employment"] == "pass(yes)" and rows[4]["dnc"] == "fail(yes)"
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_plan_counts_unique_questions():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 2
    assert "GET /people" not in calls
    # h1 5, h2 4, h3 5, h4 0 (duplicate), h5 5, h6 0 (invalid), h7 5, h8 3, h10 5
    assert calls["POST /decisions"].requests == 32


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(checks=[]), "checks must be a non-empty list"),
    (lambda c: c.update(checks=["email", "phone"]), "checks\\[2\\] must be one of"),
    (lambda c: c.update(checks=["email", "email"]), "'email' is listed twice"),
    (lambda c: c.update(fetch_person="yes"), "fetch_person must be true or false"),
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
    assert plan_json["published_estimate_upper_bound"] == 4.56   # 2 profiles + 32 answers with reasoning
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_rows_without_an_identifier(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name,expected_title\nAlex Example,Manager\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2
