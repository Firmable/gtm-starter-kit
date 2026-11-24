"""Inbound lead router: routes an inbound lead to an owner or queue with ordered, explicit rules.

Workflow for each lead:
1. Classify the identifier. The domain comes from the `domain` column, or from the work email when
   no domain is given. When both are given and differ, the given domain is used and the lead is flagged.
2. A lead whose domain is in `personal_email_domains` goes to the personal email queue. No API call is made.
3. Fetch the company profile (GET /company by fqdn). No profile sends the lead to the review queue.
4. When `icp` is configured, ask `is_icp_fit` (POST /decisions) once for the company.
5. When `fetch_person` is true and a work email is given, fetch the person (GET /people by work_email).
6. Evaluate every rule in order. The first rule that is not ruled out decides the lead.

Each condition is met, not_met, missing (the fact was not returned, or is_icp_fit answered unknown),
error (the request for the fact failed) or not_asked (the run stopped). A rule is not_matched when a
condition is not met, error when a condition has an error or was not asked, skipped_missing_fact
when a condition's fact is missing, and matched when every condition is met.

Lead status:
- routed: the first rule that is not not_matched matched;
- needs_review: that rule was skipped because a fact was missing, so it might have matched;
- error: that rule needs a fact whose request failed, or the company request failed;
- routed_default: every rule is not_matched;
- personal_email, company_not_found, invalid_input and not_processed are set before the rules.
Nothing is written to a CRM.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts, person_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "inbound-lead-router"
TITLE = "Inbound lead router"
DESCRIPTION = ("Routes each inbound lead from a work email or domain to an owner or queue by applying ordered "
               "rules to the Firmable company profile, an optional ICP fit answer and an optional person "
               "profile. Leads the rules cannot settle go to a review queue. Nothing is written to a CRM.")

ICP_TOOL = "is_icp_fit"
STATUS_TONE = {"routed": "good", "routed_default": "good", "needs_review": "review", "personal_email": "neutral",
               "company_not_found": "unknown", "invalid_input": "error", "error": "error",
               "not_processed": "unknown"}
OUTCOME_TONE = {"matched": "good", "not_matched": "neutral", "skipped_missing_fact": "unknown", "error": "error"}
REVIEW_STATUSES = ("needs_review", "company_not_found", "invalid_input", "error")

_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_TOP_KEYS = {"personal_email_domains", "icp", "fetch_person", "rules", "review_queue", "default_queue",
             "personal_email_queue", "with_reasoning"}
_QUEUE_KEYS = ["review_queue", "default_queue", "personal_email_queue"]
_RULE_KEYS = {"id", "queue", "when"}
_COND_ORDER = ["hq_country_in", "employee_count_min", "employee_count_max", "industries_any", "icp_fit",
               "seniority_in"]
_COND_FIELD = {"hq_country_in": "hq_country", "employee_count_min": "employee_count",
               "employee_count_max": "employee_count", "industries_any": "industries", "icp_fit": "icp_fit",
               "seniority_in": "seniority"}
_LIST_CONDS = {"hq_country_in", "industries_any", "seniority_in"}
_ICP_ORDER = ["min_staff", "max_staff", "staff_in", "industries", "countries", "revenue_bands", "criteria"]
_STAFF_IN = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
_REVENUE_BANDS = ["<$1M", "$1M - $5M", "$5M - $10M", "$10M - $50M", "$50M - $100M", "$100M - $250M",
                  "$250M - $500M", "$500M - $1B", ">$1B+"]
_ID_RULE = ("must start with a lower-case letter and use only lower-case letters, digits or underscores, "
            "up to 40 characters.")


def _unknown_keys(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ConfigError(f"{where} has unknown key(s): {', '.join(extra)}")


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _text_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x.strip() for x in v)


def _load_icp(icp: Any) -> dict[str, Any]:
    if not isinstance(icp, dict):
        raise ConfigError("icp must be an object.")
    if "description" in icp:
        raise ConfigError("icp.description is not accepted by is_icp_fit. Give icp.criteria instead.")
    _unknown_keys(icp, set(_ICP_ORDER), "icp")
    out: dict[str, Any] = {}
    for key in _ICP_ORDER:
        if key not in icp or icp[key] is None:
            continue
        value = icp[key]
        if key in ("min_staff", "max_staff"):
            if not _is_int(value) or value < 0:
                raise ConfigError(f"icp.{key} must be a whole number, 0 or more.")
        elif key == "staff_in":
            if value not in _STAFF_IN:
                raise ConfigError(f"icp.staff_in must be one of: {', '.join(_STAFF_IN)}.")
        elif key == "criteria":
            if (not isinstance(value, list) or not value or len(value) > 10
                    or not all(isinstance(c, str) and len(c.strip()) >= 2 for c in value)):
                raise ConfigError("icp.criteria must be a list of 1 to 10 text items, each at least 2 characters.")
            if sum(len(c) for c in value) > 2000:
                raise ConfigError("icp.criteria must be 2,000 characters or fewer in all.")
        else:
            if not _text_list(value):
                raise ConfigError(f"icp.{key} must be a non-empty list of text values.")
            if key == "revenue_bands":
                for band in value:
                    if band not in _REVENUE_BANDS:
                        raise ConfigError(f"icp.revenue_bands has {band!r}, which is not a Firmable revenue band.")
        out[key] = value
    if "min_staff" in out and "max_staff" in out and out["min_staff"] > out["max_staff"]:
        raise ConfigError("icp.min_staff is more than icp.max_staff.")
    if not out:
        raise ConfigError("icp must hold at least one filter or criterion.")
    return out


def _load_when(when: Any, where: str, has_icp: bool, fetch_person: bool) -> dict[str, Any]:
    if not isinstance(when, dict) or not when:
        raise ConfigError(f"{where} must be an object with at least one condition.")
    _unknown_keys(when, set(_COND_ORDER), where)
    out: dict[str, Any] = {}
    for key in _COND_ORDER:
        if key not in when:
            continue
        value = when[key]
        if key in _LIST_CONDS:
            if not _text_list(value):
                raise ConfigError(f"{where}.{key} must be a non-empty list of text values.")
            if key == "seniority_in" and not fetch_person:
                raise ConfigError(f"{where}.seniority_in needs fetch_person set to true.")
        elif key == "icp_fit":
            if value not in ("yes", "no", "unknown"):
                raise ConfigError(f"{where}.icp_fit must be yes, no or unknown.")
            if not has_icp:
                raise ConfigError(f"{where}.icp_fit needs icp in the configuration.")
        elif not _is_int(value) or value < 0:
            raise ConfigError(f"{where}.{key} must be a whole number, 0 or more.")
        out[key] = value
    if "employee_count_min" in out and "employee_count_max" in out \
            and out["employee_count_min"] > out["employee_count_max"]:
        raise ConfigError(f"{where}.employee_count_min is more than employee_count_max.")
    return out


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    _unknown_keys(raw, _TOP_KEYS, "The configuration")
    queues: dict[str, str] = {}
    for key in _QUEUE_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a non-empty queue name.")
        queues[key] = value
    personal_raw = raw.get("personal_email_domains", [])
    if not isinstance(personal_raw, list):
        raise ConfigError("personal_email_domains must be a list of domains.")
    personal: list[str] = []
    for i, d in enumerate(personal_raw, start=1):
        nd = normalise_domain(d) if isinstance(d, str) else None
        if not nd:
            raise ConfigError(f"personal_email_domains[{i}] is not a valid domain.")
        if nd not in personal:
            personal.append(nd)
    icp = _load_icp(raw["icp"]) if raw.get("icp") is not None else None
    fetch_person = raw.get("fetch_person", False)
    if not isinstance(fetch_person, bool):
        raise ConfigError("fetch_person must be true or false.")
    with_reasoning = raw.get("with_reasoning", True)
    if not isinstance(with_reasoning, bool):
        raise ConfigError("with_reasoning must be true or false.")

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError("rules must be a non-empty list.")
    rules: list[dict[str, Any]] = []
    rule_ids: set[str] = set()
    for i, r in enumerate(rules_raw, start=1):
        if not isinstance(r, dict):
            raise ConfigError(f"rules[{i}] must be an object.")
        _unknown_keys(r, _RULE_KEYS, f"rules[{i}]")
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID.match(rid):
            raise ConfigError(f"rules[{i}].id {_ID_RULE}")
        if rid in rule_ids:
            raise ConfigError(f"rules[{i}].id {rid!r} is used twice.")
        rule_ids.add(rid)
        queue = r.get("queue")
        if not isinstance(queue, str) or not queue.strip():
            raise ConfigError(f"rules[{i}].queue must be a non-empty queue name.")
        when = _load_when(r.get("when"), f"rules[{i}].when", icp is not None, fetch_person)
        rules.append({"id": rid, "queue": queue, "when": when})
    return {"personal_email_domains": personal, "icp": icp, "fetch_person": fetch_person, "rules": rules,
            **queues, "with_reasoning": with_reasoning}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=["work_email", "domain"])


def identify(row: dict[str, str], personal: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Returns (identifier, problem). problem explains an invalid work email or domain."""
    raw_email = (row.get("work_email") or "").strip()
    raw_domain = (row.get("domain") or "").strip()
    email = None
    email_domain = None
    if raw_email:
        if not _EMAIL.match(raw_email):
            return None, f"work_email {raw_email!r} is not an email address."
        email_domain = normalise_domain(raw_email.rsplit("@", 1)[1])
        if not email_domain:
            return None, f"work_email {raw_email!r} does not end in a valid domain."
        email = raw_email.lower()
    given = None
    if raw_domain:
        given = normalise_domain(raw_domain)
        if not given:
            return None, f"domain {raw_domain!r} is not a valid domain."
    domain = given or email_domain
    return {"work_email": email, "email_domain": email_domain, "given_domain": given, "domain": domain,
            "domain_source": "domain" if given else "work_email",
            "work_email_personal": email_domain in personal if email_domain else False,
            "domain_personal": domain in personal}, None


