import csv
import json

import pytest

from crm_enrichment.recipe import RECIPE, load_config, normalise_identifier, plan, value_text
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("crm-enrichment")


def run(name, config="config.json", max_credits=100.0, out=None, ext="csv"):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.{ext}", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def changes(row):
    return {c["column"]: c for c in row["changes"]}


def test_happy_path_statuses(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert [r["row_id"] for r in result["rows"]] == ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]
    assert rows["h1"]["status"] == "enriched" and rows["h1"]["fields_filled"] == 8
    assert rows["h2"]["status"] == "enriched" and rows["h2"]["lookups"]["person"]["status"] == "no_identifier"
    assert rows["h3"]["status"] == "not_matched"
    assert rows["h4"]["duplicate_of"] == "h1"
    assert rows["h5"]["status"] == "invalid_input"
    assert "'not a domain' is not a valid domain" in rows["h5"]["status_reason"]
    assert rows["h6"]["status"] == "no_change"
    assert rows["h7"]["status"] == "enriched" and rows["h7"]["lookups"]["company"]["status"] == "no_identifier"
    assert transport.unused() == []
    # The duplicate row reused responses: one request per unique identifier.
    assert len([r for r in transport.sent if r.path == "/company"]) == 4
    assert len([r for r in transport.sent if r.path == "/people"]) == 4


def test_fill_empty_keeps_conflicting_values():
    result, _ = run("happy")
    c = changes(by_id(result)["h2"])
    # A conflicting company classification is reported, not corrected.
    assert c["company_type"]["action"] == "conflict_kept"
    assert c["company_type"]["existing"] == "Government" and c["company_type"]["api_value"] == "Private company"
    assert c["company_type"]["output"] == "Government"
    assert c["year_founded"]["action"] == "conflict_kept" and c["year_founded"]["api_value"] == "2031"
    assert c["employee_count"]["action"] == "filled" and c["employee_count"]["output"] == "4"
    assert {(x["row_id"], x["column"]) for x in result["summary"]["conflicts"]} == {
        ("h2", "company_type"), ("h2", "year_founded"), ("h4", "industry"), ("h4", "contact_title")}


def test_comparison_rules_treat_case_spaces_and_domains_as_equal():
    result, _ = run("happy")
    c = changes(by_id(result)["h1"])
    assert c["company_name"]["action"] == "unchanged" and c["company_name"]["output"] == "ACME   example"
    assert c["website"]["action"] == "unchanged"
    assert c["industry"]["output"] == "Computer software; Information technology"
    assert changes(by_id(result)["h6"])["contact_title"]["action"] == "unchanged"


def test_missing_values_and_dnd_phones_are_not_written():
    result, _ = run("happy")
    c = changes(by_id(result)["h6"])
    assert c["website"]["action"] == "no_api_value" and c["website"]["api_value"] is None
    assert c["industry"]["action"] == "no_api_value"
    assert c["mobile"]["action"] == "skipped_dnd" and c["mobile"]["is_dnd"] is True and c["mobile"]["output"] == ""
    h1 = changes(by_id(result)["h1"])
    assert h1["mobile"]["action"] == "filled" and h1["mobile"]["is_dnd"] is False
    h3 = changes(by_id(result)["h3"])
    assert {x["action"] for x in h3.values()} == {"source_unavailable"}


def test_overwrite_policy_replaces_and_reports():
    result, _ = run("overwrite", config="overwrite.config.json")
    row = by_id(result)["o1"]
    c = changes(row)
    assert c["company_name"]["action"] == "overwritten" and c["company_name"]["output"] == "Beta Example"
    assert c["website"]["action"] == "unchanged"          # compared as domains
    assert row["fields_overwritten"] == 4 and row["conflicts"] == 4
    assert all(x["action"] == "overwritten" for x in result["summary"]["conflicts"])


