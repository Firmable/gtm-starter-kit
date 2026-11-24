import csv
import json

import pytest

from ai_adoption_segmentation.recipe import RECIPE, load_config, plan
from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, run_with_fixtures

F = fixtures_dir("ai-adoption-segmentation")


def run(name, max_credits=100.0, out=None):
    cfg = f"{name}.config.json" if (F / f"{name}.config.json").exists() else "config.json"
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / cfg,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def test_first_matching_segment_wins():
    result, transport = run("happy")
    rows = by_id(result)
    assert (rows["h1"]["status"], rows["h1"]["segment_id"]) == ("assigned", "ai_leaders")
    assert (rows["h2"]["status"], rows["h2"]["segment_id"]) == ("assigned", "ai_expanding")
    assert [t["outcome"] for t in rows["h2"]["trace"]] == ["not_matched", "matched"]
    assert rows["h3"]["segment_id"] == "ai_tooling" and "Claude (Anthropic)" in rows["h3"]["status_reason"]
    assert rows["h4"]["segment_id"] == "low_adoption"
    assert (rows["h8"]["segment_id"], rows["h8"]["duplicate_of"]) == ("ai_leaders", "h1")
    assert rows["h9"]["status"] == "invalid_input"
    assert (rows["h12"]["status"], rows["h12"]["segment_id"]) == ("no_segment_matched", "unmatched")
    assert transport.unused() == []
    assert result["summary"]["segment_counts"] == {"ai_leaders": 3, "ai_expanding": 1, "ai_tooling": 1,
                                                   "low_adoption": 1, "review": 4, "unmatched": 1}


def test_tech_intel_is_requested_only_when_a_technology_condition_is_tried():
    result, transport = run("happy")
    tech_ids = [r.query["id"] for r in transport.sent if r.path == "/tech-intel"]
    assert tech_ids == ["f910000000003", "f910000000004", "f910000000005", "f910000000010"]
    assert by_id(result)["h1"]["technologies"] is None


def test_missing_data_goes_to_review_not_a_lower_segment():
    rows = by_id(run("happy")[0])
    assert (rows["h5"]["status"], rows["h5"]["segment_id"]) == ("needs_review", "review")
    assert "only 1 of 230 technologies were retrieved" in rows["h5"]["status_reason"]
    assert rows["h6"]["status"] == "needs_review" and rows["h6"]["ai_adoption_found"] is False
    assert "no AI Adoption data" in rows["h6"]["status_reason"]
    assert rows["h10"]["status"] == "needs_review" and "workforce area was not returned" in rows["h10"]["status_reason"]
    # The complete technology list with no AI tool is a valid no, so delta reaches the next segment.
    delta = rows["h4"]["trace"][2]["conditions"][1]
    assert delta["outcome"] == "not_met" and "observed in retrieved data" in delta["explanation"]


def test_unfamiliar_levels():
    result, _ = run("happy")
    rows = by_id(result)
    assert rows["h7"]["status"] == "needs_review" and rows["h7"]["trace"] == []
    assert "overall_level EXTREME" in rows["h7"]["status_reason"]
    # An unfamiliar level in an area no rule reads is reported but does not block the account.
    assert rows["h12"]["unfamiliar_levels"] == ["dimensions.hiring.level=EXTREME"]
    assert rows["h12"]["status"] == "no_segment_matched"
    assert any("not in the configured list" in c for c in result["coverage"])


def test_low_observability_is_surfaced():
    rows = by_id(run("happy")[0])
    assert rows["h11"]["segment_id"] == "ai_leaders"
    assert "observability for this area is LOW" in rows["h11"]["status_reason"]


def test_evidence_summary_separates_qualifying_and_truncated():
    result, transport = run("evidence")
    row = result["rows"][0]
    assert transport.sent[0].query == {"withEvidence": "true"}
    wf = row["evidence_summary"]["workforce"]
    assert wf == {"item_count": 40, "items_returned": 2, "qualifying": 1, "not_qualifying": 1, "truncated": True,
                  "as_of": "2026-11-05"}
    assert any("1 evidence area(s) are truncated" in c for c in result["coverage"])


