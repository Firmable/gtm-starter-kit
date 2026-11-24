import csv
import json
import threading
import urllib.error
import urllib.request

import pytest

from firmable_client.cli import main as cli_main
from firmable_client.errors import ConfigError
from firmable_client.testing import fixtures_dir, make_client, run_with_fixtures
from inbound_lead_router.cli import main as recipe_cli
from inbound_lead_router.recipe import RECIPE, load_config, plan
from inbound_lead_router.server import MAX_BODY, handle_request, make_server
from inbound_lead_router.server import main as serve_main

F = fixtures_dir("inbound-lead-router")


def run(name, config="config.json", max_credits=100.0, out=None):
    return run_with_fixtures(RECIPE, input_path=F / f"{name}.input.csv", config_path=F / config,
                             scenario=F / f"{name}.scenario.json", max_credits=max_credits, out_dir=out)


def by_id(result):
    return {r["row_id"]: r for r in result["rows"]}


def config(name="config.json"):
    return load_config(json.loads((F / name).read_text()))


def test_first_rule_that_is_not_ruled_out_decides():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["l1"]["status"] == "routed" and rows["l1"]["queue"] == "Enterprise AE"
    assert rows["l1"]["matched_rule"] == "enterprise_exec"
    # Every rule is evaluated and shown, including rules after the match.
    assert [r["outcome"] for r in rows["l1"]["rules"]] == ["not_matched", "matched", "matched", "not_matched", "matched"]
    l2 = rows["l2"]
    assert l2["matched_rule"] == "enterprise"
    assert l2["rules"][1]["failing_condition"] == "icp_fit"
    assert l2["rules"][1]["reason"] == "icp_fit not met: wanted yes, observed no."
    assert rows["l3"]["queue"] == "NZ team" and rows["l3"]["person"]["status"] == "no_work_email"
    assert rows["l5"]["matched_rule"] == "commercial_fit"
    assert rows["l6"]["status"] == "routed_default" and rows["l6"]["queue"] == "Marketing nurture"
    assert rows["l12"]["matched_rule"] == "software"
    assert transport.unused() == []


def test_missing_fact_goes_to_review_not_to_a_later_rule():
    rows = by_id(run("happy")[0])
    l7 = rows["l7"]
    assert l7["status"] == "needs_review" and l7["queue"] == "Sales ops review"
    assert l7["rules"][0]["outcome"] == "skipped_missing_fact" and l7["rules"][0]["missing_facts"] == ["hq_country"]
    assert l7["person"]["status"] == "not_found"
    assert l7["icp"]["answer"] == "unknown"
    l11 = rows["l11"]
    assert l11["status"] == "needs_review" and l11["rules"][1]["missing_facts"] == ["seniority"]
    assert "seniority" in l11["status_reason"] and l11["person"]["status"] == "found"


def test_seniority_rule_does_not_apply_without_a_work_email():
    rows = by_id(run("happy")[0])
    l14 = rows["l14"]
    assert l14["person"]["status"] == "no_work_email"
    assert l14["rules"][1]["outcome"] == "not_matched" and l14["rules"][1]["failing_condition"] == "seniority_in"
    assert "no person to check" in l14["rules"][1]["reason"]
    assert l14["status"] == "routed" and l14["matched_rule"] == "enterprise"


def test_personal_email_and_domain_rules():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["l4"]["status"] == "personal_email" and rows["l4"]["queue"] == "Self-serve"
    assert rows["l4"]["company"] is None
    assert rows["l13"]["status"] == "personal_email"
    l5 = rows["l5"]
    assert l5["identifier"]["domain"] == "delta.example" and l5["identifier"]["domain_source"] == "domain"
    assert l5["person"]["status"] == "personal_email"
    assert len(l5["flags"]) == 2
    l6 = rows["l6"]
    assert l6["identifier"]["domain"] == "other.example"
    assert "differs from the work email domain eps.example" in l6["flags"][0]
    assert "theta-group.example differs" in rows["l12"]["flags"][0]
    assert "f900000000099" in rows["l2"]["flags"][0]
    # No call was made for either personal email lead.
    assert not any("personal.example" in json.dumps(r.query) or "outlook.com" in json.dumps(r.query) for r in transport.sent)


def test_invalid_missing_and_duplicate_rows():
    result, transport = run("happy")
    rows = by_id(result)
    assert rows["l9"]["status"] == "invalid_input" and "is not an email address" in rows["l9"]["status_reason"]
    assert rows["l10"]["status"] == "company_not_found" and rows["l10"]["rules"] == []
    assert rows["l8"]["duplicate_of"] == "l1" and rows["l8"]["matched_rule"] == "enterprise_exec"
    assert len([r for r in transport.sent if r.path == "/company"]) == 10
    assert len([r for r in transport.sent if r.path == "/decisions"]) == 9
    assert len([r for r in transport.sent if r.path == "/people"]) == 6
    assert sum(1 for e in result["ledger"] if e["outcome"] == "cache_hit") == 3
    assert result["summary"]["review_rows"] == ["l7", "l9", "l10", "l11"]


