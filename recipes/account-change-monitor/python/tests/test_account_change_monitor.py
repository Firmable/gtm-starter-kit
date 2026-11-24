import csv
import json

import pytest

from account_change_monitor.cli import main as recipe_cli, write_snapshot
from account_change_monitor.recipe import RECIPE, SNAPSHOT_FORMAT, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("account-change-monitor")
K1 = "company:fqdn=acme.example|person:work_email=alex.example@acme.example"


def run(name, config=None, max_credits=100.0, out=None, scenario=None):
    cfg = config or (f"{name}.config.json" if (F / f"{name}.config.json").exists() else "config.json")
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / cfg if isinstance(cfg, str) else cfg,
                             scenario=F / f"{scenario or name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def changes(row):
    return {(c["source"], c["field"]): c for c in row["changes"]}


def test_first_run_creates_a_baseline_without_change_claims():
    result, transport = run("baseline")
    rows = by_id(result)
    assert [rows[k]["status"] for k in ("b1", "b2", "b3", "b6")] == ["baseline_created"] * 4
    assert all(r["changes"] == [] for r in result["rows"])
    assert rows["b3"]["duplicate_of"] == "b1"
    assert rows["b4"]["status"] == "invalid_input" and rows["b5"]["status"] == "invalid_input"
    assert rows["b6"]["sources"]["company"]["result"] == "not_requested"
    assert rows["b2"]["sources"]["ai_adoption"]["result"] == "not_found"
    assert transport.unused() == []
    assert len([r for r in transport.sent if r.path == "/company"]) == 2   # the duplicate reused responses
    snap = result["summary"]["snapshot"]
    assert snap["snapshot_format"] == SNAPSHOT_FORMAT and snap["taken_on"] == "2026-11-24"
    assert sorted(snap["accounts"]) == [K1, "company:fqdn=beta.example", "person:work_email=sam.sample@other.example"]
    assert snap["accounts"][K1]["sources"]["tech_intel"]["values"]["complete"] is False
    assert any("No previous snapshot was given" in c for c in result["coverage"])
    assert any(c.startswith("1 Tech Intel list(s) are incomplete") for c in result["coverage"])


def test_change_types_are_distinct_and_never_confirmed():
    result, transport = run("changes")
    rows = by_id(result)
    c1 = changes(rows["c1"])
    assert rows["c1"]["status"] == "changes_detected"
    assert c1[("company", "employee_count")]["change_type"] == "value_changed"
    assert c1[("company", "industries")]["change_type"] == "newly_observed"
    assert c1[("company", "revenue")]["change_type"] == "no_longer_observed_in_retrieved_data"
    assert c1[("company", "year_founded")]["previous"] == 2011 and c1[("company", "year_founded")]["current"] == 2024
    assert c1[("person", "position")]["current"] == "Chief Revenue Officer"
    assert "framework version changed from 2026.09 to 2026.10" in c1[("ai_adoption", "overall_level")]["note"]
    assert c1[("ai_adoption", "momentum.label")]["note"] is None
    assert c1[("tech_intel", "technology:marketing-automation/hubspot")]["change_type"] == "newly_observed"
    assert c1[("tech_intel", "technology:marketing-automation/marketo")]["change_type"] == "no_longer_observed_in_retrieved_data"
    assert c1[("tech_intel", "technology:help-desk/zendesk.confidence")]["current"] == "high"
    assert "not a confirmed real-world change" in rows["c1"]["status_reason"]
    assert rows["c1"]["baseline_fields"] == ["ai_adoption.size_band"]
    assert rows["c2"]["status"] == "no_changes_detected"
    assert rows["c4"]["status"] == "baseline_created" and rows["c4"]["changes"] == []
    assert transport.unused() == []


def test_failed_and_missing_responses_are_not_deletions():
    result, _ = run("changes")
    rows = by_id(result)
    c3 = changes(rows["c3"])
    assert rows["c3"]["status"] == "partially_compared"
    assert c3[("company", "*")]["change_type"] == "not_compared_fetch_failed"
    assert c3[("ai_adoption", "*")]["change_type"] == "not_compared_not_found"
    assert not any(c["change_type"] == "no_longer_observed_in_retrieved_data" for c in rows["c3"]["changes"])
    gamma = result["summary"]["snapshot"]["accounts"]["company:fqdn=gamma.example"]["sources"]
    assert gamma["company"]["values"]["name"] == "Gamma Example"
    assert gamma["company"]["observed_on"] == "2026-11-01" and gamma["company"]["last_result"] == "error"
    assert gamma["ai_adoption"]["last_result"] == "not_found" and gamma["ai_adoption"]["values"]["overall_level"] == "BASIC"


def test_incomplete_technology_list_is_not_read_as_removal():
    result, _ = run("changes")
    c6 = by_id(result)["c6"]
    assert not any(c["source"] == "tech_intel" for c in c6["changes"])
    assert "1 technology(ies) from the previous snapshot were not in the retrieved pages" in c6["status_reason"]
    # An unfamiliar AI Adoption level is reported as returned.
    assert changes(c6)[("ai_adoption", "dimensions.hiring.level")]["current"] == "EXTREME"


