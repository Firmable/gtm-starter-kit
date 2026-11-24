"""New role outreach: finds contacts whose role change falls inside a configured outreach window.

Workflow for each row:
1. A row with person_id, work_email or linkedin_url is a contact. Any other row is an account and needs
   company_id or domain.
2. Account: resolve a domain to a company ID (GET /company), then search people at the company
   (POST /people/search) with the configured filters, up to search.max_people people.
3. Ask each configured trigger for each person (POST /decisions): is_new_to_role, is_new_exec and
   has_changed_jobs_since, with since set to the evaluation date minus the window.
4. Apply the eligibility rules below.
5. Fetch the person profile (GET /people) for eligible people only, when fetch_contact_details is true.

Eligibility rules for one person, first that applies. Qualifying triggers are all configured triggers,
or only new_exec when executive_only is true:
- eligible: a qualifying trigger answered yes;
- incomplete: a qualifying trigger has no answer because of an error or because the run stopped;
- needs_review: a qualifying trigger answered unknown. Unknown is never a yes or a no;
- not_eligible: every qualifying trigger answered no.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, compare, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts, contact_details, person_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext, parse_date
from firmable_client.report import Raw, badge, card, details_json, esc, kv, note, table

NAME = "new-role-outreach"
TITLE = "New role outreach"
DESCRIPTION = ("Finds contacts whose new role, new executive role or job change falls inside a configured outreach "
               "window, using Firmable people search and Decisions, and retrieves contact details only for the "
               "people selected. A role change is a timing signal, not evidence of buying intent.")

TRIGGERS = ["new_to_role", "new_exec", "changed_jobs"]
TOOLS = {"new_to_role": "is_new_to_role", "new_exec": "is_new_exec", "changed_jobs": "has_changed_jobs_since"}
LABELS = {"new_to_role": "New to role", "new_exec": "New executive", "changed_jobs": "Changed jobs since window start"}
EXEC_LEVELS = ["C-Suite", "VP", "Director", "Head of", "Founder", "Owner"]
COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
ID_COLUMNS = ["person_id", "work_email", "linkedin_url"]
ACCOUNT_COLUMNS = ["company_id", "domain"]

STATUS_TONE = {"eligible": "good", "eligible_contacts_found": "good", "not_eligible": "bad",
               "no_eligible_contacts": "bad", "needs_review": "review", "incomplete": "error",
               "no_people_found": "unknown", "company_not_found": "unknown", "invalid_input": "error",
               "error": "error", "not_processed": "unknown"}
RESULT_TONE = {"pass": "good", "fail": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral"}
_TOP_KEYS = {"evaluation_date", "window_months", "triggers", "executive_only", "executive_seniorities",
             "count_internal_moves", "with_reasoning", "fetch_contact_details", "search"}
_SEARCH_KEYS = {"selected_country", "position", "seniority", "department", "max_people"}
_PERSON_ID = re.compile(r"^fp\d{12}$")
_COMPANY_ID = re.compile(r"^f\d{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINKEDIN = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.IGNORECASE)
_DATE = re.compile(r"^(\d{4})-(\d{2})(-(\d{2}))?$")
_GET_PEOPLE_PARAM = {"person_id": "id", "work_email": "work_email", "linkedin_url": "ln_url"}


def _whole(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    evaluation_date = None
    if raw.get("evaluation_date") is not None:
        evaluation_date = parse_date(raw["evaluation_date"], "evaluation_date").isoformat()
    window = raw.get("window_months", 6)
    if not _whole(window, 1, 60):
        raise ConfigError("window_months must be a whole number from 1 to 60.")
    triggers = raw.get("triggers", list(TRIGGERS))
    if not isinstance(triggers, list) or not triggers:
        raise ConfigError(f"triggers must be a non-empty list of: {', '.join(sorted(TRIGGERS))}.")
    seen: list[str] = []
    for i, t in enumerate(triggers, start=1):
        if t not in TRIGGERS:
            raise ConfigError(f"triggers[{i}] must be one of: {', '.join(sorted(TRIGGERS))}.")
        if t in seen:
            raise ConfigError(f"triggers[{i}] {t!r} is listed twice.")
        seen.append(t)
    defaults = {"executive_only": False, "count_internal_moves": True, "with_reasoning": True,
                "fetch_contact_details": True}
    for key, default in defaults.items():
        if not isinstance(raw.get(key, default), bool):
            raise ConfigError(f"{key} must be true or false.")
    executive_only = raw.get("executive_only", False)
    if executive_only and "new_exec" not in seen:
        raise ConfigError("executive_only needs new_exec in triggers.")
    if "new_exec" in seen and window > 36:
        raise ConfigError("window_months must be 36 or less when new_exec is in triggers, because is_new_exec "
                          "accepts at most 36.")
    levels = raw.get("executive_seniorities")
    if levels is not None:
        if not isinstance(levels, list) or not levels or any(s not in EXEC_LEVELS for s in levels) \
                or len(set(levels)) != len(levels):
            raise ConfigError(f"executive_seniorities must be a non-empty list without repeats, taken from: "
                              f"{', '.join(EXEC_LEVELS)}.")
        if "new_exec" not in seen:
            raise ConfigError("executive_seniorities needs new_exec in triggers.")
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
    return {"evaluation_date": evaluation_date, "window_months": window,
            "triggers": [t for t in TRIGGERS if t in seen], "executive_only": executive_only,
            "executive_seniorities": levels, "count_internal_moves": raw.get("count_internal_moves", True),
            "with_reasoning": raw.get("with_reasoning", True),
            "fetch_contact_details": raw.get("fetch_contact_details", True),
            "search": {"selected_country": country, "position": position, "seniority": seniority,
                       "department": department, "max_people": max_people}}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=ID_COLUMNS + ACCOUNT_COLUMNS)


def row_kind(row: dict[str, str]) -> str:
    return "contact" if any(row.get(c) for c in ID_COLUMNS) else "account"


def parse_row(row: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """Returns the checked identifier of one row, or a reason the row cannot be used."""
    if row_kind(row) == "contact":
        for col in ID_COLUMNS:
            v = row.get(col)
            if not v:
                continue
            if col == "person_id" and not _PERSON_ID.match(v):
                return None, f"person_id {v!r} is not a Firmable person ID (fp followed by 12 digits)."
            if col == "work_email" and not _EMAIL.match(v):
                return None, f"work_email {v!r} is not an email address."
            if col == "linkedin_url" and not _LINKEDIN.search(v):
                return None, f"linkedin_url {v!r} is not a LinkedIn profile URL (linkedin.com/in/...)."
        kind = next(c for c in ID_COLUMNS if row.get(c))
        return {"kind": "contact", "id_type": kind, "id_value": row[kind], "key": f"{kind}={row[kind]}"}, None
    cid = row.get("company_id")
    if cid:
        if not _COMPANY_ID.match(cid):
            return None, f"company_id {cid!r} is not a Firmable company ID (f followed by 12 digits)."
        return {"kind": "account", "company_id": cid, "domain": None, "key": f"company_id={cid}"}, None
    domain = normalise_domain(row.get("domain"))
    if not domain:
        return None, f"domain {row.get('domain')!r} is not a valid domain."
    return {"kind": "account", "company_id": None, "domain": domain, "key": f"domain={domain}"}, None


def months_before(iso: str, months: int) -> str:
    """The same day `months` calendar months earlier, moved to the month's last day when it is shorter."""
    d = _dt.date.fromisoformat(iso)
    y, m = d.year, d.month - months
    while m < 1:
        m += 12
        y -= 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return f"{y:04d}-{m:02d}-{day:02d}"


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    accounts: list[str] = []
    contacts: list[str] = []
    domains: list[str] = []
    for r in rows:
        p, _ = parse_row(r)
        if p is None:
            continue
        target = accounts if p["kind"] == "account" else contacts
        if p["key"] not in target:
            target.append(p["key"])
        if p["kind"] == "account" and p["domain"] and p["domain"] not in domains:
            domains.append(p["domain"])
    max_people = config["search"]["max_people"]
    people = len(accounts) * max_people + len(contacts)
    answers = people * len(config["triggers"])
    calls = [PlannedCall("GET /decisions", "Read the decision tool list and check the trigger tools", 1, []),
             PlannedCall("GET /company", "Resolve each unique account domain to a company ID", len(domains),
                         [("company", pricing.round4(pricing.COMPANY_PROFILE * len(domains)))]),
             PlannedCall("POST /people/search", "Search people at each unique account", len(accounts), []),
             PlannedCall("POST /decisions", f"{len(config['triggers'])} trigger(s) for up to {max_people} people "
                         "per account and for each unique contact", answers,
                         pricing.estimate_decision(answers, config["with_reasoning"]))]
    if config["fetch_contact_details"]:
        calls.append(PlannedCall("GET /people", "Person profile for each eligible person, assuming everyone is "
                                 "eligible", people, [("person", pricing.round4(pricing.PERSON_PROFILE * people))]))
    return calls