def test_icp_unknown_never_matches_yes_or_no():
    result, transport = run("icp-unknown", config="icp-unknown.config.json")
    rows = by_id(result)
    assert rows["u1"]["matched_rule"] == "icp_unknown"
    assert rows["u2"]["matched_rule"] == "icp_yes"
    assert rows["u2"]["rules"][0]["outcome"] == "not_matched"
    assert rows["u3"]["status"] == "routed_default"
    assert all(r.query == {} for r in transport.sent if r.path == "/decisions")
    assert any("uses a decision model" in c for c in result["coverage"])


def test_errors_are_not_answers_and_run_stops_on_402():
    result, transport = run("errors")
    rows = by_id(result)
    assert rows["e1"]["status"] == "error" and rows["e1"]["error"]["status"] == 500
    assert rows["e1"]["queue"] == "Sales ops review"
    # The ICP request failed, but the rule that needed it was already ruled out by seniority.
    assert rows["e2"]["icp"]["error"]["status"] == 502 and rows["e2"]["matched_rule"] == "enterprise"
    assert rows["e3"]["status"] == "error" and rows["e3"]["icp"]["error"]["kind"] == "timeout"
    assert rows["e3"]["rules"][3]["outcome"] == "error"
    assert rows["e4"]["icp"]["error"]["kind"] == "invalid_response"
    assert rows["e5"]["status"] == "error" and rows["e5"]["person"]["error"]["status"] == 503
    assert rows["e6"]["error"]["kind"] == "insufficient_credits"
    assert rows["e7"]["status"] == "not_processed" and rows["e7"]["queue"] is None
    assert result["credits"]["requests_with_uncertain_charge"] == 5
    assert result["coverage"][0].startswith("The run stopped early")
    assert len(transport.sent) == 12


def test_budget_stops_before_next_paid_call():
    result, transport = run("budget", config="budget.config.json", max_credits=1.5)
    rows = by_id(result)
    assert rows["row-1"]["status"] == "routed"
    assert rows["row-2"]["status"] == "error" and result["errors"][0]["kind"] == "budget_exceeded"
    assert rows["row-3"]["status"] == "not_processed"
    assert len(transport.sent) == 1


def test_outputs_are_safe(tmp_path):
    run("happy", out=tmp_path)
    html = (tmp_path / "report.html").read_text()
    assert "<b>Theta</b>" not in html and "&lt;b&gt;Theta&lt;/b&gt;" in html
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["row_id"] for r in rows] == [f"l{i}" for i in range(1, 15)]
    assert rows[11]["source"].startswith("'=HYPERLINK")
    assert rows[0]["rule_results"].startswith("nz_leads=not_matched; enterprise_exec=matched")
    assert "fbl_dev_" not in (tmp_path / "results.json").read_text()