def test_errors_are_kept_per_lookup_and_run_stops_on_402():
    result, transport = run("errors", config="errors.config.json")
    rows = by_id(result)
    assert rows["e1"]["status"] == "incomplete"
    assert rows["e1"]["lookups"]["company"]["error"]["status"] == 500
    assert changes(rows["e1"])["contact_name"]["action"] == "filled"
    assert rows["e2"]["lookups"]["company"]["error"]["kind"] == "timeout"
    assert rows["e3"]["lookups"]["company"]["error"]["kind"] == "invalid_response"
    assert rows["e4"]["lookups"]["person"]["error"]["kind"] == "insufficient_credits"
    assert rows["e5"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 6


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", config="budget.config.json", max_credits=1.5)
    rows = by_id(result)
    assert rows["b1"]["status"] == "incomplete"
    assert rows["b1"]["lookups"]["person"]["error"]["kind"] == "budget_exceeded"
    assert rows["b2"]["status"] == "not_processed"
    assert len(transport.sent) == 1


def test_abn_identifiers_from_json_input():
    result, transport = run("abn", config="abn.config.json", ext="json")
    rows = by_id(result)
    assert rows["a1"]["status"] == "enriched" and rows["a1"]["lookups"]["company"]["value"] == "51900000001"
    assert changes(rows["a1"])["hq"]["action"] == "conflict_kept"
    assert rows["a2"]["status"] == "invalid_input"
    assert [r.query for r in transport.sent] == [{"abn": "51900000001"}]


def test_enriched_csv_keeps_input_columns_and_order(tmp_path):
    run("happy", out=tmp_path)
    with (tmp_path / "results.csv").open() as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)
    source = (F / "happy.input.csv").read_text().splitlines()[0].split(",")
    assert header[:len(source)] == source
    assert header[-8:] == ["enrichment_status", "enrichment_reason", "company_match", "person_match",
                           "fields_filled", "fields_overwritten", "conflicts", "duplicate_of"]
    assert [r["row_id"] for r in rows] == ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]
    assert rows[0]["contact_title"] == "Chief Financial Officer"
    assert rows[6]["company_name"].startswith("'=")       # formula neutralised
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_value_text_and_identifiers():
    assert value_text(["a", None, "b"]) == "a; b"
    assert value_text(4.0) == "4" and value_text(True) == "true" and value_text({}) == ""
    assert value_text([{"value": "x"}]) == '[{"value":"x"}]'
    assert normalise_identifier("work_email", " Jane.Citizen@Example.com ") == "jane.citizen@example.com"
    assert normalise_identifier("abn", "51 824 753 556") == "51824753556"
    assert normalise_identifier("fqdn", "https://www.acme.example/x") == "acme.example"
    assert normalise_identifier("id", "f9 0") is None


def test_plan_counts_unique_valid_identifiers():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 4    # acme (twice), beta, ghost, gamma; invalid and empty skipped
    assert calls["GET /people"].requests == 4     # alex (twice), nobody, jo, sam


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(mappings=[]), "mappings must be a non-empty list"),
    (lambda c: c.update(merge_policy="replace"), "merge_policy must be fill_empty or overwrite"),
    (lambda c: c["mappings"][1].update(column="company_name"), "is mapped twice"),
    (lambda c: c["mappings"][0].update(column="domain"), "is an identifier column"),
    (lambda c: c["mappings"][0].update(field="nickname"), "does not start with a documented company field"),
    (lambda c: c["mappings"][6].update(field="gender"), "does not start with a documented person field"),
    (lambda c: c["identifiers"]["person"].update(type="phone"), "identifiers.person.type must be one of"),
    (lambda c: c["identifiers"].pop("person"), "identifiers.person is not set"),
    (lambda c: c.update(comparison={"trim": True}), "comparison has unknown key"),
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
    assert plan_json["published_estimate_upper_bound"] == 8.0
    assert "no API calls were made" in capsys.readouterr().out


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_bad_config(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"identifiers": {}, "mappings": []}))
    assert cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(bad), "--out", str(tmp_path)]) == 2