def _row_key(row: dict[str, str], personal: list[str]) -> str:
    ident, _ = identify(row, personal)
    return f"{ident['work_email'] or ''}|{ident['domain']}" if ident else ""


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    domains: list[str] = []
    emails: list[str] = []
    for r in rows:
        ident, _ = identify(r, config["personal_email_domains"])
        if not ident or ident["domain_personal"]:
            continue
        if ident["domain"] not in domains:
            domains.append(ident["domain"])
        if ident["work_email"] and not ident["work_email_personal"] and ident["work_email"] not in emails:
            emails.append(ident["work_email"])
    n = len(domains)
    calls = [PlannedCall("GET /company", "Company profile for each unique business domain", n,
                         [("company", pricing.round4(pricing.COMPANY_PROFILE * n))])]
    if config["icp"] is not None:
        calls.append(PlannedCall("POST /decisions", "is_icp_fit for each unique business domain", n,
                                 pricing.estimate_decision(n, config["with_reasoning"])))
    if config["fetch_person"]:
        m = len(emails)
        calls.append(PlannedCall("GET /people", "Person profile for each unique work email", m,
                                 [("person", pricing.round4(pricing.PERSON_PROFILE * m))]))
    return calls


def _norm(s: str) -> str:
    return s.strip().lower()


