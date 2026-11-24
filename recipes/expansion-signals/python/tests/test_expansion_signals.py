import csv
import json

import pytest

from expansion_signals.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("expansion-signals")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def signals(row):
    return {s["signal_id"]: s for s in row["signals"]}


def test_happy_path_tiers_follow_policy():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["r1"]["status"] == "tier_assigned" and rows["r1"]["tier_id"] == "global_expand"
    assert rows["r2"]["status"] == "tier_assigned" and rows["r2"]["tier_id"] == "multi_site"
    assert rows["r4"]["duplicate_of"] == "r1" and rows["r4"]["tier_id"] == "global_expand"
    assert rows["r5"]["status"] == "invalid_input" and rows["r6"]["status"] == "invalid_input"
    assert "f followed by 12 digits" in rows["r6"]["status_reason"]
    assert rows["r7"]["status"] == "no_tier"
    assert "1 signal(s) answered unknown" in rows["r7"]["status_reason"]
    assert rows["r9"]["status"] == "tier_assigned" and rows["r9"]["tier_id"] == "regional"
    assert transport.unused() == []
    # 7 unique subjects asked 6 signals each, the last subject stops after one 404, plus the tool list.
    assert len(transport.sent) == 38


def test_unknown_sends_to_review_never_to_a_lower_tier():
    result, _ = run("happy")
    r3 = by_id(result)["r3"]
    assert r3["status"] == "needs_review" and r3["tier_id"] is None
    assert r3["deciding_tier_id"] == "global_expand"
    assert [t["outcome"] for t in r3["tiers"]] == ["needs_review", "not_considered", "not_considered"]
    assert signals(r3)["multiple_sites"]["result"] == "yes"   # tier 2 would match, but tier 1 is unresolved
    r8 = by_id(result)["r8"]
    assert r8["status"] == "needs_review" and r8["deciding_tier_id"] == "regional"


def test_signal_details_are_kept_as_returned():
    result, _ = run("happy")
    s = signals(by_id(result)["r1"])
    assert s["multinational"]["details"] == {"countries": ["AU", "NZ", "US"]}
    assert s["multiple_sites"]["fact"] == {"key": "locations_held", "value": 4}
    assert s["local_team:AU"]["fact"] == {"key": "staff_in_country", "value": 25}
    assert s["local_team:SG"]["result"] == "unknown" and s["local_team:SG"]["fact"] is None
    assert s["office_in:NZ"]["fact"]["value"] == ["NZ", "AU"]
    assert s["office_in:US"]["result"] == "no"


def test_company_id_wins_and_both_identifiers_dedupe():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["r2"]["lookup"] == {"by": "id", "value": "f900000000002"}
    assert rows["r11"]["duplicate_of"] == "r2"
    bodies = [r.json_body for r in transport.sent if r.path == "/decisions" and r.method == "POST"]
    assert not any((b["question"].get("domain") == "other.example") for b in bodies)


def test_conflicting_answers_are_surfaced_not_corrected():
    result, _ = run("happy")
    rows = by_id(result)
    assert "is_multinational answered no" in rows["r2"]["data_conflicts"][0]
    assert "has_multiple_sites answered no" in rows["r9"]["data_conflicts"][0]
    assert signals(rows["r2"])["multinational"]["result"] == "no"


def test_decisions_company_not_found_stops_that_row():
    result, _ = run("happy")
    r10 = by_id(result)["r10"]
    assert r10["status"] == "company_not_found" and r10["signals"] == []
    assert result["errors"] == []


