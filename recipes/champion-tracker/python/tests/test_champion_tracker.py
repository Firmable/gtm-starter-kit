import csv
import json

import pytest

from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures
from champion_tracker.recipe import RECIPE, fill_templates, load_config, plan

F = fixtures_dir("champion-tracker")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def people(row):
    return {p["person_id"]: p for p in row["people"]}


def test_happy_path_statuses_follow_rules():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["a1"]["status"] == "champions_found" and rows["a1"]["champion_count"] == 1
    a1 = people(rows["a1"])
    assert a1["fp900000000001"]["status"] == "champion"
    assert a1["fp900000000002"]["status"] == "not_alumni"
    assert a1["fp900000000003"]["status"] == "unknown"      # unknown is never a no
    assert rows["a2"]["status"] == "no_people_found"
    assert rows["a3"]["duplicate_of"] == "a1"
    assert rows["a4"]["status"] == "invalid_input"
    assert rows["a5"]["status"] == "company_not_found"
    assert rows["a8"]["status"] == "no_champions_found"
    assert rows["a9"]["status"] == "needs_review"
    assert people(rows["a9"])["fp900000000010"]["status"] == "person_not_found"
    assert rows["a10"]["status"] == "no_people_found"
    assert result["summary"]["unique_champions"] == 3
    assert result["summary"]["people_checked"] == 10
    assert result["errors"] == []
    assert transport.unused() == []
    assert len(transport.sent) == 29


def test_question_sends_past_companies_and_include_current():
    _, transport = run("happy")
    q = next(r.json_body for r in transport.sent if r.path == "/decisions" and r.method == "POST")
    assert q["tool"] == "previously_worked_at"
    assert q["question"] == {"person_id": "fp900000000001", "companies": ["Delta Example", "f900000000050"],
                             "include_current": False}
    company_lookups = [r.query for r in transport.sent if r.path == "/company"]
    assert {"id": "f900000000002"} in company_lookups and {"fqdn": "acme.example"} in company_lookups


def test_details_are_kept_as_returned():
    rows = by_id(run("happy")[0])
    alex = people(rows["a1"])["fp900000000001"]
    assert alex["alumni"] == {"employer": "Delta Example", "years_there": 3, "title": "Operations Lead",
                              "past_employers_checked": 4}
    assert people(rows["a7"])["fp900000000006"]["alumni"]["years_there"] == 2.5


def test_not_checked_people_are_counted():
    rows = by_id(run("happy")[0])
    search = rows["a1"]["search"]
    assert search["total"] == 6 and search["retrieved"] == 4 and search["not_checked"] == 2
    assert search["skipped"][0]["id"] == "f900000000099"
    assert "2 more matching person(s) were not checked" in rows["a1"]["status_reason"]
    assert "No champion was observed among the 2 person(s) checked" in rows["a8"]["status_reason"]


def test_contact_details_only_for_champions():
    result, transport = run("happy")
    fetched = sorted(r.query["id"] for r in transport.sent if r.path == "/people")
    assert fetched == ["fp900000000001", "fp900000000004", "fp900000000006"]
    rows = by_id(result)
    assert people(rows["a1"])["fp900000000002"]["contact"] is None
    assert people(rows["a7"])["fp900000000006"]["contact"]["phones"] == [{"value": "+61 400 000 001", "is_dnd": True}]
    assert "no person profile" in people(rows["a6"])["fp900000000004"]["contact_note"]


def test_openers_are_filled_from_facts_or_skipped():
    rows = by_id(run("happy")[0])
    alex = people(rows["a1"])["fp900000000001"]
    assert alex["suggested_openers"][0]["text"] == ("Hi Alex, I noticed you spent time at Delta Example. Would you "
                                                    "introduce us to the team at Acme key account?")
    assert alex["skipped_openers"] == []
    dana = people(rows["a6"])["fp900000000004"]
    assert [o["template"] for o in dana["suggested_openers"]] == [3]
    assert dana["skipped_openers"] == [{"template": 1, "missing": ["first_name", "past_employer"]},
                                       {"template": 2, "missing": ["past_employer"]}]
    assert people(rows["a1"])["fp900000000002"]["suggested_openers"] == []
    assert "{account_name}" not in json.dumps(rows["a3"])


def test_conflicts_are_surfaced_not_corrected():
    rows = by_id(run("happy")[0])
    frankie = people(rows["a7"])["fp900000000006"]
    assert frankie["status"] == "champion"
    assert frankie["notes"][0].startswith("previously_worked_at returned the account itself (Delta Example)")
    assert "Delta Example Pty Ltd" in frankie["notes"][1]
    assert "returned no past employer" in people(rows["a6"])["fp900000000004"]["notes"][0]
    assert "answered no but returned a past employer" in people(rows["a6"])["fp900000000005"]["notes"][0]