def _text(v: Any) -> str:
    if v is None:
        return "not returned"
    if isinstance(v, list):
        return ", ".join(_text(x) for x in v) or "none"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _condition(key: str, expected: Any, company: dict[str, Any], icp: dict[str, Any] | None,
               person: dict[str, Any] | None) -> dict[str, Any]:
    field = _COND_FIELD[key]
    observed: Any = None
    if key in ("hq_country_in", "industries_any"):
        observed = company.get(field)
        values = observed if isinstance(observed, list) else [observed]
        values = [v for v in values if isinstance(v, str) and v.strip()]
        if not values:
            result = "missing"
        else:
            wanted = {_norm(e) for e in expected}
            result = "met" if any(_norm(v) in wanted for v in values) else "not_met"
    elif key in ("employee_count_min", "employee_count_max"):
        observed = company.get(field)
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            result = "missing"
        elif key == "employee_count_min":
            result = "met" if observed >= expected else "not_met"
        else:
            result = "met" if observed <= expected else "not_met"
    elif key == "icp_fit":
        rec = icp or {}
        observed = rec.get("answer")
        if not rec.get("asked"):
            result = "not_asked"
        elif rec.get("error") is not None:
            result = "error"
        elif observed == expected:
            result = "met"
        elif observed == "unknown":
            result = "missing"
        else:
            result = "not_met"
    else:
        rec = person or {}
        observed = (rec.get("facts") or {}).get("seniority")
        status = rec.get("status")
        if status == "error":
            result = "error"
        elif status == "not_asked":
            result = "not_asked"
        elif status in ("no_work_email", "personal_email"):
            result = "no_person"
        elif not isinstance(observed, str) or not observed.strip():
            result = "missing"
        else:
            wanted = {_norm(e) for e in expected}
            result = "met" if _norm(observed) in wanted else "not_met"
    return {"condition": key, "field": field, "expected": expected, "observed": observed, "result": result}