def test_fetch_company_with_reasoning():
    result, transport = run("company", config="company.config.json")
    rows = by_id(result)
    assert rows["c1"]["status"] == "tier_assigned" and rows["c1"]["company"]["id"] == "f900000000001"
    assert signals(rows["c1"])["multinational"]["reasoning"]["reason"] == "Synthetic reason for a yes answer."
    assert rows["c2"]["status"] == "company_not_found"
    assert rows["c3"]["status"] == "no_tier"
    assert "differs from the profile's fqdn gamma.example" in rows["c3"]["data_conflicts"][0]
    assert rows["c4"]["status"] == "error" and rows["c4"]["error"]["status"] == 500
    assert transport.unused() == []
    assert all(r.query == {"withReasoning": "true"} for r in transport.sent if r.method == "POST")


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    e1 = signals(rows["e1"])
    assert e1["multinational"]["result"] == "error" and e1["multinational"]["error"]["status"] == 502
    assert e1["multiple_sites"]["error"]["kind"] == "timeout"
    assert rows["e1"]["status"] == "incomplete"
    e2 = signals(rows["e2"])
    assert e2["multiple_sites"]["error"]["kind"] == "invalid_response"
    assert rows["e2"]["status"] == "tier_assigned"   # the error is in a signal tier 1 does not need
    assert signals(rows["e3"])["multiple_sites"]["result"] == "not_asked"
    assert rows["e3"]["status"] == "incomplete"
    assert rows["e4"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 14


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=1.1)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "incomplete"
    assert signals(rows["row-1"])["local_team:AU"]["error"]["kind"] == "budget_exceeded"
    assert rows["row-2"]["status"] == "not_processed"
    assert len(transport.sent) == 3


def test_tool_list_drift_stops_before_paid_calls():
    result, transport = run("drift")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    msgs = [e["message"] for e in result["errors"]]
    assert len(msgs) == 2 and any("min_sites" in m for m in msgs) and any("has_local_team" in m for m in msgs)
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_local_team_outside_anz_warns_and_names_model_tool():
    result, _ = run("happy")
    assert any("These configured countries will answer unknown: SG." in c for c in result["coverage"])
    assert any("decision model" in c and "is_located_in" in c for c in result["coverage"])


def test_outputs_are_safe_and_csv_has_a_column_per_signal(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html
    with (tmp_path / "results.csv").open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames
    for col in ("multinational", "multiple_sites", "local_team:AU", "local_team:SG", "office_in:NZ", "office_in:US"):
        assert col in header
    assert [r["row_id"] for r in rows] == [f"r{i}" for i in range(1, 12)]
    assert rows[0]["local_team:SG"] == "unknown"
    assert "multiple_sites locations_held=4" in rows[0]["signal_facts"]
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_plan_counts_unique_valid_identifiers_and_warns():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert "GET /company" not in calls
    assert calls["POST /decisions"].requests == 42       # 11 rows, 2 duplicates, 2 invalid: 7 subjects
    assert "Config warning: has_local_team answers only for AU and NZ" in calls["POST /decisions"].purpose


def _cfg():
    return json.loads((F / "config.json").read_text())


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(checks={}), "checks must turn on at least one signal"),
    (lambda c: c.update(checks=[]), "checks must be an object"),
    (lambda c: c["checks"].update(colour=True), "Unknown key(s) in checks: colour"),
    (lambda c: c["checks"].update(multiple_sites={"min_sites": 1}), "min_sites must be a whole number, 2 or more"),
    (lambda c: c["checks"]["local_teams"].append({"country": "GB"}), "country must be one of"),
    (lambda c: c["checks"]["local_teams"].append({"country": "AU"}), "'AU' is listed twice"),
    (lambda c: c["checks"].update(offices_in=["nz"]), "two-letter upper-case country code"),
    (lambda c: c.update(tiers=[]), "tiers must be a list of 1 to 20 tiers"),
    (lambda c: c["tiers"][0]["when"].update(all=["office_in:SG"]), "names signal 'office_in:SG'"),
    (lambda c: c["tiers"][1].update(id="global_expand"), "used twice"),
    (lambda c: c["tiers"][1].update(when={}), "needs at least one signal"),
    (lambda c: c.update(with_reasoning="yes"), "with_reasoning must be true or false"),
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
])
def test_invalid_config(mutate, message):
    cfg = _cfg()
    mutate(cfg)
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_config(cfg)


def test_defaults():
    cfg = load_config({"checks": {"multiple_sites": True, "local_teams": [{"country": "NZ"}]},
                       "tiers": [{"id": "t", "when": {"any": ["multiple_sites", "local_team:NZ"]}}]})
    assert cfg["signals"][0]["inputs"] == {"min_sites": 2}
    assert cfg["signals"][1]["inputs"] == {"country": "NZ", "min_staff": 5}
    assert cfg["warnings"] == [] and cfg["with_reasoning"] is False and cfg["fetch_company"] is False
    assert cfg["tiers"][0]["label"] == "t"


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 1.68   # 42 answers without reasoning
    out = capsys.readouterr().out
    assert "no API calls were made" in out and "Config warning" in out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_bad_input(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name\nAcme\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2
