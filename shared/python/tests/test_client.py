import json

import pytest

from firmable_client import CreditBudget, FirmableClient
from firmable_client.errors import (AuthenticationError, BudgetExceededError, ForbiddenError,
                                    InsufficientCreditsError, InvalidResponseError, NotFoundError,
                                    RateLimitedError, RedirectBlockedError, ServerError,
                                    TransportTimeoutError, BadRequestError)
from firmable_client.testing import TEST_KEY, FixtureError, FixtureTransport, make_client


def ex(method, path, response, query=None, body=None, times=1):
    req = {"method": method, "path": path}
    if query is not None:
        req["query"] = query
    if body is not None:
        req["body"] = body
    return {"request": req, "response": response, "times": times}


COMPANY = {"id": "f000000000001", "name": "Acme Example", "fqdn": "acme.example"}


def test_company_ok_records_ledger_without_key():
    client, t = make_client({"exchanges": [ex("GET", "/company", {"json": COMPANY}, {"fqdn": "acme.example"})]})
    assert client.get_company(fqdn="acme.example")["id"] == "f000000000001"
    entry = client.ledger.entries[0]
    assert entry["outcome"] == "ok" and entry["published_estimate"] == 1.0
    assert TEST_KEY not in json.dumps(client.ledger.entries)
    assert "headers" not in entry


def test_identical_request_is_cached_not_resent():
    client, t = make_client({"exchanges": [ex("GET", "/company", {"json": COMPANY}, {"fqdn": "acme.example"})]})
    client.get_company(fqdn="acme.example")
    client.get_company(fqdn="acme.example")
    assert len(t.sent) == 1
    assert client.ledger.entries[1]["outcome"] == "cache_hit"
    assert client.budget.published == 1.0


def test_budget_stops_before_request_is_sent():
    client, t = make_client({"exchanges": []}, max_credits=0.5)
    with pytest.raises(BudgetExceededError):
        client.get_company(fqdn="acme.example")
    assert t.sent == []
    assert client.ledger.entries[0]["outcome"] == "not_sent"


def test_fractional_reservation_adds_one_allowance_per_category():
    budget = CreditBudget(10)
    assert budget.reserve([("decision", 0.04)], "x") == 1.04
    assert budget.reserve([("decision", 0.04)], "x") == 0.04
    assert budget.reserved == 1.08


@pytest.mark.parametrize("status,cls", [
    (400, BadRequestError), (401, AuthenticationError), (402, InsufficientCreditsError),
    (403, ForbiddenError), (404, NotFoundError), (429, RateLimitedError), (500, ServerError),
    (502, ServerError),
])
def test_status_mapping_and_no_retry(status, cls):
    resp = {"status": status, "json": {"error": {"code": "x_code", "message": "went wrong"}},
            "headers": {"Retry-After": "3"}}
    client, t = make_client({"exchanges": [ex("GET", "/company", resp)]})
    with pytest.raises(cls) as info:
        client.get_company(fqdn="acme.example")
    assert len(t.sent) == 1  # never retried
    assert info.value.status == status and info.value.message == "went wrong"
    if status == 429:
        assert info.value.retry_after == "3"
    if status >= 500:
        assert info.value.charge_uncertain and client.budget.uncertain_requests == 1
    else:
        assert not info.value.charge_uncertain and client.budget.reserved == 0


def test_free_endpoint_5xx_is_not_charge_uncertain():
    client, _ = make_client({"exchanges": [ex("GET", "/decisions", {"status": 503, "text": "busy"})]})
    with pytest.raises(ServerError) as info:
        client.list_decision_tools()
    assert not info.value.charge_uncertain
    assert "busy" in info.value.message


def test_timeout_is_charge_uncertain_and_not_retried():
    client, t = make_client({"exchanges": [ex("GET", "/company", {"timeout": True})]})
    with pytest.raises(TransportTimeoutError) as info:
        client.get_company(fqdn="acme.example")
    assert info.value.charge_uncertain
    assert len(t.sent) == 1
    assert client.budget.uncertain_requests == 1
    assert client.budget.reserved == 1.0  # kept: the server may have charged


def test_non_json_success_is_invalid_response():
    client, _ = make_client({"exchanges": [ex("GET", "/company", {"status": 200, "text": "<html>oops</html>"})]})
    with pytest.raises(InvalidResponseError):
        client.get_company(fqdn="acme.example")


def test_malformed_decision_is_invalid_response():
    client, _ = make_client({"exchanges": [ex("POST", "/decisions", {"json": {"decision": "maybe"}})]})
    with pytest.raises(InvalidResponseError):
        client.make_decision("is_b2b", {"domain": "acme.example"})


def test_redirect_is_blocked():
    client, _ = make_client({"exchanges": [ex("GET", "/company", {"status": 302, "headers": {"Location": "https://evil.example/"}})]})
    with pytest.raises(RedirectBlockedError):
        client.get_company(fqdn="acme.example")


def test_key_in_error_body_is_redacted():
    body = {"error": f"bad key {TEST_KEY}"}
    client, _ = make_client({"exchanges": [ex("GET", "/company", {"status": 401, "json": body})]})
    with pytest.raises(AuthenticationError):
        client.get_company(fqdn="acme.example")
    assert TEST_KEY not in json.dumps(client.ledger.entries)
    assert "[REDACTED]" in json.dumps(client.ledger.entries)


def test_decision_reasoning_query_and_reported_credits():
    resp = {"json": {"decision": "yes", "credits": 0.08, "error": None}}
    client, _ = make_client({"exchanges": [ex("POST", "/decisions", resp, {"withReasoning": "true"},
                                              {"tool": "is_b2b", "question": {"domain": "acme.example"}})]})
    assert client.make_decision("is_b2b", {"domain": "acme.example"}, with_reasoning=True)["decision"] == "yes"
    assert client.budget.reported == 0.08


