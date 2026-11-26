"""Pre-call account pack: assembles factual information about an account before a sales call.

Workflow for each row:
1. Normalise the domain. An invalid domain is reported and skipped.
2. Fetch the company profile (GET /company). No profile means no pack.
3. Ask each configured company check (POST /decisions).
4. Run each configured people search (POST /people/search), select contacts, fetch their profiles
   (GET /people) and ask each configured contact check (POST /decisions).
5. Retrieve Tech Intel (GET /tech-intel), following pages up to tech_intel.max_pages.
6. Retrieve AI Adoption (POST /ai-adoption) when ai_adoption.enabled is true.
7. Fill the predefined question templates from the facts above.

Contact selection rule: searches run in configuration order and return up to `size` people each.
People are kept in the order they were found, a person found by two searches is kept once, and the
first contacts.max_contacts people are selected.

Every fact carries `source`: the endpoint and the ledger sequence number of the response it came
from. Suggested questions are predefined templates and are kept apart from facts.

Row status: complete when every requested section was retrieved, partial when a section has an
error, was not asked because the run stopped, or was refused with 403 because the API key cannot use it. A valid "not found"
or "no data" answer is not an error.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, compare, tool_index, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts, contact_details, person_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import FATAL_KINDS, PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, link, note, table

NAME = "pre-call-account-pack"
TITLE = "Pre-call account pack"
DESCRIPTION = ("Assembles a factual account pack before a sales call: the company profile, selected contacts, "
               "Tech Intel, Decisions checks and, when requested, AI Adoption. Every fact shows the response it "
               "came from. Suggested questions are predefined templates.")

STATUS_TONE = {"complete": "good", "partial": "review", "company_not_found": "unknown", "invalid_input": "error",
               "error": "error", "not_processed": "unknown"}
SECTION_TONE = {"ok": "good", "no_data": "unknown", "no_match": "unknown", "not_found": "unknown",
                "not_available": "review", "error": "error", "not_asked": "neutral", "not_requested": "neutral"}
RESULT_TONE = {"pass": "good", "fail": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral"}
PROBLEM_WORDS = {"error": "error", "not_asked": "not asked because the run stopped",
                 "not_available": "access denied"}
SECTION_LABELS = [("company_checks", "Company checks"), ("contacts", "Contacts"), ("tech_intel", "Tech Intel"),
                  ("ai_adoption", "AI Adoption")]
COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
SENIORITY_IDS = [str(i) for i in range(1, 8)]
DEPARTMENT_IDS = [str(i) for i in range(1, 18)]
TECH_FIELDS = ["product", "product_slug", "vendor", "vendor_slug", "category", "category_slug", "confidence",
               "recency", "last_observed", "nature"]
AI_DIMENSION_FIELDS = ["level", "headline", "observability", "counts_toward_overall"]
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_TOP_KEYS = {"with_reasoning", "company_checks", "contact_checks", "contacts", "tech_intel", "ai_adoption",
             "suggested_questions"}
_CHECK_KEYS = {"id", "label", "tool", "inputs", "want"}
_CONTACT_KEYS = {"selected_country", "searches", "max_contacts", "fetch_profiles"}
_SEARCH_KEYS = {"id", "label", "position", "seniority", "department", "size"}
_TECH_KEYS = {"enabled", "with_evidence", "max_pages"}
_AI_KEYS = {"enabled", "with_evidence"}
_QUESTION_KEYS = {"enabled", "max_questions"}


# ----- configuration -------------------------------------------------------------------------

def _obj(raw: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(key, {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object.")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"Unknown {key} key(s): {', '.join(unknown)}")
    return value


def _bool(value: Any, default: bool, what: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{what} must be true or false.")
    return value


def _int(value: Any, default: int, low: int, high: int, what: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < low or value > high:
        raise ConfigError(f"{what} must be a whole number from {low} to {high}.")
    return value


def _checks(raw: Any, where: str, seen: set[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list.")
    out: list[dict[str, Any]] = []
    for i, c in enumerate(raw, start=1):
        if not isinstance(c, dict):
            raise ConfigError(f"{where}[{i}] must be an object.")
        unknown = sorted(set(c) - _CHECK_KEYS)
        if unknown:
            raise ConfigError(f"{where}[{i}] has unknown key(s): {', '.join(unknown)}")
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise ConfigError(f"{where}[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if cid in seen:
            raise ConfigError(f"{where}[{i}].id {cid!r} is used twice.")
        seen.add(cid)
        tool = c.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ConfigError(f"{where}[{i}].tool must name a decision tool.")
        inputs = c.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ConfigError(f"{where}[{i}].inputs must be an object.")
        want = c.get("want", "yes")
        if want not in ("yes", "no"):
            raise ConfigError(f"{where}[{i}].want must be yes or no.")
        label = c.get("label", cid)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"{where}[{i}].label must be text.")
        out.append({"id": cid, "label": label, "tool": tool, "inputs": inputs, "want": want})
    return out


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    seen: set[str] = set()
    company_checks = _checks(raw.get("company_checks"), "company_checks", seen)
    contact_checks = _checks(raw.get("contact_checks"), "contact_checks", seen)
    with_reasoning = _bool(raw.get("with_reasoning"), True, "with_reasoning")

    c = _obj(raw, "contacts", _CONTACT_KEYS)
    country = c.get("selected_country", "AU")
    if country not in COUNTRIES:
        raise ConfigError(f"contacts.selected_country must be one of {', '.join(COUNTRIES)}.")
    searches_raw = c.get("searches", [])
    if searches_raw is None:
        searches_raw = []
    if not isinstance(searches_raw, list):
        raise ConfigError("contacts.searches must be a list.")
    searches: list[dict[str, Any]] = []
    search_ids: set[str] = set()
    for i, s in enumerate(searches_raw, start=1):
        where = f"contacts.searches[{i}]"
        if not isinstance(s, dict):
            raise ConfigError(f"{where} must be an object.")
        bad = sorted(set(s) - _SEARCH_KEYS)
        if bad:
            raise ConfigError(f"{where} has unknown key(s): {', '.join(bad)}")
        sid = s.get("id")
        if not isinstance(sid, str) or not _ID.match(sid):
            raise ConfigError(f"{where}.id must be 1 to 40 lower-case letters, digits or underscores.")
        if sid in search_ids:
            raise ConfigError(f"{where}.id {sid!r} is used twice.")
        search_ids.add(sid)
        label = s.get("label", sid)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"{where}.label must be text.")
        position = s.get("position")
        if position is not None and (not isinstance(position, str) or not position.strip()):
            raise ConfigError(f"{where}.position must be non-empty text.")
        seniority = s.get("seniority")
        if seniority is not None and seniority not in SENIORITY_IDS:
            raise ConfigError(f"{where}.seniority must be a seniority id from 1 to 7, written as text.")
        department = s.get("department")
        if department is not None and department not in DEPARTMENT_IDS:
            raise ConfigError(f"{where}.department must be a department id from 1 to 17, written as text.")
        if position is None and seniority is None and department is None:
            raise ConfigError(f"{where} needs at least one of position, seniority or department.")
        size = _int(s.get("size"), 5, 1, 100, f"{where}.size")
        searches.append({"id": sid, "label": label, "position": position.strip() if position else None,
                         "seniority": seniority, "department": department, "size": size})
    max_contacts = _int(c.get("max_contacts"), 3, 0, 50, "contacts.max_contacts")
    fetch_profiles = _bool(c.get("fetch_profiles"), True, "contacts.fetch_profiles")
    if contact_checks and not searches:
        raise ConfigError("contact_checks need at least one entry in contacts.searches.")

    t = _obj(raw, "tech_intel", _TECH_KEYS)
    a = _obj(raw, "ai_adoption", _AI_KEYS)
    q = _obj(raw, "suggested_questions", _QUESTION_KEYS)
    return {
        "with_reasoning": with_reasoning,
        "company_checks": company_checks,
        "contact_checks": contact_checks,
        "contacts": {"selected_country": country, "searches": searches, "max_contacts": max_contacts,
                     "fetch_profiles": fetch_profiles},
        "tech_intel": {"enabled": _bool(t.get("enabled"), True, "tech_intel.enabled"),
                       "with_evidence": _bool(t.get("with_evidence"), False, "tech_intel.with_evidence"),
                       "max_pages": _int(t.get("max_pages"), 1, 1, 20, "tech_intel.max_pages")},
        "ai_adoption": {"enabled": _bool(a.get("enabled"), False, "ai_adoption.enabled"),
                        "with_evidence": _bool(a.get("with_evidence"), False, "ai_adoption.with_evidence")},
        "suggested_questions": {"enabled": _bool(q.get("enabled"), True, "suggested_questions.enabled"),
                                "max_questions": _int(q.get("max_questions"), 6, 0, 20,
                                                      "suggested_questions.max_questions")},
    }


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, required=["domain"])


# ----- plan ----------------------------------------------------------------------------------

def _valid_unique_domains(rows: list[dict[str, str]]) -> list[str]:
    out: list[str] = []
    for r in rows:
        d = normalise_domain(r.get("domain"))
        if d and d not in out:
            out.append(d)
    return out


def _max_contacts(config: dict[str, Any]) -> int:
    c = config["contacts"]
    return min(c["max_contacts"], sum(s["size"] for s in c["searches"]))


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    n = len(_valid_unique_domains(rows))
    cc, kc = config["company_checks"], config["contact_checks"]
    searches = config["contacts"]["searches"]
    contacts = _max_contacts(config)
    calls: list[PlannedCall] = []
    if cc or kc:
        calls.append(PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []))
    calls.append(PlannedCall("GET /company", "Company profile for each unique valid domain", n,
                             [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    if searches:
        calls.append(PlannedCall("POST /people/search", f"{len(searches)} people search(es) for each account",
                                 n * len(searches), []))
    if searches and config["contacts"]["fetch_profiles"] and contacts:
        calls.append(PlannedCall("GET /people", f"Up to {contacts} contact profile(s) for each account",
                                 n * contacts, [("person", pricing.round4(pricing.PERSON_PROFILE * n * contacts))]))
    answers = n * (len(cc) + contacts * len(kc))
    if answers:
        calls.append(PlannedCall("POST /decisions",
                                 f"{len(cc)} company check(s) and {len(kc)} check(s) for each of up to {contacts} contact(s)",
                                 answers, pricing.estimate_decision(answers, config["with_reasoning"])))
    t = config["tech_intel"]
    if t["enabled"]:
        parts = [("tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n))]
        if t["with_evidence"]:
            parts.append(("tech_intel_evidence",
                          pricing.round4(pricing.TECH_INTEL_EVIDENCE_REQUEST * n * t["max_pages"])))
        calls.append(PlannedCall("GET /tech-intel", f"Up to {t['max_pages']} page(s) for each account",
                                 n * t["max_pages"], parts))
    a = config["ai_adoption"]
    if a["enabled"]:
        calls.append(PlannedCall("POST /ai-adoption", "AI Adoption for each account", n,
                                 pricing.estimate_ai_adoption(n, a["with_evidence"])))
    return calls


# ----- helpers -------------------------------------------------------------------------------

def _resolve(entries: list[dict[str, Any]], e: dict[str, Any]) -> dict[str, Any]:
    """The source of a ledger entry. A cache hit points to the response it reused."""
    if e.get("outcome") == "cache_hit":
        for o in entries:
            if (o.get("outcome") == "ok" and o.get("method") == e.get("method") and o.get("path") == e.get("path")
                    and o.get("query") == e.get("query") and o.get("body") == e.get("body")):
                return {"endpoint": e["endpoint"], "ledger_seq": o["seq"], "reused": True}
    return {"endpoint": e["endpoint"], "ledger_seq": e["seq"], "reused": False}


def _source(ctx: RunContext, start: int) -> dict[str, Any] | None:
    entries = ctx.client.ledger.entries
    if len(entries) <= start:
        return None
    return _resolve(entries, entries[-1])


def _record_dict(ctx: RunContext, row_id: str, err: dict[str, Any]) -> dict[str, Any]:
    entry = {"row_id": row_id, **err}
    ctx.errors.append(entry)
    if err.get("kind") in FATAL_KINDS and ctx.stopped is None:
        ctx.stopped = entry
    return entry


def _product(item: dict[str, Any]) -> str:
    return item.get("product") or item.get("product_slug") or "an unnamed technology"


def _section_status(statuses: list[str]) -> str:
    if "error" in statuses:
        return "error"
    if "not_asked" in statuses:
        return "not_asked"
    return "ok"


def _questions(cfg: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Predefined templates, filled from API fields. They are prompts for the seller, not facts."""
    out: list[dict[str, Any]] = []
    for c in pack["company_checks"]["checks"]:
        if c["result"] == "unknown":
            out.append({"template_id": "confirm_unknown_check",
                        "question": f"Can you confirm this for us: {c['label']}?",
                        "basis": f"The company check \"{c['label']}\" answered unknown. Firmable does not hold that fact."})
    contacts = pack["contacts"]
    if contacts["status"] == "no_match":
        out.append({"template_id": "who_else",
                    "question": "Who else should be part of this conversation?",
                    "basis": "No contacts were selected from the configured people searches in retrieved data."})
    categories: list[str] = []
    for item in pack["tech_intel"].get("items") or []:
        cat = item.get("category")
        if item.get("confidence") == "high" and isinstance(cat, str) and cat and cat not in categories:
            categories.append(cat)
            out.append({"template_id": "technology_category",
                        "question": f"How does your team handle {cat} today, and what is working well?",
                        "basis": (f"Tech Intel observed {_product(item)} in the {cat} category with high "
                                  f"confidence, last observed {item.get('last_observed') or 'not given'}. An observation does not "
                                  "show active use, spend or buying intent.")})
            if len(categories) == 2:
                break
    ai = pack["ai_adoption"]
    if ai["status"] == "ok" and ai.get("overall_level"):
        out.append({"template_id": "ai_adoption",
                    "question": "Where is AI already part of your team's work, and where would you like it to be?",
                    "basis": (f"AI Adoption overall level {ai['overall_level']} as of {ai.get('as_of') or 'not given'}. A level "
                              "does not show buying intent.")})
    out.append({"template_id": "why_now", "question": "What prompted you to look at this now?",
                "basis": "Standard opening question. It is not based on API data."})
    return out[: cfg["suggested_questions"]["max_questions"]]


