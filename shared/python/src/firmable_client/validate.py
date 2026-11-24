"""Minimal response validation.

These checks catch responses that would silently produce a wrong report: a missing list, a decision
that is not yes, no or unknown, a profile without an ID. They do not reject extra fields, because
the API adds fields without notice.
"""
from __future__ import annotations

from typing import Any

from .errors import InvalidResponseError

DECISION_VALUES = {"yes", "no", "unknown"}


def _obj(x: Any, what: str) -> dict[str, Any]:
    if not isinstance(x, dict):
        raise InvalidResponseError(f"{what} response is not a JSON object.")
    return x


def company(x: Any) -> None:
    d = _obj(x, "Company")
    if not isinstance(d.get("id"), str) or not d["id"]:
        raise InvalidResponseError("Company response has no id.")


def person(x: Any) -> None:
    d = _obj(x, "Person")
    if not isinstance(d.get("id"), str) or not d["id"]:
        raise InvalidResponseError("Person response has no id.")


def people_search(x: Any) -> None:
    d = _obj(x, "People search")
    if d.get("success") is False and "records" not in d:
        return  # documented no-match response: success false, no records, totalPeople 0
    if not isinstance(d.get("records"), list):
        raise InvalidResponseError("People search response has no records list.")


def decisions_list(x: Any) -> None:
    d = _obj(x, "Decision tool list")
    tools = d.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools):
        raise InvalidResponseError("Decision tool list has no tools with names.")


def decision_row(r: Any) -> None:
    d = _obj(r, "Decision")
    value = d.get("decision")
    if value in DECISION_VALUES:
        return
    if value is None and d.get("error"):
        return
    raise InvalidResponseError(f"Decision answer is {value!r}, not yes, no or unknown.")


def decision(x: Any) -> None:
    d = _obj(x, "Decision")
    if "results" in d:
        if not isinstance(d["results"], list):
            raise InvalidResponseError("Decision batch response has no results list.")
        for r in d["results"]:
            decision_row(r)
        return
    decision_row(d)


def ai_adoption(x: Any) -> None:
    d = _obj(x, "AI adoption")
    if "results" in d:
        if not isinstance(d["results"], list):
            raise InvalidResponseError("AI adoption batch response has no results list.")
        for r in d["results"]:
            _ai_row(r)
        return
    _ai_row(d)


def _ai_row(r: Any) -> None:
    d = _obj(r, "AI adoption")
    if "company" not in d:
        raise InvalidResponseError("AI adoption response has no company field.")
    c = d["company"]
    if c is not None and not isinstance(c, dict):
        raise InvalidResponseError("AI adoption company field is not an object or null.")


def tech_intel(x: Any) -> None:
    d = _obj(x, "Tech Intel")
    if not isinstance(d.get("tech_intelligence"), list):
        raise InvalidResponseError("Tech Intel response has no tech_intelligence list.")
    if "total" in d and d["total"] is not None and not isinstance(d["total"], int):
        raise InvalidResponseError("Tech Intel total is not a number.")