def _missing_label(c: dict[str, Any]) -> str:
    if c["condition"] == "icp_fit":
        return "icp_fit (is_icp_fit answered unknown)"
    return c["field"]


def _evaluate(rule: dict[str, Any], company: dict[str, Any], icp: dict[str, Any] | None,
              person: dict[str, Any] | None) -> dict[str, Any]:
    conds = [_condition(k, rule["when"][k], company, icp, person) for k in _COND_ORDER if k in rule["when"]]
    results = [c["result"] for c in conds]
    failing = None
    missing: list[str] = []
    if "not_met" in results or "no_person" in results:
        first = next(c for c in conds if c["result"] in ("not_met", "no_person"))
        failing = first["condition"]
        outcome = "not_matched"
        if first["result"] == "no_person":
            reason = (f"{first['condition']} does not apply: the lead has no business work email, "
                      "so there is no person to check.")
        else:
            reason = (f"{first['condition']} not met: wanted {_text(first['expected'])}, "
                      f"observed {_text(first['observed'])}.")
    elif "error" in results or "not_asked" in results:
        bad = [c["field"] for c in conds if c["result"] in ("error", "not_asked")]
        outcome = "error"
        reason = f"Not evaluated: the request for {', '.join(bad)} failed or was not made."
    elif "missing" in results:
        missing = [_missing_label(c) for c in conds if c["result"] == "missing"]
        outcome = "skipped_missing_fact"
        reason = f"Skipped because these facts were missing: {', '.join(missing)}."
    else:
        outcome = "matched"
        reason = "All conditions met."
    return {"rule_id": rule["id"], "queue": rule["queue"], "outcome": outcome, "failing_condition": failing,
            "missing_facts": missing, "reason": reason, "conditions": conds}


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    personal = cfg["personal_email_domains"]
    coverage = [
        "Rules are read in order. The first rule that is not ruled out decides the lead.",
        "A condition on a missing fact does not match. When the deciding rule was skipped for a missing fact, "
        "the lead goes to the review queue, because that rule might have matched.",
        "Personal email domains are matched against the configured personal_email_domains list only.",
        "Routes are written to this run's output files only. Nothing is written to a CRM.",
    ]
    if cfg["icp"] is not None:
        coverage.append("is_icp_fit uses a decision model inside Firmable's service (uses_decision_model is true "
                        "in the tool list read on 24 Nov 2026). This code applies only the rules in the "
                        "configuration.")
        coverage.append("An is_icp_fit answer of unknown never matches an icp_fit condition of yes or no. "
                        "It is never treated as no.")
    if cfg["fetch_person"]:
        coverage.append("Person profiles are looked up by work email only. A lead with no work email, or a "
                        "work email on a personal domain, has no person facts.")

    for r in ctx.rows:
        r["_key"] = _row_key(r, personal)
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} lead(s) repeat an earlier work email and domain; identical requests were "
                        "answered from memory, not sent again.")

    def one(row: dict[str, str]) -> dict[str, Any]:
        row_id = row["row_id"]
        ident, problem = identify(row, personal)
        base: dict[str, Any] = {
            "row_id": row_id, "input": row, "duplicate_of": dupes.get(row_id), "identifier": ident,
            "company": None, "icp": None, "person": None, "rules": [], "matched_rule": None, "queue": None,
            "flags": []}
        if ident is None:
            return {**base, "status": "invalid_input", "status_reason": problem, "queue": cfg["review_queue"]}
        domain = ident["domain"]
        flags: list[str] = []
        if ident["given_domain"] and ident["email_domain"] and ident["given_domain"] != ident["email_domain"]:
            flags.append(f"The given domain {ident['given_domain']} differs from the work email domain "
                         f"{ident['email_domain']}. The given domain was used.")
        base["flags"] = flags
        if ident["domain_personal"]:
            return {**base, "status": "personal_email", "queue": cfg["personal_email_queue"],
                    "status_reason": f"{domain} is a personal email domain in personal_email_domains, so no "
                                     "API call was made."}
        if ident["work_email_personal"]:
            flags.append(f"The work email uses the personal email domain {ident['email_domain']}. The lead was "
                         "routed on the given domain.")
        try:
            company = client.get_company(fqdn=domain)
        except NotFoundError:
            return {**base, "status": "company_not_found", "queue": cfg["review_queue"],
                    "status_reason": "Firmable has no company profile for this domain, so no rules were evaluated."}
        facts = company_facts(company) or {}
        base["company"] = facts
        profile_domain = normalise_domain(company.get("fqdn") if isinstance(company.get("fqdn"), str) else None)
        if profile_domain and profile_domain != domain:
            flags.append(f"The company profile's fqdn {profile_domain} differs from the domain looked up, {domain}.")

        icp: dict[str, Any] | None = None
        if cfg["icp"] is not None:
            if ctx.stopped is not None:
                icp = {"tool": ICP_TOOL, "asked": False, "answer": None, "error": None}
            else:
                try:
                    resp = client.make_decision(ICP_TOOL, {"company_id": company["id"], "icp": cfg["icp"]},
                                                with_reasoning=cfg["with_reasoning"])
                except FirmableError as e:
                    icp = {"tool": ICP_TOOL, "asked": True, "answer": None, "error": ctx.record_error(row_id, e)}
                else:
                    ans = answer_from(resp)
                    bad = ans["answer"] not in ("yes", "no", "unknown")
                    icp = {"tool": ICP_TOOL, "asked": True, **ans, "error": ans["answer_error"] if bad else None}
        base["icp"] = icp

        person: dict[str, Any] | None = None
        if cfg["fetch_person"]:
            person = {"status": None, "work_email": ident["work_email"], "facts": None, "error": None}
            if not ident["work_email"]:
                person["status"] = "no_work_email"
            elif ident["work_email_personal"]:
                person["status"] = "personal_email"
            elif ctx.stopped is not None:
                person["status"] = "not_asked"
            else:
                try:
                    p = client.get_person(work_email=ident["work_email"])
                except NotFoundError:
                    person["status"] = "not_found"
                except FirmableError as e:
                    person["status"] = "error"
                    person["error"] = ctx.record_error(row_id, e)
                else:
                    person["status"] = "found"
                    person["facts"] = person_facts(p)
                    current = person["facts"].get("current_company_id")
                    if current and current != company["id"]:
                        flags.append(f"The person's current company in Firmable ({current}) is not the company "
                                     f"found for the domain ({company['id']}).")
        base["person"] = person

        evaluated = [_evaluate(rule, facts, icp, person) for rule in cfg["rules"]]
        out = {**base, "rules": evaluated}
        decider = next((r for r in evaluated if r["outcome"] != "not_matched"), None)
        if decider is None:
            return {**out, "status": "routed_default", "queue": cfg["default_queue"],
                    "status_reason": "No rule matched; routed to the default queue."}
        if decider["outcome"] == "matched":
            return {**out, "status": "routed", "queue": decider["queue"], "matched_rule": decider["rule_id"],
                    "status_reason": f"Rule {decider['rule_id']} matched; routed to {decider['queue']}."}
        if decider["outcome"] == "skipped_missing_fact":
            return {**out, "status": "needs_review", "queue": cfg["review_queue"],
                    "status_reason": f"Rule {decider['rule_id']} was skipped because these facts were missing: "
                                     f"{', '.join(decider['missing_facts'])}. It might have matched, so the lead "
                                     "goes to the review queue rather than to a later rule or the default queue."}
        return {**out, "status": "error", "queue": cfg["review_queue"],
                "status_reason": f"Rule {decider['rule_id']} could not be evaluated because a request failed or "
                                 "was not made. The lead goes to the review queue."}

    rows = ctx.run_rows(ctx.rows, one)
    for r in rows:
        if "queue" not in r:
            r["queue"] = cfg["review_queue"] if r["status"] == "error" else None
    counts: dict[str, int] = {}
    queue_counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {r["id"]: 0 for r in cfg["rules"]}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["queue"] is not None:
            queue_counts[r["queue"]] = queue_counts.get(r["queue"], 0) + 1
        if r.get("matched_rule"):
            rule_counts[r["matched_rule"]] += 1
    flagged = sum(1 for r in rows if r.get("flags"))
    if flagged:
        coverage.append(f"{flagged} lead(s) have flags where identifiers or profile facts disagree. The recipe "
                        "reports them and does not correct upstream data.")
    review = [r["row_id"] for r in rows if r["status"] in REVIEW_STATUSES]
    return {"rows": rows, "summary": {"status_counts": counts, "queue_counts": queue_counts,
                                      "rule_match_counts": rule_counts, "review_rows": review},
            "coverage": coverage}


