import csv
import json

import pytest

from account_qualification.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("account-qualification")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def test_happy_path_statuses_follow_policy(tmp_path):
    result, transport = run("happy", out=tmp_path)
    rows = by_id(result)
    assert rows["r1"]["status"] == "qualified"
    assert rows["r2"]["status"] == "disqualified"
    assert rows["r3"]["status"] == "needs_review"          # unknown mandatory never qualifies
    assert rows["r3"]["missing_information"] == ["Head office in Australia"]
    assert rows["r4"]["status"] == "qualified" and rows["r4"]["duplicate_of"] == "r1"
    assert rows["r5"]["status"] == "invalid_input"
    assert rows["r6"]["status"] == "company_not_found" and rows["r6"]["checks"] == []
    assert rows["r7"]["status"] == "not_qualified"
    assert rows["r8"]["status"] == "qualified" and "1 optional check(s) answered unknown" in rows["r8"]["status_reason"]
    assert transport.unused() == []
    # The duplicate row reused responses: one company call per unique domain.
    company_calls = [r for r in transport.sent if r.path == "/company"]
    assert len(company_calls) == 6


def test_reasoning_and_answer_metadata_are_preserved():
    result, _ = run("happy")
    check = by_id(result)["r2"]["checks"][0]
    assert check["answer"] == "no" and check["result"] == "fail"
    assert check["reasoning"]["reason"] == "Sells to consumers."
    assert check["confidence"] == 0.95 and check["credits"] == 0.08


def test_unknown_is_distinct_from_no():
    result, _ = run("happy")
    gamma = by_id(result)["r3"]["checks"][1]
    assert gamma["answer"] == "unknown" and gamma["result"] == "unknown"


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    e1 = {c["check_id"]: c for c in rows["e1"]["checks"]}
    assert e1["au_head_office"]["result"] == "error" and e1["au_head_office"]["error"]["status"] == 502
    assert e1["size_200_plus"]["error"]["kind"] == "timeout"
    assert rows["e1"]["status"] == "incomplete"
    assert by_id(result)["e2"]["checks"][0]["error"]["kind"] == "invalid_response"
    assert rows["e3"]["checks"][1]["result"] == "not_asked"
    assert rows["e4"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert result["coverage"][0].startswith("The run stopped early")
    # 13 exchanges in the scenario, none retried.
    assert len(transport.sent) == 13


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", max_credits=2.1)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "incomplete"
    assert rows["row-2"]["status"] == "not_processed"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert len(transport.sent) == 3


def test_bad_tool_stops_before_paid_calls():
    result, transport = run("bad-tool", config="bad-tool.config.json")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    msgs = [e["message"] for e in result["errors"]]
    assert any("is_imaginary" in m for m in msgs) and any("colour" in m for m in msgs)
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Eps</b>" not in html and "&lt;b&gt;Eps&lt;/b&gt;" in html
    assert "<script>alert" not in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == ["r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8"]
    data = json.loads((tmp_path / "results.json").read_text())
    assert "fbl_dev_" not in json.dumps(data)


def test_plan_counts_unique_valid_domains():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, cfg)}
    assert calls["GET /company"].requests == 6       # 8 rows, 1 duplicate, 1 invalid
    assert calls["POST /decisions"].requests == 24


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(checks=[]), "checks must be a non-empty list"),
    (lambda c: c["checks"][0].update(want="maybe"), "want must be yes or no"),
    (lambda c: c["checks"][1].update(id="b2b"), "used twice"),
    (lambda c: c.update(policy={"min_optional_passes": 5}), "only 2 optional"),
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
    assert plan_json["published_estimate_upper_bound"] == 7.92   # 6 profiles + 24 answers with reasoning
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
