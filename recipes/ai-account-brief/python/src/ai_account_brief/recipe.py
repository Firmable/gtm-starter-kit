"""AI account brief: turns AI Adoption and Tech Intel into a factual, readable account brief.

Workflow for each row:
1. Normalise the domain. An invalid domain is reported and skipped.
2. Fetch the company profile (GET /company). No profile means no brief.
3. Read AI Adoption (POST /ai-adoption), with evidence when configured.
4. Read Tech Intel (GET /tech-intel) up to tech_intel_max_pages pages, when configured.
5. Lay out the overall level, explanation, momentum and the five areas exactly as returned, split
   each area's evidence into qualifying, not qualifying and unstated items, and flag missing or
   truncated evidence and data that conflicts between endpoints.
6. Fill the discovery-question templates from the configuration. A template is filled only from API
   facts and seller context; a template with a placeholder that has no value is skipped and listed.
   Every filled template is a suggested question, not a fact.

This code computes no AI score and no trend. Levels, explanations and headlines are the API's own.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import FATAL_KINDS, PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "ai-account-brief"
TITLE = "AI account brief"
DESCRIPTION = ("Builds a factual account brief from each company's Firmable profile, AI Adoption and Tech "
               "Intel, with evidence, missing data and template-based discovery questions kept separate "
               "from API facts.")

AREAS = ["architecture", "footprint", "hiring", "initiatives", "workforce"]
KNOWN_LEVELS = ["NONE", "BASIC", "MEANINGFUL", "MODERATE", "STRONG", "ADVANCED"]
BASE_AREA_KEYS = {"level", "headline", "observability", "counts_toward_overall"}
ORG_ID_FIELDS = ["company_id", "firmable_id", "organisation_id"]
ORG_NAME_FIELDS = ["company_name", "employer", "organisation", "company"]
PLACEHOLDER_SOURCES = {
    "company_name": "company profile",
    "industry": "company profile",
    "hq_country": "company profile",
    "overall_level": "AI Adoption",
    "momentum_label": "AI Adoption",
    "architecture_level": "AI Adoption",
    "footprint_level": "AI Adoption",
    "hiring_level": "AI Adoption",
    "initiatives_level": "AI Adoption",
    "workforce_level": "AI Adoption",
    "ai_tool": "AI Adoption evidence",
    "ai_role": "AI Adoption evidence",
    "technology": "Tech Intel",
}
STATUS_TONE = {"brief_ready": "good", "no_ai_adoption_data": "unknown", "incomplete": "error",
               "company_not_found": "unknown", "invalid_input": "error", "error": "error",
               "not_processed": "unknown"}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TOKEN = re.compile(r"\{([^{}]*)\}")
_TOP_KEYS = {"ai_evidence", "tech_intel", "tech_intel_with_evidence", "tech_intel_max_pages", "seller_context",
             "discovery_questions"}
MAX_PAGES_LIMIT = 100


def _placeholders(template: str) -> list[str]:
    out: list[str] = []
    for name in _TOKEN.findall(template):
        if name not in out:
            out.append(name)
    return out


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    for key, default in (("ai_evidence", True), ("tech_intel", True), ("tech_intel_with_evidence", False)):
        if not isinstance(raw.get(key, default), bool):
            raise ConfigError(f"{key} must be true or false.")
    pages = raw.get("tech_intel_max_pages", 1)
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= MAX_PAGES_LIMIT:
        raise ConfigError(f"tech_intel_max_pages must be a whole number from 1 to {MAX_PAGES_LIMIT}.")
    seller_raw = raw.get("seller_context", {})
    if not isinstance(seller_raw, dict):
        raise ConfigError("seller_context must be an object of text values.")
    seller: dict[str, str] = {}
    for k, v in seller_raw.items():
        if not _NAME.match(k) or len(k) > 40:
            raise ConfigError(f"seller_context key {k!r} must be lower-case letters, digits or underscores, "
                              "starting with a letter.")
        if k in PLACEHOLDER_SOURCES:
            raise ConfigError(f"seller_context key {k!r} is reserved for an API fact.")
        if not isinstance(v, str) or not v.strip():
            raise ConfigError(f"seller_context.{k} must be non-empty text.")
        seller[k] = v.strip()
    questions_raw = raw.get("discovery_questions", [])
    if not isinstance(questions_raw, list):
        raise ConfigError("discovery_questions must be a list.")
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, q in enumerate(questions_raw, start=1):
        if not isinstance(q, dict):
            raise ConfigError(f"discovery_questions[{i}] must be an object.")
        extra = sorted(set(q) - {"id", "template"})
        if extra:
            raise ConfigError(f"discovery_questions[{i}] has unknown key(s): {', '.join(extra)}")
        qid = q.get("id")
        if not isinstance(qid, str) or not _ID.match(qid):
            raise ConfigError(f"discovery_questions[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if qid in seen:
            raise ConfigError(f"discovery_questions[{i}].id {qid!r} is used twice.")
        seen.add(qid)
        template = q.get("template")
        if not isinstance(template, str) or not template.strip():
            raise ConfigError(f"discovery_questions[{i}].template must be non-empty text.")
        names = _placeholders(template)
        for name in names:
            if not _NAME.match(name):
                raise ConfigError(f"discovery_questions[{i}].template has an invalid placeholder {{{name}}}.")
            if name not in PLACEHOLDER_SOURCES and name not in seller:
                raise ConfigError(f"discovery_questions[{i}].template uses {{{name}}}, which is not an API fact "
                                  "placeholder or a seller_context key.")
        if "{" in _TOKEN.sub("", template) or "}" in _TOKEN.sub("", template):
            raise ConfigError(f"discovery_questions[{i}].template has an unmatched brace.")
        questions.append({"id": qid, "template": template.strip(), "placeholders": names})
    return {"ai_evidence": raw.get("ai_evidence", True), "tech_intel": raw.get("tech_intel", True),
            "tech_intel_with_evidence": raw.get("tech_intel_with_evidence", False),
            "tech_intel_max_pages": pages, "seller_context": seller, "discovery_questions": questions}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, required=["domain"])


def _valid_unique_domains(rows: list[dict[str, str]]) -> list[str]:
    out: list[str] = []
    for r in rows:
        d = normalise_domain(r.get("domain"))
        if d and d not in out:
            out.append(d)
    return out


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    n = len(_valid_unique_domains(rows))
    calls = [
        PlannedCall("GET /company", "Company profile for each unique valid domain", n,
                    [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]),
        PlannedCall("POST /ai-adoption", "AI Adoption" + (" with evidence" if config["ai_evidence"] else "")
                    + " for each unique valid domain", n, pricing.estimate_ai_adoption(n, config["ai_evidence"])),
    ]
    if config["tech_intel"]:
        pages = config["tech_intel_max_pages"]
        parts = [("tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n))]
        if config["tech_intel_with_evidence"]:
            parts.append(("tech_intel_evidence", pricing.round4(pricing.TECH_INTEL_EVIDENCE_REQUEST * n * pages)))
        calls.append(PlannedCall("GET /tech-intel", f"Up to {pages} page(s) of Tech Intel for each unique valid domain",
                                 n * pages, parts))
    return calls


def _record_error(ctx: RunContext, row_id: str | None, err: dict[str, Any]) -> dict[str, Any]:
    entry = {"row_id": row_id, **err}
    ctx.errors.append(entry)
    if err.get("kind") in FATAL_KINDS and ctx.stopped is None:
        ctx.stopped = entry
    return entry


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _other_org(item: dict[str, Any], company_id: Any, names: list[str]) -> list[dict[str, Any]]:
    found = []
    for field in ORG_ID_FIELDS:
        v = _text(item.get(field))
        if v and v != company_id:
            found.append({"field": field, "value": v})
    for field in ORG_NAME_FIELDS:
        v = _text(item.get(field))
        if v and v.lower() not in names:
            found.append({"field": field, "value": v})
    return found


def _area(area: str, dim: Any, available: Any, ev_all: Any, ai_evidence: bool, company_id: Any,
          names: list[str]) -> dict[str, Any]:
    dim = dim if isinstance(dim, dict) else None
    level = dim.get("level") if dim else None
    out: dict[str, Any] = {
        "area": area, "level": level,
        "level_recognised": level in KNOWN_LEVELS if isinstance(level, str) else None,
        "headline": dim.get("headline") if dim else None,
        "observability": dim.get("observability") if dim else None,
        "counts_toward_overall": dim.get("counts_toward_overall") if dim else None,
        "evidence_available": available,
        "detail": {k: v for k, v in dim.items() if k not in BASE_AREA_KEYS} if dim else {},
        "flags": [], "notes": [],
    }
    if dim is None:
        out["flags"].append("The API returned no data for this area.")
    elif isinstance(level, str) and level not in KNOWN_LEVELS:
        out["notes"].append(f"The level {level} is not one of the documented levels. It is shown as returned.")
    if not ai_evidence:
        out["evidence"] = {"status": "not_requested"}
        return out
    ev = ev_all.get(area) if isinstance(ev_all, dict) else None
    if not isinstance(ev, dict):
        out["evidence"] = {"status": "not_returned"}
        if isinstance(available, int) and available > 0:
            out["flags"].append(f"Firmable holds {available} evidence item(s) for this area, but none were returned.")
        else:
            out["notes"].append("No evidence was returned for this area.")
        return out
    items = [i for i in (ev.get("items") or []) if isinstance(i, dict)]
    qualifying = [i for i in items if i.get("qualifies") is True]
    not_qualifying = [i for i in items if i.get("qualifies") is False]
    unstated = [i for i in items if not isinstance(i.get("qualifies"), bool)]
    count = ev.get("item_count")
    truncated = ev.get("truncated") is True
    if truncated:
        count_text = str(count) if isinstance(count, int) else "an unknown number of"
        out["flags"].append(f"Evidence is truncated: {len(items)} of {count_text} item(s) were returned.")
    if unstated:
        out["notes"].append(f"{len(unstated)} evidence item(s) do not say whether they qualify.")
    other = []
    if area == "workforce":
        for i in items:
            hits = _other_org(i, company_id, names)
            if hits:
                other.append({"ref": i.get("ref"), "fields": hits})
        if other:
            out["flags"].append(f"{len(other)} workforce evidence item(s) name a different organisation in fields "
                                "the API returned. They are shown as returned, not removed.")
    out["evidence"] = {"status": "returned", "as_of": ev.get("as_of"), "framework_version": ev.get("framework_version"),
                       "item_count": count, "items_returned": len(items), "truncated": truncated,
                       "qualifying": qualifying, "not_qualifying": not_qualifying, "qualification_unstated": unstated,
                       "other_organisation_items": other}
    return out


def _ai_section(resp: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    c = resp.get("company")
    if not isinstance(c, dict):
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        return {"status": "not_found", "error_code": err.get("code"), "error_message": err.get("message"),
                "billable": (resp.get("usage") or {}).get("billable")}
    names = [n.lower() for n in (_text(c.get("company_name")), _text(profile.get("name"))) if n]
    dims = c.get("dimensions") if isinstance(c.get("dimensions"), dict) else {}
    available = c.get("evidence_available") if isinstance(c.get("evidence_available"), dict) else {}
    momentum = c.get("momentum") if isinstance(c.get("momentum"), dict) else {}
    level = c.get("overall_level")
    notes = []
    if isinstance(level, str) and level not in KNOWN_LEVELS:
        notes.append(f"The overall level {level} is not one of the documented levels. It is shown as returned.")
    if cfg["ai_evidence"] and "evidence" not in resp:
        notes.append("No evidence was returned for any area.")
    return {
        "status": "found", "firmable_id": c.get("firmable_id"), "company_name": c.get("company_name"),
        "country": c.get("country"), "industry_group": c.get("industry_group"), "size_band": c.get("size_band"),
        "overall_level": level, "explanation": c.get("explanation"),
        "momentum": {"label": momentum.get("label"), "signals": [s for s in (momentum.get("signals") or []) if isinstance(s, dict)]},
        "as_of": c.get("as_of"), "framework_version": c.get("framework_version"),
        "billable": (resp.get("usage") or {}).get("billable"), "notes": notes,
        "areas": [_area(a, dims.get(a), available.get(a), resp.get("evidence"), cfg["ai_evidence"],
                        c.get("firmable_id"), names) for a in AREAS],
    }


def _conflicts(profile: dict[str, Any], ai: dict[str, Any]) -> list[str]:
    if ai.get("status") != "found":
        return []
    out = []
    if _text(ai.get("firmable_id")) and ai["firmable_id"] != profile.get("id"):
        out.append(f"AI Adoption returned company ID {ai['firmable_id']}, but the company profile has ID {profile.get('id')}.")
    a, b = _text(ai.get("company_name")), _text(profile.get("name"))
    if a and b and a.lower() != b.lower():
        out.append(f"AI Adoption names the company \"{a}\"; the company profile names it \"{b}\".")
    a, b = _text(ai.get("country")), _text(profile.get("hq_country"))
    if a and b and a.upper() != b.upper():
        out.append(f"AI Adoption gives the country {a}; the company profile gives the head office country {b}.")
    return out


def _first(items: list[dict[str, Any]], field: str) -> str | None:
    for i in items:
        v = _text(i.get(field))
        if v:
            return v
    return None


def _facts_for_templates(profile: dict[str, Any], ai: dict[str, Any], tech: dict[str, Any]) -> dict[str, str | None]:
    industries = profile.get("industries") if isinstance(profile.get("industries"), list) else []
    values: dict[str, str | None] = {
        "company_name": _text(profile.get("name")),
        "industry": _text(industries[0]) if industries else None,
        "hq_country": _text(profile.get("hq_country")),
        "overall_level": None, "momentum_label": None, "ai_tool": None, "ai_role": None, "technology": None,
    }
    for a in AREAS:
        values[f"{a}_level"] = None
    if ai.get("status") == "found":
        values["overall_level"] = _text(ai.get("overall_level"))
        values["momentum_label"] = _text((ai.get("momentum") or {}).get("label"))
        for area in ai["areas"]:
            values[f"{area['area']}_level"] = _text(area.get("level"))
            ev = area.get("evidence") or {}
            if area["area"] == "footprint":
                values["ai_tool"] = _first(ev.get("qualifying") or [], "technology")
            if area["area"] == "hiring":
                values["ai_role"] = _first(ev.get("qualifying") or [], "title")
    if tech.get("status") == "retrieved":
        values["technology"] = _first([t for t in tech["technologies"] if t.get("confidence") == "high"], "product")
    return values


def _questions(cfg: dict[str, Any], facts: dict[str, str | None]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filled, skipped = [], []
    for q in cfg["discovery_questions"]:
        values, sources, missing = {}, [], []
        for name in q["placeholders"]:
            if name in cfg["seller_context"]:
                values[name] = cfg["seller_context"][name]
                sources.append({"placeholder": name, "value": values[name], "source": "seller context"})
            elif facts.get(name):
                values[name] = facts[name]
                sources.append({"placeholder": name, "value": values[name], "source": PLACEHOLDER_SOURCES[name]})
            else:
                missing.append(name)
        if missing:
            skipped.append({"id": q["id"], "template": q["template"], "missing": missing})
            continue
        text = _TOKEN.sub(lambda m: values[m.group(1)], q["template"])
        filled.append({"id": q["id"], "kind": "suggested_question", "text": text, "template": q["template"],
                       "filled_from": sources})
    return filled, skipped


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = [
        "AI Adoption levels, explanations, headlines and momentum are shown as returned by the API. This code "
        "computes no AI score and no trend.",
        "An AI adoption level or a detected technology does not prove buying intent.",
        "Discovery questions are filled from fixed templates in the configuration, using only API facts and "
        "seller context. They are suggested questions, not facts.",
        "Workforce evidence is checked for a different organisation only in fields the API returns that name "
        "an organisation (company_id, firmable_id, organisation_id, company_name, employer, organisation, "
        "company). The documented workforce item fields do not include an employer, so an item that belongs to "
        "another organisation may not be detectable.",
    ]
    if not cfg["ai_evidence"]:
        coverage.append("AI Adoption evidence was not requested (ai_evidence is false), so qualifying and "
                        "non-qualifying evidence is not shown.")
    if cfg["tech_intel"]:
        coverage.append("A technology not listed is not observed in retrieved data. It does not mean the company "
                        "does not use it.")
    else:
        coverage.append("Tech Intel was not requested (tech_intel is false).")

    for r in ctx.rows:
        r["_domain"] = normalise_domain(r.get("domain")) or ""
    dupes = find_duplicates(ctx.rows, "_domain")
    for r in ctx.rows:
        r.pop("_domain")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier domain; their API responses were reused, not "
                        "requested again.")
    state = {"tech_forbidden": False}

    def tech_section(row_id: str, company_id: str) -> dict[str, Any]:
        if not cfg["tech_intel"]:
            return {"status": "not_requested", "reason": "Tech Intel is turned off in the configuration."}
        if ctx.stopped is not None:
            return {"status": "not_requested", "reason": "The run stopped before Tech Intel was requested."}
        if state["tech_forbidden"]:
            return {"status": "not_on_plan", "reason": "Tech Intel returned 403 earlier in this run, so it was not requested."}
        ti = client.get_tech_intel({"id": company_id}, with_evidence=cfg["tech_intel_with_evidence"],
                                   max_pages=cfg["tech_intel_max_pages"])
        entry = _record_error(ctx, row_id, ti["error"]) if ti["error"] else None
        items = [i for i in ti["items"] if isinstance(i, dict)]
        out = {"total": ti["total"], "retrieved": len(items), "pages_fetched": ti["pages_fetched"],
               "complete": ti["complete"], "stop_reason": ti["stop_reason"], "error": entry, "technologies": items}
        if ti["pages_fetched"] == 0:
            kind = (entry or {}).get("kind")
            if kind == "forbidden":
                state["tech_forbidden"] = True
                return {**out, "status": "not_on_plan",
                        "reason": "Tech Intel returned 403: it is not on your plan, or this key cannot use it."}
            if kind == "not_found":
                return {**out, "status": "not_found", "reason": "Tech Intel has no company for this Firmable ID (404)."}
            message = str((entry or {}).get("message") or "").rstrip(".")
            return {**out, "status": "error", "reason": f"Tech Intel could not be read: {message}."}
        total = ti["total"]
        total_text = str(total) if isinstance(total, int) else "an unknown number of"
        if ti["complete"] and total == 0:
            reason = "Firmable holds no technology data for this company."
        elif ti["complete"]:
            reason = f"Every technology Firmable holds for this company was retrieved: {total_text} in total."
        else:
            reason = (f"{len(items)} of {total_text} technologies were retrieved in {ti['pages_fetched']} page(s). "
                      "Technologies not listed are not observed in retrieved data.")
        return {**out, "status": "retrieved", "reason": reason}

    def one(row: dict[str, str]) -> dict[str, Any]:
        domain = normalise_domain(row.get("domain"))
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "domain": domain,
                                "duplicate_of": dupes.get(row["row_id"]), "company": None, "ai_adoption": None,
                                "tech_intel": None, "data_conflicts": [], "suggested_questions": [],
                                "skipped_questions": []}
        if not domain:
            return {**base, "status": "invalid_input", "status_reason": f"{row.get('domain')!r} is not a valid domain."}
        try:
            profile = client.get_company(fqdn=domain)
        except NotFoundError:
            return {**base, "status": "company_not_found",
                    "status_reason": "Firmable has no company profile for this domain, so no brief was built."}
        base["company"] = company_facts(profile)
        try:
            resp = client.get_ai_adoption({"company_id": profile["id"]}, with_evidence=cfg["ai_evidence"])
            ai = _ai_section(resp, cfg, profile)
        except FirmableError as e:
            ai = {"status": "error", "error": ctx.record_error(row["row_id"], e)}
        tech = tech_section(row["row_id"], profile["id"])
        base["ai_adoption"] = ai
        base["tech_intel"] = tech
        base["data_conflicts"] = _conflicts(profile, ai)
        facts = _facts_for_templates(profile, ai, tech)
        base["suggested_questions"], base["skipped_questions"] = _questions(cfg, facts)

        issues = []
        if ai["status"] == "error":
            issues.append(f"AI Adoption could not be read: {str(ai['error']['message'] or '').rstrip('.')}.")
        if cfg["tech_intel"] and tech["status"] != "retrieved":
            issues.append(tech["reason"])
        if issues:
            return {**base, "status": "incomplete", "status_reason": " ".join(issues)}
        tech_text = f" Tech Intel: {tech['reason']}" if tech["status"] == "retrieved" else ""
        if ai["status"] == "not_found":
            return {**base, "status": "no_ai_adoption_data",
                    "status_reason": "Firmable holds no AI adoption data for this company." + tech_text}
        return {**base, "status": "brief_ready",
                "status_reason": (f"AI Adoption overall level {_text(ai.get('overall_level')) or 'not returned'}, "
                                  f"as of {_text(ai.get('as_of')) or 'an unstated date'}.{tech_text}")}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    levels: dict[str, int] = {}
    skipped: dict[str, int] = {}
    flagged = 0
    for r in rows:
        ai = r.get("ai_adoption") or {}
        if ai.get("status") == "found":
            key = _text(ai.get("overall_level")) or "not returned"
            levels[key] = levels.get(key, 0) + 1
            if any(a["flags"] for a in ai["areas"]):
                flagged += 1
        for s in r.get("skipped_questions") or []:
            skipped[s["id"]] = skipped.get(s["id"], 0) + 1
    if flagged:
        coverage.append(f"{flagged} brief(s) have missing, truncated or questionable evidence flagged in at least "
                        "one area.")
    if skipped:
        coverage.append("Some discovery-question templates were skipped because a placeholder had no value: "
                        + ", ".join(f"{k} ({v} row(s))" for k, v in skipped.items()) + ".")
    if any(r.get("data_conflicts") for r in rows):
        coverage.append("Some company facts differ between the company profile and AI Adoption. Both values are "
                        "shown; this code does not correct upstream data.")
    if state["tech_forbidden"]:
        coverage.append("Tech Intel returned 403: it is not on your plan, or this key cannot use it. It was not "
                        "requested again in this run.")
    return {"rows": rows, "summary": {"status_counts": counts, "overall_level_counts": levels,
                                      "skipped_question_counts": skipped},
            "coverage": coverage}


CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
               "overall_level", "momentum_label", "architecture_level", "footprint_level", "hiring_level",
               "initiatives_level", "workforce_level", "ai_as_of", "technologies_total", "technologies_retrieved",
               "tech_list_complete", "flags", "suggested_questions", "skipped_questions"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        ai = r.get("ai_adoption") or {}
        tech = r.get("tech_intel") or {}
        areas = {a["area"]: a for a in ai.get("areas") or []}
        flags = list(r.get("data_conflicts") or [])
        for a in ai.get("areas") or []:
            flags += [f"{a['area']}: {f}" for f in a["flags"]]
        row = {
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_id": company.get("id"), "company_name": company.get("name"),
            "overall_level": ai.get("overall_level"), "momentum_label": (ai.get("momentum") or {}).get("label"),
            "ai_as_of": ai.get("as_of"), "technologies_total": tech.get("total"),
            "technologies_retrieved": tech.get("retrieved"), "tech_list_complete": tech.get("complete"),
            "flags": flags,
            "suggested_questions": [q["text"] for q in r.get("suggested_questions") or []],
            "skipped_questions": [q["id"] for q in r.get("skipped_questions") or []],
        }
        for a in AREAS:
            row[f"{a}_level"] = (areas.get(a) or {}).get("level")
        out.append(row)
    return out


def _items_table(items: list[dict[str, Any]]) -> str:
    rows = []
    for i in items:
        label = (i.get("technology") or i.get("title") or i.get("summary") or "")
        when = i.get("date") or i.get("last_seen") or i.get("first_seen")
        rows.append([label, i.get("type") or i.get("category"), when,
                     i.get("confidence_band") or i.get("seniority"), i.get("state") or i.get("observation"),
                     (i.get("ref") or {}).get("id") if isinstance(i.get("ref"), dict) else None])
    return table(["Item", "Type or category", "Date", "Confidence or seniority", "State", "Reference"], rows)


def _area_html(a: dict[str, Any]) -> str:
    html = f"<h3>{esc(a['area'].capitalize())}: {esc(a.get('level') or 'no level')}</h3>"
    html += f"<p>{esc(a.get('headline'))}</p>" if a.get("headline") else "<p class=\"muted\">No headline returned.</p>"
    for f in a["flags"]:
        html += note(esc(f), "review")
    for n in a["notes"]:
        html += note(esc(n), "unknown")
    ev = a.get("evidence") or {}
    if ev.get("status") == "returned":
        html += (f"<p class=\"muted\">Evidence as of {esc(ev.get('as_of'))}: {esc(ev.get('items_returned'))} of "
                 f"{esc(ev.get('item_count'))} item(s) returned"
                 + (", truncated" if ev.get("truncated") else "") + ".</p>")
        if ev["qualifying"]:
            html += "<p><strong>Qualifying evidence</strong></p>" + _items_table(ev["qualifying"])
        if ev["not_qualifying"]:
            html += "<p><strong>Evidence that does not qualify</strong></p>" + _items_table(ev["not_qualifying"])
        if ev["qualification_unstated"]:
            html += "<p><strong>Evidence that does not say whether it qualifies</strong></p>" + _items_table(ev["qualification_unstated"])
        if ev["other_organisation_items"]:
            html += details_json("Workforce items that name another organisation", ev["other_organisation_items"])
    return html


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Brief summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("Levels, explanations and headlines are the API's own words. This report computes no AI "
                      "score and no trend. An AI adoption level or a detected technology does not prove buying "
                      "intent.", "unknown"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = (r.get("company") or {}).get("name") or r.get("domain") or r["input"].get("domain") or r["row_id"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        company = r.get("company")
        if company:
            left += kv([("Firmable ID", company.get("id")), ("Domain", company.get("fqdn")),
                        ("Head office country", company.get("hq_country")), ("Size", company.get("company_size")),
                        ("Industries", ", ".join(company.get("industries") or []) or None)])
        ai = r.get("ai_adoption") or {}
        if ai.get("status") == "found":
            left += "<h3>AI Adoption</h3>" + kv([
                ("Overall level", ai.get("overall_level")), ("Explanation", ai.get("explanation")),
                ("Momentum", (ai.get("momentum") or {}).get("label")),
                ("Momentum signals", "; ".join(f"{s.get('kind')}: {s.get('count')}, latest {s.get('latest')}"
                                               for s in (ai.get("momentum") or {}).get("signals") or []) or None),
                ("Data as of", ai.get("as_of")), ("Framework version", ai.get("framework_version"))])
            for n in ai.get("notes") or []:
                left += note(esc(n), "unknown")
        elif ai.get("status") == "not_found":
            left += note("Firmable holds no AI adoption data for this company.", "unknown")
        elif ai.get("status") == "error":
            left += note(f"AI Adoption could not be read: {esc(ai['error'].get('message'))}", "error")
        for c in r.get("data_conflicts") or []:
            left += note(esc(c), "review")
        if r.get("duplicate_of"):
            left += note(f"Same domain as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        if ai.get("status") == "found":
            right += table(["Area", "Level", "Observability", "Counts toward overall", "Evidence held", "Flags"], [
                [a["area"], a.get("level"), a.get("observability"), a.get("counts_toward_overall"),
                 a.get("evidence_available"), len(a["flags"])] for a in ai["areas"]])
            right += "".join(_area_html(a) for a in ai["areas"])
        tech = r.get("tech_intel") or {}
        if tech:
            right += f"<h3>Tech Intel</h3><p class=\"muted\">{esc(tech.get('reason'))}</p>"
            techs = tech.get("technologies") or []
            if techs:
                right += table(["Product", "Vendor", "Category", "Confidence", "Recency", "Last observed", "Nature"], [
                    [t.get("product"), t.get("vendor"), t.get("category"), t.get("confidence"), t.get("recency"),
                     t.get("last_observed"), t.get("nature")] for t in techs])
        qs = r.get("suggested_questions") or []
        skipped = r.get("skipped_questions") or []
        if qs or skipped:
            body = "<p><strong>Suggested questions.</strong> Filled from fixed templates. These are not facts.</p>"
            body += bullet_list(q["text"] for q in qs)
            if skipped:
                body += "<p class=\"muted\">Skipped templates (a placeholder had no value):</p>" + bullet_list(
                    f"{s['id']}: missing {', '.join(s['missing'])}" for s in skipped)
            right += f"<div class=\"template\">{body}</div>"
        if ai or tech:
            right += details_json("Brief as JSON", {"ai_adoption": ai, "tech_intel": tech})
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No brief was built for this row.</p>", sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account brief", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