# ----- run -----------------------------------------------------------------------------------

def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = [
        "Each fact carries source.endpoint and source.ledger_seq, which point to its request and response in "
        "the ledger and the raw responses section.",
        "Company and person profiles carry no date in the response. Dates shown come from the API fields that "
        "hold them: Decisions answered_at and last_refreshed, Tech Intel last_observed and AI Adoption as_of.",
        "Suggested questions are predefined templates filled from API fields. They are not API facts.",
        "A detected technology or an AI Adoption level shows what Firmable observed. It does not show buying "
        "intent, spend or active use.",
        "A technology that is not listed was not observed in retrieved data. That does not mean the company "
        "does not use it.",
        "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no "
        "or as a pass.",
    ]
    if cfg["contacts"]["searches"]:
        coverage.append("People search filters by company, position, seniority and department only. It has no "
                        "name or date filters. Contacts are the first people found, in configuration order.")
        coverage.append("Personal email addresses are not copied into the pack. Phone numbers are shown with "
                        "the do-not-call flag as returned.")

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    all_checks = cfg["company_checks"] + cfg["contact_checks"]
    tools: dict[str, dict[str, Any]] = {}
    if all_checks:
        try:
            catalogue = client.list_decision_tools()
        except FirmableError as e:
            ctx.record_error(None, e)
            return stop_all("The decision tool list could not be read, so no account was processed.")
        problems = (check_tools(catalogue, cfg["company_checks"], "company")
                    + check_tools(catalogue, cfg["contact_checks"], "person"))
        if problems:
            for p in problems:
                ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                                   "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                                   "retry_after": None})
            coverage.extend(problems)
            return stop_all("The configuration names decision tools or inputs the API does not accept.")
        tools = tool_index(catalogue)
        model_tools = sorted({c["tool"] for c in all_checks if uses_model(catalogue, c["tool"])})
        if model_tools:
            coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                            f"{', '.join(model_tools)}. This code applies only the rules in the configuration.")
        with_company = sorted({c["tool"] for c in cfg["contact_checks"]
                               if "company_id" in ((tools[c["tool"]].get("input_schema") or {}).get("properties") or {})})
        if with_company:
            coverage.append(f"Contact checks with these tools were asked with the account's company_id: "
                            f"{', '.join(with_company)}.")

    for r in ctx.rows:
        r["_domain"] = normalise_domain(r.get("domain")) or ""
    dupes = find_duplicates(ctx.rows, "_domain")
    for r in ctx.rows:
        r.pop("_domain")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier domain; their pack was copied from the earlier "
                        "row and no request was repeated.")
    flags = {"empty_search": False}
    packs: dict[str, dict[str, Any]] = {}

    def ask(row_id: str, c: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
        entry = {"check_id": c["id"], "label": c["label"], "tool": c["tool"], "want": c["want"]}
        if ctx.stopped is not None:
            return {**entry, "result": "not_asked", "answer": None, "error": None, "source": None}
        start = len(client.ledger.entries)
        try:
            resp = client.make_decision(c["tool"], {**subject, **c["inputs"]}, with_reasoning=cfg["with_reasoning"])
        except FirmableError as e:
            err = ctx.record_error(row_id, e)
            return {**entry, "result": "error", "answer": None, "error": err, "source": _source(ctx, start)}
        ans = answer_from(resp)
        result = compare(ans["answer"], c["want"])
        return {**entry, "result": result, **ans, "error": ans["answer_error"] if result == "error" else None,
                "source": _source(ctx, start)}

    def attempt(row_id: str, fn: Callable[[], Any]) -> tuple[Any, str, dict[str, Any] | None, dict[str, Any] | None]:
        if ctx.stopped is not None:
            return None, "not_asked", None, None
        start = len(client.ledger.entries)
        try:
            resp = fn()
        except NotFoundError:
            return None, "not_found", None, _source(ctx, start)
        except FirmableError as e:
            return None, "error", ctx.record_error(row_id, e), _source(ctx, start)
        return resp, "ok", None, _source(ctx, start)

    def contacts_section(row_id: str, company_id: str, notes: list[str]) -> dict[str, Any]:
        cc = cfg["contacts"]
        if not cc["searches"]:
            return {"status": "not_requested", "searches": [], "selected": [], "not_selected": 0}
        searches: list[dict[str, Any]] = []
        found: dict[str, dict[str, Any]] = {}
        for s in cc["searches"]:
            body = {"companyId": company_id, "selectedCountry": cc["selected_country"], "from": "0",
                    "size": str(s["size"])}
            for k in ("position", "seniority", "department"):
                if s[k] is not None:
                    body[k] = s[k]
            entry = {"search_id": s["id"], "label": s["label"], "request": body, "total": None, "returned": 0,
                     "error": None, "source": None}
            if ctx.stopped is not None:
                searches.append({**entry, "status": "not_asked"})
                continue
            start = len(client.ledger.entries)
            try:
                resp = client.search_people(body)
            except FirmableError as e:
                searches.append({**entry, "status": "error", "error": ctx.record_error(row_id, e),
                                 "source": _source(ctx, start)})
                continue
            src = _source(ctx, start)
            if resp.get("success") is False:
                flags["empty_search"] = True
            records = [r for r in resp.get("records") or [] if isinstance(r, dict)]
            searches.append({**entry, "status": "ok" if records else "no_match", "total": resp.get("total"),
                             "returned": len(records), "source": src})
            for rec in records:
                pid = rec.get("person_id") or rec.get("id")
                if not isinstance(pid, str) or not pid:
                    continue
                if pid in found:
                    found[pid]["matched_searches"].append(s["id"])
                    continue
                found[pid] = {"person_id": pid, "name": rec.get("name"), "position": rec.get("position"),
                              "company_name": rec.get("company_name"), "matched_searches": [s["id"]],
                              "search_flags": {k: rec.get(k) for k in ("has_email", "has_phone", "has_mobile",
                                                                        "has_dnd_phone")},
                              "search_source": src}
        people = list(found.values())
        selected = people[: cc["max_contacts"]]
        statuses = [s["status"] for s in searches]
        for p in selected:
            p["profile"] = None
            p["profile_error"] = None
            p["profile_source"] = None
            if not cc["fetch_profiles"]:
                p["profile_status"] = "not_requested"
            else:
                resp, status, err, src = attempt(row_id, lambda pid=p["person_id"]: client.get_person(id=pid))
                p["profile_status"] = status
                p["profile_error"] = err
                p["profile_source"] = src
                statuses.append(status)
                if resp is not None:
                    contact = contact_details(resp)
                    p["profile"] = {**(person_facts(resp) or {}), "work_emails": contact["work_emails"],
                                    "phones": contact["phones"]}
                    cur = p["profile"].get("current_company_id")
                    if cur and cur != company_id:
                        notes.append(f"Contact {p['name'] or p['person_id']}: the person profile's current company ID ({cur}) "
                                     f"differs from this account ({company_id}).")
            p["checks"] = []
            for c in cfg["contact_checks"]:
                subject = {"person_id": p["person_id"]}
                props = (tools[c["tool"]].get("input_schema") or {}).get("properties") or {}
                if "company_id" in props:
                    subject["company_id"] = company_id
                p["checks"].append(ask(row_id, c, subject))
            statuses.extend(c["result"] for c in p["checks"])
        status = _section_status(statuses)
        if status == "ok" and not people:
            status = "no_match"
        return {"status": status, "searches": searches, "selected": selected,
                "not_selected": len(people) - len(selected)}

    def tech_section(row_id: str, company_id: str, notes: list[str]) -> dict[str, Any]:
        t = cfg["tech_intel"]
        base = {"items": [], "total": None, "pages_fetched": 0, "complete": False, "stop_reason": None,
                "error": None}
        if not t["enabled"]:
            return {**base, "status": "not_requested"}
        if ctx.stopped is not None:
            return {**base, "status": "not_asked"}
        start = len(client.ledger.entries)
        res = client.get_tech_intel({"id": company_id}, with_evidence=t["with_evidence"], max_pages=t["max_pages"])
        err = _record_dict(ctx, row_id, res["error"]) if res["error"] else None
        entries = client.ledger.entries
        items: list[dict[str, Any]] = []
        for e in entries[start:]:
            if e.get("endpoint") != "GET /tech-intel" or e.get("outcome") not in ("ok", "cache_hit"):
                continue
            src = _resolve(entries, e)
            page = entries[src["ledger_seq"] - 1].get("response") or {}
            for it in page.get("tech_intelligence") or []:
                if not isinstance(it, dict):
                    continue
                item = {k: it.get(k) for k in TECH_FIELDS}
                if t["with_evidence"]:
                    ev = it.get("evidence_detail") if isinstance(it.get("evidence_detail"), dict) else {}
                    records = [r for r in ev.get("records") or [] if isinstance(r, dict)]
                    missing = sum(1 for r in records if not r.get("location"))
                    item.update({"evidence_count": ev.get("evidence_count"), "first_seen": ev.get("first_seen"),
                                 "last_seen": ev.get("last_seen"), "evidence_records_returned": len(records),
                                 "evidence_records_without_location": missing})
                    if missing:
                        notes.append(f"Tech Intel evidence for {_product(item)}: {missing} record(s) have no "
                                     "source location.")
                item["source"] = src
                items.append(item)
        if res["id"] and res["id"] != company_id:
            notes.append(f"The Tech Intel response is for company ID {res['id']}, not {company_id}.")
        out = {"items": items, "total": res["total"], "pages_fetched": res["pages_fetched"],
               "complete": res["complete"], "stop_reason": res["stop_reason"], "error": err}
        if err:
            kind = err.get("kind")
            if kind == "forbidden" and not items:
                return {**out, "status": "not_available"}
            return {**out, "status": "error"}
        if not items and not res["total"]:
            return {**out, "status": "no_data"}
        return {**out, "status": "ok"}

    def ai_section(row_id: str, company_id: str, notes: list[str]) -> dict[str, Any]:
        a = cfg["ai_adoption"]
        base = {"overall_level": None, "explanation": None, "momentum": None, "dimensions": {},
                "evidence_available": None, "as_of": None, "framework_version": None, "firmable_id": None,
                "error": None, "source": None}
        if not a["enabled"]:
            return {**base, "status": "not_requested"}
        resp, status, err, src = attempt(row_id, lambda: client.get_ai_adoption({"company_id": company_id},
                                                                             with_evidence=a["with_evidence"]))
        if resp is None:
            return {**base, "status": status, "error": err, "source": src}
        comp = resp.get("company")
        if not isinstance(comp, dict):
            rerr = resp.get("error") if isinstance(resp.get("error"), dict) else None
            code = (rerr or {}).get("code")
            return {**base, "status": "not_found" if code == "not_found" else "error", "error": rerr, "source": src}
        dims_raw = comp.get("dimensions") if isinstance(comp.get("dimensions"), dict) else {}
        dims = {k: {f: v.get(f) for f in AI_DIMENSION_FIELDS} for k, v in dims_raw.items() if isinstance(v, dict)}
        out = {**base, "status": "ok", "overall_level": comp.get("overall_level"),
               "explanation": comp.get("explanation"), "momentum": comp.get("momentum"), "dimensions": dims,
               "evidence_available": comp.get("evidence_available"), "as_of": comp.get("as_of"),
               "framework_version": comp.get("framework_version"), "firmable_id": comp.get("firmable_id"),
               "source": src}
        if comp.get("firmable_id") and comp.get("firmable_id") != company_id:
            notes.append(f"The AI Adoption answer is for company ID {comp.get('firmable_id')}, not {company_id}.")
        if a["with_evidence"]:
            summary: dict[str, Any] = {}
            evidence = resp.get("evidence") if isinstance(resp.get("evidence"), dict) else {}
            for area, ev in evidence.items():
                if not isinstance(ev, dict):
                    continue
                items = [i for i in ev.get("items") or [] if isinstance(i, dict)]
                summary[area] = {
                    "item_count": ev.get("item_count"), "items_returned": len(items),
                    "qualifying": sum(1 for i in items if i.get("qualifies") is True),
                    "not_qualifying": sum(1 for i in items if i.get("qualifies") is False),
                    "qualifies_missing": sum(1 for i in items if not isinstance(i.get("qualifies"), bool)),
                    "truncated": ev.get("truncated"), "as_of": ev.get("as_of")}
                if ev.get("truncated") is True:
                    notes.append(f"AI Adoption {area} evidence is truncated; {len(items)} item(s) returned.")
            out["evidence_summary"] = summary
        return out

    def one(row: dict[str, str]) -> dict[str, Any]:
        row_id = row["row_id"]
        domain = normalise_domain(row.get("domain"))
        base = {"row_id": row_id, "input": row, "domain": domain, "duplicate_of": dupes.get(row_id),
                "company": None}
        if not domain:
            return {**base, "status": "invalid_input", "status_reason": f"{row.get('domain')!r} is not a valid domain."}
        if row_id in dupes and domain in packs:
            return {**copy.deepcopy(packs[domain]), "row_id": row_id, "input": row, "duplicate_of": dupes[row_id]}
        start = len(client.ledger.entries)
        try:
            company = client.get_company(fqdn=domain)
        except NotFoundError:
            out = {**base, "status": "company_not_found", "company_source": _source(ctx, start),
                   "status_reason": "Firmable has no company profile for this domain, so no pack was built."}
            packs[domain] = out
            return out
        except FirmableError as e:
            err = ctx.record_error(row_id, e)
            out = {**base, "status": "error", "error": err, "company_source": _source(ctx, start),
                   "status_reason": f"The company profile could not be retrieved: {err['message']}"}
            packs[domain] = out
            return out
        cid = company["id"]
        notes: list[str] = []
        pack: dict[str, Any] = {**base, "company": {**(company_facts(company) or {}), "source": _source(ctx, start)}}
        checks = [ask(row_id, c, {"company_id": cid}) for c in cfg["company_checks"]]
        for c in checks:
            ent = c.get("entity_id")
            if ent and ent != cid:
                notes.append(f"The answer to \"{c['label']}\" is about entity {ent}, not {cid}.")
        pack["company_checks"] = {"status": _section_status([c["result"] for c in checks]) if checks
                                  else "not_requested", "checks": checks}
        pack["contacts"] = contacts_section(row_id, cid, notes)
        pack["tech_intel"] = tech_section(row_id, cid, notes)
        pack["ai_adoption"] = ai_section(row_id, cid, notes)
        pack["data_quality_notes"] = notes
        problems = [f"{label} ({PROBLEM_WORDS[pack[key]['status']]})" for key, label in SECTION_LABELS
                    if pack[key]["status"] in PROBLEM_WORDS]
        if problems:
            pack["status"] = "partial"
            pack["status_reason"] = f"Some requested sections are incomplete: {', '.join(problems)}."
        else:
            pack["status"] = "complete"
            pack["status_reason"] = "Every requested section was retrieved."
        t = pack["tech_intel"]
        if t["status"] == "ok" and not t["complete"]:
            total = str(t["total"]) if isinstance(t["total"], int) and not isinstance(t["total"], bool) else "unknown"
            pack["status_reason"] += (f" Tech Intel is incomplete: {len(t['items'])} of {total} "
                                      "technologies retrieved within the page limit.")
        pack["suggested_questions"] = _questions(cfg, pack) if cfg["suggested_questions"]["enabled"] else []
        packs[domain] = pack
        return pack

    rows = ctx.run_rows(ctx.rows, one)
    if flags["empty_search"]:
        coverage.append("A people search with no matches returns success false without a records list; it is "
                        "counted as zero matches.")
    counts: dict[str, int] = {}
    sections: dict[str, dict[str, int]] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        for key, _ in SECTION_LABELS:
            if isinstance(r.get(key), dict):
                s = sections.setdefault(key, {})
                st = r[key]["status"]
                s[st] = s.get(st, 0) + 1
    return {"rows": rows, "summary": {"status_counts": counts, "section_counts": sections}, "coverage": coverage}


# ----- outputs -------------------------------------------------------------------------------

CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
               "hq_country", "company_size", "company_checks", "contacts_selected", "contacts", "contact_checks",
               "tech_intel_status", "tech_total", "tech_retrieved", "tech_complete", "technologies",
               "ai_adoption_status", "ai_overall_level", "ai_as_of", "data_quality_notes", "suggested_questions"]


