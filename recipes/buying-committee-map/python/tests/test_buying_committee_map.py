import csv
import json

import pytest

from buying_committee_map.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("buying-committee-map")


def run(name, config="config.json", max_credits=100.0, out=None, ext="csv"):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.{ext}", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def people(row):
    return {p["person_id"]: p for p in row["people"]}


def roles(row):
    return {x["role_id"]: x for x in row["roles"]}


def test_happy_path_account_statuses():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["h1"]["status"] == "committee_complete"
    assert rows["h2"]["status"] == "needs_review"
    assert rows["h3"]["status"] == "company_not_found"
    assert rows["h4"]["duplicate_of"] == "h1" and rows["h4"]["status"] == "committee_complete"
    assert rows["h5"]["status"] == "invalid_input"
    assert rows["h6"]["status"] == "no_people_found"
    assert rows["h7"]["status"] == "gaps_found"
    assert transport.unused() == []
    # The duplicate account reused every response.
    assert len([r for r in transport.sent if r.path == "/company"]) == 4


def test_roles_are_assigned_from_returned_details():
    result, _ = run("happy")
    h1 = by_id(result)["h1"]
    r = roles(h1)
    assert r["economic_buyer"]["status"] == "filled" and r["economic_buyer"]["filled_by"][0]["name"] == "Alex Example"
    assert r["finance_lead"]["status"] == "filled" and r["finance_lead"]["filled_by"][0]["name"] == "Bo Example"
    assert r["it_owner"]["status"] == "possible" and r["it_owner"]["possible"][0]["name"] == "Cam Example"
    p = people(h1)
    assert p["fp900000000001"]["roles_filled"] == ["economic_buyer"]
    assert p["fp900000000003"]["status"] == "unclassified"
    assert "matches_seniority answered unknown" in p["fp900000000003"]["reason"]


def test_union_of_role_values_is_asked_once_per_person():
    _, transport = run("happy")
    qs = [r.json_body for r in transport.sent if r.path == "/decisions" and r.method == "POST"]
    dep = [q["question"] for q in qs if q["tool"] == "matches_department"]
    sen = [q["question"] for q in qs if q["tool"] == "matches_seniority"]
    assert dep[0] == {"person_id": "fp900000000001", "departments": ["Finance", "Engineering & Technical"]}
    assert sen[0] == {"person_id": "fp900000000001",
                      "seniorities": ["C-Suite / Partner", "VP/Director / Head of", "Manager"]}
    # Pat appears twice in one search and is asked about once.
    assert len([q for q in dep if q["person_id"] == "fp900000000006"]) == 1


def test_yes_without_a_value_is_unclassified():
    result, _ = run("happy")
    dee = people(by_id(result)["h2"])["fp900000000004"]
    assert dee["status"] == "unclassified"
    assert dee["roles_possible"] == ["economic_buyer", "finance_lead"]
    assert "returned no seniority value" in dee["reason"]
    assert people(by_id(result)["h2"])["fp900000000005"]["status"] == "no_role"


def test_conflicting_answers_are_surfaced_not_corrected():
    result, _ = run("happy")
    p = people(by_id(result)["h7"])
    assert "returned department 'Accounting', which is not a configured department" in p["fp900000000007"]["notes"][0]
    assert p["fp900000000007"]["status"] == "no_role"
    assert "answered no but returned department 'Finance'" in p["fp900000000008"]["notes"][0]
    assert p["fp900000000008"]["status"] == "no_role"


def test_person_not_found_is_not_an_error():
    result, transport = run("happy")
    ivy = people(by_id(result)["h7"])["fp900000000009"]
    assert ivy["status"] == "person_not_found"
    assert ivy["seniority_check"]["result"] == "not_asked"
    assert result["errors"] == []
    assert not any(r.json_body["question"].get("person_id") == "fp900000000009" and r.json_body["tool"] ==
                   "matches_seniority" for r in transport.sent if r.path == "/decisions" and r.method == "POST")
    assert "1 person(s) were not found by Decisions" in by_id(result)["h7"]["status_reason"]


def test_decision_maker_only_for_people_who_fill_a_role():
    result, transport = run("happy")
    asked = [r.json_body["question"]["person_id"] for r in transport.sent
             if r.path == "/decisions" and r.method == "POST" and r.json_body["tool"] == "is_decision_maker"]
    assert asked == ["fp900000000001", "fp900000000002", "fp900000000006"]
    p = people(by_id(result)["h1"])
    assert p["fp900000000001"]["decision_maker"]["answer"] == "yes"
    assert p["fp900000000002"]["decision_maker"]["result"] == "unknown"
    assert p["fp900000000003"]["decision_maker"] is None
    assert any("is_decision_maker, matches_department, matches_seniority" in c for c in result["coverage"])