def test_include_current_is_passed_through():
    result, transport = run("include-current", config="include-current.config.json")
    row = result["rows"][0]
    assert row["status"] == "champions_found" and row["people"][0]["notes"] == []
    assert transport.sent[-1].json_body["question"]["include_current"] is True
    assert transport.sent[-1].query == {}
    assert row["people"][0]["contact_note"] == "Not retrieved, because fetch_contact_details is false."


def test_coverage_states_limits():
    result, _ = run("happy")
    cov = " ".join(result["coverage"])
    assert "no past-employer filter" in cov and "does not show buying intent" in cov
    assert "names Firmable holds" in cov and "not observed in retrieved data" in cov
    assert any("success false" in c for c in result["coverage"])


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["x1"]["status"] == "error"
    x2 = people(rows["x2"])
    assert x2["fp900000000020"]["error"]["status"] == 500
    assert x2["fp900000000021"]["error"]["kind"] == "timeout"
    assert x2["fp900000000022"]["status"] == "error"
    assert rows["x2"]["status"] == "incomplete"
    assert rows["x3"]["status"] == "champions_found"
    assert "could not be retrieved" in rows["x3"]["people"][0]["contact_note"]
    assert people(rows["x4"])["fp900000000025"]["status"] == "not_asked"
    assert rows["x5"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 5
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 14


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=2.5)
    rows = by_id(result)
    assert rows["b1"]["status"] == "champions_found"
    assert people(rows["b1"])["fp900000000027"]["status"] == "not_asked"
    assert rows["b2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert "Stopped before calling GET /people" in people(rows["b1"])["fp900000000026"]["contact_note"]
    assert len(transport.sent) == 4


def test_changed_tool_stops_before_paid_calls():
    result, transport = run("bad-tool")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert any("include_current" in e["message"] for e in result["errors"])
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html and "<b>Alex</b>" not in html and "&lt;b&gt;Alex&lt;/b&gt;" in html
    assert 'class="template"' in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows][:5] == ["a1", "a1", "a1", "a2", "a3"]
    assert rows[0]["suggested_openers"].startswith("Hi Alex")
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_fill_templates():
    filled, skipped = fill_templates(["{title} {title}", "{first_name}!"], {"title": "CTO", "first_name": None})
    assert filled == [{"template": 1, "text": "CTO CTO"}]
    assert skipped == [{"template": 2, "missing": ["first_name"]}]


def test_plan_counts_upper_bounds():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 8
    assert calls["POST /people/search"].requests == 8
    assert calls["POST /decisions"].requests == 32     # 8 unique accounts x 4 people
    assert calls["GET /people"].requests == 32


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.pop("past_companies"), "past_companies is required"),
    (lambda c: c.update(past_companies=[]), "past_companies must be a list of 1 to 50"),
    (lambda c: c.update(past_companies=["x"] * 51), "past_companies must be a list of 1 to 50"),
    (lambda c: c.update(past_companies=["Delta", " "]), "past_companies\\[2\\] must be text"),
    (lambda c: c.update(past_companies=["Delta", "delta"]), "past_companies\\[2\\] 'delta' is listed twice"),
    (lambda c: c.update(include_current="yes"), "include_current must be true or false"),
    (lambda c: c.update(opener_templates="Hi"), "opener_templates must be a list"),
    (lambda c: c.update(opener_templates=["Hi {name}"]), "unknown placeholder\\(s\\): \\{name\\}"),
    (lambda c: c["search"].update(seniority=4), "search.seniority must be one seniority id"),
    (lambda c: c["search"].update(max_people=500), "search.max_people"),
    (lambda c: c["search"].update(selected_country="GB"), "search.selected_country"),
    (lambda c: c["search"].update(name="Alex"), "Unknown search key"),
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
])
def test_invalid_config(mutate, message):
    cfg = json.loads((F / "config.json").read_text())
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_defaults():
    cfg = load_config({"past_companies": ["Delta"]})
    assert cfg["include_current"] is False and cfg["with_reasoning"] is False
    assert cfg["fetch_contact_details"] is False and cfg["opener_templates"] == []
    assert cfg["search"] == {"selected_country": "AU", "position": None, "seniority": None, "department": None,
                             "max_people": 25}


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 42.56   # 8 companies + 32 answers + 32 people
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_rows_without_account(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name\nAcme\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2