def _check_text(c: dict[str, Any]) -> str:
    return f"{c['check_id']}={c['result']}({c.get('answer') or '-'})"


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        contacts = (r.get("contacts") or {}).get("selected") or []
        tech = r.get("tech_intel") or {}
        ai = r.get("ai_adoption") or {}
        out.append({
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_id": company.get("id"), "company_name": company.get("name"),
            "hq_country": company.get("hq_country"), "company_size": company.get("company_size"),
            "company_checks": [_check_text(c) for c in (r.get("company_checks") or {}).get("checks") or []],
            "contacts_selected": len(contacts) if r.get("contacts") else None,
            "contacts": [f"{p.get('name') or p['person_id']} ({p.get('position') or '-'})" for p in contacts],
            "contact_checks": [f"{p['person_id']}:{_check_text(c)}" for p in contacts for c in p.get("checks") or []],
            "tech_intel_status": tech.get("status"), "tech_total": tech.get("total"),
            "tech_retrieved": len(tech.get("items") or []) if tech else None,
            "tech_complete": tech.get("complete") if tech else None,
            "technologies": [i.get("product") for i in (tech.get("items") or [])[:10]],
            "ai_adoption_status": ai.get("status"), "ai_overall_level": ai.get("overall_level"),
            "ai_as_of": ai.get("as_of"), "data_quality_notes": r.get("data_quality_notes") or [],
            "suggested_questions": [q["question"] for q in r.get("suggested_questions") or []],
        })
    return out


