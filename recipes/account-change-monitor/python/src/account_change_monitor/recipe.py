"""Account change monitor: compares a new snapshot of each account with a saved snapshot.

Workflow for each row:
1. Work out the company identifier (company_id or domain) and the contact identifier (person_id,
   work_email or linkedin_url). A row with an invalid identifier is reported and skipped.
2. Fetch each enabled source: company profile, person profile, AI Adoption and Tech Intel.
3. Compare the monitored fields with the same account in the previous snapshot.
4. Build the account's entry in the new snapshot.

Comparison policy:
- Only the configured fields are compared. Volatile fields (request IDs, usage, AI Adoption as_of,
  Tech Intel recency and last_observed) are never compared.
- newly_observed: the field was empty in the previous snapshot and has a value now.
- no_longer_observed_in_retrieved_data: the profile came back, but the field is now empty. For Tech
  Intel, only when both technology lists are complete.
- value_changed: both snapshots hold a value and the values differ.
- not_compared_fetch_failed / not_compared_not_found: the source failed or returned nothing in this
  run. This is never reported as a deletion, and the previous values are kept in the new snapshot.
- An account or source with no previous data gets a baseline and no change claims.
A difference is a difference in the retrieved data. It is never a confirmed real-world change.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import FATAL_KINDS, PlannedCall, Recipe, RunContext, parse_date
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "account-change-monitor"
TITLE = "Account change monitor"
DESCRIPTION = ("Takes a new snapshot of company, contact, Tech Intel and AI Adoption data for each account, "
               "compares it with a saved snapshot, and reports what differs in the retrieved data.")

SNAPSHOT_FORMAT = "gtm-account-change-monitor/1"
SOURCES = ["company", "person", "ai_adoption", "tech_intel"]
SOURCE_LABEL = {"company": "Company profile", "person": "Person profile", "ai_adoption": "AI Adoption",
                "tech_intel": "Tech Intel"}
ENDPOINT = {"company": "GET /company", "person": "GET /people", "ai_adoption": "POST /ai-adoption",
            "tech_intel": "GET /tech-intel"}

COMPANY_TOP = {"id", "name", "website", "fqdn", "description", "linkedin", "employee_count", "year_founded",
               "hq_location", "hq_country", "company_size", "company_type", "locations", "industries", "revenue",
               "phones", "emails", "social_media", "industry_codes", "keywords", "org_registers", "states",
               "abn_operation_status"}
PERSON_TOP = {"id", "name", "first_name", "last_name", "position", "seniority", "department", "headline", "country",
              "location", "time_in_current_role", "month_joined", "year_joined", "experience", "current_company",
              "emails", "phones", "skills", "linkedin", "slug", "social_media", "secondary_position"}
AI_TOP = {"firmable_id", "company_name", "country", "industry_group", "size_band", "overall_level", "explanation",
          "momentum", "dimensions", "evidence_available", "framework_version"}
DEFAULT_COMPANY = ["name", "company_size", "employee_count", "hq_country", "hq_location", "industries",
                   "company_type", "year_founded", "revenue", "abn_operation_status"]
DEFAULT_PERSON = ["position", "seniority", "department", "current_company.id", "current_company.name", "country"]
DEFAULT_AI = ["overall_level", "momentum.label", "dimensions.architecture.level", "dimensions.footprint.level",
              "dimensions.hiring.level", "dimensions.initiatives.level", "dimensions.workforce.level"]

CHANGE_TYPES = ["newly_observed", "no_longer_observed_in_retrieved_data", "value_changed",
                "not_compared_fetch_failed", "not_compared_not_found"]
DIFFERENCE_TYPES = {"newly_observed", "no_longer_observed_in_retrieved_data", "value_changed"}
CHANGE_MEANING = {
    "newly_observed": "Empty in the previous snapshot and has a value in this run's retrieved data.",
    "no_longer_observed_in_retrieved_data": "Had a value in the previous snapshot and is empty in this run's "
                                            "retrieved data. It does not mean the thing no longer exists.",
    "value_changed": "Both snapshots hold a value and the values differ.",
    "not_compared_fetch_failed": "The request failed or was not sent in this run, so nothing was compared.",
    "not_compared_not_found": "Firmable returned no data for this subject in this run, so nothing was compared.",
}
STATUS_TONE = {"changes_detected": "review", "no_changes_detected": "good", "partially_compared": "unknown",
               "baseline_created": "info", "fetch_failed": "error", "not_monitored": "unknown",
               "invalid_input": "error", "error": "error", "not_processed": "unknown"}
CHANGE_TONE = {"newly_observed": "info", "no_longer_observed_in_retrieved_data": "review",
               "value_changed": "review", "not_compared_fetch_failed": "error", "not_compared_not_found": "unknown"}

_TOP_KEYS = {"previous_snapshot", "evaluation_date", "company_fields", "person_fields", "ai_adoption_fields",
             "tech_intel"}
_FIELD = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ----- configuration --------------------------------------------------------------------------

def _fields(raw: dict[str, Any], key: str, allowed: set[str], default: list[str]) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not all(isinstance(f, str) for f in value):
        raise ConfigError(f"{key} must be a list of field names.")
    out: list[str] = []
    for f in value:
        if not _FIELD.match(f) or f.split(".")[0] not in allowed:
            raise ConfigError(f"{key} has an unknown field {f!r}. Allowed top-level fields: "
                              f"{', '.join(sorted(allowed))}.")
        if f in out:
            raise ConfigError(f"{key} lists {f!r} twice.")
        out.append(f)
    return out


def _load_previous(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        p = Path(value)
        if not p.is_file():
            raise ConfigError(f"previous_snapshot file not found: {value}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ConfigError(f"previous_snapshot file {value} is not valid JSON.") from e
    elif isinstance(value, dict):
        data = value
    else:
        raise ConfigError("previous_snapshot must be a file path, a snapshot object or null.")
    if not isinstance(data, dict) or data.get("snapshot_format") != SNAPSHOT_FORMAT:
        raise ConfigError(f"previous_snapshot is not a {SNAPSHOT_FORMAT} snapshot. Use the snapshot.json "
                          "written by an earlier run.")
    parse_date(data.get("taken_on"), "previous_snapshot taken_on")
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        raise ConfigError("previous_snapshot has no accounts object.")
    for key, acc in accounts.items():
        if not isinstance(acc, dict) or not isinstance(acc.get("sources"), dict):
            raise ConfigError(f"previous_snapshot account {key!r} has no sources object.")
    return data


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    company = _fields(raw, "company_fields", COMPANY_TOP, DEFAULT_COMPANY)
    person = _fields(raw, "person_fields", PERSON_TOP, DEFAULT_PERSON)
    ai = _fields(raw, "ai_adoption_fields", AI_TOP, DEFAULT_AI)
    ti = raw.get("tech_intel", {})
    if ti is None:
        ti = {}
    if not isinstance(ti, dict):
        raise ConfigError("tech_intel must be an object.")
    extra = sorted(set(ti) - {"enabled", "max_pages"})
    if extra:
        raise ConfigError(f"Unknown tech_intel key(s): {', '.join(extra)}")
    enabled = ti.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("tech_intel.enabled must be true or false.")
    max_pages = ti.get("max_pages", 1)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 20:
        raise ConfigError("tech_intel.max_pages must be a whole number from 1 to 20.")
    if not company and not person and not ai and not enabled:
        raise ConfigError("Nothing to monitor: every field list is empty and tech_intel is disabled.")
    evaluation_date = raw.get("evaluation_date")
    if evaluation_date is not None:
        evaluation_date = parse_date(evaluation_date, "evaluation_date").isoformat()
    prev_raw = raw.get("previous_snapshot")
    previous = _load_previous(prev_raw)
    return {"previous_snapshot": prev_raw if isinstance(prev_raw, str) else ("inline" if previous else None),
            "evaluation_date": evaluation_date, "company_fields": company, "person_fields": person,
            "ai_adoption_fields": ai, "tech_intel": {"enabled": enabled, "max_pages": max_pages},
            "previous_snapshot_data": previous}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=["domain", "company_id", "person_id", "work_email", "linkedin_url"])


# ----- subjects -------------------------------------------------------------------------------

def _subject(row: dict[str, str]) -> tuple[dict[str, str] | None, dict[str, str] | None, list[str]]:
    """Returns the company identifier, the person identifier and any problems."""
    problems: list[str] = []
    company: dict[str, str] | None = None
    person: dict[str, str] | None = None
    if row.get("company_id"):
        company = {"id": row["company_id"]}
    elif row.get("domain"):
        d = normalise_domain(row["domain"])
        if d:
            company = {"fqdn": d}
        else:
            problems.append(f"{row['domain']!r} is not a valid domain.")
    if row.get("person_id"):
        person = {"id": row["person_id"]}
    elif row.get("work_email"):
        e = row["work_email"].lower()
        if _EMAIL.match(e):
            person = {"work_email": e}
        else:
            problems.append(f"{row['work_email']!r} is not a valid work email.")
    elif row.get("linkedin_url"):
        u = row["linkedin_url"]
        if re.match(r"^https?://([a-z]+\.)?linkedin\.com/in/[^/?#]+", u, re.IGNORECASE):
            person = {"ln_url": u}
        else:
            problems.append(f"{u!r} is not a LinkedIn profile URL.")
    return company, person, problems


def _ident(identifier: dict[str, str] | None) -> str:
    if not identifier:
        return ""
    k = next(iter(identifier))
    return f"{k}={identifier[k]}"


def _key(company: dict[str, str] | None, person: dict[str, str] | None) -> str:
    parts = []
    if company:
        parts.append(f"company:{_ident(company)}")
    if person:
        parts.append(f"person:{_ident(person)}")
    return "|".join(parts)


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    companies: list[str] = []
    persons: list[str] = []
    for r in rows:
        company, person, problems = _subject(r)
        if problems:
            continue
        if company and _ident(company) not in companies:
            companies.append(_ident(company))
        if person and _ident(person) not in persons:
            persons.append(_ident(person))
    nc, npn = len(companies), len(persons)
    calls: list[PlannedCall] = []
    if config["company_fields"]:
        calls.append(PlannedCall("GET /company", "Company profile for each unique company identifier", nc,
                                 [("company", pricing.round4(pricing.COMPANY_PROFILE * nc))]))
    if config["person_fields"]:
        calls.append(PlannedCall("GET /people", "Person profile for each unique contact identifier", npn,
                                 [("person", pricing.round4(pricing.PERSON_PROFILE * npn))]))
    if config["ai_adoption_fields"]:
        calls.append(PlannedCall("POST /ai-adoption", "AI Adoption for each unique company identifier", nc,
                                 pricing.estimate_ai_adoption(nc, False)))
    ti = config["tech_intel"]
    if ti["enabled"]:
        calls.append(PlannedCall("GET /tech-intel", f"Up to {ti['max_pages']} page(s) of technologies for each "
                                 "unique company identifier", nc * ti["max_pages"],
                                 [("tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * nc))]))
    return calls


# ----- values and comparison ------------------------------------------------------------------

def _pick(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _canon(x: Any) -> str:
    def norm(v: Any) -> Any:
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, dict):
            return {k: norm(w) for k, w in v.items()}
        if isinstance(v, list):
            return [norm(w) for w in v]
        return v
    return json.dumps(norm(x), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _change(source: str, field: str, kind: str, previous: Any, current: Any, note_text: str | None) -> dict[str, Any]:
    return {"source": source, "field": field, "change_type": kind, "previous": previous, "current": current,
            "note": note_text}


def _tech_values(result: dict[str, Any]) -> dict[str, Any]:
    techs: dict[str, Any] = {}
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        key = f"{item.get('filter_slug') or '-'}/{item.get('product_slug') or item.get('product') or '-'}"
        if key in techs:
            continue
        techs[key] = {"product": item.get("product"), "vendor": item.get("vendor"),
                      "category": item.get("category"), "confidence": item.get("confidence"),
                      "nature": item.get("nature")}
    return {"total": result.get("total"), "complete": bool(result.get("complete")), "technologies": techs}


def _compare_fields(source: str, prev: dict[str, Any], cur: dict[str, Any], context_note: Any) -> tuple[list[dict[str, Any]], list[str]]:
    changes: list[dict[str, Any]] = []
    baseline: list[str] = []
    for f, b in cur.items():
        if f not in prev:
            baseline.append(f)
            continue
        a = prev[f]
        if _canon(a) == _canon(b):
            continue
        if _empty(a):
            changes.append(_change(source, f, "newly_observed", a, b, None))
        elif _empty(b):
            changes.append(_change(source, f, "no_longer_observed_in_retrieved_data", a, b, None))
        else:
            changes.append(_change(source, f, "value_changed", a, b, context_note(f) if context_note else None))
    return changes, baseline


def _compare_tech(prev: dict[str, Any], cur: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Returns the changes and how many previous technologies could not be checked."""
    changes: list[dict[str, Any]] = []
    if "total" in prev and _canon(prev.get("total")) != _canon(cur.get("total")):
        a, b = prev.get("total"), cur.get("total")
        kind = "newly_observed" if a is None else ("no_longer_observed_in_retrieved_data" if b is None else "value_changed")
        changes.append(_change("tech_intel", "total", kind, a, b, None))
    pt = prev.get("technologies") or {}
    ct = cur.get("technologies") or {}
    prev_complete = bool(prev.get("complete"))
    both_complete = prev_complete and bool(cur.get("complete"))
    new_note = None if prev_complete else ("The previous technology list was incomplete, so this technology may "
                                           "have been present but not retrieved.")
    for k in sorted(set(ct) - set(pt)):
        changes.append(_change("tech_intel", f"technology:{k}", "newly_observed", None, ct[k], new_note))
    unchecked = 0
    for k in sorted(set(pt) - set(ct)):
        if both_complete:
            changes.append(_change("tech_intel", f"technology:{k}", "no_longer_observed_in_retrieved_data",
                                   pt[k], None, None))
        else:
            unchecked += 1
    for k in sorted(set(pt) & set(ct)):
        for attr in ("confidence", "nature"):
            a, b = (pt[k] or {}).get(attr), (ct[k] or {}).get(attr)
            if _canon(a) != _canon(b):
                kind = "newly_observed" if _empty(a) else ("no_longer_observed_in_retrieved_data" if _empty(b) else "value_changed")
                changes.append(_change("tech_intel", f"technology:{k}.{attr}", kind, a, b, None))
    return changes, unchecked