CSV_COLUMNS = ["row_id", "input_work_email", "input_domain", "lead_name", "source", "duplicate_of", "status",
               "status_reason", "queue", "matched_rule", "domain_used", "domain_source", "company_id",
               "company_name", "hq_country", "employee_count", "industries", "icp_fit", "person_status",
               "person_seniority", "flags", "rule_results"]


def _reason(r: dict[str, Any]) -> Any:
    return r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message")


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        ident = r.get("identifier") or {}
        person = r.get("person") or {}
        out.append({
            "row_id": r["row_id"], "input_work_email": r["input"].get("work_email"),
            "input_domain": r["input"].get("domain"), "lead_name": r["input"].get("lead_name"),
            "source": r["input"].get("source"), "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": _reason(r), "queue": r.get("queue"), "matched_rule": r.get("matched_rule"),
            "domain_used": ident.get("domain"), "domain_source": ident.get("domain_source"),
            "company_id": company.get("id"), "company_name": company.get("name"),
            "hq_country": company.get("hq_country"), "employee_count": company.get("employee_count"),
            "industries": company.get("industries") if isinstance(company.get("industries"), list) else [],
            "icp_fit": (r.get("icp") or {}).get("answer"), "person_status": person.get("status"),
            "person_seniority": (person.get("facts") or {}).get("seniority"),
            "flags": r.get("flags") or [],
            "rule_results": [f"{x['rule_id']}={x['outcome']}" for x in r.get("rules") or []],
        })
    return out


