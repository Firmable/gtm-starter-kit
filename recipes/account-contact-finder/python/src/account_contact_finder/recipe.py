"""Account contact finder: finds relevant people at supplied accounts.

Workflow for each row:
1. Use the row's company_id, or resolve the domain to a Firmable company with GET /company.
2. Run each configured people search (POST /people/search) with the documented fields only, paging with
   from and size up to max_pages. Candidates found by several searches are kept once.
3. Ask each configured Decisions check about the first max_candidates_checked candidates.
4. Apply the selection policy below.
5. Fetch the person profile (GET /people) for selected candidates only, up to max_details_per_account.

Selection policy for one candidate, in order of precedence:
- rejected: a mandatory check got the opposite of the wanted answer;
- incomplete: a mandatory check returned an error or was not asked because the run stopped;
- needs_review: a mandatory check answered unknown. Unknown is never a pass or a fail;
- selected: every mandatory check passed and at least policy.min_optional_passes optional checks passed;
- not_selected: every mandatory check passed but too few optional checks passed;
- not_checked: the account already reached max_candidates_checked.
With no checks configured, every checked candidate is selected on the search criteria alone.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, compare, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts, contact_details, person_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "account-contact-finder"
TITLE = "Account contact finder"
DESCRIPTION = ("Finds people at each account with Firmable people search, checks each candidate with Decisions, "
               "and retrieves contact details only for the people the selection policy selects.")

COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
# Search ids and labels from the published people search reference.
SENIORITY = {"1": "Board Member / Company Director", "2": "Owner / Founder", "3": "C-Suite / Partner",
             "4": "VP/Director / Head of", "5": "Manager", "6": "Other", "7": "No Data Available"}
DEPARTMENT = {"1": "General Management", "2": "Sales", "3": "Trades", "4": "Operations",
              "5": "Engineering & Technical", "6": "Human Resources", "7": "Customer Service",
              "8": "Medicine & Healthcare", "9": "Research & Analysis", "10": "Legal", "11": "Marketing",
              "12": "Education & Training", "13": "Consulting", "14": "Finance", "15": "Product", "16": "Other",
              "17": "No Data Available"}
SELECTIONS = ["selected", "needs_review", "not_selected", "rejected", "incomplete", "not_checked"]
STATUS_TONE = {"contacts_selected": "good", "no_contacts_selected": "bad", "no_candidates": "unknown",
               "incomplete": "error", "company_not_found": "unknown", "invalid_input": "error", "error": "error",
               "not_processed": "unknown"}
SELECTION_TONE = {"selected": "good", "needs_review": "review", "not_selected": "bad", "rejected": "bad",
                  "incomplete": "error", "not_checked": "neutral"}
RESULT_TONE = {"pass": "good", "fail": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral"}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_COMPANY_ID = re.compile(r"^f\d+$")
_TOP_KEYS = {"search", "checks", "policy", "with_reasoning", "max_candidates_checked", "max_details_per_account"}
_SEARCH_KEYS = {"country", "queries", "page_size", "max_pages"}
_QUERY_KEYS = {"id", "label", "position", "seniority", "department"}
_CHECK_KEYS = {"id", "label", "tool", "inputs", "want", "mandatory"}


def _whole(value: Any, low: int, high: int | None) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool) and value >= low
            and (high is None or value <= high))


def _code(value: Any, table_: dict[str, str]) -> str | None:
    """Maps a documented id or its label to the id string."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v in table_:
        return v
    for k, label in table_.items():
        if label.lower() == v.lower():
            return k
    return None


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    s = raw.get("search")
    if not isinstance(s, dict):
        raise ConfigError("search must be an object.")
    bad = sorted(set(s) - _SEARCH_KEYS)
    if bad:
        raise ConfigError(f"search has unknown key(s): {', '.join(bad)}")
    country = s.get("country", "AU")
    if country not in COUNTRIES:
        raise ConfigError(f"search.country must be one of: {', '.join(COUNTRIES)}.")
    qs = s.get("queries")
    if not isinstance(qs, list) or not qs:
        raise ConfigError("search.queries must be a non-empty list.")
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, qr in enumerate(qs, start=1):
        if not isinstance(qr, dict):
            raise ConfigError(f"search.queries[{i}] must be an object.")
        bad = sorted(set(qr) - _QUERY_KEYS)
        if bad:
            raise ConfigError(f"search.queries[{i}] has unknown key(s): {', '.join(bad)}")
        qid = qr.get("id")
        if not isinstance(qid, str) or not _ID.match(qid):
            raise ConfigError(f"search.queries[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if qid in seen:
            raise ConfigError(f"search.queries[{i}].id '{qid}' is used twice.")
        seen.add(qid)
        label = qr.get("label", qid)
        if not isinstance(label, str):
            raise ConfigError(f"search.queries[{i}].label must be text.")
        position = qr.get("position", "")
        if not isinstance(position, str):
            raise ConfigError(f"search.queries[{i}].position must be text.")
        seniority = None
        if qr.get("seniority") not in (None, ""):
            seniority = _code(qr["seniority"], SENIORITY)
            if seniority is None:
                raise ConfigError(f"search.queries[{i}].seniority must be a seniority id 1 to 7 or its label.")
        department = None
        if qr.get("department") not in (None, ""):
            department = _code(qr["department"], DEPARTMENT)
            if department is None:
                raise ConfigError(f"search.queries[{i}].department must be a department id 1 to 17 or its label.")
        if not position.strip() and seniority is None and department is None:
            raise ConfigError(f"search.queries[{i}] needs at least one of position, seniority or department.")
        queries.append({"id": qid, "label": label, "position": position.strip() or None, "seniority": seniority,
                        "department": department})
    page_size = s.get("page_size", 25)
    if not _whole(page_size, 1, 100):
        raise ConfigError("search.page_size must be a whole number from 1 to 100.")
    max_pages = s.get("max_pages", 1)
    if not _whole(max_pages, 1, 10):
        raise ConfigError("search.max_pages must be a whole number from 1 to 10.")

    checks_raw = raw.get("checks", [])
    if not isinstance(checks_raw, list):
        raise ConfigError("checks must be a list.")
    checks: list[dict[str, Any]] = []
    seen = set()
    for i, c in enumerate(checks_raw, start=1):
        if not isinstance(c, dict):
            raise ConfigError(f"checks[{i}] must be an object.")
        bad = sorted(set(c) - _CHECK_KEYS)
        if bad:
            raise ConfigError(f"checks[{i}] has unknown key(s): {', '.join(bad)}")
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise ConfigError(f"checks[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if cid in seen:
            raise ConfigError(f"checks[{i}].id '{cid}' is used twice.")
        seen.add(cid)
        tool = c.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ConfigError(f"checks[{i}].tool must name a decision tool.")
        inputs = c.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ConfigError(f"checks[{i}].inputs must be an object.")
        want = c.get("want", "yes")
        if want not in ("yes", "no"):
            raise ConfigError(f"checks[{i}].want must be yes or no.")
        mandatory = c.get("mandatory", True)
        if not isinstance(mandatory, bool):
            raise ConfigError(f"checks[{i}].mandatory must be true or false.")
        label = c.get("label", cid)
        if not isinstance(label, str):
            raise ConfigError(f"checks[{i}].label must be text.")
        checks.append({"id": cid, "label": label, "tool": tool, "inputs": inputs, "want": want,
                       "mandatory": mandatory})
    policy = raw.get("policy", {}) or {}
    if not isinstance(policy, dict):
        raise ConfigError("policy must be an object.")
    mop = policy.get("min_optional_passes", 0)
    if not _whole(mop, 0, None):
        raise ConfigError("policy.min_optional_passes must be a whole number, 0 or more.")
    optional = sum(1 for c in checks if not c["mandatory"])
    if mop > optional:
        raise ConfigError(f"policy.min_optional_passes is {mop} but only {optional} optional check(s) exist.")
    with_reasoning = raw.get("with_reasoning", False)
    if not isinstance(with_reasoning, bool):
        raise ConfigError("with_reasoning must be true or false.")
    mcc = raw.get("max_candidates_checked", 10)
    if not _whole(mcc, 1, None):
        raise ConfigError("max_candidates_checked must be a whole number, 1 or more.")
    mdp = raw.get("max_details_per_account", 3)
    if not _whole(mdp, 0, None):
        raise ConfigError("max_details_per_account must be a whole number, 0 or more.")
    return {"search": {"country": country, "queries": queries, "page_size": page_size, "max_pages": max_pages},
            "checks": checks, "policy": {"min_optional_passes": mop}, "with_reasoning": with_reasoning,
            "max_candidates_checked": mcc, "max_details_per_account": mdp}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=["domain", "company_id"])


def _account_key(row: dict[str, str]) -> str:
    cid = (row.get("company_id") or "").strip()
    if cid:
        return f"id:{cid}" if _COMPANY_ID.match(cid) else ""
    d = normalise_domain(row.get("domain"))
    return f"domain:{d}" if d else ""


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    keys: list[str] = []
    for r in rows:
        k = _account_key(r)
        if k and k not in keys:
            keys.append(k)
    n = len(keys)
    domains = sum(1 for k in keys if k.startswith("domain:"))
    s = config["search"]
    nq = len(s["queries"])
    per_account = nq * s["page_size"] * s["max_pages"]
    checked = min(config["max_candidates_checked"], per_account)
    details = min(config["max_details_per_account"], checked)
    nchecks = len(config["checks"])
    calls = []
    if nchecks:
        calls.append(PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []))
    calls.append(PlannedCall("GET /company", "Company profile for each unique valid domain without a company_id",
                             domains, [("company", pricing.round4(pricing.COMPANY_PROFILE * domains))]))
    calls.append(PlannedCall("POST /people/search", f"{nq} search(es) of up to {s['max_pages']} page(s) for each "
                             "unique account", n * nq * s["max_pages"], []))
    if nchecks:
        calls.append(PlannedCall("POST /decisions", f"{nchecks} check(s) for up to {checked} candidate(s) per account",
                                 n * checked * nchecks,
                                 pricing.estimate_decision(n * checked * nchecks, config["with_reasoning"])))
    calls.append(PlannedCall("GET /people", f"Person profile for up to {details} selected candidate(s) per account",
                             n * details, [("person", pricing.round4(pricing.PERSON_PROFILE * n * details))]))
    return calls