def _src(s: Any) -> str:
    if not isinstance(s, dict):
        return "no response"
    return f"#{s.get('ledger_seq')} {s.get('endpoint')}" + (" (reused)" if s.get("reused") else "")


def _section_badge(status: str) -> str:
    return badge(status.replace("_", " "), SECTION_TONE.get(status, "neutral"))


def _left(r: dict[str, Any]) -> str:
    html = f"<p>{esc(r.get('status_reason') or r.get('reason') or (r.get('error') or {}).get('message') or '')}</p>"
    company = r.get("company")
    if company:
        html += "<h3>Company profile</h3>" + kv([
            ("Firmable ID", company.get("id")), ("Name", company.get("name")),
            ("Website", Raw(link(company.get("website")))), ("Head office", company.get("hq_location")),
            ("Head office country", company.get("hq_country")), ("Size", company.get("company_size")),
            ("Employee count", company.get("employee_count")), ("Company type", company.get("company_type")),
            ("Year founded", company.get("year_founded")),
            ("Industries", ", ".join(company.get("industries") or []) or None),
            ("Revenue", company.get("revenue"))])
        html += f'<p class="muted">Source: {esc(_src(company.get("source")))}. The profile carries no date.</p>'
    if r.get("duplicate_of"):
        html += note(f"Same domain as row {esc(r['duplicate_of'])}; the pack was copied.", "info")
    if r.get("data_quality_notes"):
        html += ("<h3>Data quality notes</h3>" + bullet_list(r["data_quality_notes"])
                 + '<p class="muted">Shown as returned. This recipe does not correct upstream data.</p>')
    cc = r.get("company_checks")
    if cc and cc.get("checks"):
        html += "<h3>Company checks " + _section_badge(cc["status"]) + "</h3>" + table(
            ["Check", "Wanted", "Answer", "Result", "Confidence", "Answered at", "Source"],
            [[c["label"], c["want"], c.get("answer") or "none",
              Raw(badge(c["result"].replace("_", " "), RESULT_TONE.get(c["result"], "neutral"))),
              c.get("confidence"), c.get("answered_at"), _src(c.get("source"))] for c in cc["checks"]])
        for c in cc["checks"]:
            rs = c.get("reasoning") or {}
            if rs.get("reason"):
                html += (f"<p><strong>{esc(c['label'])}:</strong> {esc(rs.get('reason'))}</p>"
                         f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                         f"{esc(rs.get('last_refreshed'))}.</p>")
            if isinstance(c.get("error"), dict):
                html += note(f"{esc(c['label'])}: {esc(c['error'].get('message'))}", "error")
    return html


