import csv
import json

import pytest

from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures
from territory_routing.recipe import RECIPE, load_config, plan

F = fixtures_dir("territory-routing")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def test_first_match_routes_in_rule_order(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["h1"]["status"] == "assigned" and rows["h1"]["team_id"] == "enterprise"
    assert rows["h1"]["owner"] == "Enterprise queue"
    assert rows["h2"]["team_id"] == "commercial"
    assert rows["h3"]["team_id"] == "nz"
    assert rows["h4"]["team_id"] == "public_sector" and rows["h4"]["matched_rules"] == ["public_sector"]
    # Later rules are not evaluated once the first rule settles the row.
    assert [r["outcome"] for r in rows["h4"]["rules"]] == ["match", "not_evaluated", "not_evaluated", "not_evaluated"]
    assert rows["h10"]["status"] == "unassigned"
    assert transport.unused() == []


def test_unknown_goes_to_review_never_to_a_team():
    rows = by_id(run("happy")[0])
    assert rows["h5"]["status"] == "review" and rows["h5"]["team_id"] is None
    assert rows["h5"]["decisions"][0]["answer"] == "unknown"
    assert rows["h5"]["candidate_teams"] == ["public_sector"]
    h6 = rows["h6"]
    assert h6["status"] == "review"
    nz = h6["rules"][1]
    assert nz["outcome"] == "unknown" and nz["conditions"][0]["result"] == "unknown"
    assert nz["conditions"][0]["observed"] is None


def test_invalid_missing_and_duplicate_rows():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["h8"]["status"] == "invalid_input"
    assert rows["h13"]["status"] == "invalid_input" and "f followed by 12 digits" in rows["h13"]["status_reason"]
    assert rows["h9"]["status"] == "company_not_found" and rows["h9"]["rules"] == []
    assert rows["h7"]["duplicate_of"] == "h1" and rows["h7"]["team_id"] == "enterprise"
    # The duplicate reused both responses: 10 company lookups for 13 rows (2 invalid).
    assert len([r for r in transport.sent if r.path == "/company"]) == 10
    assert len([r for r in transport.sent if r.path == "/decisions" and r.method == "POST"]) == 9
    assert sum(1 for e in result["ledger"] if e["outcome"] == "cache_hit") == 2


def test_data_conflicts_are_surfaced_not_corrected():
    rows = by_id(run("happy")[0])
    h11 = rows["h11"]
    assert h11["status"] == "review" and h11["matched_rules"] == ["au_commercial"]
    assert h11["company"]["employee_count"] == 50 and h11["company"]["company_size"] == "1001 - 5000"
    assert "does not correct" in h11["data_conflicts"][0]
    h12 = rows["h12"]
    assert h12["status"] == "review" and h12["lookup"] == {"by": "id", "value": "f900000000009"}
    assert "other.example differs" in h12["status_reason"]


def test_review_overlap_mode():
    result, transport = run("review", config="review.config.json")
    rows = by_id(result)
    assert rows["o1"]["status"] == "review" and rows["o1"]["candidate_teams"] == ["enterprise", "finance"]
    assert rows["o2"]["status"] == "assigned" and rows["o2"]["matched_rules"] == ["big", "big_au"]
    assert rows["o3"]["status"] == "assigned_default" and rows["o3"]["team_id"] == "anz"
    assert rows["o4"]["status"] == "review" and rows["o4"]["candidate_teams"] == ["finance", "enterprise"]
    assert rows["o5"]["status"] == "review" and len(rows["o5"]["review_reasons"]) == 2
    assert rows["o6"]["status"] == "assigned"  # the unknown rule routes to the same team
    assert rows["o7"]["team_id"] == "vic"
    assert result["summary"]["review_queue"] == ["o1", "o4", "o5"]
    # No decision checks configured: no tool list call and no decision calls.
    assert {r.path for r in transport.sent} == {"/company"}


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "error" and rows["e1"]["error"]["status"] == 500
    assert rows["e2"]["status"] == "incomplete" and rows["e2"]["decisions"][0]["error"]["status"] == 502
    assert rows["e3"]["decisions"][0]["error"]["kind"] == "timeout"
    assert rows["e4"]["decisions"][0]["error"]["kind"] == "invalid_response"
    assert rows["e5"]["status"] == "incomplete"
    assert rows["e6"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 4
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 10


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=2.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "assigned"
    assert rows["row-2"]["status"] == "error" and result["errors"][0]["kind"] == "budget_exceeded"
    assert rows["row-3"]["status"] == "not_processed"
    assert len(transport.sent) == 3


def test_bad_tool_stops_before_paid_calls():
    result, transport = run("bad-tool", config="bad-tool.config.json")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert any("is_imaginary" in e["message"] for e in result["errors"])
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Theta</b>" not in html and "&lt;b&gt;Theta&lt;/b&gt;" in html
    assert "<script>alert" not in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == [f"h{i}" for i in range(1, 14)]
    assert rows[4]["review_queue"] == "true" and rows[0]["review_queue"] == "false"
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_plan_counts_unique_valid_identifiers():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 10       # 13 rows, 1 duplicate, 2 invalid
    assert calls["POST /decisions"].requests == 10
    review = load_config(json.loads((F / "review.config.json").read_text()))
    assert [c.endpoint for c in plan(rows, review)] == ["GET /company"]


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(teams=[]), "teams must be a non-empty list"),
    (lambda c: c["rules"][0].update(team="nobody"), "team must be one of the team ids"),
    (lambda c: c["rules"][1].update(id="public_sector"), "used twice"),
    (lambda c: c["rules"][1].update(when={}), "at least one condition"),
    (lambda c: c["rules"][1]["when"].update(colour=["red"]), "unknown key"),
    (lambda c: c["rules"][2]["when"].update(employee_count_max=10), "more than employee_count_max"),
    (lambda c: c["rules"][0]["when"].update(decisions={"nope": "yes"}), "not a decision_checks id"),
    (lambda c: c["rules"][0]["when"].update(decisions={"public_sector": "maybe"}), "must be yes or no"),
    (lambda c: c["rules"].pop(0), "is not used by any rule"),
    (lambda c: c.update(overlap="random"), "overlap must be"),
    (lambda c: c.update(default_team="nobody"), "default_team must be"),
    (lambda c: c["teams"][0].update(id="1team"), "must start with a lower-case letter"),
    (lambda c: c.update(colour="orange"), "unknown key"),
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
    assert plan_json["published_estimate_upper_bound"] == 10.8   # 10 profiles + 10 answers with reasoning
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