def test_plan_counts_unique_business_domains_and_emails():
    rows = RECIPE.read_input(F / "happy.input.csv")
    calls = {c.endpoint: c for c in plan(rows, config())}
    assert calls["GET /company"].requests == 10    # 14 rows: 2 personal, 1 invalid, 1 duplicate
    assert calls["POST /decisions"].requests == 10
    assert calls["GET /people"].requests == 7      # personal and missing emails are not looked up
    assert [c.endpoint for c in plan(rows, config("basic.config.json"))] == ["GET /company"]


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.update(rules=[]), "rules must be a non-empty list"),
    (lambda c: c.pop("review_queue"), "review_queue must be a non-empty queue name"),
    (lambda c: c["rules"][1].update(id="nz_leads"), "used twice"),
    (lambda c: c["rules"][0].update(queue=""), "queue must be a non-empty queue name"),
    (lambda c: c["rules"][0].update(when={}), "at least one condition"),
    (lambda c: c["rules"][0]["when"].update(states_any=["VIC"]), "unknown key"),
    (lambda c: c["rules"][1]["when"].update(employee_count_max=10), "more than employee_count_max"),
    (lambda c: c["rules"][1]["when"].update(icp_fit="maybe"), "icp_fit must be yes, no or unknown"),
    (lambda c: c.pop("icp"), "icp_fit needs icp in the configuration"),
    (lambda c: c.update(fetch_person=False), "seniority_in needs fetch_person set to true"),
    (lambda c: c["icp"].update(description="Mid-market SaaS"), "icp.description is not accepted"),
    (lambda c: c["icp"].update(staff_in="UK"), "icp.staff_in must be one of"),
    (lambda c: c["icp"].update(revenue_bands=["$2M"]), "not a Firmable revenue band"),
    (lambda c: c["icp"].update(criteria=["x"]), "icp.criteria must be a list"),
    (lambda c: c["icp"].update(colour="orange"), "unknown key"),
    (lambda c: c.update(personal_email_domains=["not a domain"]), "personal_email_domains[1] is not a valid domain"),
    (lambda c: c.update(with_reasoning="yes"), "with_reasoning must be true or false"),
    (lambda c: c["rules"][0].update(id="1rule"), "must start with a lower-case letter"),
    (lambda c: c.update(colour="orange"), "unknown key"),
])
def test_invalid_config(mutate, message):
    cfg = json.loads((F / "config.json").read_text())
    mutate(cfg)
    with pytest.raises(ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_config(cfg)


def test_cli_plan_mode_makes_no_calls(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    code = cli_main(RECIPE, ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"),
                             "--out", str(tmp_path)])
    assert code == 0
    plan_json = json.loads((tmp_path / "plan.json").read_text())
    assert plan_json["published_estimate_upper_bound"] == 17.8  # 10 profiles, 10 answers at 0.08, 7 people
    assert "no API calls were made" in capsys.readouterr().out


def test_recipe_cli_keeps_batch_behaviour(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    with pytest.raises(SystemExit) as e:
        recipe_cli(args)
    assert e.value.code == 0 and (tmp_path / "plan.json").exists()
    with pytest.raises(SystemExit) as e:
        recipe_cli(args + ["--live"])
    assert e.value.code == 2


def test_cli_live_needs_limit_and_key(tmp_path, monkeypatch):
    args = ["--input", str(F / "happy.input.csv"), "--config", str(F / "config.json"), "--out", str(tmp_path)]
    assert cli_main(RECIPE, args + ["--live"]) == 2
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert cli_main(RECIPE, args + ["--live", "--max-credits", "5"]) == 2


def test_cli_rejects_bad_input(tmp_path):
    bad = tmp_path / "in.csv"
    bad.write_text("name\nAcme\n")
    assert cli_main(RECIPE, ["--input", str(bad), "--config", str(F / "config.json"), "--out", str(tmp_path)]) == 2


# ----- single-lead server ---------------------------------------------------------------------------

LEAD = json.loads((F / "serve.input.json").read_text())[0]


def factory(max_credits=5.0):
    return lambda: make_client(F / "serve.scenario.json", max_credits)[0]


def test_handle_request_routes_one_lead_like_the_batch():
    cfg = config("serve.config.json")
    status, body = handle_request("POST", "/route", json.dumps(LEAD).encode(), config=cfg, client_factory=factory())
    assert status == 200
    batch = run_with_fixtures(RECIPE, input_path=F / "serve.input.json", config_path=F / "serve.config.json",
                              scenario=F / "serve.scenario.json")[0]["rows"][0]
    assert body == batch
    assert body["status"] == "routed" and body["queue"] == "Enterprise AE"
    assert "ledger" not in body and "fbl_dev_" not in json.dumps(body)


def test_handle_request_rejects_bad_requests():
    cfg = config("serve.config.json")

    def call(method, path, body):
        return handle_request(method, path, body, config=cfg, client_factory=factory())

    assert call("POST", "/other", b"{}")[0] == 404
    assert call("GET", "/route", b"")[0] == 405
    assert call("POST", "/route", b"x" * (MAX_BODY + 1))[0] == 413
    assert call("POST", "/route", b"{not json")[0] == 400
    assert call("POST", "/route", b"[1]")[0] == 400
    assert call("POST", "/route", json.dumps({"lead_name": "No identifier"}).encode())[1]["error"]["code"] == "bad_lead"
    assert call("POST", "/route", json.dumps({"domain": {"nested": 1}}).encode())[0] == 400


def test_each_request_gets_a_fresh_budget():
    cfg = config("serve.config.json")
    status, body = handle_request("POST", "/route", json.dumps(LEAD).encode(), config=cfg,
                                  client_factory=factory(max_credits=0.5))
    assert status == 200 and body["status"] == "error" and body["error"]["kind"] == "budget_exceeded"
    status, body = handle_request("POST", "/route", json.dumps(LEAD).encode(), config=cfg, client_factory=factory())
    assert body["status"] == "routed"


def test_server_on_port_zero_routes_one_lead():
    server = make_server(config("serve.config.json"), factory(), port=0)
    host, port = server.server_address[:2]
    assert host == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/route", data=json.dumps(LEAD).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") is None
            body = json.loads(resp.read())
        assert body["matched_rule"] == "enterprise_exec"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/route", timeout=10)
        assert e.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_needs_limit_and_key(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        serve_main(["--config", str(F / "config.json")])  # --max-credits-per-lead is required
    monkeypatch.delenv("FIRMABLE_API_KEY", raising=False)
    assert serve_main(["--config", str(F / "config.json"), "--max-credits-per-lead", "2"]) == 2
    assert "FIRMABLE_API_KEY" in capsys.readouterr().err
