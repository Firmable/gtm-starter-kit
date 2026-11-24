import csv
import json

import pytest

from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures
from tech_sales_opportunities.recipe import RECIPE, load_config, plan

F = fixtures_dir("tech-sales-opportunities")


def run(name, config=None, max_credits=100.0, out=None):
    cfg = F / f"{name}.config.json"
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv",
                             config_path=F / (config or (cfg.name if cfg.exists() else "config.json")),
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def targets(row):
    return {t["target_id"]: t for t in row["targets"]}


def test_happy_path_statuses(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["h1"]["status"] == "matched"
    assert rows["h2"]["status"] == "not_observed_incomplete"
    assert rows["h3"]["status"] == "no_technology_data"
    assert rows["h4"]["status"] == "matched" and rows["h4"]["duplicate_of"] == "h1"
    assert rows["h5"]["status"] == "invalid_input"
    assert rows["h6"]["status"] == "company_not_found"
    assert rows["h7"]["status"] == "matched"
    assert transport.unused() == []
    # The duplicate row reused both pages: 7 Tech Intel requests for 5 valid unique domains.
    assert len([r for r in transport.sent if r.path == "/tech-intel"]) == 7


def test_target_results_keep_observed_and_not_observed_distinct():
    result, _ = run("happy")
    rows = by_id(result)
    h1 = targets(rows["h1"])
    assert h1["salesforce"]["result"] == "observed"
    assert h1["snowflake"]["result"] == "not_observed"
    assert h1["snowflake"]["result_text"] == "Not observed in retrieved data."
    h2 = targets(rows["h2"])
    assert h2["salesforce"]["result"] == "not_observed_in_pages_retrieved"
    assert "page limit was reached: 20 of 25" in rows["h2"]["status_reason"]
    assert all(t["result"] == "not_checked" for t in rows["h6"]["targets"])


def test_matching_is_explicit_with_no_fuzzy_matching():
    result, _ = run("happy")
    rows = by_id(result)
    hub = targets(rows["h1"])["hubspot"]["matches"]
    assert [m["technology"]["product"] for m in hub] == ["HubSpot Marketing Hub"]
    assert hub[0]["match_basis"] == ["alias of vendor name HubSpot"]
    snow = targets(rows["h7"])["snowflake"]["matches"]
    # "Snowflake Connector" is not an exact alias, so it does not match.
    assert [m["technology"]["product"] for m in snow] == ["SNOWFLAKE"]
    assert targets(rows["h1"])["salesforce"]["matches"][0]["match_basis"] == ["vendor_slug salesforce"]


def test_evidence_locations_are_flagged_not_dropped():
    result, _ = run("happy")
    sf = targets(by_id(result)["h1"])["salesforce"]["matches"][0]
    ev = sf["evidence"]
    assert ev["status"] == "returned" and ev["records_listed"] == 4
    flags = [r["flags"] for r in ev["records"]]
    assert flags == [[], ["location_differs_from_company_domain"], ["no_location"], ["location_not_a_domain"]]
    assert ev["records"][1]["flag_messages"] == ["Evidence location differs from the company domain."]
    assert ev["capped_types"] == ["job_ad"]
    assert any("4 of 30 evidence record(s) are listed" in n for n in ev["notes"])
    assert sf["technology"]["confidence"] == "high" and sf["technology"]["last_observed"] == "2026-11-10"
    hub = targets(by_id(result)["h1"])["hubspot"]["matches"][0]
    assert hub["evidence"]["status"] == "none_returned"
    assert any("differs from the company domain" in c for c in result["coverage"])


def test_errors_are_kept_and_402_stops_the_run():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "error" and rows["e1"]["tech_intel"]["error"]["status"] == 500
    assert rows["e2"]["status"] == "matched" and rows["e2"]["tech_intel"]["complete"] is False
    assert rows["e2"]["tech_intel"]["error"]["kind"] == "timeout"
    assert targets(rows["e2"])["hubspot"]["result"] == "not_observed_in_pages_retrieved"
    assert rows["e3"]["tech_intel"]["error"]["kind"] == "invalid_response"
    assert rows["e4"]["tech_intel"]["error"]["kind"] == "insufficient_credits"
    assert rows["e5"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 5


def test_forbidden_means_not_on_plan_and_stops():
    result, transport = run("forbidden")
    rows = by_id(result)
    assert rows["f1"]["status"] == "not_on_plan"
    assert rows["f2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "forbidden"
    assert len(transport.sent) == 1


def test_budget_keeps_pages_already_fetched():
    result, transport = run("budget", max_credits=6.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "matched"
    assert rows["row-1"]["tech_intel"]["stop_reason"] == "budget"
    assert rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 2


def test_company_lookup_uses_profile_domains():
    result, transport = run("company")
    rows = by_id(result)
    recs = targets(rows["c1"])["salesforce"]["matches"][0]["evidence"]["records"]
    assert [r["flags"] for r in recs] == [[], []]       # acme-group.example is the profile's website domain
    assert rows["c1"]["company"]["name"] == "Acme Example"
    assert rows["c2"]["status"] == "company_not_found"
    assert [r.path for r in transport.sent] == ["/company", "/tech-intel", "/company"]
    assert transport.sent[1].query == {"id": "f900000000001", "withEvidence": "true"}


def test_no_evidence_mode():
    result, transport = run("no-evidence")
    m = targets(result["rows"][0])["hubspot"]["matches"][0]
    assert m["evidence"] == {"status": "not_requested"} and m["match_basis"] == ["product_slug hubspot-crm", "alias of vendor name HubSpot"]
    assert transport.sent[0].query == {"fqdn": "acme.example"}


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Bold</b>" not in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]
    assert "fbl_dev_" not in json.dumps(json.loads((tmp_path / "results.json").read_text()))


def test_plan_counts_pages_per_unique_domain():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /tech-intel"].requests == 10      # 7 rows, 1 duplicate, 1 invalid; 2 pages each
    assert "GET /company" not in calls


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(targets=[]), "targets must be a non-empty list"),
    (lambda c: c["targets"][0].update(match={}), "needs at least one product slug"),
    (lambda c: c["targets"][0]["match"].update(fuzzy=True), "match has unknown key"),
    (lambda c: c["targets"][1].update(id="salesforce"), "used twice"),
    (lambda c: c["targets"][0].pop("relationship"), "relationship must be text"),
    (lambda c: c["targets"][0]["match"].update(aliases=[""]), "non-empty text values"),
    (lambda c: c.update(max_pages=0), "max_pages must be a whole number"),
    (lambda c: c.update(with_evidence="yes"), "with_evidence must be true or false"),
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
    assert plan_json["published_estimate_upper_bound"] == 27.0    # 5 companies x 5 + 10 evidence pages x 0.2
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
