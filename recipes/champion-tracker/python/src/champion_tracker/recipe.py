"""Champion tracker: finds people at target accounts who used to work at companies you name.

Workflow for each account row:
1. Resolve the company with GET /company, by fqdn for a domain or by id for a company ID.
2. Search people at the company (POST /people/search) with the configured filters, up to search.max_people.
3. Ask previously_worked_at (POST /decisions) once for each unique person ID from the search, with the
   configured past_companies and include_current.
4. Apply the rules below.
5. Fetch the person profile (GET /people) for champions only, when fetch_contact_details is true.
6. Fill the configured opener templates for champions from API facts and input only.

Person results: champion (yes), not_alumni (no), unknown, person_not_found, error, not_asked.

Account status, first that applies after the company and search succeed:
- no_people_found: people search returned nobody this recipe could check;
- champions_found: at least one person is a champion;
- incomplete: no champion, and at least one person has an error or was not asked;
- needs_review: no champion, and at least one person answered unknown or was not found by Decisions;
- no_champions_found: every person checked answered no.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts, contact_details, person_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, card, details_json, esc, kv, note, table

NAME = "champion-tracker"
TITLE = "Champion tracker"
DESCRIPTION = ("Finds people at target accounts who previously worked at your customers or at other companies you "
               "name, using Firmable people search and the previously_worked_at decision tool. A past employer is a "
               "route to a warm introduction, not evidence of buying intent.")

TOOL = "previously_worked_at"
COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
PLACEHOLDERS = ["account_name", "first_name", "past_employer", "title"]
ACCOUNT_COLUMNS = ["company_id", "domain"]
DETAIL_KEYS = ["employer", "years_there", "title", "past_employers_checked"]

STATUS_TONE = {"champions_found": "good", "champion": "good", "no_champions_found": "bad", "not_alumni": "bad",
               "needs_review": "review", "unknown": "unknown", "incomplete": "error", "no_people_found": "unknown",
               "company_not_found": "unknown", "person_not_found": "unknown", "invalid_input": "error",
               "error": "error", "not_asked": "neutral", "not_processed": "unknown"}
_TOP_KEYS = {"past_companies", "include_current", "search", "with_reasoning", "fetch_contact_details",
             "opener_templates"}
_SEARCH_KEYS = {"selected_country", "position", "seniority", "department", "max_people"}
_PERSON_ID = re.compile(r"^fp\d{12}$")
_COMPANY_ID = re.compile(r"^f\d{12}$")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _whole(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    companies = raw.get("past_companies")
    if companies is None:
        raise ConfigError("past_companies is required: a list of 1 to 50 company names or Firmable company IDs.")
    if not isinstance(companies, list) or not 1 <= len(companies) <= 50:
        raise ConfigError("past_companies must be a list of 1 to 50 company names or Firmable company IDs.")
    seen: list[str] = []
    for i, c in enumerate(companies, start=1):
        if not isinstance(c, str) or not c.strip() or len(c) > 200:
            raise ConfigError(f"past_companies[{i}] must be text of 1 to 200 characters.")
        if c.strip().lower() in seen:
            raise ConfigError(f"past_companies[{i}] {c!r} is listed twice.")
        seen.append(c.strip().lower())
    defaults = {"include_current": False, "with_reasoning": False, "fetch_contact_details": False}
    for key, default in defaults.items():
        if not isinstance(raw.get(key, default), bool):
            raise ConfigError(f"{key} must be true or false.")
    search = raw.get("search")
    if search is None:
        search = {}
    if not isinstance(search, dict):
        raise ConfigError("search must be an object.")
    unknown_search = sorted(set(search) - _SEARCH_KEYS)
    if unknown_search:
        raise ConfigError(f"Unknown search key(s): {', '.join(unknown_search)}")
    country = search.get("selected_country", "AU")
    if country not in COUNTRIES:
        raise ConfigError(f"search.selected_country must be one of: {', '.join(COUNTRIES)}.")
    position = search.get("position")
    if position is not None and (not isinstance(position, str) or not position.strip() or len(position) > 200):
        raise ConfigError("search.position must be text of 1 to 200 characters.")
    seniority = search.get("seniority")
    if seniority is not None and seniority not in [str(i) for i in range(1, 8)]:
        raise ConfigError("search.seniority must be one seniority id from 1 to 7, written as text.")
    department = search.get("department")
    if department is not None and department not in [str(i) for i in range(1, 18)]:
        raise ConfigError("search.department must be one department id from 1 to 17, written as text.")
    max_people = search.get("max_people", 25)
    if not _whole(max_people, 1, 100):
        raise ConfigError("search.max_people must be a whole number from 1 to 100.")
    templates = raw.get("opener_templates")
    if templates is not None:
        if not isinstance(templates, list) or not 1 <= len(templates) <= 10:
            raise ConfigError("opener_templates must be a list of 1 to 10 text templates.")
        for i, t in enumerate(templates, start=1):
            if not isinstance(t, str) or not t.strip() or len(t) > 500:
                raise ConfigError(f"opener_templates[{i}] must be text of 1 to 500 characters.")
            bad = sorted({m for m in _PLACEHOLDER.findall(t) if m not in PLACEHOLDERS})
            if bad:
                names = ", ".join("{" + b + "}" for b in bad)
                allowed = ", ".join("{" + p + "}" for p in PLACEHOLDERS)
                raise ConfigError(f"opener_templates[{i}] uses unknown placeholder(s): {names}. Allowed: {allowed}.")
    return {"past_companies": list(companies), "include_current": raw.get("include_current", False),
            "with_reasoning": raw.get("with_reasoning", False),
            "fetch_contact_details": raw.get("fetch_contact_details", False),
            "opener_templates": list(templates) if templates is not None else [],
            "search": {"selected_country": country, "position": position, "seniority": seniority,
                       "department": department, "max_people": max_people}}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=ACCOUNT_COLUMNS)


def parse_row(row: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """Returns the checked account identifier of one row, or a reason the row cannot be used."""
    cid = row.get("company_id")
    if cid:
        if not _COMPANY_ID.match(cid):
            return None, f"company_id {cid!r} is not a Firmable company ID (f followed by 12 digits)."
        return {"company_id": cid, "domain": None, "key": f"company_id={cid}"}, None
    domain = normalise_domain(row.get("domain"))
    if not domain:
        return None, f"domain {row.get('domain')!r} is not a valid domain."
    return {"company_id": None, "domain": domain, "key": f"domain={domain}"}, None


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    accounts: list[str] = []
    for r in rows:
        p, _ = parse_row(r)
        if p is not None and p["key"] not in accounts:
            accounts.append(p["key"])
    max_people = config["search"]["max_people"]
    people = len(accounts) * max_people
    calls = [PlannedCall("GET /decisions", "Read the decision tool list and check previously_worked_at", 1, []),
             PlannedCall("GET /company", "Resolve each unique account by domain or company ID", len(accounts),
                         [("company", pricing.round4(pricing.COMPANY_PROFILE * len(accounts)))]),
             PlannedCall("POST /people/search", "Search people at each unique account", len(accounts), []),
             PlannedCall("POST /decisions", f"previously_worked_at for up to {max_people} people per account",
                         people, pricing.estimate_decision(people, config["with_reasoning"]))]
    if config["fetch_contact_details"]:
        calls.append(PlannedCall("GET /people", "Person profile for each champion, assuming everyone checked is a "
                                 "champion", people, [("person", pricing.round4(pricing.PERSON_PROFILE * people))]))
    return calls


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def fill_templates(templates: list[str], values: dict[str, str | None]) -> tuple[list[dict[str, Any]],
                                                                                 list[dict[str, Any]]]:
    """Fills each template; a template with any empty placeholder is skipped and listed with what was missing."""
    filled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for i, t in enumerate(templates, start=1):
        missing: list[str] = []
        for name in _PLACEHOLDER.findall(t):
            if values.get(name) is None and name not in missing:
                missing.append(name)
        if missing:
            skipped.append({"template": i, "missing": missing})
            continue
        filled.append({"template": i, "text": _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), t)})
    return filled, skipped


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    srch = cfg["search"]
    coverage = [
        "An unknown answer means Firmable does not hold the facts needed to answer. It is never counted as a no "
        "or as a yes.",
        "A past employer is a route to a warm introduction. It does not show buying intent.",
        f"Past companies checked ({len(cfg['past_companies'])}): {', '.join(cfg['past_companies'])}. "
        "previously_worked_at matches the past employer names Firmable holds, so a past employer recorded under "
        "another name may not match.",
        ("include_current is true, so a person's current employer and earlier roles there also count." if
         cfg["include_current"] else
         "include_current is false, so a person's current employer does not count as a past employer."),
        f"People search has no past-employer filter, so only the first {srch['max_people']} people per account are "
        "checked. A champion who was not checked is not observed in retrieved data. That does not mean none exist.",
        "Past employer details (employer, years_there, title, past_employers_checked) are shown as returned. They "
        "are not corrected.",
        "This recipe sends no outreach.",
    ]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    inputs = {"companies": cfg["past_companies"], "include_current": cfg["include_current"]}
    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no people were checked.")
    problems = check_tools(catalogue, [{"tool": TOOL, "inputs": inputs}], "person")
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("previously_worked_at is missing or no longer accepts the inputs this recipe sends.")
    if uses_model(catalogue, TOOL):
        coverage.append(f"{TOOL} uses a decision model inside Firmable's service. This code applies only the "
                        "rules in the README.")
    filters = [f"country {srch['selected_country']}"]
    for k in ("position", "seniority", "department"):
        if srch[k]:
            filters.append(f"{k} {srch[k]}")
    coverage.append(f"People search filters: {', '.join(filters)}.")
    if not cfg["fetch_contact_details"]:
        coverage.append("fetch_contact_details is false, so no contact details were retrieved and {first_name} "
                        "has no value in opener templates.")
    if cfg["opener_templates"]:
        coverage.append("Suggested openers are predefined templates filled from API facts or input. They are not "
                        "API facts. A template with an empty placeholder is skipped and listed.")

    parsed = [parse_row(r)[0] for r in ctx.rows]
    for r, p in zip(ctx.rows, parsed):
        r["_key"] = p["key"] if p else ""
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier account; identical requests were answered from "
                        "memory, not sent again.")
    empty_searches = [0]

    def ask(pid: str, row_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": TOOL, "answer": None, "confidence": None, "details": None, "reasoning": None,
                               "alumni": {k: None for k in DETAIL_KEYS}, "error": None}
        if ctx.stopped is not None:
            return {**out, "result": "not_asked", "status_reason": "Not asked, because the run stopped."}
        try:
            resp = client.make_decision(TOOL, {"person_id": pid, **inputs}, with_reasoning=cfg["with_reasoning"])
        except NotFoundError as e:
            if e.code == "person_not_found":
                return {**out, "result": "person_not_found",
                        "status_reason": "Decisions found no Firmable person for this person ID, so the question has "
                                         "no answer."}
            return {**out, "result": "error", "error": ctx.record_error(row_id, e),
                    "status_reason": "The question has no answer because of an error."}
        except FirmableError as e:
            return {**out, "result": "error", "error": ctx.record_error(row_id, e),
                    "status_reason": "The question has no answer because of an error."}
        ans = answer_from(resp)
        details = ans["details"] if isinstance(ans["details"], dict) else {}
        out = {**out, **ans, "alumni": {k: details.get(k) for k in DETAIL_KEYS}}
        employer = _text(details.get("employer"))
        if ans["answer"] == "yes":
            reason = f"{TOOL} answered yes. " + (f"Past employer returned: {employer}." if employer else
                                                 "No past employer was returned.")
            return {**out, "result": "champion", "status_reason": reason}
        if ans["answer"] == "no":
            return {**out, "result": "not_alumni",
                    "status_reason": f"{TOOL} answered no for the past companies checked."}
        if ans["answer"] == "unknown":
            return {**out, "result": "unknown",
                    "status_reason": f"{TOOL} answered unknown. Firmable does not hold the facts needed to answer; "
                                     "it is not a no."}
        return {**out, "result": "error", "error": ans["answer_error"],
                "status_reason": "The answer is not yes, no or unknown, so it is treated as an error."}

    def notes(q: dict[str, Any], company_name: str | None, search_company: str | None) -> list[str]:
        out: list[str] = []
        employer = _text(q["alumni"]["employer"])
        if q["result"] == "champion" and employer is None:
            out.append(f"{TOOL} answered yes but returned no past employer in details.employer.")
        if q["result"] == "champion" and employer and not cfg["include_current"] and company_name \
                and employer.lower() == company_name.strip().lower():
            out.append(f"{TOOL} returned the account itself ({employer}) as the past employer although "
                       "include_current is false. The answer is shown as returned.")
        if q["result"] == "not_alumni" and employer:
            out.append(f"{TOOL} answered no but returned a past employer ({employer}) in details.employer. Both are "
                       "shown as returned.")
        if company_name and search_company and search_company.lower() != company_name.strip().lower():
            out.append(f"People search returned company name {search_company}, which differs from the account's "
                       f"company name {company_name.strip()}.")
        return out

    def contact_info(pid: str, row_id: str) -> tuple[dict[str, Any] | None, str | None]:
        if not cfg["fetch_contact_details"]:
            return None, "Not retrieved, because fetch_contact_details is false."
        if ctx.stopped is not None:
            return None, "Not retrieved, because the run stopped."
        try:
            person = client.get_person(id=pid)
        except NotFoundError:
            return None, "Firmable has no person profile for this person ID, so no contact details were retrieved."
        except FirmableError as e:
            err = ctx.record_error(row_id, e)
            return None, f"Contact details could not be retrieved: {err['message']}"
        return {"profile": person_facts(person), "first_name": _text(person.get("first_name")),
                "contact": contact_details(person)}, None

    def one(row: dict[str, str]) -> dict[str, Any]:
        p, problem = parse_row(row)
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "account_name": row.get("account_name") or None,
                                "duplicate_of": dupes.get(row["row_id"]), "company": None, "company_id": None,
                                "search": None, "people": [], "champion_count": 0}
        if p is None:
            return {**base, "status": "invalid_input", "status_reason": problem}
        try:
            company = client.get_company(fqdn=p["domain"]) if p["domain"] else client.get_company(id=p["company_id"])
        except NotFoundError:
            what = "domain" if p["domain"] else "company ID"
            return {**base, "status": "company_not_found",
                    "status_reason": f"Firmable has no company profile for this {what}, so no people were searched."}
        base["company"] = company_facts(company)
        company_id = company.get("id") if isinstance(company.get("id"), str) else p["company_id"]
        base["company_id"] = company_id
        company_name = _text(company.get("name"))
        account_name = _text(row.get("account_name")) or company_name
        body: dict[str, Any] = {"companyId": company_id, "selectedCountry": srch["selected_country"], "from": "0",
                                "size": str(srch["max_people"])}
        for k in ("position", "seniority", "department"):
            if srch[k]:
                body[k] = srch[k]
        resp = client.search_people(body)
        empty = resp.get("success") is False
        if empty:
            empty_searches[0] += 1
        records = [r for r in resp.get("records") or [] if isinstance(r, dict)]
        total = resp.get("total")
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        not_checked = total - len(records) if total is not None and total > len(records) else 0
        skipped: list[dict[str, Any]] = []
        people: list[dict[str, Any]] = []
        seen: list[str] = []
        for rec in records:
            pid = rec.get("person_id") or rec.get("id")
            if not isinstance(pid, str) or not _PERSON_ID.match(pid):
                skipped.append({"id": pid, "name": rec.get("name"),
                                "reason": "The person ID from people search is not in the fp format that Decisions "
                                          "accepts."})
                continue
            if pid in seen:
                continue
            seen.append(pid)
            q = ask(pid, row["row_id"])
            info, info_note = (None, None)
            if q["result"] == "champion":
                info, info_note = contact_info(pid, row["row_id"])
            profile = info["profile"] if info else None
            position = _text(rec.get("position")) or _text((profile or {}).get("position"))
            person = {"person_id": pid, "name": rec.get("name") or (profile or {}).get("name"),
                      "position": position, "search_company_name": rec.get("company_name"),
                      "status": q["result"], **{k: v for k, v in q.items() if k != "result"},
                      "notes": notes(q, company_name, _text(rec.get("company_name"))),
                      "profile": profile, "contact": info["contact"] if info else None, "contact_note": info_note,
                      "suggested_openers": [], "skipped_openers": []}
            if q["result"] == "champion" and cfg["opener_templates"]:
                values = {"first_name": info["first_name"] if info else None,
                          "past_employer": _text(q["alumni"]["employer"]), "account_name": account_name,
                          "title": position}
                person["suggested_openers"], person["skipped_openers"] = fill_templates(cfg["opener_templates"],
                                                                                        values)
            people.append(person)
        base["search"] = {"request": body, "total": total, "retrieved": len(records), "not_checked": not_checked,
                          "skipped": skipped, "no_match_response": empty}
        base["people"] = people
        statuses = [x["status"] for x in people]
        n = len(people)
        champions = statuses.count("champion")
        base["champion_count"] = champions
        unresolved = statuses.count("error") + statuses.count("not_asked")
        unclear = statuses.count("unknown") + statuses.count("person_not_found")
        if not people:
            status, reason = "no_people_found", ("People search returned no people for this company with the "
                                                 "configured filters." if not records else
                                                 "People search returned no person this recipe could check.")
        elif champions:
            status, reason = "champions_found", (f"{champions} of {n} person(s) checked previously worked at a listed "
                                                 "company.")
        elif unresolved:
            status, reason = "incomplete", (f"No champion was observed among the {n} person(s) checked, and "
                                            f"{unresolved} of {n} have no answer because of an error or because the "
                                            "run stopped.")
        elif unclear:
            status, reason = "needs_review", (f"No champion was observed among the {n} person(s) checked, and "
                                              f"{unclear} of {n} answered unknown or were not found by Decisions.")
        else:
            status, reason = "no_champions_found", (f"No champion was observed among the {n} person(s) checked. Every "
                                                    "person checked answered no.")
        if not_checked:
            reason += (f" {not_checked} more matching person(s) were not checked, because search.max_people is "
                       f"{srch['max_people']}.")
        if skipped:
            reason += (f" {len(skipped)} search record(s) were skipped, because their person ID is not in the format "
                       "Decisions accepts.")
        return {**base, "status": status, "status_reason": reason}

    rows = ctx.run_rows(ctx.rows, one)
    if empty_searches[0]:
        coverage.append(f"People search answered success false with no records list for {empty_searches[0]} "
                        "account(s). The API docs describe this as the response when nobody matches; it is counted as "
                        "zero people.")
    counts: dict[str, int] = {}
    person_counts: dict[str, int] = {}
    champion_ids: list[str] = []
    checked = 0
    not_checked = 0
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r.get("duplicate_of"):
            continue
        not_checked += ((r.get("search") or {}).get("not_checked") or 0)
        for person in r.get("people") or []:
            checked += 1
            person_counts[person["status"]] = person_counts.get(person["status"], 0) + 1
            if person["status"] == "champion" and person["person_id"] not in champion_ids:
                champion_ids.append(person["person_id"])
    return {"rows": rows, "summary": {"status_counts": counts, "person_status_counts": person_counts,
                                      "people_checked": checked, "people_not_checked": not_checked,
                                      "unique_champions": len(champion_ids)},
            "coverage": coverage}


CSV_COLUMNS = ["row_id", "account_name", "duplicate_of", "row_status", "row_status_reason", "company_id",
               "company_name", "people_matching", "people_not_checked", "person_id", "person_name", "position",
               "person_status", "person_status_reason", "answer", "confidence", "past_employer", "years_there",
               "past_title", "past_employers_checked", "notes", "work_emails", "phones", "contact_note",
               "suggested_openers", "skipped_openers"]


def _dnd(v: Any) -> str:
    return "yes" if v is True else "no" if v is False else "unknown"


def _skipped_text(s: dict[str, Any]) -> str:
    return f"template {s['template']} missing " + ", ".join(s["missing"])


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        search = r.get("search") or {}
        head = {"row_id": r["row_id"], "account_name": r.get("account_name"), "duplicate_of": r.get("duplicate_of"),
                "row_status": r["status"],
                "row_status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
                "company_id": r.get("company_id"), "company_name": company.get("name"),
                "people_matching": search.get("total"), "people_not_checked": search.get("not_checked")}
        people = r.get("people") or []
        if not people:
            out.append(head)
            continue
        for person in people:
            contact = person.get("contact") or {}
            alumni = person.get("alumni") or {}
            out.append({**head, "person_id": person["person_id"], "person_name": person.get("name"),
                        "position": person.get("position"), "person_status": person["status"],
                        "person_status_reason": person["status_reason"], "answer": person.get("answer"),
                        "confidence": person.get("confidence"), "past_employer": alumni.get("employer"),
                        "years_there": alumni.get("years_there"), "past_title": alumni.get("title"),
                        "past_employers_checked": alumni.get("past_employers_checked"), "notes": person["notes"],
                        "work_emails": contact.get("work_emails") or [],
                        "phones": [f"{ph['value']} (do not call flag: {_dnd(ph.get('is_dnd'))})"
                                   for ph in contact.get("phones") or []],
                        "contact_note": person.get("contact_note"),
                        "suggested_openers": [o["text"] for o in person["suggested_openers"]],
                        "skipped_openers": [_skipped_text(s) for s in person["skipped_openers"]]})
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    summary = result["summary"]
    counts = summary.get("status_counts", {})
    report.add("Result", "Champion summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + kv([("People checked", summary.get("people_checked")),
                     ("Matching people not checked", summary.get("people_not_checked")),
                     ("Unique champions", summary.get("unique_champions"))])
               + note("A past employer is a route to a warm introduction. It does not show buying intent.", "info")
               + note("A missing champion means none was observed among the people checked. It does not mean none "
                      "exist. Unknown is never counted as a no.", "unknown"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        inp = r["input"]
        company = r.get("company") or {}
        title = r.get("account_name") or company.get("name") or inp.get("domain") or inp.get("company_id") \
            or r["row_id"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        left += kv([("Firmable company ID", r.get("company_id")), ("Company name", company.get("name")),
                    ("Domain", company.get("fqdn")), ("Head office country", company.get("hq_country"))])
        search = r.get("search")
        if search:
            left += kv([("People matching the search", search.get("total")), ("Retrieved", search.get("retrieved")),
                        ("Matching people not checked", search.get("not_checked"))])
            for s in search.get("skipped") or []:
                left += note(f"Skipped {esc(s.get('name'))} ({esc(s.get('id'))}): {esc(s.get('reason'))}", "review")
        if r.get("duplicate_of"):
            left += note(f"Same account as row {esc(r['duplicate_of'])}; identical requests were reused.", "info")
        right = ""
        for person in r.get("people") or []:
            alumni = person.get("alumni") or {}
            right += (f"<h3>{esc(person.get('name') or person['person_id'])} "
                      f"{badge(person['status'].replace('_', ' '), STATUS_TONE.get(person['status'], 'neutral'))}</h3>"
                      f"<p>{esc(person['status_reason'])}</p>")
            right += kv([("Firmable person ID", person["person_id"]), ("Current position", person.get("position")),
                         ("Answer", person.get("answer")), ("Confidence", person.get("confidence"))])
            if person.get("answer") in ("yes", "no"):
                right += ("<p class=\"muted\">Past employer details as returned by previously_worked_at:</p>"
                          + table(["Past employer", "Years there", "Title there", "Past employers checked"],
                                  [[alumni.get("employer"), alumni.get("years_there"), alumni.get("title"),
                                    alumni.get("past_employers_checked")]]))
            rs = person.get("reasoning") or {}
            if rs.get("reason"):
                right += (f"<p><strong>Reason:</strong> {esc(rs.get('reason'))}</p>"
                          f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                          f"{esc(rs.get('last_refreshed'))}.</p>")
            if person.get("error"):
                err = person["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                right += note(esc(msg if msg is not None else "No valid answer was returned."), "error")
            for c in person.get("notes") or []:
                right += note(esc(c), "review")
            contact = person.get("contact")
            if contact:
                right += kv([("Work emails", ", ".join(contact.get("work_emails") or []) or None),
                             ("Phones", ", ".join(f"{ph['value']} (do not call flag: {_dnd(ph.get('is_dnd'))})"
                                                  for ph in contact.get("phones") or []) or None)])
            if person.get("contact_note"):
                right += f"<p class=\"muted\">{esc(person['contact_note'])}</p>"
            if person.get("suggested_openers") or person.get("skipped_openers"):
                right += ('<div class="template"><h3>Suggested openers</h3><p class="muted">Predefined templates '
                          "filled from API facts or input. They are not API facts.</p><ul>"
                          + "".join(f"<li>{esc(o['text'])}</li>" for o in person["suggested_openers"]) + "</ul>"
                          + "".join(f"<p class=\"muted\">Skipped {esc(_skipped_text(s))}.</p>"
                                    for s in person["skipped_openers"])
                          + "</div>")
            right += details_json("Answer as JSON", {k: person.get(k) for k in
                                                     ("answer", "confidence", "details", "reasoning", "answered_at",
                                                      "credits", "entity_id", "request_id", "error")})
        cards.append(card(str(title), badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No people were checked for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Rows", "Each account with the people checked", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