# ----- run ------------------------------------------------------------------------------------

def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    today = cfg["evaluation_date"] or ctx.today.isoformat()
    previous = cfg["previous_snapshot_data"]
    prev_accounts: dict[str, Any] = (previous or {}).get("accounts") or {}
    prev_date = (previous or {}).get("taken_on")
    coverage = [
        "A difference means the data Firmable returned in this run differs from the saved snapshot. It is not a "
        "confirmed real-world change: Firmable may have refreshed, corrected or newly found the data.",
        "A failed or missing response is never reported as a deletion. The previous values are kept in the new "
        "snapshot.",
        "Volatile fields are never compared: request IDs, usage, AI Adoption as_of, and Tech Intel recency and "
        "last_observed.",
    ]
    if previous is None:
        coverage.append("No previous snapshot was given, so this run records a baseline only and makes no claims "
                        "about change.")
    else:
        coverage.append(f"Compared with the snapshot taken on {prev_date}.")
    coverage.append("This code does not schedule itself. See the README for how to run it on a schedule.")

    keys: dict[str, str] = {}
    for r in ctx.rows:
        company, person, problems = _subject(r)
        keys[r["row_id"]] = "" if problems else _key(company, person)
    keyed = [{**r, "_key": keys[r["row_id"]]} for r in ctx.rows]
    dupes = find_duplicates(keyed, "_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier account; their API responses were reused, not "
                        "requested again.")

    new_accounts: dict[str, Any] = {}
    incomplete_tech: set[str] = set()
    forbidden_tech = [0]

    def record(row_id: str, err: dict[str, Any]) -> dict[str, Any]:
        entry = {"row_id": row_id, **err}
        ctx.errors.append(entry)
        if entry.get("kind") in FATAL_KINDS and ctx.stopped is None:
            ctx.stopped = entry
        return entry

    def fetch(row_id: str, source: str, company: dict[str, str] | None, person: dict[str, str] | None,
              company_id: str | None) -> dict[str, Any]:
        """Returns {result, values, context, error, raw_id, raw_name}."""
        out: dict[str, Any] = {"result": "not_requested", "values": None, "context": None, "error": None,
                               "id": None, "name": None}
        if source == "company" and (not cfg["company_fields"] or not company):
            return out
        if source == "person" and (not cfg["person_fields"] or not person):
            return out
        if source == "ai_adoption" and (not cfg["ai_adoption_fields"] or not company):
            return out
        if source == "tech_intel" and (not cfg["tech_intel"]["enabled"] or not company):
            return out
        if ctx.stopped is not None:
            return {**out, "result": "skipped"}
        try:
            if source == "company":
                c = client.get_company(**company)
                fields = ["id"] + [f for f in cfg["company_fields"] if f != "id"]
                return {**out, "result": "ok", "values": {f: _pick(c, f) for f in fields}, "id": c.get("id"),
                        "name": c.get("name")}
            if source == "person":
                p = client.get_person(**person)
                fields = ["id"] + [f for f in cfg["person_fields"] if f != "id"]
                return {**out, "result": "ok", "values": {f: _pick(p, f) for f in fields}, "id": p.get("id"),
                        "name": p.get("name")}
            if source == "ai_adoption":
                body = {"company_id": company_id} if company_id else (
                    {"company_id": company["id"]} if "id" in company else {"domain": company["fqdn"]})
                resp = client.get_ai_adoption(body)
                comp = resp.get("company")
                err = resp.get("error") if isinstance(resp.get("error"), dict) else None
                if comp is None:
                    if err and err.get("code") not in (None, "not_found"):
                        e = record(row_id, {"kind": "answer_error", "status": 200, "code": err.get("code"),
                                            "endpoint": ENDPOINT[source], "message": str(err.get("message") or err.get("code")),
                                            "charge_uncertain": False, "retry_after": None})
                        return {**out, "result": "error", "error": e}
                    return {**out, "result": "not_found"}
                fields = ["firmable_id"] + [f for f in cfg["ai_adoption_fields"] if f != "firmable_id"]
                return {**out, "result": "ok", "values": {f: _pick(comp, f) for f in fields},
                        "context": {"framework_version": comp.get("framework_version"), "as_of": comp.get("as_of")},
                        "id": comp.get("firmable_id"), "name": comp.get("company_name")}
            ident = {"id": company_id} if company_id else company
            res = client.get_tech_intel(ident, max_pages=cfg["tech_intel"]["max_pages"])
            err_entry = None
            if res.get("error"):
                err_entry = record(row_id, res["error"])
                if res["error"].get("kind") == "forbidden":
                    forbidden_tech[0] += 1
            if res.get("pages_fetched", 0) == 0:
                return {**out, "result": "error", "error": err_entry}
            if not res.get("complete"):
                incomplete_tech.add(_ident(ident))
            return {**out, "result": "ok", "values": _tech_values(res), "error": err_entry, "id": res.get("id")}
        except NotFoundError:
            return {**out, "result": "not_found"}
        except FirmableError as e:
            return {**out, "result": "error", "error": ctx.record_error(row_id, e)}

    def ai_note(prev_ctx: Any, cur_ctx: Any) -> Any:
        pv = (prev_ctx or {}).get("framework_version")
        cv = (cur_ctx or {}).get("framework_version")
        if pv is None or cv is None or pv == cv:
            return None
        text = (f"The AI Adoption framework version changed from {pv} to {cv}. The level may differ because of "
                "the new scoring rules, not because of the company.")
        return lambda f: text if (f == "overall_level" or f.endswith(".level")) else None

    def id_note(source: str) -> Any:
        if source == "person":
            text = ("The contact identifier now returns a different Firmable person ID. The other person fields "
                    "may compare two different people.")
        else:
            text = ("The company identifier now returns a different Firmable company ID. The other "
                    f"{SOURCE_LABEL[source]} fields may compare two different companies.")
        return lambda f: text if f in ("id", "firmable_id") else None

    def one(row: dict[str, str]) -> dict[str, Any]:
        company, person, problems = _subject(row)
        key = keys[row["row_id"]]
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "key": key or None,
                                "duplicate_of": dupes.get(row["row_id"]), "company": None, "person": None,
                                "sources": {}, "changes": [], "baseline_fields": [], "errors": []}
        if problems:
            return {**base, "status": "invalid_input", "status_reason": " ".join(problems)}
        prev_acc = prev_accounts.get(key)
        prev_sources = (prev_acc or {}).get("sources") or {}
        fetched: dict[str, dict[str, Any]] = {}
        company_id = None
        for source in SOURCES:
            fetched[source] = fetch(row["row_id"], source, company, person, company_id)
            if source == "company" and fetched[source]["result"] == "ok":
                company_id = fetched[source]["id"]
        if fetched["company"]["result"] == "ok":
            base["company"] = {"id": fetched["company"]["id"], "name": fetched["company"]["name"]}
        elif fetched["ai_adoption"]["result"] == "ok":
            base["company"] = {"id": fetched["ai_adoption"]["id"], "name": fetched["ai_adoption"]["name"]}
        if fetched["person"]["result"] == "ok":
            base["person"] = {"id": fetched["person"]["id"], "name": fetched["person"]["name"]}

        sources_out: dict[str, Any] = {}
        changes: list[dict[str, Any]] = []
        baseline_fields: list[str] = []
        new_sources: dict[str, Any] = {}
        unchecked_tech = 0
        for source in SOURCES:
            cur = fetched[source]
            prev_entry = prev_sources.get(source)
            prev_vals = prev_entry.get("values") if isinstance(prev_entry, dict) else None
            result = cur["result"]
            # The new snapshot keeps the last values that were retrieved.
            if result == "ok":
                entry = {"values": cur["values"], "observed_on": today, "last_result": "ok", "last_attempt_on": today}
                if cur["context"] is not None:
                    entry["context"] = cur["context"]
            elif isinstance(prev_entry, dict) and result != "not_requested":
                entry = {**copy.deepcopy(prev_entry), "last_result": result, "last_attempt_on": today}
            elif isinstance(prev_entry, dict):
                entry = copy.deepcopy(prev_entry)
            else:
                entry = {"values": None, "observed_on": None, "last_result": result,
                         "last_attempt_on": today if result != "not_requested" else None}
            new_sources[source] = entry

            if result == "not_requested":
                comparison = "not_requested"
            elif prev_acc is None:
                comparison = "baseline"
            elif result in ("error", "skipped"):
                comparison = "not_compared"
                msg = ("The run stopped before this request, so nothing was compared."
                       if result == "skipped" else "The request failed in this run, so nothing was compared.")
                changes.append(_change(source, "*", "not_compared_fetch_failed", None, None,
                                       msg + " The previous values are kept in the new snapshot."))
            elif result == "not_found":
                if prev_vals is None:
                    comparison = "compared"
                else:
                    comparison = "not_compared"
                    changes.append(_change(source, "*", "not_compared_not_found", None, None,
                                           "Firmable returned no data for this subject in this run. This is not "
                                           "reported as a deletion; the previous values are kept in the new snapshot."))
            elif prev_vals is None and (not isinstance(prev_entry, dict)
                                        or prev_entry.get("last_result") != "not_found"):
                comparison = "baseline"
            else:
                comparison = "compared"
                if source == "tech_intel":
                    prev_t = prev_vals if prev_vals is not None else {"total": None, "complete": True,
                                                                       "technologies": {}}
                    found, unchecked_tech = _compare_tech(prev_t, cur["values"])
                    changes.extend(found)
                else:
                    prev_f = prev_vals if prev_vals is not None else {f: None for f in cur["values"]}
                    if source == "ai_adoption":
                        n_ai = ai_note((prev_entry or {}).get("context"), cur["context"])
                        n_id = id_note(source)
                        note_fn = (lambda f, a=n_ai, b=n_id: b(f) or (a(f) if a else None))
                    else:
                        note_fn = id_note(source)
                    found, base_f = _compare_fields(source, prev_f, cur["values"], note_fn)
                    changes.extend(found)
                    baseline_fields.extend(f"{source}.{f}" for f in base_f)
            sources_out[source] = {"result": result, "comparison": comparison, "error": cur["error"]}

        requested = [s for s in SOURCES if fetched[s]["result"] != "not_requested"]
        if not requested:
            return {**base, "status": "not_monitored", "sources": sources_out,
                    "status_reason": "No enabled source applies to this row's identifiers, so nothing was fetched."}
        new_accounts[key] = {"key": key, "identifiers": {"company": company, "person": person},
                             "sources": new_sources}
        failed = [s for s in requested if fetched[s]["result"] in ("error", "skipped")]
        diffs = [c for c in changes if c["change_type"] in DIFFERENCE_TYPES]
        not_compared = [s for s in SOURCES if sources_out[s]["comparison"] == "not_compared"]
        extra = ""
        if unchecked_tech:
            extra = (f" {unchecked_tech} technology(ies) from the previous snapshot were not in the retrieved pages; "
                     "the list is incomplete, so they are not reported as no longer observed.")
        if len(failed) == len(requested):
            status = "fetch_failed"
            reason = ("Every requested source failed in this run, so no baseline values were recorded for this "
                      "account." if prev_acc is None else
                      "Every requested source failed in this run, so nothing was compared. The previous values are "
                      "kept in the new snapshot.")
        elif prev_acc is None:
            status = "baseline_created"
            reason = ("No previous snapshot was given. This run records a baseline and makes no claims about change."
                      if previous is None else
                      "This account is not in the previous snapshot. This run records a baseline for it and makes "
                      "no claims about change.")
            if failed:
                reason += f" {len(failed)} source(s) failed and have no baseline yet."
        elif diffs:
            status = "changes_detected"
            reason = (f"{len(diffs)} difference(s) from the snapshot taken on {prev_date}. A difference is a change "
                      "in the retrieved data, not a confirmed real-world change.")
            if not_compared:
                reason += f" {len(not_compared)} source(s) could not be compared."
        elif not_compared:
            status = "partially_compared"
            reason = f"No differences in the sources compared. {len(not_compared)} source(s) could not be compared."
        else:
            status = "no_changes_detected"
            reason = f"No differences from the snapshot taken on {prev_date} in the monitored fields."
        new_baseline = [s for s in requested if sources_out[s]["comparison"] == "baseline"]
        if new_baseline and prev_acc is not None:
            reason += f" {len(new_baseline)} source(s) had no previous values and now have a baseline."
        if baseline_fields and prev_acc is not None:
            reason += f" {len(baseline_fields)} field(s) were not in the previous snapshot and now have a baseline."
        reason += extra
        errors = [f"{SOURCE_LABEL[s]}: {(sources_out[s]['error'] or {}).get('message') or 'not sent because the run stopped'}"
                  for s in failed]
        return {**base, "status": status, "status_reason": reason, "sources": sources_out, "changes": changes,
                "baseline_fields": baseline_fields, "errors": errors}

    rows = ctx.run_rows(ctx.rows, one)

    snapshot_accounts: dict[str, Any] = copy.deepcopy(prev_accounts)
    snapshot_accounts.update(new_accounts)
    carried = sorted(k for k in prev_accounts if k not in new_accounts)
    not_in_input = [k for k in carried if k not in set(keys.values())]
    if not_in_input:
        coverage.append(f"{len(not_in_input)} account(s) in the previous snapshot are not in this input; they were "
                        "copied to the new snapshot unchanged.")
    if incomplete_tech:
        coverage.append(f"{len(incomplete_tech)} Tech Intel list(s) are incomplete because of the page limit or an "
                        "error. Technologies missing from an incomplete list are not reported as no longer observed.")
    if forbidden_tech[0]:
        coverage.append(f"Tech Intel returned 403 for {forbidden_tech[0]} request(s). Tech Intel must be on your plan.")
    snapshot = {"snapshot_format": SNAPSHOT_FORMAT, "taken_on": today,
                "monitored": {"company_fields": cfg["company_fields"], "person_fields": cfg["person_fields"],
                              "ai_adoption_fields": cfg["ai_adoption_fields"], "tech_intel": cfg["tech_intel"]},
                "accounts": snapshot_accounts}

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    change_counts = {t: 0 for t in CHANGE_TYPES}
    for r in rows:
        for c in r.get("changes") or []:
            change_counts[c["change_type"]] += 1
    summary = {"status_counts": counts, "change_counts": change_counts, "change_type_meanings": CHANGE_MEANING,
               "previous_snapshot_taken_on": prev_date, "snapshot_taken_on": today, "snapshot": snapshot}
    return {"rows": rows, "summary": summary, "coverage": coverage}


