"""Tech sales opportunities: finds the technologies a seller cares about in Firmable Tech Intel.

Workflow for each row:
1. Normalise the domain. An invalid domain is reported and skipped.
2. Optionally fetch the company profile (GET /company) when fetch_company is true.
3. Read Tech Intel (GET /tech-intel), with evidence when configured, following next_cursor up to
   max_pages pages.
4. Match every retrieved technology against each configured target using only explicit product
   slugs, vendor slugs and exact, case-insensitive aliases of the product or vendor name.
5. Check each evidence record's location against the company domain.

Target results:
- observed: a retrieved technology matched the target;
- not_observed: no match, and every page of the technology list was retrieved;
- not_observed_in_pages_retrieved: no match, but the technology list is incomplete;
- not_checked: no Tech Intel data was retrieved for the row.
"Not observed" means not observed in retrieved data. It never means the company does not use it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.errors import ConfigError, NotFoundError
from firmable_client.facts import company_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import FATAL_KINDS, PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "tech-sales-opportunities"
TITLE = "Tech sales opportunities"
DESCRIPTION = ("Finds the technologies in the configuration in each company's Firmable Tech Intel, with "
               "confidence, recency, observation dates and evidence locations, and reports what was not "
               "observed in retrieved data.")

STATUS_TONE = {"matched": "good", "not_observed": "neutral", "not_observed_incomplete": "review",
               "no_technology_data": "unknown", "company_not_found": "unknown", "not_on_plan": "error",
               "invalid_input": "error", "error": "error", "not_processed": "unknown"}
RESULT_TONE = {"observed": "good", "not_observed": "neutral", "not_observed_in_pages_retrieved": "review",
               "not_checked": "error"}
RESULT_TEXT = {
    "observed": "Observed in retrieved data.",
    "not_observed": "Not observed in retrieved data.",
    "not_observed_in_pages_retrieved": "Not observed in the pages retrieved. The technology list is incomplete.",
    "not_checked": "Not checked: no Tech Intel data was retrieved.",
}
FLAG_TEXT = {
    "no_location": "Evidence record has no location.",
    "location_differs_from_company_domain": "Evidence location differs from the company domain.",
    "location_not_a_domain": ("Evidence location is not a domain or URL, so it could not be compared with "
                              "the company domain."),
}
STOP_TEXT = {
    "page_limit": "the page limit was reached",
    "error": "a later page failed",
    "budget": "the credit limit was reached",
    "repeated_cursor": "the API returned a cursor it had already returned",
}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_TOP_KEYS = {"targets", "with_evidence", "max_pages", "fetch_company"}
_TARGET_KEYS = {"id", "label", "relationship", "match"}
_MATCH_KEYS = {"product_slugs", "vendor_slugs", "aliases"}
MAX_PAGES_LIMIT = 100


def _text_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where} must be a list of non-empty text values.")
    return [v.strip() for v in value]


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError("targets must be a non-empty list.")
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, t in enumerate(targets_raw, start=1):
        if not isinstance(t, dict):
            raise ConfigError(f"targets[{i}] must be an object.")
        extra = sorted(set(t) - _TARGET_KEYS)
        if extra:
            raise ConfigError(f"targets[{i}] has unknown key(s): {', '.join(extra)}")
        tid = t.get("id")
        if not isinstance(tid, str) or not _ID.match(tid):
            raise ConfigError(f"targets[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if tid in seen:
            raise ConfigError(f"targets[{i}].id {tid!r} is used twice.")
        seen.add(tid)
        label = t.get("label", tid)
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"targets[{i}].label must be text.")
        rel = t.get("relationship")
        if not isinstance(rel, str) or not rel.strip():
            raise ConfigError(f"targets[{i}].relationship must be text, such as competitor or integration.")
        match = t.get("match")
        if not isinstance(match, dict):
            raise ConfigError(f"targets[{i}].match must be an object.")
        extra = sorted(set(match) - _MATCH_KEYS)
        if extra:
            raise ConfigError(f"targets[{i}].match has unknown key(s): {', '.join(extra)}")
        m = {k: _text_list(match.get(k), f"targets[{i}].match.{k}") for k in ("product_slugs", "vendor_slugs", "aliases")}
        if not (m["product_slugs"] or m["vendor_slugs"] or m["aliases"]):
            raise ConfigError(f"targets[{i}].match needs at least one product slug, vendor slug or alias.")
        targets.append({"id": tid, "label": label.strip(), "relationship": rel.strip(), "match": m})
    for key, default in (("with_evidence", True), ("fetch_company", False)):
        if not isinstance(raw.get(key, default), bool):
            raise ConfigError(f"{key} must be true or false.")
    max_pages = raw.get("max_pages", 3)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= MAX_PAGES_LIMIT:
        raise ConfigError(f"max_pages must be a whole number from 1 to {MAX_PAGES_LIMIT}.")
    return {"targets": targets, "with_evidence": raw.get("with_evidence", True), "max_pages": max_pages,
            "fetch_company": raw.get("fetch_company", False)}


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
    pages = config["max_pages"]
    calls: list[PlannedCall] = []
    if config["fetch_company"]:
        calls.append(PlannedCall("GET /company", "Company profile for each unique valid domain", n,
                                 [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    parts = [("tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n))]
    if config["with_evidence"]:
        parts.append(("tech_intel_evidence", pricing.round4(pricing.TECH_INTEL_EVIDENCE_REQUEST * n * pages)))
    calls.append(PlannedCall("GET /tech-intel", f"Up to {pages} page(s) of Tech Intel for each unique valid domain",
                             n * pages, parts))
    return calls


def _match_basis(item: dict[str, Any], match: dict[str, list[str]]) -> list[str]:
    basis: list[str] = []
    if isinstance(item.get("product_slug"), str) and item["product_slug"] in match["product_slugs"]:
        basis.append(f"product_slug {item['product_slug']}")
    if isinstance(item.get("vendor_slug"), str) and item["vendor_slug"] in match["vendor_slugs"]:
        basis.append(f"vendor_slug {item['vendor_slug']}")
    aliases = [a.lower() for a in match["aliases"]]
    for field in ("product", "vendor"):
        value = item.get(field)
        if isinstance(value, str) and value.strip().lower() in aliases:
            basis.append(f"alias of {field} name {value}")
    return basis


def _same_domain(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _evidence(item: dict[str, Any], with_evidence: bool, domains: list[str]) -> dict[str, Any]:
    if not with_evidence:
        return {"status": "not_requested"}
    ev = item.get("evidence_detail")
    if not isinstance(ev, dict):
        return {"status": "none_returned", "notes": ["No evidence was returned for this technology."]}
    records = []
    for rec in ev.get("records") or []:
        if not isinstance(rec, dict):
            continue
        location = rec.get("location")
        flags: list[str] = []
        host = None
        if not isinstance(location, str) or not location.strip():
            flags.append("no_location")
        else:
            host = normalise_domain(location)
            if host is None:
                flags.append("location_not_a_domain")
            elif not _same_domain(host, domains):
                flags.append("location_differs_from_company_domain")
        records.append({"api_record": rec, "location_host": host, "flags": flags,
                        "flag_messages": [FLAG_TEXT[f] for f in flags]})
    by_type = [t for t in (ev.get("evidence_by_type") or []) if isinstance(t, dict)]
    capped = [t["type"] for t in by_type if t.get("capped") is True and isinstance(t.get("type"), str)]
    count = ev.get("evidence_count")
    notes: list[str] = []
    if isinstance(count, int) and len(records) < count:
        notes.append(f"{len(records)} of {count} evidence record(s) are listed. The API lists up to 25 records per type.")
    if capped:
        notes.append(f"The source reported more evidence than it sent for: {', '.join(capped)}. Those counts are minimums.")
    return {"status": "returned", "confidence": ev.get("confidence"), "first_seen": ev.get("first_seen"),
            "last_seen": ev.get("last_seen"), "evidence_count": count, "evidence_by_type": by_type,
            "records_listed": len(records), "capped_types": capped, "records": records, "notes": notes}


def _technology(item: dict[str, Any]) -> dict[str, Any]:
    keys = ["product", "product_slug", "vendor", "vendor_slug", "filter", "filter_slug", "category",
            "category_slug", "pillar_slug", "confidence", "recency", "last_observed", "nature"]
    return {k: item.get(k) for k in keys}


def _record_error(ctx: RunContext, row_id: str | None, err: dict[str, Any], stop: bool = False) -> dict[str, Any]:
    entry = {"row_id": row_id, **err}
    ctx.errors.append(entry)
    if (stop or err.get("kind") in FATAL_KINDS) and ctx.stopped is None:
        ctx.stopped = entry
    return entry


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = [
        "A target that was not matched is not observed in retrieved data. This does not mean the company "
        "does not use it.",
        "Tech Intel reports observed technology signals with Firmable's confidence and recency. It does not "
        "report replacement intent, spend, licence count or active usage, and a detected technology does not "
        "prove buying intent.",
        "Matching uses only the product slugs, vendor slugs and exact aliases in the configuration. There is "
        "no fuzzy matching.",
    ]
    if not cfg["with_evidence"]:
        coverage.append("Evidence was not requested (with_evidence is false), so no evidence locations are shown.")

    for r in ctx.rows:
        r["_domain"] = normalise_domain(r.get("domain")) or ""
    dupes = find_duplicates(ctx.rows, "_domain")
    for r in ctx.rows:
        r.pop("_domain")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier domain; their API responses were reused, not "
                        "requested again.")

    def one(row: dict[str, str]) -> dict[str, Any]:
        domain = normalise_domain(row.get("domain"))
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "domain": domain,
                                "duplicate_of": dupes.get(row["row_id"]), "company": None, "tech_intel": None,
                                "targets": [], "evidence_flag_counts": {}}
        if not domain:
            return {**base, "status": "invalid_input", "status_reason": f"{row.get('domain')!r} is not a valid domain."}
        company = None
        identifier = {"fqdn": domain}
        if cfg["fetch_company"]:
            try:
                company = client.get_company(fqdn=domain)
            except NotFoundError:
                return {**base, "status": "company_not_found",
                        "status_reason": "Firmable has no company profile for this domain, so Tech Intel was not requested."}
            base["company"] = company_facts(company)
            identifier = {"id": company["id"]}
        domains = [domain]
        if company:
            for value in (company.get("fqdn"), company.get("website")):
                d = normalise_domain(value) if isinstance(value, str) else None
                if d and d not in domains:
                    domains.append(d)

        ti = client.get_tech_intel(identifier, with_evidence=cfg["with_evidence"], max_pages=cfg["max_pages"])
        err = ti["error"]
        entry = None
        if err:
            entry = _record_error(ctx, row["row_id"], err, stop=err.get("kind") == "forbidden")
        items = [i for i in ti["items"] if isinstance(i, dict)]
        total = ti["total"]
        base["tech_intel"] = {"company_id": ti["id"], "total": total, "retrieved": len(items),
                              "pages_fetched": ti["pages_fetched"], "complete": ti["complete"],
                              "stop_reason": ti["stop_reason"], "error": entry}
        targets_out = [{"target_id": t["id"], "label": t["label"], "relationship": t["relationship"]} for t in cfg["targets"]]
        if ti["pages_fetched"] == 0:
            for t in targets_out:
                t.update(result="not_checked", result_text=RESULT_TEXT["not_checked"], matches=[])
            base["targets"] = targets_out
            kind = (err or {}).get("kind")
            if kind == "not_found":
                return {**base, "status": "company_not_found",
                        "status_reason": "Tech Intel has no company for this identifier (404)."}
            if kind == "forbidden":
                return {**base, "status": "not_on_plan",
                        "status_reason": ("Tech Intel returned 403: it is not on your plan, or this key cannot use it. "
                                          "No further rows were requested.")}
            return {**base, "status": "error",
                    "status_reason": f"Tech Intel could not be read: {(err or {}).get('message')}"}

        complete = ti["complete"]
        flag_counts = {k: 0 for k in FLAG_TEXT}
        for t, out in zip(cfg["targets"], targets_out):
            matches = []
            for item in items:
                basis = _match_basis(item, t["match"])
                if basis:
                    ev = _evidence(item, cfg["with_evidence"], domains)
                    for rec in ev.get("records") or []:
                        for f in rec["flags"]:
                            flag_counts[f] += 1
                    matches.append({"match_basis": basis, "technology": _technology(item), "evidence": ev})
            result = "observed" if matches else ("not_observed" if complete else "not_observed_in_pages_retrieved")
            out.update(result=result, result_text=RESULT_TEXT[result], matches=matches)
        base["targets"] = targets_out
        base["evidence_flag_counts"] = {k: v for k, v in flag_counts.items() if v}

        observed = [t for t in targets_out if t["result"] == "observed"]
        n = len(targets_out)
        total_text = str(total) if isinstance(total, int) else "an unknown number of"
        tail = ""
        if not complete:
            why = STOP_TEXT.get(ti["stop_reason"], ti["stop_reason"])
            tail = (f" The technology list is incomplete because {why}: {len(items)} of {total_text} "
                    f"technologies were retrieved in {ti['pages_fetched']} page(s).")
        if observed:
            labels = ", ".join(f"{t['label']} ({t['relationship']})" for t in observed)
            return {**base, "status": "matched",
                    "status_reason": f"{len(observed)} of {n} target(s) observed in retrieved data: {labels}.{tail}"}
        if complete and total == 0:
            return {**base, "status": "no_technology_data",
                    "status_reason": (f"Firmable holds no technology data for this company (total 0), so none of the "
                                      f"{n} target(s) were observed in retrieved data.")}
        if complete:
            return {**base, "status": "not_observed",
                    "status_reason": (f"None of the {n} target(s) were observed in retrieved data. All {total_text} "
                                      "technologies were retrieved.")}
        return {**base, "status": "not_observed_incomplete",
                "status_reason": f"None of the {n} target(s) were observed in the pages retrieved.{tail}"}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    target_counts: dict[str, dict[str, int]] = {}
    flags_total = {k: 0 for k in FLAG_TEXT}
    incomplete = 0
    for r in rows:
        for t in r.get("targets") or []:
            tc = target_counts.setdefault(t["target_id"], {k: 0 for k in RESULT_TEXT})
            tc[t["result"]] += 1
        for k, v in (r.get("evidence_flag_counts") or {}).items():
            flags_total[k] += v
        ti = r.get("tech_intel") or {}
        if ti.get("pages_fetched") and not ti.get("complete"):
            incomplete += 1
    if incomplete:
        coverage.append(f"{incomplete} row(s) have an incomplete technology list (max_pages is {cfg['max_pages']}). "
                        "Targets not matched for them are not observed in the pages retrieved.")
    if flags_total["no_location"]:
        coverage.append(f"{flags_total['no_location']} evidence record(s) have no location, so their source "
                        "cannot be shown.")
    if flags_total["location_differs_from_company_domain"]:
        coverage.append(f"{flags_total['location_differs_from_company_domain']} evidence record(s) have a location "
                        "that differs from the company domain. They are shown as returned, not dropped. Job ads "
                        "and review sites are often on other domains.")
    if flags_total["location_not_a_domain"]:
        coverage.append(f"{flags_total['location_not_a_domain']} evidence record(s) have a location that is not a "
                        "domain or URL.")
    return {"rows": rows, "summary": {"status_counts": counts, "target_counts": target_counts,
                                      "evidence_flag_counts": {k: v for k, v in flags_total.items() if v}},
            "coverage": coverage}


def _s(value: Any) -> str:
    return "" if value is None else str(value)


CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
               "technologies_total", "technologies_retrieved", "pages_fetched", "list_complete",
               "targets_observed", "target_results", "observed_technologies", "evidence_flags"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        targets = r.get("targets") or []
        company = r.get("company") or {}
        ti = r.get("tech_intel") or {}
        observed_tech = []
        for t in targets:
            for m in t.get("matches") or []:
                tech = m["technology"]
                observed_tech.append(f"{t['target_id']}:{_s(tech.get('product'))}({_s(tech.get('confidence'))},"
                                     f"{_s(tech.get('recency'))},{_s(tech.get('last_observed'))})")
        out.append({
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_id": company.get("id") or ti.get("company_id"), "company_name": company.get("name"),
            "technologies_total": ti.get("total"), "technologies_retrieved": ti.get("retrieved"),
            "pages_fetched": ti.get("pages_fetched"), "list_complete": ti.get("complete"),
            "targets_observed": [t["target_id"] for t in targets if t["result"] == "observed"],
            "target_results": [f"{t['target_id']}={t['result']}" for t in targets],
            "observed_technologies": observed_tech,
            "evidence_flags": [f"{k}={v}" for k, v in (r.get("evidence_flag_counts") or {}).items()],
        })
    return out


def _evidence_html(ev: dict[str, Any]) -> str:
    if ev["status"] == "not_requested":
        return "<p class=\"muted\">Evidence was not requested.</p>"
    if ev["status"] == "none_returned":
        return note("No evidence was returned for this technology.", "unknown")
    html = kv([("Evidence confidence", ev.get("confidence")), ("First seen", ev.get("first_seen")),
               ("Last seen", ev.get("last_seen")), ("Evidence count", ev.get("evidence_count")),
               ("Records listed", ev.get("records_listed")),
               ("By type", ", ".join(f"{t.get('type')}: {t.get('count')}" + (" (minimum)" if t.get("capped") else "")
                                     for t in ev.get("evidence_by_type") or []) or None)])
    for n in ev.get("notes") or []:
        html += note(esc(n), "unknown")
    recs = ev.get("records") or []
    if recs:
        html += table(["Type", "Found", "Location", "Record", "Belongs because", "Seen", "Confidence", "Flags"], [
            [rec["api_record"].get("type"), rec["api_record"].get("found"), rec["api_record"].get("location"),
             rec["api_record"].get("record"), ", ".join(str(b) for b in rec["api_record"].get("belongs_because") or []),
             f"{rec['api_record'].get('first_seen') or '?'} to {rec['api_record'].get('last_seen') or '?'}",
             rec["api_record"].get("confidence"),
             Raw(" ".join(badge(m, "review") for m in rec["flag_messages"]) or "")] for rec in recs])
    return html


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Technology match summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("Not observed means not observed in retrieved data. It does not mean the company does not "
                      "use the technology. A detected technology does not prove buying intent, spend, licence "
                      "count or active usage.", "unknown"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = (r.get("company") or {}).get("name") or r.get("domain") or r["input"].get("domain") or r["row_id"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        ti = r.get("tech_intel")
        if ti:
            left += kv([("Firmable ID", ti.get("company_id")), ("Technologies in Firmable", ti.get("total")),
                        ("Technologies retrieved", ti.get("retrieved")), ("Pages fetched", ti.get("pages_fetched")),
                        ("List complete", ti.get("complete")), ("Stopped because", ti.get("stop_reason"))])
            if ti.get("error"):
                left += note(esc(ti["error"].get("message")), "error")
        if r.get("duplicate_of"):
            left += note(f"Same domain as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        targets = r.get("targets") or []
        right = ""
        if targets:
            right += table(["Target", "Relationship", "Result"], [
                [t["label"], t["relationship"], Raw(badge(t["result"].replace("_", " "), RESULT_TONE.get(t["result"], "neutral")))]
                for t in targets])
            for t in targets:
                for m in t.get("matches") or []:
                    tech = m["technology"]
                    right += (f"<h3>{esc(t['label'])}: {esc(tech.get('product'))}</h3>"
                              + kv([("Vendor", tech.get("vendor")), ("Category", tech.get("category")),
                                    ("Filter", tech.get("filter")), ("Confidence", tech.get("confidence")),
                                    ("Recency", tech.get("recency")), ("Last observed", tech.get("last_observed")),
                                    ("Nature", tech.get("nature")), ("Matched by", "; ".join(m["match_basis"]))])
                              + _evidence_html(m["evidence"]))
            missing = [t["label"] for t in targets if t["result"] != "observed"]
            if missing:
                right += "<h3>Not observed in retrieved data</h3>" + bullet_list(missing)
            right += details_json("Target results as JSON", targets)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No Tech Intel was retrieved for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its target technologies", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