def test_search_coverage():
    result, transport = run("happy")
    h1 = by_id(result)["h1"]
    assert h1["search"]["total"] == 6 and h1["search"]["retrieved"] == 4 and h1["search"]["not_checked"] == 2
    assert "2 were not checked" in h1["status_reason"]
    assert "1 search record(s) had no person ID" in h1["status_reason"]
    bodies = [r.json_body for r in transport.sent if r.path == "/people/search"]
    assert bodies[0] == {"companyId": "f900000000001", "selectedCountry": "AU", "from": "0", "size": "5"}
    assert by_id(result)["h2"]["company_id_source"] == "input"
    assert any("success false" in c for c in result["coverage"])


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors", config="errors.config.json")
    rows = by_id(result)
    assert rows["e1"]["status"] == "error" and rows["e1"]["error"]["status"] == 500
    assert rows["e2"]["status"] == "incomplete"
    p = people(rows["e2"])
    assert p["fp900000000001"]["department_check"]["error"]["status"] == 502
    assert p["fp900000000001"]["seniority_check"]["error"]["kind"] == "timeout"
    assert p["fp900000000002"]["department_check"]["result"] == "error"
    assert p["fp900000000002"]["roles_undecided"] == ["economic_buyer", "it_owner"]
    assert roles(rows["e2"])["economic_buyer"]["status"] == "incomplete"
    assert people(rows["e3"])["fp900000000003"]["seniority_check"]["result"] == "not_asked"
    assert rows["e4"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert len(transport.sent) == 12


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", config="budget.config.json", max_credits=2.1)
    rows = by_id(result)
    p = people(rows["row-1"])
    assert p["fp900000000001"]["status"] == "assigned"
    assert p["fp900000000002"]["status"] == "incomplete"
    assert rows["row-1"]["status"] == "incomplete" and rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 5


def test_missing_tool_stops_before_any_search():
    result, transport = run("bad-tool", config="bad-tool.config.json")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert "is_decision_maker" in result["errors"][0]["message"]
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_reasoning_and_search_filters():
    result, transport = run("reasoning", config="reasoning.config.json", ext="json")
    row = result["rows"][0]
    assert row["status"] == "committee_complete"
    search = [r for r in transport.sent if r.path == "/people/search"][0]
    assert search.json_body["department"] == "14" and search.json_body["seniority"] == "3"
    assert all(r.query == {"withReasoning": "true"} for r in transport.sent if r.path == "/decisions" and r.method == "POST")
    dm = row["people"][0]["decision_maker"]
    assert dm["reasoning"]["source"] == "Firmable person profile"
    dm_q = [r.json_body["question"] for r in transport.sent if r.method == "POST" and r.path == "/decisions"
            and r.json_body["tool"] == "is_decision_maker"][0]
    assert dm_q["buyer"] == "CFO and finance team"


def test_outputs_are_safe_and_flat(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Pat</b>" not in html and "&lt;b&gt;Pat&lt;/b&gt;" in html
    assert "Roles by account" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]
    assert rows[0]["role_economic_buyer"] == "Alex Example" and rows[0]["role_it_owner"] == ""
    assert rows[1]["required_roles_possible"] == "Economic buyer; Finance lead"
    assert rows[6]["required_roles_not_observed"] == "Finance lead"
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_plan_upper_bounds():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    # 7 rows: acme twice, bad ID, so 5 unique accounts; 4 of them need a company lookup.
    assert calls["GET /company"].requests == 4
    assert calls["POST /people/search"].requests == 5
    assert calls["POST /decisions"].requests == 75       # 5 accounts x 5 people x 3 questions


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(roles=[]), "roles must be a list of 1 to 10 roles"),
    (lambda c: c["roles"][0].update(seniorities=["Senior"]), "must use Firmable's seniority labels"),
    (lambda c: c["roles"][0].update(seniorities=["c-suite / partner"]), "must use Firmable's seniority labels"),
    (lambda c: c["roles"][0].update(departments=[""]), "departments must hold non-empty text"),
    (lambda c: c["roles"][0].update(departments=[]), "departments must be a non-empty list"),
    (lambda c: c["roles"][1].update(id="economic_buyer"), "is used twice"),
    (lambda c: c["roles"][0].update(colour="orange"), "has unknown key"),
    (lambda c: [r.update(required=False) for r in c["roles"]], "At least one role must be required"),
    (lambda c: c["roles"][0].update(departments=[f"Dept {n}" for n in range(21)]), "more than 20 departments"),
    (lambda c: c.update(search={"selected_country": "GB"}), "search.selected_country must be one of"),
    (lambda c: c.update(search={"max_people": 500}), "max_people must be a whole number from 1 to 100"),
    (lambda c: c.update(search={"seniority": "Senior"}), "search.seniority must be a seniority id"),
    (lambda c: c.update(decision_maker={"seller": "x"}), "decision_maker.seller must be text"),
    (lambda c: c.update(decision_maker={"seller": "Software", "team": "x"}), "decision_maker has unknown key"),
    (lambda c: c.update(with_reasoning="yes"), "with_reasoning must be true or false"),
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
])
def test_invalid_config(mutate, message):
    cfg = json.loads((F / "config.json").read_text())
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_config_defaults():
    cfg = load_config({"roles": [{"id": "cfo", "departments": [" Finance "], "seniorities": ["C-Suite / Partner"]}]})
    assert cfg["roles"][0] == {"id": "cfo", "label": "cfo", "departments": ["Finance"],
                               "seniorities": ["C-Suite / Partner"], "required": True}
    assert cfg["search"] == {"selected_country": "AU", "position": None, "seniority": None, "department": None,
                             "max_people": 25}
    assert cfg["decision_maker"] is None and cfg["with_reasoning"] is False


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 7.0   # 4 companies + 75 answers at 0.04
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
