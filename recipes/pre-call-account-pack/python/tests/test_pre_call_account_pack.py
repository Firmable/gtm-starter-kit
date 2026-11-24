import csv
import json

import pytest

from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures
from pre_call_account_pack.recipe import RECIPE, load_config, plan

F = fixtures_dir("pre-call-account-pack")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def ledger_entry(result, source):
    return result["ledger"][source["ledger_seq"] - 1]


def test_happy_path_statuses_and_duplicate_copy():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["h1"]["status"] == "complete"
    assert rows["h2"]["status"] == "complete" and rows["h2"]["duplicate_of"] == "h1"
    assert rows["h2"]["contacts"] == rows["h1"]["contacts"]
    assert rows["h3"]["status"] == "invalid_input"
    assert rows["h4"]["status"] == "company_not_found"
    assert rows["h5"]["status"] == "complete"
    assert transport.unused() == []
    # The duplicate row made no request: one company call per unique valid domain.
    assert len([r for r in transport.sent if r.path == "/company"]) == 3
    assert result["errors"] == []


def test_contact_selection_order_dedupe_and_company_id():
    result, transport = run("happy")
    contacts = by_id(result)["h1"]["contacts"]
    assert [p["person_id"] for p in contacts["selected"]] == ["fp900000000001", "fp900000000002", "fp900000000003"]
    assert contacts["selected"][1]["matched_searches"] == ["sales_leaders", "executives"]
    assert contacts["not_selected"] == 0
    assert [s["status"] for s in contacts["searches"]] == ["ok", "ok"]
    sent = [r.json_body["question"] for r in transport.sent if r.path == "/decisions" and r.method == "POST"
            and r.json_body["tool"] == "works_at_company"]
    assert sent[0] == {"person_id": "fp900000000001", "company_id": "f900000000001"}
    new_in_role = [r.json_body["question"] for r in transport.sent if r.method == "POST" and r.path == "/decisions"
                   and r.json_body["tool"] == "is_new_to_role"]
    assert "company_id" not in new_in_role[0]
    assert any("works_at_company" in c for c in result["coverage"])


def test_every_fact_carries_its_source():
    result, _ = run("happy")
    h1 = by_id(result)["h1"]
    assert ledger_entry(result, h1["company"]["source"])["endpoint"] == "GET /company"
    assert ledger_entry(result, h1["company"]["source"])["response"]["id"] == "f900000000001"
    for c in h1["company_checks"]["checks"]:
        assert ledger_entry(result, c["source"])["body"]["tool"] == c["tool"]
    for p in h1["contacts"]["selected"]:
        assert ledger_entry(result, p["search_source"])["endpoint"] == "POST /people/search"
        assert ledger_entry(result, p["profile_source"])["query"] == {"id": p["person_id"]}
    items = h1["tech_intel"]["items"]
    assert [ledger_entry(result, i["source"])["query"].get("cursor") for i in items] == [None, None, "cur-2"]
    assert h1["tech_intel"]["complete"] is True and h1["tech_intel"]["total"] == 3
    assert ledger_entry(result, h1["ai_adoption"]["source"])["endpoint"] == "POST /ai-adoption"


def test_unknown_stays_unknown_and_becomes_a_template_question():
    result, _ = run("happy")
    h1 = by_id(result)["h1"]
    check = {c["check_id"]: c for c in h1["company_checks"]["checks"]}["au_head_office"]
    assert check["answer"] == "unknown" and check["result"] == "unknown"
    q = h1["suggested_questions"]
    assert q[0]["template_id"] == "confirm_unknown_check"
    assert [x["template_id"] for x in q].count("technology_category") == 2   # high confidence only
    assert q[-1]["template_id"] == "why_now"
    assert all("question" in x and "basis" in x for x in q)