# ----- output ---------------------------------------------------------------------------------

CSV_COLUMNS = ["row_id", "key", "duplicate_of", "status", "status_reason", "company_id", "company_name",
               "person_id", "person_name", "differences", "not_compared", "baseline_fields", "changes"]


def _short(v: Any) -> str:
    if v is None:
        return "-"
    s = v if isinstance(v, str) else _canon(v)
    return s if len(s) <= 80 else s[:77] + "..."


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        changes = r.get("changes") or []
        company = r.get("company") or {}
        person = r.get("person") or {}
        out.append({
            "row_id": r["row_id"], "key": r.get("key"), "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_id": company.get("id"), "company_name": company.get("name"),
            "person_id": person.get("id"), "person_name": person.get("name"),
            "differences": sum(1 for c in changes if c["change_type"] in DIFFERENCE_TYPES),
            "not_compared": sum(1 for c in changes if c["change_type"] not in DIFFERENCE_TYPES),
            "baseline_fields": r.get("baseline_fields") or [],
            "changes": [f"{c['source']}.{c['field']}={c['change_type']}({_short(c['previous'])} -> {_short(c['current'])})"
                        for c in changes],
        })
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    s = result["summary"]
    counts = s.get("status_counts", {})
    meanings = s.get("change_type_meanings") or CHANGE_MEANING
    body = ("<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                             for k, v in counts.items()) + "</p>"
            + kv([("Previous snapshot taken on", s.get("previous_snapshot_taken_on") or "none (baseline run)"),
                  ("New snapshot taken on", s.get("snapshot_taken_on"))])
            + note("A difference is a difference in the data Firmable returned. It is <strong>not a confirmed "
                   "real-world change</strong>. A failed or missing response is never shown as a deletion.", "unknown")
            + table(["Change type", "Count", "Meaning"],
                    [[t, (s.get("change_counts") or {}).get(t, 0), meanings.get(t)] for t in CHANGE_TYPES]))
    report.add("Result", "Change summary", body)
    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = ((r.get("company") or {}).get("name") or (r.get("person") or {}).get("name") or r.get("key")
                 or r["row_id"])
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        left += kv([("Account key", r.get("key")), ("Company ID", (r.get("company") or {}).get("id")),
                    ("Person ID", (r.get("person") or {}).get("id"))])
        srcs = r.get("sources") or {}
        if srcs:
            left += table(["Source", "This run", "Comparison"],
                          [[SOURCE_LABEL[k], v["result"].replace("_", " "), v["comparison"].replace("_", " ")]
                           for k, v in srcs.items()])
        if r.get("duplicate_of"):
            left += note(f"Same account as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        changes = r.get("changes") or []
        diffs = [c for c in changes if c["change_type"] in DIFFERENCE_TYPES]
        gaps = [c for c in changes if c["change_type"] not in DIFFERENCE_TYPES]
        if diffs:
            right += "<h3>Differences in retrieved data</h3>" + table(
                ["Source", "Field", "Change", "Previous", "Current"],
                [[SOURCE_LABEL[c["source"]], c["field"],
                  Raw(badge(c["change_type"].replace("_", " "), CHANGE_TONE.get(c["change_type"], "neutral"))),
                  _short(c["previous"]), _short(c["current"])] for c in diffs])
            for c in diffs:
                if c.get("note"):
                    right += note(f"{esc(c['field'])}: {esc(c['note'])}", "info")
        if gaps:
            right += "<h3>Not compared</h3>" + bullet_list(
                [f"{SOURCE_LABEL[c['source']]}: {c['note']}" for c in gaps])
        if r.get("baseline_fields") and status != "baseline_created":
            right += "<h3>New baseline fields</h3>" + bullet_list(r["baseline_fields"])
        if changes:
            right += details_json("Changes as JSON", changes)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No differences to show for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its differences", "".join(cards))
    report.add("Next run", "Snapshot for the next run",
               "<p>The new snapshot is in <code>results.json</code> under <code>summary.snapshot</code>. The "
               "command line also writes it to <code>snapshot.json</code>. Set <code>previous_snapshot</code> in "
               "the configuration to that file for the next run.</p>"
               + note("This code does not schedule itself. To run it on a schedule, use cron, a CI schedule or "
                      "another scheduler you control; see the README.", "info"))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
