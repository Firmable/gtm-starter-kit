import csv
import json

import pytest

from duplicate_account_review.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError, InputError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("duplicate-account-review")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def test_outcomes_follow_the_rules(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["p1"]["status"] == "likely_duplicate"
    assert rows["p1"]["left_row_id"] == "crm-001" and rows["p1"]["right_row_id"] == "crm-101"
    assert rows["p2"]["status"] == "distinct"
    assert rows["p3"]["status"] == "likely_duplicate" and rows["p3"]["decision"] is None
    assert rows["p6"]["status"] == "needs_review" and rows["p6"]["decision"]["answer"] == "unknown"
    assert rows["p7"]["status"] == "needs_review" and rows["p7"]["left"]["found"] is False
    assert rows["p7"]["right"]["found"] is True
    assert transport.unused() == []


def test_identifiers_explain_support_and_conflict():
    rows = by_id(run("happy")[0])
    p1 = rows["p1"]
    # ABNs written with and without spaces compare as the same.
    abn = next(i for i in p1["identifiers"] if i["identifier"] == "abn")
    assert abn == {"identifier": "abn", "left": "11 000 000 001", "right": "11000000001", "comparison": "same"}
    assert p1["supporting_identifiers"] == ["abn", "linkedin", "hq_country"]
    assert "acn" in p1["missing_identifiers"]
    assert p1["decision"]["details"]["matched"] == "abn"
    assert p1["decision"]["reasoning"]["reason"] == "The two records share an ABN."


def test_contradictions_go_to_review():
    rows = by_id(run("happy")[0])
    assert rows["p4"]["status"] == "needs_review" and rows["p4"]["contradictions"] == ["abn differs"]
    assert rows["p4"]["decision"]["answer"] == "yes"
    assert rows["p5"]["status"] == "needs_review" and rows["p5"]["contradictions"] == ["linkedin is the same"]
    assert rows["p10"]["status"] == "needs_review" and rows["p10"]["decision"] is None
    assert "beta-old.example differs" in rows["p10"]["notes"][0]


def test_invalid_sides_and_duplicate_pairs():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["p9"]["status"] == "invalid_input"
    assert rows["p11"]["status"] == "invalid_input" and "f followed by 12 digits" in rows["p11"]["status_reason"]
    assert rows["p8"]["duplicate_of"] == "p2" and rows["p8"]["status"] == "distinct"
    assert rows["p8"]["decision"]["reused_from_row"] == "p2"
    # Only supplied pairs are compared: 5 decisions for 11 rows.
    assert len([r for r in transport.sent if r.path == "/decisions" and r.method == "POST"]) == 5
    assert len([r for r in transport.sent if r.path == "/company"]) == 13


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "incomplete" and rows["e1"]["decision"]["error"]["status"] == 502
    assert rows["e2"]["decision"]["error"]["kind"] == "timeout"
    assert rows["e3"]["status"] == "error" and rows["e3"]["error"]["status"] == 500
    assert rows["e4"]["decision"]["error"]["kind"] == "invalid_response"
    assert rows["e5"]["status"] == "incomplete"
    assert rows["e6"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 4
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 13


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=3.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "distinct"
    assert rows["row-2"]["status"] == "error" and result["errors"][0]["kind"] == "budget_exceeded"
    assert rows["row-3"]["status"] == "not_processed"
    assert len(transport.sent) == 4


def test_missing_tool_stops_before_paid_calls():
    result, transport = run("no-tool")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert any("is_same_business" in e["message"] for e in result["errors"])
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_without_reasoning():
    result, transport = run("no-reasoning", config="no-reasoning.config.json")
    assert result["rows"][0]["status"] == "likely_duplicate"
    assert result["rows"][0]["decision"]["reasoning"] is None
    assert transport.sent[-1].query == {}


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Zeta</b>" not in html and "&lt;b&gt;Zeta&lt;/b&gt;" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == [f"p{i}" for i in range(1, 12)]
    assert rows[10]["left_row_id"] == "'=SUM(1)"
    assert "fbl_dev_" not in json.dumps(json.loads((tmp_path / "results.json").read_text()))


def test_plan_counts_unique_sides_and_pairs():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 13     # unique valid sides
    assert calls["POST /decisions"].requests == 8    # 9 valid pairs, 1 reversed repeat; p3 counts because its keys differ


def test_input_needs_both_sides(tmp_path):
    bad = tmp_path / "pairs.csv"
    bad.write_text("left_domain,right_domain\nacme.example,\n")
    with pytest.raises(InputError, match="needs one of right_domain, right_company_id"):
        RECIPE.read_input(bad)


@pytest.mark.parametrize("raw,message", [
    ({"with_reasoning": "yes"}, "with_reasoning must be true or false"),
    ({"colour": "orange"}, "Unknown configuration key"),
])
def test_invalid_config(raw, message):
    with pytest.raises(ConfigError, match=message):
        load_config(raw)


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 13.64   # 13 profiles + 8 answers with reasoning
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