def _filters(qr: dict[str, Any]) -> str:
    parts = []
    if qr["position"]:
        parts.append(f"position '{qr['position']}'")
    if qr["seniority"]:
        parts.append(f"seniority {SENIORITY[qr['seniority']]}")
    if qr["department"]:
        parts.append(f"department {DEPARTMENT[qr['department']]}")
    return ", ".join(parts)


def _select(checks: list[dict[str, Any]], mop: int) -> tuple[str, str]:
    if not checks:
        return "selected", "Selected on the search criteria alone; no checks are configured."

    def labels(items: list[dict[str, Any]]) -> str:
        return ", ".join(c["label"] for c in items)

    mandatory = [c for c in checks if c["mandatory"]]
    optional = [c for c in checks if not c["mandatory"]]
    failed = [c for c in mandatory if c["result"] == "fail"]
    errored = [c for c in mandatory if c["result"] in ("error", "not_asked")]
    unknown = [c for c in mandatory if c["result"] == "unknown"]
    if failed:
        return "rejected", f"Mandatory check(s) failed: {labels(failed)}."
    if errored:
        return "incomplete", f"Mandatory check(s) have no answer because of an error or because the run stopped: {labels(errored)}."
    if unknown:
        return "needs_review", (f"Mandatory check(s) answered unknown: {labels(unknown)}. "
                                "Unknown means Firmable does not hold the fact; it is not a no.")
    passes = sum(1 for c in optional if c["result"] == "pass")
    opt_unknown = sum(1 for c in optional if c["result"] == "unknown")
    tail = f" {opt_unknown} optional check(s) answered unknown." if opt_unknown else ""
    if passes >= mop:
        return "selected", (f"All mandatory checks passed; {passes} of {len(optional)} optional check(s) "
                            f"passed (minimum {mop}).{tail}")
    return "not_selected", (f"All mandatory checks passed, but only {passes} of {len(optional)} optional "
                            f"check(s) passed (minimum {mop}).{tail}")


