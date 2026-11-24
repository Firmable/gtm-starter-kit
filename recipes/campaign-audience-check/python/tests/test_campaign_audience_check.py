import csv
import json

import pytest

from campaign_audience_check.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("campaign-audience-check")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def results_of(row):
    return [c["result"] for c in row["campaigns"]]


def test_happy_path_statuses_follow_policy():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["r1"]["status"] == "in_campaigns"
    assert results_of(rows["r1"]) == ["include", "exclude", "review"]
    assert rows["r1"]["needs_review"] == ["Enterprise payroll"]
    assert rows["r2"]["status"] == "no_campaign" and rows["r2"]["asked_with"] == {"company_id": "f900000000002"}
    assert rows["r3"]["status"] == "needs_review"           # unknown is never an exclude
    assert rows["r4"]["status"] == "in_campaigns" and rows["r4"]["duplicate_of"] == "r1"
    assert rows["r5"]["status"] == "invalid_input"
    assert rows["r7"]["status"] == "invalid_input" and "'maybe'" in rows["r7"]["status_reason"]
    assert rows["r8"]["status"] == "invalid_input" and "'f123'" in rows["r8"]["status_reason"]
    assert rows["r9"]["status"] == "in_campaigns"
    assert rows["r10"]["status"] == "no_campaign"
    assert transport.unused() == []
    # 1 tool list + 5 unique accounts x 3 campaigns; the duplicate reused responses.
    assert len(transport.sent) == 16


def test_existing_customer_is_excluded_before_any_call():
    result, transport = run("happy")
    r6 = by_id(result)["r6"]
    assert r6["status"] == "excluded_customer" and r6["existing_customer"] is True
    assert results_of(r6) == ["excluded_customer"] * 3
    assert not any((r.json_body or {}).get("question", {}).get("domain") == "delta.example" for r in transport.sent)
    assert result["summary"]["campaign_counts"]["au_fleet"]["excluded_customer"] == 1


def test_reason_and_filters_are_kept_as_returned():
    result, _ = run("happy")
    rows = by_id(result)
    nz = rows["r9"]["campaigns"][1]
    assert nz["answer"] == "yes" and nz["reasoning"]["reason"] == "Sells through <b>several</b> stores in New Zealand."
    assert nz["credits"] == 0.08 and nz["confidence"] == 0.9
    au = rows["r10"]["campaigns"][0]
    assert au["result"] == "exclude" and au["filters"] == {"countries": "passed", "min_staff": "passed"}
    assert au["details"] == {"filters": au["filters"]}


def test_model_tool_is_named_in_coverage():
    result, _ = run("happy")
    assert any("decision model" in c and "matches_campaign_audience" in c for c in result["coverage"])


def test_fetch_company_for_display_and_missing_profile():
    result, transport = run("fetch", config="fetch.config.json")
    rows = by_id(result)
    assert rows["f1"]["company_lookup"] == "found" and rows["f1"]["asked_with"] == {"company_id": "f900000000001"}
    assert rows["f2"]["company_lookup"] == "not_found" and rows["f2"]["asked_with"] == {"domain": "ghost.example"}
    assert rows["f2"]["status"] == "needs_review"
    # Conflicting classification is shown as returned, not corrected.
    assert rows["f3"]["company"]["company_type"] == "Government agency" and rows["f3"]["company"]["year_founded"] == 2031
    assert rows["f4"]["status"] == "excluded_customer"
    assert transport.unused() == []
    assert len([r for r in transport.sent if r.path == "/company"]) == 3


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    e1 = rows["e1"]["campaigns"]
    assert e1[0]["error"]["status"] == 502 and e1[1]["error"]["kind"] == "timeout"
    assert rows["e1"]["status"] == "in_campaigns" and "have no answer" in rows["e1"]["status_reason"]
    e2 = rows["e2"]["campaigns"]
    assert e2[0]["error"]["kind"] == "invalid_response"
    assert e2[1]["result"] == "error" and e2[1]["error"]["code"] == "decision_failed"
    assert rows["e2"]["status"] == "incomplete"
    assert results_of(rows["e3"]) == ["error", "not_asked", "not_asked"]
    assert rows["e4"]["status"] == "not_processed"
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 8


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=1.2)
    rows = by_id(result)
    assert results_of(rows["row-1"]) == ["include", "exclude", "error"]
    assert rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 3