def test_identity_change_and_source_baselines():
    result, _ = run("changes")
    rows = by_id(result)
    c7 = changes(rows["c7"])
    assert "different Firmable company ID" in c7[("company", "id")]["note"]
    c8 = rows["c8"]
    assert c8["sources"]["company"]["comparison"] == "baseline"
    assert changes(c8)[("person", "position")]["change_type"] == "newly_observed"
    assert changes(c8)[("person", "id")]["note"] is None
    assert "1 source(s) had no previous values" in c8["status_reason"]
    snap = result["summary"]["snapshot"]
    assert "company:fqdn=gone.example" in snap["accounts"]
    assert any("not in this input" in c for c in result["coverage"])


def test_errors_are_recorded_and_402_stops_the_run():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "fetch_failed"
    kinds = {r["sources"][s]["error"]["kind"] for r in [rows["e1"]] for s in r["sources"]}
    assert kinds == {"timeout", "server_error", "invalid_response", "forbidden"}
    assert rows["e2"]["status"] == "partially_compared"
    assert rows["e2"]["sources"]["tech_intel"]["result"] == "skipped"
    assert rows["e3"]["status"] == "not_processed"
    assert result["coverage"][0].startswith("The run stopped early")
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    accounts = result["summary"]["snapshot"]["accounts"]
    assert accounts[K1]["sources"]["company"]["values"]["employee_count"] == 320
    assert accounts["company:fqdn=gamma.example"]["sources"]["company"]["last_attempt_on"] == "2026-11-01"
    assert len(transport.sent) == 6


def test_budget_stops_before_tech_intel():
    result, transport = run("budget", max_credits=2.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "baseline_created"
    assert rows["row-1"]["sources"]["tech_intel"]["error"]["kind"] == "budget_exceeded"
    assert rows["row-2"]["status"] == "not_processed"
    assert len(transport.sent) == 2


def test_snapshot_round_trip_reports_no_changes(tmp_path):
    first, _ = run("baseline", out=tmp_path)
    path = write_snapshot(tmp_path)
    assert json.loads(path.read_text()) == first["summary"]["snapshot"]
    cfg = json.loads((F / "config.json").read_text())
    cfg["previous_snapshot"] = str(path)
    cfg_path = tmp_path / "second.config.json"
    cfg_path.write_text(json.dumps(cfg))
    second, _ = run("baseline", config=cfg_path)
    statuses = {r["row_id"]: r["status"] for r in second["rows"]}
    assert [statuses[k] for k in ("b1", "b2", "b3", "b6")] == ["no_changes_detected"] * 4
    assert second["input"]["config"]["previous_snapshot"] == str(path)


def test_outputs_are_safe(tmp_path):
    run("baseline", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>html</b>" not in html and "&lt;b&gt;html&lt;/b&gt;" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["b1", "b2", "b3", "b4", "b5", "b6", "b7"]
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_plan_counts_unique_valid_subjects():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "baseline.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 2
    assert calls["GET /people"].requests == 2
    assert calls["POST /ai-adoption"].requests == 2
    assert calls["GET /tech-intel"].requests == 2


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
    (lambda c: c.update(company_fields=["request_id"]), "unknown field 'request_id'"),
    (lambda c: c.update(person_fields=["position", "position"]), "lists 'position' twice"),
    (lambda c: c.update(tech_intel={"enabled": True, "max_pages": 0}), "max_pages must be a whole number"),
    (lambda c: c.update(tech_intel={"evidence": True}), "Unknown tech_intel key"),
    (lambda c: c.update(company_fields=[], person_fields=[], ai_adoption_fields=[], tech_intel={"enabled": False}),
     "Nothing to monitor"),
    (lambda c: c.update(evaluation_date="24/11/2026"), "evaluation_date must be a date"),
    (lambda c: c.update(previous_snapshot={"accounts": {}}), "is not a gtm-account-change-monitor/1 snapshot"),
    (lambda c: c.update(previous_snapshot="/nonexistent/snapshot.json"), "previous_snapshot file not found"),
    (lambda c: c.update(previous_snapshot=7), "must be a file path"),
])
def test_invalid_config(mutate, message):
    cfg = json.loads((F / "config.json").read_text())
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_previous_snapshot_file_must_be_json(tmp_path):
    bad = tmp_path / "snap.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="is not valid JSON"):
        load_config({"previous_snapshot": str(bad)})


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = recipe_cli(["--input", str(F / "baseline.input.csv"), "--config", str(F / "config.json"),
                       "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 14.4   # 2 companies, 2 people, 0.4 AI, 10 Tech Intel
    assert not (tmp_path / "snapshot.json").exists()
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "baseline.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert recipe_cli(args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_bad_input(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name\nAcme\n")
    assert recipe_cli(["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2
