"""Firmable REST client for the developer API.

- Live requests always go to https://api.firmable.com. The base URL cannot be changed.
- The API key is read by the caller (normally from FIRMABLE_API_KEY) and sent as a bearer token.
- Every request has a timeout. Nothing is retried automatically, because a retried request may be
  charged twice.
- Each request is checked against the credit budget before it is sent.
- Identical read requests in one run are answered from memory and not sent again.
- Every call, including refused and failed ones, is written to the ledger without the key.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import pricing, validate
from .budget import CreditBudget
from .errors import (FirmableError, InvalidResponseError, RedirectBlockedError,
                     TransportTimeoutError, error_for_status)
from .ledger import Ledger
from .transport import HttpRequest, HttpResponse, Transport, UrllibTransport

DEFAULT_TIMEOUT = 30.0
DEFAULT_MIN_INTERVAL = 1.0 / 12  # stays under the documented 15 requests a second per endpoint


class FirmableClient:
    def __init__(
        self,
        api_key: str,
        *,
        budget: CreditBudget,
        ledger: Ledger | None = None,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("An API key is required for live requests.")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._key = api_key
        self.budget = budget
        self.ledger = ledger or Ledger(secret=api_key)
        self._transport = transport or UrllibTransport()
        self._timeout = timeout
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        self._cache: dict[str, Any] = {}

    # ----- endpoints -------------------------------------------------------------------------

    def get_company(self, **identifier: str) -> dict[str, Any]:
        """GET /company with one identifier: fqdn, id, abn, website, ln_slug or ln_url."""
        _one_identifier(identifier, {"id", "fqdn", "abn", "website", "ln_slug", "ln_url"}, "company")
        return self._call("GET", "/company", query=identifier, parts=pricing.estimate_company(),
                          check=validate.company)

    def get_person(self, **identifier: str) -> dict[str, Any]:
        """GET /people with one identifier: id, work_email, personal_email, ln_slug or ln_url."""
        _one_identifier(identifier, {"id", "work_email", "personal_email", "ln_slug", "ln_url"}, "person")
        return self._call("GET", "/people", query=identifier, parts=pricing.estimate_person(),
                          check=validate.person)

    def search_people(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /people/search. companyId is required by the published schema. Free.

        With no matches the API returns success false and no records; this returns records [] and total 0.
        """
        if not body.get("companyId"):
            raise ValueError("people search needs companyId")
        allowed = {"companyId", "selectedCountry", "position", "seniority", "department", "from", "size"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"people search does not accept: {', '.join(sorted(unknown))}")
        resp = self._call("POST", "/people/search", body=body, parts=[], check=validate.people_search)
        if resp.get("success") is False and "records" not in resp:
            # Documented no-match response. The ledger keeps it as returned; callers get an empty list.
            return {**resp, "records": [], "total": resp.get("totalPeople", 0)}
        return resp

    def list_decision_tools(self) -> dict[str, Any]:
        """GET /decisions. Free."""
        return self._call("GET", "/decisions", parts=[], check=validate.decisions_list)

    def make_decision(self, tool: str, question: dict[str, Any], *, with_reasoning: bool = False) -> dict[str, Any]:
        """POST /decisions with {tool, question}. withReasoning is a query parameter."""
        answers = len(question["records"]) if isinstance(question.get("records"), list) else 1
        query = {"withReasoning": "true"} if with_reasoning else {}
        return self._call("POST", "/decisions", query=query, body={"tool": tool, "question": question},
                          parts=pricing.estimate_decision(answers, with_reasoning),
                          check=validate.decision, reported=_decision_credits)

    def get_ai_adoption(self, body: dict[str, Any], *, with_evidence: bool = False) -> dict[str, Any]:
        """POST /ai-adoption with company_id or domain, or records. withEvidence is a query parameter."""
        companies = len(body["records"]) if isinstance(body.get("records"), list) else 1
        query = {"withEvidence": "true"} if with_evidence else {}
        return self._call("POST", "/ai-adoption", query=query, body=body,
                          parts=pricing.estimate_ai_adoption(companies, with_evidence),
                          check=validate.ai_adoption)

    def get_tech_intel_page(self, identifier: dict[str, str], *, with_evidence: bool = False,
                            cursor: str | None = None) -> dict[str, Any]:
        """GET /tech-intel for one page."""
        _one_identifier(identifier, {"id", "fqdn", "abn", "website", "ln_slug", "ln_url"}, "company")
        query = dict(identifier)
        if with_evidence:
            query["withEvidence"] = "true"
        if cursor:
            query["cursor"] = cursor
        return self._call("GET", "/tech-intel", query=query,
                          parts=pricing.estimate_tech_intel_page(cursor is None, with_evidence),
                          check=validate.tech_intel)

    def get_tech_intel(self, identifier: dict[str, str], *, with_evidence: bool = False,
                       max_pages: int = 1) -> dict[str, Any]:
        """Follows next_cursor up to max_pages and reports whether the list is complete.

        Stops at the last page, the page limit, a repeated cursor, or an error. Pages already
        fetched are kept when a later page fails.

        Errors are returned in the result's "error" field, not raised, so partial pages survive.
        Callers must record that error themselves (see RunContext.record_error_dict) so a fatal
        error such as budget_exceeded still stops the run.
        """
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        pages = 0
        total = None
        company_id = None
        error = None
        stop = "last_page"
        while True:
            if pages >= max_pages:
                stop = "page_limit"
                break
            try:
                page = self.get_tech_intel_page(identifier, with_evidence=with_evidence, cursor=cursor)
            except FirmableError as e:
                error = e
                stop = "budget" if e.kind == "budget_exceeded" else "error"
                break
            pages += 1
            items.extend(page.get("tech_intelligence") or [])
            total = page.get("total", total)
            company_id = page.get("id", company_id)
            nxt = page.get("next_cursor")
            if not nxt:
                stop = "last_page"
                break
            if nxt in seen or nxt == cursor:
                stop = "repeated_cursor"
                break
            seen.add(nxt)
            cursor = nxt
        complete = stop == "last_page"
        if complete and pages == 0:
            complete = False
        return {
            "id": company_id,
            "total": total,
            "items": items,
            "pages_fetched": pages,
            "complete": complete,
            "stop_reason": stop,
            "error": error.to_dict() if error else None,
        }

    # ----- core ------------------------------------------------------------------------------

    def _call(self, method: str, path: str, *, query: dict[str, str] | None = None,
              body: Any = None, parts: list[tuple[str, float]],
              check: Callable[[Any], None],
              reported: Callable[[Any], float | None] | None = None) -> Any:
        query = {k: str(v) for k, v in (query or {}).items()}
        endpoint = f"{method} {path}"
        cache_key = json.dumps([method, path, sorted(query.items()), body], sort_keys=True)
        published = pricing.total(parts)
        base = {"endpoint": endpoint, "method": method, "path": path, "query": query, "body": body}

        if cache_key in self._cache:
            self.ledger.record(**base, outcome="cache_hit", status=None, published_estimate=0,
                               reserved=0, reported_credits=None, charge_uncertain=False,
                               note="Same request already made in this run; reused the response.")
            return self._cache[cache_key]

        try:
            hold = self.budget.reserve(parts, endpoint)
        except FirmableError as e:
            self.ledger.record(**base, outcome="not_sent", status=None, published_estimate=published,
                               reserved=0, reported_credits=None, charge_uncertain=False,
                               error=e.to_dict())
            raise

        self._throttle(endpoint)
        request = HttpRequest(method, path, query, body,
                              {"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
                              self._timeout)
        started = self._clock()
        try:
            response = self._transport.send(request)
        except TransportTimeoutError as e:
            e.endpoint = endpoint
            e.charge_uncertain = published > 0
            if e.charge_uncertain:
                self.budget.mark_uncertain()
            else:
                self.budget.release(hold, published)
            self.ledger.record(**base, outcome="timeout", status=None, published_estimate=published,
                               reserved=hold if e.charge_uncertain else 0, reported_credits=None,
                               charge_uncertain=e.charge_uncertain, error=e.to_dict(),
                               duration_ms=self._ms(started))
            raise
        except FirmableError as e:
            e.endpoint = endpoint
            self.budget.release(hold, published)
            self.ledger.record(**base, outcome="error", status=e.status, published_estimate=published,
                               reserved=0, reported_credits=None, charge_uncertain=False,
                               error=e.to_dict(), duration_ms=self._ms(started))
            raise

        return self._handle(response, base, endpoint, published, hold, started, check, reported,
                            cache_key)

    def _handle(self, response: HttpResponse, base: dict[str, Any], endpoint: str, published: float,
                hold: float, started: float, check: Callable[[Any], None],
                reported: Callable[[Any], float | None] | None, cache_key: str) -> Any:
        parsed: Any = None
        is_json = True
        try:
            parsed = json.loads(response.text) if response.text else None
        except ValueError:
            is_json = False

        if 300 <= response.status <= 399:
            err = RedirectBlockedError(
                f"The API answered with a redirect ({response.status}). Redirects are not followed, "
                "so the API key is not sent to another location.", status=response.status, endpoint=endpoint,
                charge_uncertain=published > 0)
            if err.charge_uncertain:
                self.budget.mark_uncertain()
            else:
                self.budget.release(hold, published)
            self.ledger.record(**base, outcome="error", status=response.status, published_estimate=published,
                               reserved=hold if err.charge_uncertain else 0, reported_credits=None,
                               charge_uncertain=err.charge_uncertain, error=err.to_dict(),
                               duration_ms=self._ms(started))
            raise err

        if not 200 <= response.status <= 299:
            message, code = _error_message(parsed, response)
            err = error_for_status(response.status, message, code=code, endpoint=endpoint,
                                   billable=published > 0, retry_after=response.headers.get("retry-after"))
            if err.charge_uncertain:
                self.budget.mark_uncertain()
            else:
                self.budget.release(hold, published)
            self.ledger.record(**base, outcome="error", status=response.status,
                               published_estimate=published,
                               reserved=hold if err.charge_uncertain else 0, reported_credits=None,
                               charge_uncertain=err.charge_uncertain, error=err.to_dict(),
                               response=parsed if is_json else response.text[:2000],
                               duration_ms=self._ms(started))
            raise err

        try:
            if not is_json:
                raise InvalidResponseError("The API returned a success status with a body that is not JSON.")
            check(parsed)
        except InvalidResponseError as e:
            e.endpoint = endpoint
            e.status = response.status
            e.charge_uncertain = published > 0
            if e.charge_uncertain:
                self.budget.mark_uncertain()
            self.ledger.record(**base, outcome="invalid_response", status=response.status,
                               published_estimate=published, reserved=hold, reported_credits=None,
                               charge_uncertain=e.charge_uncertain, error=e.to_dict(),
                               response=parsed if is_json else response.text[:2000],
                               duration_ms=self._ms(started))
            raise

        credits = reported(parsed) if reported else None
        self.budget.add_reported(credits)
        self.ledger.record(**base, outcome="ok", status=response.status, published_estimate=published,
                           reserved=hold, reported_credits=credits, charge_uncertain=False,
                           response=parsed, duration_ms=self._ms(started))
        self._cache[cache_key] = parsed
        return parsed

    def _throttle(self, endpoint: str) -> None:
        if self._min_interval <= 0:
            return
        last = self._last_sent.get(endpoint)
        now = self._clock()
        if last is not None and now - last < self._min_interval:
            self._sleep(self._min_interval - (now - last))
        self._last_sent[endpoint] = self._clock()

    def _ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)