def test_missing_tool_stops_before_paid_calls():
    result, transport = run("bad-tool")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert any("matches_campaign_audience" in e["message"] for e in result["errors"])
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_live_schema_drift_stops_before_paid_calls():
    result, transport = run("schema-drift")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert "revenue_bands" in result["errors"][0]["message"]
    assert len(transport.sent) == 1


def test_tool_list_error_stops_the_run():
    result, _ = run("catalogue-error")
    assert [r["status"] for r in result["rows"]] == ["not_processed", "not_processed"]
    assert result["errors"][0]["status"] == 500


def test_existing_customers_asked_when_exclusion_is_off():
    result, transport = run("no-reasoning", config="no-reasoning.config.json")
    rows = by_id(result)
    assert rows["n1"]["status"] == "in_campaigns" and rows["n1"]["existing_customer"] is True
    assert rows["n1"]["campaigns"][0]["reasoning"] is None
    assert all(r.query == {} for r in transport.sent if r.path == "/decisions" and r.method == "POST")


def test_outputs_are_safe_and_have_a_column_per_campaign(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html and "<b>several</b>" not in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == [f"r{i}" for i in range(1, 11)]
    assert rows[0]["campaign_au_fleet"] == "include" and rows[0]["campaign_enterprise_payroll"] == "review"
    assert rows[0]["campaign_au_fleet_reason"] == "Runs a delivery fleet in Australia."
    assert rows[0]["company_id"] == "f900000000001"
    assert rows[8]["account_name"].startswith("'=")
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_plan_counts_unique_asked_accounts():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert "GET /company" not in calls
    assert calls["POST /decisions"].requests == 15   # 10 rows, 1 duplicate, 3 invalid, 1 customer
    fetch = load_config(json.loads((F / "fetch.config.json").read_text()))
    calls = {c.endpoint: c for c in plan(RECIPE.read_input(F / "fetch.input.csv"), fetch)}
    assert calls["GET /company"].requests == 3 and calls["POST /decisions"].requests == 6


def _cfg():
    return json.loads((F / "config.json").read_text())


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(campaigns=[]), "campaigns must be a non-empty list"),
    (lambda c: c.update(campaigns=c["campaigns"] * 2), "the most allowed is 5"),
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
    (lambda c: c["campaigns"][1].update(id="au_fleet"), "used twice"),
    (lambda c: c["campaigns"][0].update(tone="loud"), "unknown key(s): tone"),
    (lambda c: c["campaigns"][0]["campaign"].update(colour="orange"), "does not list: colour"),
    (lambda c: c["campaigns"][0]["campaign"].pop("message"), "message is required"),
    (lambda c: c["campaigns"][0]["campaign"].update(message="hi"), "message is required"),
    (lambda c: c["campaigns"][0]["campaign"].update(min_staff=-1), "min_staff must be a whole number"),
    (lambda c: c["campaigns"][0]["campaign"].update(max_staff=10), "min_staff is more than"),
    (lambda c: c["campaigns"][0]["campaign"].update(staff_in="GB"), "staff_in must be one of"),
    (lambda c: c["campaigns"][0]["campaign"].update(countries=["au"]), "two-letter upper-case"),
    (lambda c: c["campaigns"][2]["campaign"].update(revenue_bands=["$1B+"]), "revenue_bands has '$1B+'"),
    (lambda c: c.update(exclude_existing_customers="yes"), "exclude_existing_customers must be true or false"),
])
def test_invalid_config(mutate, message):
    cfg = _cfg()
    mutate(cfg)
    with pytest.raises(ConfigError) as e:
        load_config(cfg)
    assert message in str(e.value)


def test_config_defaults():
    cfg = load_config({"campaigns": [{"id": "a", "campaign": {"message": "Fleet telematics"}}]})
    assert cfg["exclude_existing_customers"] is True and cfg["with_reasoning"] is True
    assert cfg["fetch_company"] is False and cfg["campaigns"][0]["label"] == "a"


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 1.2   # 15 answers with reasoning
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_input_without_identifier(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("account_name\nAcme\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2