def test_decisions_are_asked_only_when_needed():
    result, transport = run("decisions")
    rows = by_id(result)
    assert rows["d1"]["segment_id"] == "b2b_ai_leaders"
    assert rows["d1"]["decisions"][0]["reasoning"]["reason"] == "Sells software to businesses."
    assert (rows["d2"]["status"], rows["d2"]["segment_id"]) == ("needs_review", "manual_review")
    assert "it is not a no" in rows["d2"]["status_reason"]
    assert rows["d3"]["segment_id"] == "everyone_else" and rows["d3"]["decisions"] == []
    assert (rows["d4"]["status"], rows["d4"]["segment_id"]) == ("incomplete", "manual_review")
    assert len([r for r in transport.sent if r.path == "/decisions" and r.method == "POST"]) == 3
    assert any("is_b2b" in c and "decision model" in c for c in result["coverage"])


def test_bad_tool_stops_before_paid_calls():
    result, transport = run("bad-tool")
    assert [r["status"] for r in result["rows"]] == ["not_processed"]
    assert [r.path for r in transport.sent] == ["/decisions"]


def test_errors_are_incomplete_and_402_stops_the_run():
    result, transport = run("errors")
    rows = by_id(result)
    for rid in ("e1", "e2", "e3", "e4", "e5", "e6"):
        assert (rows[rid]["status"], rows[rid]["segment_id"]) == ("incomplete", "review"), rid
    kinds = [e["kind"] for e in result["errors"]]
    assert kinds == ["server_error", "timeout", "invalid_response", "answer_error", "forbidden", "insufficient_credits"]
    assert rows["e7"]["status"] == "not_processed"
    assert result["credits"]["requests_with_uncertain_charge"] == 3
    assert len(transport.sent) == 7


def test_budget_stops_before_tech_intel():
    result, transport = run("budget", max_credits=1.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "incomplete"
    assert result["errors"][0]["kind"] == "budget_exceeded"
    assert rows["row-2"]["status"] == "not_processed"
    assert len(transport.sent) == 1


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Hotel</b>" not in html and "&lt;b&gt;Hotel&lt;/b&gt;" in html
    assert "<script>alert" not in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == [f"h{i}" for i in range(1, 13)]
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_plan_counts_unique_valid_domains():
    cfg = load_config(json.loads((F / "config.json").read_text()))
    calls = {c.endpoint: c for c in plan(RECIPE.read_input(F / "happy.input.csv"), cfg)}
    assert calls["POST /ai-adoption"].requests == 10      # 12 rows, 1 duplicate, 1 invalid
    assert calls["GET /tech-intel"].requests == 10
    assert "POST /decisions" not in calls
    dcfg = load_config(json.loads((F / "decisions.config.json").read_text()))
    dcalls = {c.endpoint: c for c in plan(RECIPE.read_input(F / "decisions.input.csv"), dcfg)}
    assert dcalls["POST /decisions"].requests == 4 and "GET /tech-intel" not in dcalls


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(colour="orange"), "Unknown configuration key"),
    (lambda c: c.update(segments=[]), "segments must be a non-empty list"),
    (lambda c: c.update(levels=["NONE", "NONE"]), "distinct level names"),
    (lambda c: c["segments"][0]["conditions"][0].update(at_least="HUGE"), "not in levels: HUGE"),
    (lambda c: c["segments"][0]["conditions"][0].update(at_most="BASIC"), "exactly one of at_least, at_most or in"),
    (lambda c: c["segments"][0]["conditions"].append({"type": "vibes"}), "type must be one of"),
    (lambda c: c["segments"][0]["conditions"].append({"type": "technology", "min_confidence": "high"}),
     "needs at least one of"),
    (lambda c: c["segments"][0]["conditions"].append({"type": "decision", "check": "b2b"}),
     "must name an id from decision_checks"),
    (lambda c: c["segments"][1].update(id="ai_leaders"), "used twice"),
    (lambda c: c["segments"][1].update(id="review"), "clashes with the review"),
    (lambda c: c["segments"][0].update(match="most"), "match must be all or any"),
    (lambda c: c.update(decision_checks=[{"id": "b2b", "tool": "is_b2b"}]), "is not used by any segment"),
    (lambda c: c.update(tech_intel_max_pages=0), "tech_intel_max_pages"),
    (lambda c: c.update(with_evidence="yes"), "with_evidence must be true or false"),
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
    assert plan_json["published_estimate_upper_bound"] == 52.0   # 10 AI Adoption at 0.2, 10 Tech Intel at 5
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