def _one_identifier(identifier: dict[str, str], allowed: set[str], what: str) -> None:
    present = {k for k, v in identifier.items() if v not in (None, "")}
    if len(present) != 1 or not present <= allowed:
        raise ValueError(f"{what} lookup needs exactly one of: {', '.join(sorted(allowed))}")


def _error_message(parsed: Any, response: HttpResponse) -> tuple[str, str | None]:
    if isinstance(parsed, dict):
        err = parsed.get("error")
        code = None
        message = None
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message")
        elif isinstance(err, str):
            code = err if " " not in err else None
            message = err
        message = message or parsed.get("message") or parsed.get("detail")
        if message:
            return str(message), code
    snippet = response.text.strip()[:200] if response.text else ""
    return (f"HTTP {response.status}" + (f": {snippet}" if snippet else "")), None


def _decision_credits(parsed: Any) -> float | None:
    if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        # A batch reports its total at the top level; rows may not carry credits.
        if isinstance(parsed.get("credits"), (int, float)) and not isinstance(parsed.get("credits"), bool):
            return float(parsed["credits"])
        vals = [r.get("credits") for r in parsed["results"] if isinstance(r, dict)]
        nums = [v for v in vals if isinstance(v, (int, float))]
        return pricing.round4(sum(nums)) if nums else None
    if isinstance(parsed, dict) and isinstance(parsed.get("credits"), (int, float)):
        return float(parsed["credits"])
    return None