def _disagreements(candidate: dict[str, Any], queries: dict[str, dict[str, Any]]) -> list[str]:
    """Notes where a search filter and a Decisions check about the same attribute disagree."""
    notes = []
    for c in candidate["checks"]:
        if c.get("answer") != "no" or c["tool"] not in ("matches_seniority", "matches_department"):
            continue
        attr, table_, key = (("seniority", SENIORITY, "seniorities") if c["tool"] == "matches_seniority"
                             else ("department", DEPARTMENT, "departments"))
        wanted = [str(x).lower() for x in (c["inputs"].get(key) or [])]
        for qid in candidate["matched_queries"]:
            code = queries[qid][attr]
            if code and table_[code].lower() in wanted:
                notes.append(f"Found by search '{queries[qid]['label']}' filtered on {attr} {table_[code]}, but "
                             f"check '{c['label']}' answered no. Both are shown as returned.")
                break
    return notes


def _counts_text(cands: list[dict[str, Any]]) -> str:
    parts = []
    for s in SELECTIONS:
        n = sum(1 for c in cands if c["selection"] == s)
        if n:
            parts.append(f"{s} {n}")
    return f" Selection results: {', '.join(parts)}." if parts else ""


def _phone_text(ph: dict[str, Any]) -> str:
    if ph.get("is_dnd") is True:
        return f"{ph['value']} (do not call)"
    if ph.get("is_dnd") is False:
        return str(ph["value"])
    return f"{ph['value']} (do-not-call status not returned)"


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    s = cfg["search"]
    queries = {qr["id"]: qr for qr in s["queries"]}
    checks_cfg = cfg["checks"]
    mop = cfg["policy"]["min_optional_passes"]
    coverage = [
        "People search accepts only companyId, selectedCountry, position, seniority, department, from and size. "
        "It has no name, location or date filter, so this recipe offers none.",
        f"Searches are limited to country {s['country']} (selectedCountry). People Firmable records in other "
        "countries are not returned.",
        "How people search matches position text is not documented. Results are shown as returned.",
        "Each search shows the total the API reported beside the records retrieved. Fewer records are retrieved "
        "than the total when page_size and max_pages stop paging.",
        "A person not in the search results is not observed in retrieved data. It does not mean no such person "
        "works there.",
        f"Only selected candidates get a person profile (GET /people), up to {cfg['max_details_per_account']} per "
        "account. Contact details are shown as returned; phones flagged is_dnd true are marked do not call.",
    ]
    if checks_cfg:
        coverage.insert(0, "An unknown answer means Firmable does not hold the fact needed to answer. It is never "
                           "counted as a no or as a pass.")

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    if checks_cfg:
        try:
            catalogue = client.list_decision_tools()
        except FirmableError as e:
            ctx.record_error(None, e)
            return stop_all("The decision tool list could not be read, so no searches or checks were made.")
        problems = check_tools(catalogue, checks_cfg, "person")
        if problems:
            for p in problems:
                ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                                   "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                                   "retry_after": None})
            coverage.extend(problems)
            return stop_all("The configuration names decision tools or inputs the API does not accept.")
        model_tools = sorted({c["tool"] for c in checks_cfg if uses_model(catalogue, c["tool"])})
        if model_tools:
            coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                            f"{', '.join(model_tools)}. This code applies only the rules in the configuration.")
        if any(c["tool"] == "works_at_company" for c in checks_cfg):
            coverage.append("works_at_company is asked with the account's Firmable company ID, added by this recipe.")

    for r in ctx.rows:
        r["_key"] = _account_key(r)
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier account; their API responses were reused, not "
                        "requested again.")
    empty_search_seen = [False]

    def search(company_id: str, qr: dict[str, Any], row_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        body_base: dict[str, Any] = {"companyId": company_id, "selectedCountry": s["country"]}
        if qr["position"]:
            body_base["position"] = qr["position"]
        if qr["seniority"]:
            body_base["seniority"] = qr["seniority"]
        if qr["department"]:
            body_base["department"] = qr["department"]
        info: dict[str, Any] = {"query_id": qr["id"], "label": qr["label"], "filters": _filters(qr), "total": None,
                                "retrieved": 0, "pages_fetched": 0, "complete": False, "status": "ok",
                                "error": None}
        records: list[dict[str, Any]] = []
        for page in range(s["max_pages"]):
            if ctx.stopped is not None:
                info["status"] = "not_requested"
                break
            body = {**body_base, "from": str(page * s["page_size"]), "size": str(s["page_size"])}
            try:
                resp = client.search_people(body)
            except FirmableError as e:
                info["status"] = "error"
                info["error"] = ctx.record_error(row_id, e)
                break
            if resp.get("success") is False:
                empty_search_seen[0] = True
            page_records = [x for x in resp.get("records") or [] if isinstance(x, dict)]
            info["pages_fetched"] += 1
            info["retrieved"] += len(page_records)
            if isinstance(resp.get("total"), int) and not isinstance(resp.get("total"), bool):
                info["total"] = resp["total"]
            records.extend(page_records)
            if len(page_records) < s["page_size"] or (info["total"] is not None and info["retrieved"] >= info["total"]):
                info["complete"] = True
                break
        if info["status"] == "ok" and info["retrieved"] == 0:
            info["status"] = "no_matches"
        return info, records

    def one(row: dict[str, str]) -> dict[str, Any]:
        domain = normalise_domain(row.get("domain"))
        cid_in = (row.get("company_id") or "").strip()
        base = {"row_id": row["row_id"], "input": row, "domain": domain, "duplicate_of": dupes.get(row["row_id"]),
                "company_id": None, "company_id_source": None, "company": None, "searches": [], "candidates": []}
        if cid_in and not _COMPANY_ID.match(cid_in):
            return {**base, "status": "invalid_input",
                    "status_reason": f"'{cid_in}' is not a Firmable company ID."}
        if not cid_in and not domain:
            return {**base, "status": "invalid_input",
                    "status_reason": f"'{row.get('domain') or ''}' is not a valid domain."}
        if cid_in:
            company_id, source = cid_in, "input"
        else:
            try:
                company = client.get_company(fqdn=domain)
            except NotFoundError:
                return {**base, "status": "company_not_found",
                        "status_reason": "Firmable has no company profile for this domain, so no search was made."}
            base["company"] = company_facts(company)
            company_id, source = company["id"], "company_lookup"
        base["company_id"] = company_id
        base["company_id_source"] = source

        searches = []
        found: dict[str, dict[str, Any]] = {}
        no_id = 0
        for qr in s["queries"]:
            info, records = search(company_id, qr, row["row_id"])
            searches.append(info)
            for rec in records:
                pid = rec.get("person_id") or rec.get("id")
                if not isinstance(pid, str) or not pid:
                    no_id += 1
                    continue
                if pid in found:
                    if qr["id"] not in found[pid]["matched_queries"]:
                        found[pid]["matched_queries"].append(qr["id"])
                    continue
                found[pid] = {"person_id": pid, "name": rec.get("name"), "position": rec.get("position"),
                              "company_name": rec.get("company_name"), "has_email": rec.get("has_email"),
                              "has_phone": rec.get("has_phone"), "has_mobile": rec.get("has_mobile"),
                              "has_dnd_phone": rec.get("has_dnd_phone"), "matched_queries": [qr["id"]]}
        candidates = list(found.values())
        for n, cand in enumerate(candidates):
            cand["checks"] = []
            cand["notes"] = []
            if n >= cfg["max_candidates_checked"]:
                cand["selection"] = "not_checked"
                cand["selection_reason"] = (f"Not checked: the account reached max_candidates_checked "
                                            f"({cfg['max_candidates_checked']}).")
                continue
            for c in checks_cfg:
                entry = {"check_id": c["id"], "label": c["label"], "tool": c["tool"], "inputs": c["inputs"],
                         "mandatory": c["mandatory"], "want": c["want"]}
                if ctx.stopped is not None:
                    cand["checks"].append({**entry, "result": "not_asked", "answer": None, "error": None})
                    continue
                question = {"person_id": cand["person_id"], **c["inputs"]}
                if c["tool"] == "works_at_company":
                    question["company_id"] = company_id
                try:
                    resp = client.make_decision(c["tool"], question, with_reasoning=cfg["with_reasoning"])
                except FirmableError as e:
                    err = ctx.record_error(row["row_id"], e)
                    cand["checks"].append({**entry, "result": "error", "answer": None, "error": err})
                    continue
                ans = answer_from(resp)
                result = compare(ans["answer"], c["want"])
                cand["checks"].append({**entry, "result": result, **ans,
                                       "error": ans["answer_error"] if result == "error" else None})
            cand["selection"], cand["selection_reason"] = _select(cand["checks"], mop)
            cand["notes"] = _disagreements(cand, queries)

        fetched = 0
        for cand in candidates:
            cand.update({"details_status": "not_selected", "person": None, "contact": None,
                         "current_company_matches_account": None, "details_error": None})
            if cand["selection"] != "selected":
                continue
            if fetched >= cfg["max_details_per_account"]:
                cand["details_status"] = "not_fetched_limit"
                continue
            if ctx.stopped is not None:
                cand["details_status"] = "not_requested"
                continue
            fetched += 1
            try:
                person = client.get_person(id=cand["person_id"])
            except NotFoundError:
                cand["details_status"] = "not_found"
                continue
            except FirmableError as e:
                cand["details_status"] = "error"
                cand["details_error"] = ctx.record_error(row["row_id"], e)
                continue
            cand["details_status"] = "fetched"
            cand["person"] = person_facts(person)
            cand["contact"] = contact_details(person)
            cur = cand["person"]["current_company_id"]
            cand["current_company_matches_account"] = "unknown" if not cur else ("yes" if cur == company_id else "no")
            if cand["current_company_matches_account"] == "no":
                cand["notes"].append(f"The person profile shows current company {cand['person']['current_company_id']}, "
                                     f"not the account {company_id}. Both are shown as returned.")

        selected = sum(1 for c in candidates if c["selection"] == "selected")
        got = sum(1 for c in candidates if c["details_status"] == "fetched")
        failed = any(x["status"] in ("error", "not_requested") for x in searches)
        retrieved = sum(x["retrieved"] for x in searches)
        totals = [x["total"] for x in searches]
        if all(t is not None for t in totals):
            cover = f" Searches retrieved {retrieved} of {sum(totals)} record(s) the API reported."
        else:
            cover = f" Searches retrieved {retrieved} record(s); the API did not report a total for every search."
        if no_id:
            cover += f" {no_id} search record(s) had no person ID and were left out."
        partial = any(c["selection"] == "incomplete" or c["details_status"] in ("error", "not_requested")
                      for c in candidates)
        if failed or partial:
            status = "incomplete"
            reason = ("A search, check or profile request has no result because of an error or because the run "
                      f"stopped. {selected} of {len(candidates)} candidate(s) selected; person profiles retrieved for {got}.")
        elif not candidates:
            status = "no_candidates"
            reason = "People search returned no candidates for the configured searches."
        elif selected:
            status = "contacts_selected"
            reason = f"{selected} of {len(candidates)} candidate(s) selected; person profiles retrieved for {got}."
        else:
            status = "no_contacts_selected"
            reason = f"{len(candidates)} candidate(s) found; none selected."
        reason += _counts_text(candidates) + cover
        return {**base, "status": status, "status_reason": reason, "searches": searches, "candidates": candidates,
                "candidates_found": len(candidates), "candidates_selected": selected, "profiles_fetched": got}

    rows = ctx.run_rows(ctx.rows, one)
    if empty_search_seen[0]:
        coverage.append("A people search with no matches returns success false without a records list; it is "
                        "counted as zero matches.")
    counts: dict[str, int] = {}
    sel_counts = {k: 0 for k in SELECTIONS}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        for c in r.get("candidates") or []:
            sel_counts[c["selection"]] += 1
    return {"rows": rows, "summary": {"status_counts": counts, "selection_counts": sel_counts}, "coverage": coverage}


CSV_COLUMNS = ["row_id", "domain", "company_id", "company_name", "account_status", "account_status_reason",
               "search_total", "search_retrieved", "person_id", "name", "position", "matched_queries", "selection",
               "selection_reason", "check_results", "details_status", "work_emails", "personal_emails", "phones",
               "current_company_matches_account", "notes"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        searches = r.get("searches") or []
        totals = [x["total"] for x in searches]
        account = {
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "company_id": r.get("company_id") or r["input"].get("company_id"),
            "company_name": (r.get("company") or {}).get("name"), "account_status": r["status"],
            "account_status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "search_total": sum(totals) if searches and all(t is not None for t in totals) else None,
            "search_retrieved": sum(x["retrieved"] for x in searches) if searches else None,
        }
        cands = r.get("candidates") or []
        if not cands:
            out.append(account)
            continue
        for c in cands:
            contact = c.get("contact") or {}
            out.append({**account, "person_id": c["person_id"], "name": c.get("name"), "position": c.get("position"),
                        "matched_queries": c["matched_queries"], "selection": c["selection"],
                        "selection_reason": c["selection_reason"],
                        "check_results": [f"{x['check_id']}={x['result']}({x.get('answer') or '-'})" for x in c["checks"]],
                        "details_status": c["details_status"], "work_emails": contact.get("work_emails") or [],
                        "personal_emails": contact.get("personal_emails") or [],
                        "phones": [_phone_text(p) for p in contact.get("phones") or []],
                        "current_company_matches_account": c.get("current_company_matches_account"),
                        "notes": c.get("notes") or []})
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    sel = result["summary"].get("selection_counts", {})
    report.add("Result", "Contact finder summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", SELECTION_TONE.get(k, "neutral"))
                                  for k, v in sel.items() if v) + "</p>"
               + note("Unknown means Firmable does not hold the fact. A candidate with an unknown mandatory check "
                      "<strong>needs review</strong> and is never selected automatically.", "unknown"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = (r.get("company") or {}).get("name") or r.get("domain") or r.get("company_id") or r["row_id"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        if r.get("company_id"):
            left += kv([("Firmable company ID", r.get("company_id")),
                        ("ID from", "input file" if r.get("company_id_source") == "input" else "company lookup"),
                        ("Domain", (r.get("company") or {}).get("fqdn") or r.get("domain"))])
        if r.get("duplicate_of"):
            left += note(f"Same account as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        searches = r.get("searches") or []
        if searches:
            left += "<h3>Search coverage</h3>" + table(
                ["Search", "Filters", "Total reported", "Retrieved", "Pages", "Complete", "Status"],
                [[x["label"], x["filters"], x["total"] if x["total"] is not None else "not reported", x["retrieved"],
                  x["pages_fetched"], "yes" if x["complete"] else "no", x["status"].replace("_", " ")]
                 for x in searches])
        right = ""
        cands = r.get("candidates") or []
        if cands:
            right += table(["Name", "Position", "Selection", "Checks", "Profile"], [
                [c.get("name"), c.get("position"),
                 Raw(badge(c["selection"].replace("_", " "), SELECTION_TONE.get(c["selection"], "neutral"))),
                 Raw(" ".join(badge(f"{x['label']}: {x.get('answer') or x['result']}",
                                    RESULT_TONE.get(x["result"], "neutral")) for x in c["checks"])),
                 c["details_status"].replace("_", " ")] for c in cands])
            for c in cands:
                if c["selection"] != "selected" and not c.get("notes"):
                    continue
                right += f"<h3>{esc(c.get('name') or c['person_id'])}</h3><p>{esc(c['selection_reason'])}</p>"
                contact = c.get("contact")
                if contact:
                    right += kv([("Work email", ", ".join(contact["work_emails"]) or "none returned"),
                                 ("Personal email", ", ".join(contact["personal_emails"]) or "none returned"),
                                 ("Phone", ", ".join(_phone_text(p) for p in contact["phones"]) or "none returned"),
                                 ("Current company matches account", c.get("current_company_matches_account"))])
                for x in c["checks"]:
                    rs = x.get("reasoning") or {}
                    if rs.get("reason"):
                        right += (f"<p class=\"muted\"><strong>{esc(x['label'])}:</strong> {esc(rs.get('reason'))} "
                                  f"Source: {esc(rs.get('source'))}. Last refreshed: {esc(rs.get('last_refreshed'))}.</p>")
                if c.get("notes"):
                    right += note(bullet_list(c["notes"]), "review")
            right += details_json("Candidates as JSON", cands)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No candidates for this account.</p>", sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its candidates", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
