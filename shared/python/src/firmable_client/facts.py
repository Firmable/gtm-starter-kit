"""Picks commonly used fields from API responses without changing their values.

Fields the API did not return come back as None. Nothing is inferred or corrected.
"""
from __future__ import annotations

from typing import Any

COMPANY_FIELDS = ["id", "name", "fqdn", "website", "hq_country", "hq_location", "company_size",
                  "employee_count", "industries", "company_type", "year_founded", "linkedin", "revenue"]

PERSON_FIELDS = ["id", "name", "position", "seniority", "department", "country", "location",
                 "time_in_current_role", "month_joined", "year_joined", "linkedin"]


def company_facts(c: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(c, dict):
        return None
    return {k: c.get(k) for k in COMPANY_FIELDS}


def person_facts(p: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(p, dict):
        return None
    out = {k: p.get(k) for k in PERSON_FIELDS}
    cur = p.get("current_company") if isinstance(p.get("current_company"), dict) else {}
    out["current_company_id"] = cur.get("id")
    out["current_company_name"] = cur.get("name")
    return out


def contact_details(p: dict[str, Any] | None) -> dict[str, Any]:
    """Emails and phones exactly as returned, with each phone's do-not-call flag."""
    if not isinstance(p, dict):
        return {"work_emails": [], "personal_emails": [], "phones": []}
    emails = p.get("emails") if isinstance(p.get("emails"), dict) else {}

    def vals(lst: Any) -> list[str]:
        return [e.get("value") for e in (lst or []) if isinstance(e, dict) and e.get("value")]

    phones = [{"value": ph.get("value"), "is_dnd": ph.get("is_dnd")}
              for ph in (p.get("phones") or []) if isinstance(ph, dict) and ph.get("value")]
    return {"work_emails": vals(emails.get("work")), "personal_emails": vals(emails.get("personal")),
            "phones": phones}
