import csv
import json

import pytest

from account_contact_finder.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("account-contact-finder")


def run(name, config="config.json", max_credits=100.0, out=None, ext="csv"):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.{ext}", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def cands(row):
    return {c["person_id"]: c for c in row["candidates"]}


def test_happy_path_account_statuses(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["r1"]["status"] == "contacts_selected"
    assert rows["r2"]["status"] == "no_contacts_selected"
    assert rows["r3"]["status"] == "company_not_found" and rows["r3"]["searches"] == []
    assert rows["r4"]["duplicate_of"] == "r1" and rows["r4"]["status"] == "contacts_selected"
    assert rows["r5"]["status"] == "invalid_input"
    assert rows["r6"]["status"] == "no_candidates"
    assert rows["r7"]["status"] == "contacts_selected"
    assert transport.unused() == []
    # The duplicate account reused every response.
    assert len([r for r in transport.sent if r.path == "/company"]) == 4


def test_selection_policy_and_limits():
    result, transport = run("happy")
    c = cands(by_id(result)["r1"])
    assert list(c) == ["fp900000000001", "fp900000000002", "fp900000000003", "fp900000000004", "fp900000000005"]
    assert c["fp900000000001"]["selection"] == "selected" and c["fp900000000001"]["details_status"] == "fetched"
    assert c["fp900000000002"]["selection"] == "rejected" and c["fp900000000002"]["details_status"] == "not_selected"
    assert "1 optional check(s) answered unknown" in c["fp900000000003"]["selection_reason"]
    assert c["fp900000000004"]["details_status"] == "not_fetched_limit"
    assert c["fp900000000005"]["selection"] == "not_checked" and c["fp900000000005"]["checks"] == []
    assert c["fp900000000003"]["matched_queries"] == ["finance_exec", "finance_heads"]
    # Only selected people within the limit get GET /people.
    assert sorted(r.query["id"] for r in transport.sent if r.path == "/people") == [
        "fp900000000001", "fp900000000003", "fp900000000007"]


def test_unknown_is_needs_review_not_rejected():
    result, _ = run("happy")
    c = cands(by_id(result)["r2"])["fp900000000006"]
    assert c["selection"] == "needs_review"
    senior = {x["check_id"]: x for x in c["checks"]}["senior"]
    assert senior["answer"] == "unknown" and senior["result"] == "unknown"


def test_search_coverage_and_paging():
    result, transport = run("happy")
    r1 = by_id(result)["r1"]
    s = {x["query_id"]: x for x in r1["searches"]}
    assert s["finance_exec"]["total"] == 4 and s["finance_exec"]["retrieved"] == 4
    assert s["finance_exec"]["pages_fetched"] == 2 and s["finance_exec"]["complete"] is True
    bodies = [r.json_body for r in transport.sent if r.path == "/people/search"]
    assert all(set(b) <= {"companyId", "selectedCountry", "position", "seniority", "department", "from", "size"}
               for b in bodies)
    assert bodies[0]["seniority"] == "3" and bodies[0]["department"] == "14"
    r2 = by_id(result)["r2"]
    assert r2["company_id_source"] == "input"
    assert r2["searches"][0]["status"] == "no_matches" and r2["searches"][0]["total"] == 0
    assert any("success false" in c for c in result["coverage"])
    assert "1 search record(s) had no person ID" in by_id(result)["r7"]["status_reason"]


def test_disagreements_and_company_association_are_surfaced():
    result, _ = run("happy")
    c = cands(by_id(result)["r1"])
    assert "filtered on seniority C-Suite / Partner" in c["fp900000000002"]["notes"][0]
    cam = c["fp900000000003"]
    assert cam["current_company_matches_account"] == "no" and "f900000000099" in cam["notes"][0]
    alex = c["fp900000000001"]
    assert alex["contact"]["phones"] == [{"value": "+61 400 000 101", "is_dnd": False},
                                         {"value": "+61 400 000 102", "is_dnd": True}]
    assert by_id(result)["r7"]["candidates"][0]["details_status"] == "not_found"


def test_works_at_company_gets_the_account_id():
    _, transport = run("happy")
    q = [r.json_body["question"] for r in transport.sent
         if r.path == "/decisions" and r.method == "POST" and r.json_body["tool"] == "works_at_company"]
    assert q[0] == {"person_id": "fp900000000001", "company_id": "f900000000001"}


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors", config="errors.config.json")
    rows = by_id(result)
    assert rows["e1"]["status"] == "incomplete"
    assert rows["e1"]["searches"][0]["status"] == "error" and rows["e1"]["searches"][0]["error"]["status"] == 500
    checks = {x["check_id"]: x for x in rows["e1"]["candidates"][0]["checks"]}
    assert checks["in_finance"]["error"]["status"] == 502 and checks["senior"]["error"]["kind"] == "timeout"
    assert rows["e2"]["candidates"][0]["checks"][0]["error"]["kind"] == "invalid_response"
    assert rows["e3"]["candidates"][0]["checks"][1]["result"] == "not_asked"
    assert rows["e4"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert len(transport.sent) == 15


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", config="budget.config.json", max_credits=2.1)
    rows = by_id(result)
    c = cands(rows["row-1"])
    assert c["fp900000000001"]["selection"] == "selected" and c["fp900000000001"]["details_status"] == "not_requested"
    assert c["fp900000000002"]["selection"] == "incomplete"
    assert rows["row-1"]["status"] == "incomplete" and rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 6


def test_bad_tool_stops_before_any_search():
    result, transport = run("bad-tool", config="bad-tool.config.json")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    msgs = " ".join(e["message"] for e in result["errors"])
    assert "is_imaginary" in msgs and "colour" in msgs and "not a person" in msgs
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_no_checks_selects_on_search_alone():
    result, transport = run("nochecks", config="nochecks.config.json", ext="json")
    c = cands(result["rows"][0])
    assert c["fp900000000001"]["selection_reason"].startswith("Selected on the search criteria alone")
    assert c["fp900000000008"]["details_status"] == "not_fetched_limit"
    assert [r.path for r in transport.sent] == ["/people/search", "/people"]


def test_outputs_are_safe_and_flat(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Pat</b>" not in html and "&lt;b&gt;Pat&lt;/b&gt;" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    # One line per candidate, and one line for each account without candidates.
    assert [r["row_id"] for r in rows] == ["r1"] * 5 + ["r2", "r3"] + ["r4"] * 5 + ["r5", "r6", "r7"]
    # The shared CSV writer prefixes values starting with + with an apostrophe so spreadsheets do not run them.
    assert rows[0]["phones"] == "'+61 400 000 101; '+61 400 000 102 (do not call)"
    assert rows[2]["phones"] == "'+61 400 000 103 (do-not-call status not returned)"
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_plan_upper_bounds():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    # 7 rows: acme twice, bad ID, so 5 unique accounts; 4 of them need a company lookup.
    assert calls["GET /company"].requests == 4
    assert calls["POST /people/search"].requests == 20     # 5 accounts x 2 searches x 2 pages
    assert calls["POST /decisions"].requests == 80         # 5 accounts x 4 candidates x 4 checks
    assert calls["GET /people"].requests == 10             # 5 accounts x 2 profiles


def test_labels_and_ids_are_both_accepted():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    q = cfg["search"]["queries"]
    assert (q[0]["department"], q[0]["seniority"]) == ("14", "3")
    assert (q[1]["department"], q[1]["seniority"]) == ("14", "4")


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["search"].update(queries=[]), "search.queries must be a non-empty list"),
    (lambda c: c["search"]["queries"][0].update(seniority="Senior"), "seniority must be a seniority id 1 to 7"),
    (lambda c: c["search"]["queries"][0].update(department="99"), "department must be a department id 1 to 17"),
    (lambda c: c["search"]["queries"][0].update(name="Alex"), "has unknown key"),
    (lambda c: c["search"].update(country="GB"), "search.country must be one of"),
    (lambda c: c["search"].update(page_size=500), "page_size must be a whole number from 1 to 100"),
    (lambda c: c["search"]["queries"].append({"id": "empty"}), "needs at least one of position, seniority"),
    (lambda c: c["checks"][0].update(want="maybe"), "want must be yes or no"),
    (lambda c: c.update(policy={"min_optional_passes": 3}), "only 2 optional"),
    (lambda c: c.update(max_details_per_account=-1), "max_details_per_account must be a whole number"),
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
    assert plan_json["published_estimate_upper_bound"] == 17.2   # 4 companies + 80 answers + 10 profiles
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_bad_input(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name\nAcme\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2