def _labels(items: list[dict[str, Any]]) -> str:
    return ", ".join(c["label"] for c in items)


def _window_position(started: Any, since: str, evaluation_date: str) -> str | None:
    if not isinstance(started, str) or not _DATE.match(started):
        return None
    if _DATE.match(started).group(4):
        key, lo, hi = started, since, evaluation_date
    else:
        key, lo, hi = started, since[:7], evaluation_date[:7]
    if key < lo:
        return "before"
    if key > hi:
        return "after"
    return None


def _search(client: Any, body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """People search. A documented no-match response (success false, no records) counts as zero people."""
    resp = client.search_people(body)
    return resp, resp.get("success") is False


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    evaluation_date = cfg["evaluation_date"] or ctx.today.isoformat()
    since = months_before(evaluation_date, cfg["window_months"])
    coverage = [
        "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no "
        "or as a yes.",
        "A new role, new executive role or job change is a timing signal for outreach. It does not show buying "
        "intent.",
        f"Evaluation date {evaluation_date}, window {cfg['window_months']} month(s), window start {since}. "
        f"has_changed_jobs_since is asked with since {since}.",
        "is_new_to_role and is_new_exec take no reference date, so they count back from Firmable's current date, "
        "not from the evaluation date.",
        "Start dates are reported as returned in details.started. They are not corrected.",
        "This recipe sends no outreach.",
    ]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    inputs: dict[str, dict[str, Any]] = {
        "new_to_role": {"within_months": cfg["window_months"], "count_internal_moves": cfg["count_internal_moves"]},
        "new_exec": {"within_months": cfg["window_months"]},
        "changed_jobs": {"since": since},
    }
    if cfg["executive_seniorities"]:
        inputs["new_exec"]["seniorities"] = cfg["executive_seniorities"]
    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no triggers were asked.")
    problems = check_tools(catalogue, [{"tool": TOOLS[t], "inputs": inputs[t]} for t in cfg["triggers"]], "person")
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("The trigger tools are missing or no longer accept the inputs this recipe sends.")
    model_tools = sorted({TOOLS[t] for t in cfg["triggers"] if uses_model(catalogue, TOOLS[t])})
    if model_tools:
        coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                        f"{', '.join(model_tools)}. This code applies only the eligibility rules in the README.")
    if cfg["executive_only"]:
        coverage.append("executive_only is set, so only New executive (is_new_exec) decides eligibility. Other "
                        "triggers are shown for information.")
    parsed = [parse_row(r)[0] for r in ctx.rows]
    srch = cfg["search"]
    if any(p and p["kind"] == "account" for p in parsed):
        filters = [f"country {srch['selected_country']}"]
        for k in ("position", "seniority", "department"):
            if srch[k]:
                filters.append(f"{k} {srch[k]}")
        coverage.append(f"People search filters: {', '.join(filters)}. People search has no start-date filter, so "
                        f"each retrieved person is checked with Decisions, up to {srch['max_people']} people per "
                        "account.")
    if not cfg["fetch_contact_details"]:
        coverage.append("fetch_contact_details is false, so no contact details were retrieved.")

    for r, p in zip(ctx.rows, parsed):
        r["_key"] = p["key"] if p else ""
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier account or contact; identical requests were "
                        "answered from memory, not sent again.")
    empty_searches = [0]

    def evaluate(subject: dict[str, Any], row_id: str) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for t in cfg["triggers"]:
            entry = {"trigger": t, "label": LABELS[t], "tool": TOOLS[t], "answer": None, "started": None,
                     "error": None}
            if ctx.stopped is not None:
                results.append({**entry, "result": "not_asked"})
                continue
            try:
                resp = client.make_decision(TOOLS[t], {**subject, **inputs[t]}, with_reasoning=cfg["with_reasoning"])
            except FirmableError as e:
                results.append({**entry, "result": "error", "error": ctx.record_error(row_id, e)})
                continue
            ans = answer_from(resp)
            result = compare(ans["answer"], "yes")
            details = ans["details"] if isinstance(ans["details"], dict) else {}
            results.append({**entry, **ans, "result": result, "started": details.get("started"),
                            "error": ans["answer_error"] if result == "error" else None})
        qualifying = ["new_exec"] if cfg["executive_only"] else cfg["triggers"]
        q = [r for r in results if r["trigger"] in qualifying]
        yes = [r for r in q if r["result"] == "pass"]
        errored = [r for r in q if r["result"] in ("error", "not_asked")]
        unknown = [r for r in q if r["result"] == "unknown"]
        if yes:
            status, reason = "eligible", f"Qualifying trigger(s) answered yes: {_labels(yes)}."
        elif errored:
            status, reason = "incomplete", ("No qualifying trigger answered yes, and these have no answer because of "
                                            f"an error or because the run stopped: {_labels(errored)}.")
        elif unknown:
            status, reason = "needs_review", (f"No qualifying trigger answered yes, and these answered unknown: "
                                              f"{_labels(unknown)}. Unknown means Firmable does not hold the fact; "
                                              "it is not a no.")
        else:
            status, reason = "not_eligible", "Every qualifying trigger answered no."
        if cfg["executive_only"] and status != "eligible":
            others = [r for r in results if r["trigger"] not in qualifying and r["result"] == "pass"]
            if others:
                reason += f" {_labels(others)} answered yes, but executive_only counts only New executive."
        reasons = [f"{r['label']} ({r['tool']}) answered yes; "
                   + (f"start date returned: {r['started']}." if r["started"] else "no start date returned.")
                   for r in results if r["result"] == "pass"]
        return {"status": status, "status_reason": reason, "triggers": results, "trigger_reasons": reasons,
                "conflicts": _conflicts(results)}

    def _conflicts(results: list[dict[str, Any]]) -> list[str]:
        by = {r["trigger"]: r for r in results}
        out: list[str] = []
        ntr, cj = by.get("new_to_role"), by.get("changed_jobs")
        if cfg["count_internal_moves"] and ntr and ntr["answer"] == "no":
            for t in ("new_exec", "changed_jobs"):
                other = by.get(t)
                if other and other["answer"] == "yes":
                    out.append(f"{other['tool']} answered yes but is_new_to_role answered no. Both answers are "
                               "shown as returned.")
        if ntr and cj and ntr["answer"] == "yes" and cj["answer"] == "no" \
                and (ntr.get("details") or {}).get("inside_same_company") is False:
            out.append("is_new_to_role answered yes for a move to a different company, but has_changed_jobs_since "
                       "answered no. Both answers are shown as returned.")
        yes = [r for r in results if r["answer"] == "yes" and r["started"]]
        starts: list[str] = []
        for r in yes:
            if str(r["started"]) not in starts:
                starts.append(str(r["started"]))
        if len(starts) > 1:
            out.append("Triggers that answered yes returned different start dates: "
                       + "; ".join(f"{r['tool']} {r['started']}" for r in yes) + ".")
        for r in yes:
            pos = _window_position(r["started"], since, evaluation_date)
            if pos == "before":
                out.append(f"{r['tool']} answered yes but returned start date {r['started']}, which is before the "
                           f"window start {since}.")
            elif pos == "after":
                out.append(f"{r['tool']} answered yes but returned start date {r['started']}, which is after the "
                           f"evaluation date {evaluation_date}.")
        return out

    def contact_info(params: dict[str, str], row_id: str) -> tuple[dict[str, Any] | None, str | None]:
        if not cfg["fetch_contact_details"]:
            return None, "Not retrieved, because fetch_contact_details is false."
        if ctx.stopped is not None:
            return None, "Not retrieved, because the run stopped."
        try:
            person = client.get_person(**params)
        except NotFoundError:
            return None, "Firmable has no person profile for this identifier, so no contact details were retrieved."
        except FirmableError as e:
            err = ctx.record_error(row_id, e)
            return None, f"Contact details could not be retrieved: {err['message']}"
        return {"profile": person_facts(person), "contact": contact_details(person)}, None

    def person_record(ev: dict[str, Any], info: dict[str, Any] | None, info_note: str | None,
                      **fields: Any) -> dict[str, Any]:
        profile = info["profile"] if info else None
        return {**fields, **ev, "profile": profile, "contact": info["contact"] if info else None,
                "contact_note": info_note,
                "name": fields.get("name") or (profile or {}).get("name"),
                "position": fields.get("position") or (profile or {}).get("position"),
                "company_name": fields.get("company_name") or (profile or {}).get("current_company_name")}

    def one(row: dict[str, str]) -> dict[str, Any]:
        p, problem = parse_row(row)
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "row_type": row_kind(row),
                                "duplicate_of": dupes.get(row["row_id"]), "company": None, "company_id": None,
                                "search": None, "people": [], "eligible_count": 0}
        if p is None:
            return {**base, "status": "invalid_input", "status_reason": problem}
        if p["kind"] == "contact":
            ev = evaluate({p["id_type"]: p["id_value"]}, row["row_id"])
            info, info_note = (None, None)
            if ev["status"] == "eligible":
                info, info_note = contact_info({_GET_PEOPLE_PARAM[p["id_type"]]: p["id_value"]}, row["row_id"])
            entity = next((t.get("entity_id") for t in ev["triggers"] if t.get("entity_id")), None)
            person_id = p["id_value"] if p["id_type"] == "person_id" else entity
            person = person_record(ev, info, info_note, person_id=person_id,
                                   identifier={"type": p["id_type"], "value": p["id_value"]}, source="input",
                                   name=None, position=None, company_name=None)
            return {**base, "status": ev["status"], "status_reason": ev["status_reason"], "people": [person],
                    "eligible_count": 1 if ev["status"] == "eligible" else 0}
        company_id = p["company_id"]
        if p["domain"]:
            try:
                company = client.get_company(fqdn=p["domain"])
            except NotFoundError:
                return {**base, "status": "company_not_found",
                        "status_reason": "Firmable has no company profile for this domain, so no people were searched."}
            base["company"] = company_facts(company)
            company_id = company["id"]
        base["company_id"] = company_id
        body: dict[str, Any] = {"companyId": company_id, "selectedCountry": srch["selected_country"], "from": "0",
                                "size": str(srch["max_people"])}
        for k in ("position", "seniority", "department"):
            if srch[k]:
                body[k] = srch[k]
        resp, empty = _search(client, body)
        if empty:
            empty_searches[0] += 1
        records = [r for r in resp.get("records") or [] if isinstance(r, dict)]
        total = resp.get("total")
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        not_retrieved = total - len(records) if total is not None and total > len(records) else 0
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
            ev = evaluate({"person_id": pid}, row["row_id"])
            info, info_note = (None, None)
            if ev["status"] == "eligible":
                info, info_note = contact_info({"id": pid}, row["row_id"])
            people.append(person_record(ev, info, info_note, person_id=pid, identifier=None, source="people_search",
                                        name=rec.get("name"), position=rec.get("position"),
                                        company_name=rec.get("company_name")))
        base["search"] = {"request": body, "total": total, "retrieved": len(records),
                          "not_retrieved": not_retrieved, "skipped": skipped, "no_match_response": empty}
        base["people"] = people
        statuses = [x["status"] for x in people]
        n = len(people)
        eligible = statuses.count("eligible")
        base["eligible_count"] = eligible
        if not people:
            status, reason = "no_people_found", ("People search returned no people for this company with the configured "
                                                 "filters." if not records else
                                                 "People search returned no person this recipe could check.")
        elif eligible:
            status, reason = "eligible_contacts_found", f"{eligible} of {n} person(s) evaluated are eligible."
        elif "incomplete" in statuses:
            status, reason = "incomplete", (f"No person evaluated is eligible, and {statuses.count('incomplete')} of "
                                            f"{n} have a qualifying trigger without an answer because of an error or "
                                            "because the run stopped.")
        elif "needs_review" in statuses:
            status, reason = "needs_review", (f"No person evaluated is eligible, and {statuses.count('needs_review')} "
                                              f"of {n} have a qualifying trigger that answered unknown.")
        else:
            status, reason = "no_eligible_contacts", f"None of the {n} person(s) evaluated is eligible."
        if not_retrieved:
            reason += (f" {not_retrieved} more matching person(s) were not retrieved, because search.max_people is "
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
    eligible_ids: list[str] = []
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        for person in r.get("people") or []:
            person_counts[person["status"]] = person_counts.get(person["status"], 0) + 1
            key = person.get("person_id") or f"{r['row_id']}"
            if person["status"] == "eligible" and key not in eligible_ids:
                eligible_ids.append(key)
    return {"rows": rows, "summary": {"status_counts": counts, "person_status_counts": person_counts,
                                      "unique_eligible_people": len(eligible_ids),
                                      "evaluation_date": evaluation_date, "window_start": since,
                                      "window_months": cfg["window_months"]},
            "coverage": coverage}


CSV_COLUMNS = ["row_id", "row_type", "duplicate_of", "row_status", "row_status_reason", "company_id", "company_name",
               "person_id", "person_name", "position", "person_status", "person_status_reason", "trigger_results",
               "trigger_reasons", "start_dates", "conflicts", "work_emails", "phones", "contact_note"]


def _dnd(v: Any) -> str:
    return "yes" if v is True else "no" if v is False else "unknown"


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        head = {"row_id": r["row_id"], "row_type": r.get("row_type"), "duplicate_of": r.get("duplicate_of"),
                "row_status": r["status"],
                "row_status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
                "company_id": r.get("company_id"), "company_name": company.get("name")}
        people = r.get("people") or []
        if not people:
            out.append(head)
            continue
        for person in people:
            contact = person.get("contact") or {}
            starts: list[str] = []
            for t in person["triggers"]:
                if t["result"] == "pass" and t["started"] and str(t["started"]) not in starts:
                    starts.append(str(t["started"]))
            out.append({**head, "company_name": company.get("name") or person.get("company_name"),
                        "person_id": person.get("person_id"), "person_name": person.get("name"),
                        "position": person.get("position"), "person_status": person["status"],
                        "person_status_reason": person["status_reason"],
                        "trigger_results": [f"{t['trigger']}={t['result']}({t.get('answer') or '-'})"
                                            for t in person["triggers"]],
                        "trigger_reasons": person["trigger_reasons"], "start_dates": starts,
                        "conflicts": person["conflicts"], "work_emails": contact.get("work_emails") or [],
                        "phones": [f"{ph['value']} (do not call flag: {_dnd(ph.get('is_dnd'))})"
                                   for ph in contact.get("phones") or []],
                        "contact_note": person.get("contact_note")})
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    summary = result["summary"]
    counts = summary.get("status_counts", {})
    report.add("Result", "Outreach window summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + kv([("Evaluation date", summary.get("evaluation_date")), ("Window start", summary.get("window_start")),
                     ("Window, months", summary.get("window_months")),
                     ("Unique eligible people", summary.get("unique_eligible_people"))])
               + note("A new role is a timing signal for outreach. It does not show buying intent.", "info")
               + note("Unknown means Firmable does not hold the fact. A person whose qualifying triggers answered "
                      "unknown is <strong>needs review</strong>, never eligible.", "unknown"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        inp = r["input"]
        company = r.get("company") or {}
        title = (company.get("name") or inp.get("domain") or inp.get("company_id") or inp.get("person_id")
                 or inp.get("work_email") or inp.get("linkedin_url") or r["row_id"])
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        left += kv([("Row type", r.get("row_type")), ("Firmable company ID", r.get("company_id")),
                    ("Domain", company.get("fqdn")), ("Head office country", company.get("hq_country"))])
        search = r.get("search")
        if search:
            left += kv([("People matching the search", search.get("total")), ("Retrieved", search.get("retrieved")),
                        ("Not retrieved", search.get("not_retrieved"))])
            for s in search.get("skipped") or []:
                left += note(f"Skipped {esc(s.get('name'))} ({esc(s.get('id'))}): {esc(s.get('reason'))}", "review")
        if r.get("duplicate_of"):
            left += note(f"Same account or contact as row {esc(r['duplicate_of'])}; identical requests were reused.",
                         "info")
        right = ""
        for person in r.get("people") or []:
            right += (f"<h3>{esc(person.get('name') or person.get('person_id') or 'Contact')} "
                      f"{badge(person['status'].replace('_', ' '), STATUS_TONE.get(person['status'], 'neutral'))}</h3>"
                      f"<p>{esc(person['status_reason'])}</p>")
            right += kv([("Firmable person ID", person.get("person_id")), ("Position", person.get("position")),
                         ("Company", person.get("company_name"))])
            right += table(["Trigger", "Tool", "Answer", "Result", "Start date returned", "Confidence"], [
                [t["label"], t["tool"], t.get("answer") or "none",
                 Raw(badge(t["result"].replace("_", " "), RESULT_TONE.get(t["result"], "neutral"))),
                 t.get("started"), t.get("confidence")] for t in person["triggers"]])
            for t in person["triggers"]:
                rs = t.get("reasoning") or {}
                if rs.get("reason"):
                    right += (f"<p><strong>{esc(t['label'])}:</strong> {esc(rs.get('reason'))}</p>"
                              f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                              f"{esc(rs.get('last_refreshed'))}.</p>")
                if t.get("error"):
                    msg = t["error"].get("message") if isinstance(t["error"], dict) else t["error"]
                    right += note(f"{esc(t['label'])}: {esc(msg)}", "error")
            for c in person.get("conflicts") or []:
                right += note(esc(c), "review")
            contact = person.get("contact")
            if contact:
                right += kv([("Work emails", ", ".join(contact.get("work_emails") or []) or None),
                             ("Phones", ", ".join(f"{ph['value']} (do not call flag: {_dnd(ph.get('is_dnd'))})"
                                                  for ph in contact.get("phones") or []) or None)])
            if person.get("contact_note"):
                right += f"<p class=\"muted\">{esc(person['contact_note'])}</p>"
            right += details_json("Triggers as JSON", person["triggers"])
        cards.append(card(str(title), badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No people were evaluated for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Rows", "Each account or contact with the people evaluated", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