def test_contact_details_exclude_personal_email_and_keep_dnd_flag():
    result, _ = run("happy")
    profile = by_id(result)["h1"]["contacts"]["selected"][0]["profile"]
    assert profile["work_emails"] == ["alex.example@acme.example"]
    assert profile["phones"][0] == {"value": "+61 400 000 000", "is_dnd": True}
    assert "personal" not in json.dumps(result["rows"])


def test_data_quality_notes_are_surfaced_not_corrected():
    result, _ = run("happy")
    rows = by_id(result)
    notes = rows["h1"]["data_quality_notes"]
    assert any("current company ID (f900000000099) differs" in n for n in notes)
    assert any("workforce evidence is truncated" in n for n in notes)
    p3 = rows["h1"]["contacts"]["selected"][2]
    assert p3["profile"]["current_company_id"] == "f900000000099"     # kept as returned
    assert any("about entity f900000000099" in n for n in rows["h5"]["data_quality_notes"])
    ev = rows["h1"]["ai_adoption"]["evidence_summary"]["workforce"]
    assert (ev["qualifying"], ev["not_qualifying"], ev["qualifies_missing"]) == (1, 1, 1)


def test_unrecognised_ai_level_is_kept():
    result, _ = run("happy")
    dims = by_id(result)["h1"]["ai_adoption"]["dimensions"]
    assert dims["governance"]["level"] == "EMERGING"


def test_valid_empty_answers_are_not_errors():
    result, _ = run("happy")
    h5 = by_id(result)["h5"]
    assert h5["contacts"]["status"] == "no_match"
    assert [s["status"] for s in h5["contacts"]["searches"]] == ["no_match", "no_match"]
    assert h5["tech_intel"]["status"] == "no_data"
    assert h5["ai_adoption"]["status"] == "not_found"
    assert [q["template_id"] for q in h5["suggested_questions"]] == ["who_else", "why_now"]
    assert any("success false without a records list" in c for c in result["coverage"])


def test_limits_page_limit_contact_limit_and_plan_restriction():
    result, transport = run("limits", config="limits.config.json")
    rows = by_id(result)
    l1 = rows["l1"]
    assert l1["status"] == "complete"
    assert l1["tech_intel"]["complete"] is False and l1["tech_intel"]["stop_reason"] == "page_limit"
    assert "Tech Intel is incomplete: 2 of 150" in l1["status_reason"]
    assert len(l1["contacts"]["selected"]) == 1 and l1["contacts"]["not_selected"] == 1
    assert l1["contacts"]["selected"][0]["profile_status"] == "not_requested"
    assert l1["tech_intel"]["items"][0]["evidence_records_without_location"] == 1
    assert any("Marketo: 1 record(s) have no source location" in n for n in l1["data_quality_notes"])
    assert len(l1["suggested_questions"]) == 2
    l2 = rows["l2"]
    assert l2["status"] == "partial" and l2["tech_intel"]["status"] == "not_available"
    assert "Tech Intel (not available on this plan)" in l2["status_reason"]
    assert not any(r.path == "/people" for r in transport.sent)
    assert not any(r.path == "/decisions" for r in transport.sent)


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "error" and rows["e1"]["error"]["status"] == 503
    e2 = rows["e2"]
    checks = {c["check_id"]: c for c in e2["company_checks"]["checks"]}
    assert checks["b2b"]["result"] == "error" and checks["b2b"]["error"]["kind"] == "timeout"
    assert checks["au_head_office"]["error"]["kind"] == "invalid_response"
    assert [s["status"] for s in e2["contacts"]["searches"]] == ["error", "ok"]
    assert e2["contacts"]["selected"][0]["profile_status"] == "not_found"
    assert e2["tech_intel"]["status"] == "error" and e2["tech_intel"]["error"]["kind"] == "invalid_response"
    assert e2["ai_adoption"]["status"] == "ok"
    assert e2["status"] == "partial"
    e3 = rows["e3"]
    assert e3["company_checks"]["checks"][1]["result"] == "not_asked"
    assert e3["contacts"]["status"] == "not_asked" and e3["tech_intel"]["status"] == "not_asked"
    assert rows["e4"]["status"] == "not_processed"
    assert result["coverage"][0].startswith("The run stopped early")
    assert not any(e["kind"] == "not_found" for e in result["errors"])
    # Company 503, decision timeout, malformed decision and malformed Tech Intel may have been charged.
    assert result["credits"]["requests_with_uncertain_charge"] == 4
    assert len(transport.sent) == 14 and transport.unused() == []


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", config="budget.config.json", max_credits=1.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "partial"
    assert rows["row-1"]["contacts"]["selected"][0]["profile_error"]["kind"] == "budget_exceeded"
    assert rows["row-1"]["tech_intel"]["status"] == "not_asked"
    assert rows["row-2"]["status"] == "not_processed"
    assert len(transport.sent) == 2