def _right(r: dict[str, Any]) -> str:
    html = ""
    contacts = r.get("contacts")
    if contacts and contacts["status"] != "not_requested":
        html += "<h3>Contacts " + _section_badge(contacts["status"]) + "</h3>"
        html += table(["Search", "Status", "Total", "Returned", "Source"],
                      [[s["label"], s["status"].replace("_", " "), s.get("total"), s.get("returned"),
                        _src(s.get("source"))] for s in contacts["searches"]])
        rows = []
        for p in contacts["selected"]:
            prof = p.get("profile") or {}
            phones = ", ".join(f"{ph.get('value')}{' (do not call)' if ph.get('is_dnd') else ''}"
                               for ph in prof.get("phones") or [])
            checks = "; ".join(f"{c['label']}: {c.get('answer') or c['result']}" for c in p.get("checks") or [])
            rows.append([p.get("name"), prof.get("position") or p.get("position"), prof.get("seniority"),
                         ", ".join(prof.get("work_emails") or []) or None, phones or None,
                         p["profile_status"].replace("_", " "), checks or None,
                         f"{_src(p.get('search_source'))}; {_src(p.get('profile_source'))}"])
        if rows:
            html += table(["Name", "Position", "Seniority", "Work email", "Phone", "Profile", "Checks", "Sources"], rows)
        if contacts.get("not_selected"):
            html += f'<p class="muted">{esc(contacts["not_selected"])} more person(s) matched but were not selected.</p>'
    tech = r.get("tech_intel")
    if tech and tech["status"] != "not_requested":
        html += "<h3>Tech Intel " + _section_badge(tech["status"]) + "</h3>"
        html += (f'<p class="muted">{esc(len(tech["items"]))} of {esc(tech.get("total"))} technologies retrieved '
                 f'in {esc(tech["pages_fetched"])} page(s); complete: {esc(tech["complete"])}. Technologies not '
                 "listed were not observed in retrieved data.</p>")
        if tech["items"]:
            html += table(["Product", "Vendor", "Category", "Confidence", "Recency", "Last observed", "Source"],
                          [[i.get("product"), i.get("vendor"), i.get("category"), i.get("confidence"),
                            i.get("recency"), i.get("last_observed"), _src(i.get("source"))] for i in tech["items"]])
        if isinstance(tech.get("error"), dict):
            html += note(esc(tech["error"].get("message")), "error")
    ai = r.get("ai_adoption")
    if ai and ai["status"] != "not_requested":
        html += "<h3>AI Adoption " + _section_badge(ai["status"]) + "</h3>"
        if ai["status"] == "ok":
            html += kv([("Overall level", ai.get("overall_level")), ("Explanation", ai.get("explanation")),
                        ("Momentum", (ai.get("momentum") or {}).get("label") if isinstance(ai.get("momentum"), dict) else None),
                        ("As of", ai.get("as_of")), ("Framework version", ai.get("framework_version"))])
            html += table(["Area", "Level", "Observability", "Counts toward overall", "Headline"],
                          [[k, v.get("level"), v.get("observability"), v.get("counts_toward_overall"), v.get("headline")]
                           for k, v in ai["dimensions"].items()])
            if ai.get("evidence_summary"):
                html += table(["Evidence area", "Held", "Returned", "Qualifying", "Not qualifying", "Truncated", "As of"],
                              [[k, v["item_count"], v["items_returned"], v["qualifying"], v["not_qualifying"],
                                v["truncated"], v["as_of"]] for k, v in ai["evidence_summary"].items()])
            html += '<p class="muted">An AI Adoption level does not show buying intent.</p>'
        elif ai["status"] == "not_found":
            html += "<p>Firmable has no AI Adoption data for this company. This is not an error.</p>"
        if isinstance(ai.get("error"), dict) and ai["status"] != "not_found":
            html += note(esc(ai["error"].get("message")), "error")
        html += f'<p class="muted">Source: {esc(_src(ai.get("source")))}.</p>'
    questions = r.get("suggested_questions") or []
    if questions:
        html += ('<div class="template"><h3>Suggested questions</h3><p class="muted">Predefined templates filled '
                 "from API fields. These are prompts for the seller, not API facts.</p>"
                 + bullet_list(Raw(f"{esc(q['question'])}<br><span class=\"muted\">{esc(q['basis'])}</span>")
                               for q in questions) + "</div>")
    return html


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Account pack summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("Facts come from the API response named in each source line. Suggested questions are "
                      "predefined templates and are shown separately.", "info"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = (r.get("company") or {}).get("name") or r.get("domain") or r["input"].get("domain") or r["row_id"]
        right = _right(r) if r.get("company") else ""
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), _left(r),
                          right or "<p class=\"muted\">No pack was built for this row.</p>", sub=f"Row {r['row_id']}"))
        if r.get("company"):
            cards.append(details_json(f"Pack for row {r['row_id']} as JSON",
                                      {k: v for k, v in r.items() if k != "input"}))
    report.add("Accounts", "Each account pack", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