def _cond_text(c: dict[str, Any]) -> str:
    return (f"{c['condition']}: wanted {_text(c['expected'])}, observed {_text(c['observed'])}, "
            f"{c['result'].replace('_', ' ')}")


def render(report: Any, result: dict[str, Any]) -> None:
    summary = result["summary"]
    counts = summary.get("status_counts", {})
    body = ("<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                             for k, v in counts.items()) + "</p>")
    if summary.get("queue_counts"):
        body += table(["Queue", "Leads"], [[k, v] for k, v in summary["queue_counts"].items()])
    if summary.get("rule_match_counts"):
        body += table(["Rule", "Leads routed"], [[k, v] for k, v in summary["rule_match_counts"].items()])
    body += note("A condition on a missing fact does not match. When the rule that would decide a lead was "
                 "skipped for a missing fact, the lead goes to the <strong>review queue</strong>. An is_icp_fit "
                 "answer of unknown is never treated as no.", "unknown")
    report.add("Result", "Routing summary", body)

    review = [r for r in result["rows"] if r["status"] in REVIEW_STATUSES]
    if review:
        report.add("Review queue", "Leads a person needs to route", table(
            ["Row", "Lead", "Status", "Reason"],
            [[r["row_id"], r["input"].get("lead_name") or r["input"].get("work_email") or r["input"].get("domain"),
              r["status"].replace("_", " "), _reason(r)] for r in review]))

    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = r["input"].get("lead_name") or r["input"].get("work_email") or r["input"].get("domain") or r["row_id"]
        left = f"<p>{esc(_reason(r) or '')}</p>"
        ident = r.get("identifier") or {}
        left += kv([("Queue", r.get("queue")), ("Matched rule", r.get("matched_rule")),
                    ("Source", r["input"].get("source")), ("Domain used", ident.get("domain")),
                    ("Domain taken from", ident.get("domain_source"))])
        company = r.get("company")
        if company:
            left += "<h3>Company facts from Firmable</h3>" + kv([
                ("Firmable ID", company.get("id")), ("Name", company.get("name")), ("Domain", company.get("fqdn")),
                ("Head office country", company.get("hq_country")), ("Employee count", company.get("employee_count")),
                ("Size", company.get("company_size")),
                ("Industries", ", ".join(company.get("industries") or []) or None)])
        person = r.get("person")
        if person:
            pf = person.get("facts") or {}
            left += "<h3>Person</h3>" + kv([("Lookup", person.get("status", "").replace("_", " ")),
                                            ("Position", pf.get("position")), ("Seniority", pf.get("seniority")),
                                            ("Department", pf.get("department"))])
        if r.get("flags"):
            left += "<h3>Flags</h3>" + bullet_list(r["flags"])
        if r.get("duplicate_of"):
            left += note(f"Same work email and domain as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        icp = r.get("icp")
        if icp:
            rs = icp.get("reasoning") or {}
            right += (f"<p><strong>ICP fit</strong> ({esc(icp['tool'])}): "
                      f"{esc(icp.get('answer') or ('not asked' if not icp.get('asked') else 'no answer'))}"
                      + (f", confidence {esc(icp.get('confidence'))}" if icp.get("confidence") is not None else "")
                      + "</p>")
            if rs.get("reason"):
                right += (f"<p>{esc(rs.get('reason'))}</p><p class=\"muted\">Source: {esc(rs.get('source'))}. "
                          f"Last refreshed: {esc(rs.get('last_refreshed'))}."
                          + (f" Would change if: {esc(rs.get('would_change_if'))}." if rs.get("would_change_if") else "")
                          + "</p>")
            if icp.get("error"):
                err = icp["error"]
                right += note(f"is_icp_fit: {esc(err.get('message') if isinstance(err, dict) else err)}", "error")
        if person and person.get("error"):
            right += note(f"GET /people: {esc(person['error'].get('message'))}", "error")
        rules = r.get("rules") or []
        if rules:
            right += table(["Rule", "Queue", "Outcome", "Conditions"], [
                [x["rule_id"], x["queue"],
                 Raw(badge(x["outcome"].replace("_", " "), OUTCOME_TONE.get(x["outcome"], "neutral"))),
                 Raw(f"<p>{esc(x['reason'])}</p>" + bullet_list([_cond_text(c) for c in x["conditions"]]))]
                for x in rules])
            right += details_json("Rule results as JSON", rules)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No rules were evaluated for this lead.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Leads", "Each lead with its route", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