def test_bad_tool_stops_before_paid_calls():
    result, transport = run("bad-tool", config="bad-tool.config.json")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    msgs = [e["message"] for e in result["errors"]]
    assert any("is_imaginary" in m for m in msgs) and any("not a person" in m for m in msgs)
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Beta</b>" not in html and "&lt;b&gt;Beta&lt;/b&gt;" in html
    assert "<script>alert" not in html
    assert '<div class="template">' in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["h1", "h2", "h3", "h4", "h5"]
    assert rows[1]["duplicate_of"] == "h1"
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_plan_counts_upper_bounds():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 3          # 5 rows, 1 duplicate, 1 invalid
    assert calls["POST /people/search"].requests == 6
    assert calls["GET /people"].requests == 9
    assert calls["POST /decisions"].requests == 24      # 3 x (2 company checks + 3 contacts x 2 checks)
    assert calls["GET /tech-intel"].requests == 6
    assert calls["POST /ai-adoption"].requests == 3


def test_plan_omits_disabled_sections():
    cfg = load_config(json.loads((F / "limits.config.json").read_text()))
    rows = RECIPE.read_input(F / "limits.input.csv")
    endpoints = [c.endpoint for c in plan(rows, cfg)]
    assert endpoints == ["GET /company", "POST /people/search", "GET /tech-intel"]


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
    (lambda c: c["company_checks"][0].update(want="maybe"), "want must be yes or no"),
    (lambda c: c["contact_checks"][0].update(id="b2b"), "used twice"),
    (lambda c: c["company_checks"][0].update(extra=1), "has unknown key"),
    (lambda c: c["contacts"].update(selected_country="GB"), "selected_country must be one of"),
    (lambda c: c["contacts"]["searches"][0].update(seniority=4), "seniority must be a seniority id"),
    (lambda c: c["contacts"]["searches"][0].update(department="18"), "department must be a department id"),
    (lambda c: c["contacts"]["searches"][1].pop("seniority"), "needs at least one of"),
    (lambda c: c["contacts"].update(max_contacts=51), "max_contacts must be a whole number from 0 to 50"),
    (lambda c: c["contacts"].update(searches=[]), "contact_checks need at least one"),
    (lambda c: c["tech_intel"].update(max_pages=0), "max_pages must be a whole number from 1 to 20"),
    (lambda c: c["ai_adoption"].update(enabled="yes"), "ai_adoption.enabled must be true or false"),
    (lambda c: c["tech_intel"].update(cursor="x"), "Unknown tech_intel key"),
])
def test_invalid_config(mutate, message):
    cfg = json.loads((F / "config.json").read_text())
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_config_defaults():
    cfg = load_config({})
    assert cfg["tech_intel"] == {"enabled": True, "with_evidence": False, "max_pages": 1}
    assert cfg["ai_adoption"] == {"enabled": False, "with_evidence": False}
    assert cfg["contacts"]["max_contacts"] == 3 and cfg["contacts"]["searches"] == []


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    # 3 profiles + 9 contacts + 24 answers with reasoning + 15 Tech Intel + 9 AI Adoption with evidence
    assert plan_json["published_estimate_upper_bound"] == 37.92
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
