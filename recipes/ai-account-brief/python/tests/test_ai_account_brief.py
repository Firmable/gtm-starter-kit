import csv
import json

import pytest

from ai_account_brief.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("ai-account-brief")


def run(name, config=None, max_credits=100.0, out=None):
    cfg = F / f"{name}.config.json"
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv",
                             config_path=F / (config or (cfg.name if cfg.exists() else "config.json")),
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def areas(row):
    return {a["area"]: a for a in row["ai_adoption"]["areas"]}


def test_happy_path_statuses(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["a1"]["status"] == "brief_ready"
    assert rows["a2"]["status"] == "no_ai_adoption_data"
    assert rows["a3"]["status"] == "brief_ready"
    assert rows["a4"]["status"] == "brief_ready" and rows["a4"]["duplicate_of"] == "a1"
    assert rows["a5"]["status"] == "invalid_input"
    assert rows["a6"]["status"] == "company_not_found" and rows["a6"]["ai_adoption"] is None
    assert rows["a7"]["status"] == "brief_ready"
    assert transport.unused() == []
    assert len([r for r in transport.sent if r.path == "/ai-adoption"]) == 4


def test_ai_adoption_shown_as_returned():
    result, _ = run("happy")
    ai = by_id(result)["a1"]["ai_adoption"]
    assert ai["overall_level"] == "STRONG"
    assert ai["explanation"] == "AI workforce strong, meaningful architecture, basic AI hiring."
    assert ai["momentum"] == {"label": "EXPANDING", "signals": [{"kind": "AI hiring", "count": 2, "latest": "2026-10-13"}]}
    a = areas(by_id(result)["a1"])
    assert [x for x in a] == ["architecture", "footprint", "hiring", "initiatives", "workforce"]
    assert a["initiatives"]["observability"] == "LOW" and a["initiatives"]["counts_toward_overall"] is False
    assert a["hiring"]["detail"] == {"sub_dimensions": {"hiring_scale": {"roles_12m": 3, "roles_90d": 0}}}
    assert "score" not in json.dumps(ai).lower()


def test_evidence_is_split_and_gaps_are_flagged():
    result, _ = run("happy")
    a = areas(by_id(result)["a1"])
    arch = a["architecture"]
    assert [i["technology"] for i in arch["evidence"]["qualifying"]] == ["Model Server Example"]
    assert [i["technology"] for i in arch["evidence"]["not_qualifying"]] == ["Old Queue Example"]
    assert arch["flags"] == ["Evidence is truncated: 2 of 33 item(s) were returned."]
    foot = a["footprint"]["evidence"]
    assert len(foot["qualification_unstated"]) == 1
    assert a["initiatives"]["flags"] == ["Firmable holds 5 evidence item(s) for this area, but none were returned."]
    delta = areas(by_id(result)["a7"])
    assert all(x["evidence"]["status"] == "not_returned" and x["flags"] for x in delta.values())


def test_workforce_item_naming_another_organisation_is_flagged_not_removed():
    result, _ = run("happy")
    wf = areas(by_id(result)["a1"])["workforce"]
    assert wf["evidence"]["other_organisation_items"] == [
        {"ref": {"type": "person", "id": "fp900000000002"},
         "fields": [{"field": "company_name", "value": "Other Org Example"}]}]
    # The same name in a different case is not flagged, and no item is removed.
    assert len(wf["evidence"]["qualifying"]) + len(wf["evidence"]["not_qualifying"]) == 3


def test_unknown_level_conflicts_and_missing_area():
    result, _ = run("happy")
    g = by_id(result)["a3"]
    a = areas(g)
    assert a["architecture"]["level"] == "EXEMPLARY" and a["architecture"]["level_recognised"] is False
    assert a["workforce"]["flags"] == ["The API returned no data for this area."]
    assert any("not one of the documented levels" in n for n in g["ai_adoption"]["notes"])
    assert len(g["data_conflicts"]) == 3
    assert "f900000000099" in g["data_conflicts"][0]
    assert g["tech_intel"]["complete"] is False and g["tech_intel"]["retrieved"] == 1


def test_questions_are_templates_filled_from_facts_and_skipped_when_missing():
    result, _ = run("happy")
    rows = by_id(result)
    qs = {q["id"]: q for q in rows["a1"]["suggested_questions"]}
    assert qs["tech_rollout"]["text"] == "You use Claude (Anthropic). How is it rolled out across Acme Example?"
    assert qs["tech_rollout"]["kind"] == "suggested_question"
    assert qs["tech_rollout"]["filled_from"][0] == {"placeholder": "technology", "value": "Claude (Anthropic)", "source": "Tech Intel"}
    assert qs["ai_role"]["text"].startswith("You advertised a Head of Machine Learning role")
    assert qs["offering"]["filled_from"][0]["source"] == "seller context"
    # Medium-confidence technology is not used; no high-confidence technology means the template is skipped.
    skipped = {s["id"]: s["missing"] for s in rows["a3"]["skipped_questions"]}
    assert skipped["tech_rollout"] == ["technology"]
    assert skipped["momentum"] == ["momentum_label"]
    assert [s["id"] for s in rows["a2"]["skipped_questions"]] == ["tech_rollout", "ai_role", "ai_tool", "momentum", "offering"]


def test_errors_are_kept_and_402_stops_the_run():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "incomplete" and rows["e1"]["ai_adoption"]["error"]["status"] == 500
    assert rows["e1"]["tech_intel"]["status"] == "retrieved"
    assert rows["e2"]["ai_adoption"]["error"]["kind"] == "invalid_response"
    assert rows["e3"]["tech_intel"]["error"]["kind"] == "timeout" and rows["e3"]["ai_adoption"]["status"] == "found"
    assert rows["e4"]["tech_intel"]["status"] == "not_requested"
    assert rows["e5"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 11


def test_forbidden_tech_intel_is_not_asked_again():
    result, transport = run("forbidden")
    rows = by_id(result)
    assert rows["f1"]["status"] == "incomplete" and rows["f1"]["tech_intel"]["status"] == "access_denied"
    assert rows["f1"]["ai_adoption"]["status"] == "found"
    assert rows["f2"]["tech_intel"]["status"] == "access_denied"
    assert [r.path for r in transport.sent].count("/tech-intel") == 1
    assert result["coverage"][-1].startswith("Firmable returned 403 Forbidden for Tech Intel")


def test_budget_stops_before_tech_intel():
    result, transport = run("budget", max_credits=8)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "incomplete"
    assert rows["row-1"]["ai_adoption"]["status"] == "found"
    assert rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 2


def test_lean_config_skips_evidence_and_tech_intel():
    result, transport = run("lean")
    row = result["rows"][0]
    assert row["status"] == "brief_ready"
    assert all(a["evidence"] == {"status": "not_requested"} for a in row["ai_adoption"]["areas"])
    assert row["tech_intel"]["status"] == "not_requested"
    assert [r.path for r in transport.sent] == ["/company", "/ai-adoption"]
    assert transport.sent[1].query == {}


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<script>alert" not in html and "class=\"template\"" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["a1", "a2", "a3", "a4", "a5", "a6", "a7"]
    assert "fbl_dev_" not in json.dumps(json.loads((tmp_path / "results.json").read_text()))


def test_plan_counts_unique_valid_domains():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 5
    assert calls["POST /ai-adoption"].requests == 5
    assert calls["GET /tech-intel"].requests == 5


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["discovery_questions"].append({"id": "x", "template": "Hello {nobody}"}), "not an API fact placeholder"),
    (lambda c: c["discovery_questions"].append({"id": "x", "template": "Hello {company_name"}), "unmatched brace"),
    (lambda c: c["discovery_questions"].append({"id": "x", "template": "Hello {Bad Name}"}), "invalid placeholder"),
    (lambda c: c["discovery_questions"].append({"id": "tech_rollout", "template": "Hi"}), "used twice"),
    (lambda c: c.update(seller_context={"company_name": "Me"}), "reserved for an API fact"),
    (lambda c: c.update(seller_context={"offering": ""}), "must be non-empty text"),
    (lambda c: c.update(tech_intel_max_pages=0), "tech_intel_max_pages must be a whole number"),
    (lambda c: c.update(ai_evidence="yes"), "ai_evidence must be true or false"),
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
    assert plan_json["published_estimate_upper_bound"] == 45.0    # 5 x (1 profile + 3 AI evidence + 5 Tech Intel)
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