def page(items, cursor=None, total=3):
    d = {"id": "f1", "total": total, "tech_intelligence": items}
    if cursor:
        d["next_cursor"] = cursor
    return {"json": d}


def test_tech_intel_follows_cursor_to_last_page():
    client, _ = make_client({"exchanges": [
        ex("GET", "/tech-intel", page([{"product_slug": "a"}], "c1"), {"fqdn": "acme.example"}),
        ex("GET", "/tech-intel", page([{"product_slug": "b"}]), {"fqdn": "acme.example", "cursor": "c1"}),
    ]})
    r = client.get_tech_intel({"fqdn": "acme.example"}, max_pages=5)
    assert r["complete"] and r["pages_fetched"] == 2 and len(r["items"]) == 2
    assert client.budget.published == 5.0  # company charge once, no evidence


def test_tech_intel_page_limit_is_incomplete():
    client, _ = make_client({"exchanges": [
        ex("GET", "/tech-intel", page([{"product_slug": "a"}], "c1"), {"fqdn": "acme.example", "withEvidence": "true"}),
    ]})
    r = client.get_tech_intel({"fqdn": "acme.example"}, with_evidence=True, max_pages=1)
    assert not r["complete"] and r["stop_reason"] == "page_limit"
    assert client.budget.published == 5.2


def test_tech_intel_repeated_cursor_stops():
    client, _ = make_client({"exchanges": [
        ex("GET", "/tech-intel", page([{"product_slug": "a"}], "c1"), {"fqdn": "acme.example"}),
        ex("GET", "/tech-intel", page([{"product_slug": "b"}], "c1"), {"fqdn": "acme.example", "cursor": "c1"}),
    ]})
    r = client.get_tech_intel({"fqdn": "acme.example"}, max_pages=5)
    assert r["stop_reason"] == "repeated_cursor" and not r["complete"] and len(r["items"]) == 2


def test_tech_intel_keeps_partial_pages_on_error():
    client, _ = make_client({"exchanges": [
        ex("GET", "/tech-intel", page([{"product_slug": "a"}], "c1"), {"fqdn": "acme.example"}),
        ex("GET", "/tech-intel", {"status": 500, "text": "boom"}, {"fqdn": "acme.example", "cursor": "c1"}),
    ]})
    r = client.get_tech_intel({"fqdn": "acme.example"}, max_pages=5)
    assert r["stop_reason"] == "error" and len(r["items"]) == 1 and r["error"]["status"] == 500


def test_tech_intel_budget_exhaustion_mid_paging():
    client, t = make_client({"exchanges": [
        ex("GET", "/tech-intel", page([{"product_slug": "a"}], "c1"), {"fqdn": "acme.example", "withEvidence": "true"}),
    ]}, max_credits=6.3)
    r = client.get_tech_intel({"fqdn": "acme.example"}, with_evidence=True, max_pages=5)
    # first page holds 5 + 0.2 + 1 allowance = 6.2; second page needs 0.2 more, over 6.3
    assert r["stop_reason"] == "budget" and len(t.sent) == 1 and len(r["items"]) == 1


def test_people_search_rejects_unsupported_fields():
    client, _ = make_client({"exchanges": []})
    with pytest.raises(ValueError):
        client.search_people({"companyId": "f1", "name": "Jane"})
    with pytest.raises(ValueError):
        client.search_people({"position": "CTO"})


def test_unmatched_request_fails_loudly():
    client, _ = make_client({"exchanges": []})
    with pytest.raises(FixtureError):
        client.list_decision_tools()


def test_wrong_bearer_token_is_detected():
    t = FixtureTransport({"exchanges": []})
    c = FirmableClient("fbl_dev_other", budget=CreditBudget(1), transport=t, min_interval=0)
    with pytest.raises(FixtureError):
        c.list_decision_tools()


def test_throttle_waits_between_requests_to_same_endpoint():
    waits = []
    now = [0.0]
    t = FixtureTransport({"exchanges": [ex("GET", "/decisions", {"json": {"tools": []}}, times=1),
                                        ex("GET", "/decisions", {"json": {"tools": [{"name": "x"}]}}, times=1)]})
    c = FirmableClient(TEST_KEY, budget=CreditBudget(1), transport=t, min_interval=0.1,
                       sleep=lambda s: waits.append(round(s, 3)), clock=lambda: now[0])
    c.list_decision_tools()
    c._cache.clear()
    c.list_decision_tools()
    assert waits == [0.1]


def test_people_search_no_match_returns_empty_records():
    client, _ = make_client({"exchanges": [ex("POST", "/people/search", {"json": {"success": False, "totalPeople": 0}})]})
    r = client.search_people({"companyId": "f900000000001"})
    assert r["records"] == [] and r["total"] == 0
    assert client.ledger.entries[0]["outcome"] == "ok"
    assert "records" not in client.ledger.entries[0]["response"]


def test_decision_batch_reports_top_level_credits():
    resp = {"json": {"tool": "is_b2b", "credits": 0.28, "usage": {"billable_rows": 7},
                     "results": [{"decision": "yes", "credits": None}, {"decision": "unknown", "credits": None}]}}
    client, _ = make_client({"exchanges": [ex("POST", "/decisions", resp)]})
    client.make_decision("is_b2b", {"records": [{"domain": "a.example"}, {"domain": "b.example"}]})
    assert client.budget.reported == 0.28
