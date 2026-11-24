"""Helpers for the Decisions API shared by recipes.

- `check_tools` validates configured checks against the live tool list from GET /decisions.
- `answer_from` turns one decision response into a stable record.
- `compare` maps an answer and a wanted answer to pass, fail, unknown or error.

`unknown` means Firmable does not hold the fact. It is never treated as no.
"""
from __future__ import annotations

from typing import Any

SUBJECT_KEYS = {"company_id", "domain", "person_id", "linkedin_url", "work_email", "external_id", "records"}


def tool_index(catalogue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in catalogue.get("tools", []) if isinstance(t, dict) and "name" in t}


def check_tools(catalogue: dict[str, Any], checks: list[dict[str, Any]], subject: str) -> list[str]:
    """Returns problems with configured checks; an empty list means every check can be asked.

    A check names a `tool` and optional `inputs`. The tool must exist, accept `subject`
    ("company" or "person"), and accept every input key under its input_schema.
    """
    tools = tool_index(catalogue)
    problems: list[str] = []
    for c in checks:
        name = c.get("tool")
        t = tools.get(name)
        if t is None:
            problems.append(f"Decision tool {name!r} is not in the current tool list from GET /decisions.")
            continue
        if subject not in str(t.get("subject", "")):
            problems.append(f"Decision tool {name!r} answers about {t.get('subject')}, not a {subject}.")
        props = (t.get("input_schema") or {}).get("properties") or {}
        extra = sorted(k for k in (c.get("inputs") or {}) if k not in props or k in SUBJECT_KEYS)
        if extra:
            problems.append(f"Decision tool {name!r} does not accept input(s): {', '.join(extra)}.")
        required = (t.get("input_schema") or {}).get("required") or []
        missing = sorted(k for k in required if k not in (c.get("inputs") or {}) and k not in SUBJECT_KEYS)
        if missing:
            problems.append(f"Decision tool {name!r} needs input(s): {', '.join(missing)}.")
    return problems


def answer_from(resp: dict[str, Any]) -> dict[str, Any]:
    """A stable record of one answer. Fields the API did not send are None."""
    reasoning = resp.get("reasoning")
    return {
        "answer": resp.get("decision"),
        "confidence": resp.get("confidence"),
        "details": resp.get("details"),
        "reasoning": reasoning if isinstance(reasoning, dict) else None,
        "answered_at": resp.get("answered_at"),
        "credits": resp.get("credits"),
        "answer_error": resp.get("error"),
        "entity_id": (resp.get("entity") or {}).get("id") if isinstance(resp.get("entity"), dict) else None,
        "request_id": resp.get("request_id"),
    }


def compare(answer: str | None, want: str) -> str:
    """pass when the answer is the wanted one, fail when it is the other of yes or no,
    unknown when Firmable does not hold the fact, error when no answer came back."""
    if answer == "unknown":
        return "unknown"
    if answer not in ("yes", "no"):
        return "error"
    return "pass" if answer == want else "fail"


def uses_model(catalogue: dict[str, Any], tool: str) -> bool | None:
    t = tool_index(catalogue).get(tool)
    return None if t is None else bool(t.get("uses_decision_model"))
