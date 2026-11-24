"""Buying committee map: shows who fills each buying-committee role at each account, and which roles have no one observed.

Workflow for each row:
1. Use the row's company_id, or resolve the domain to a Firmable company with GET /company.
2. Run one people search (POST /people/search) with the configured filters, up to search.max_people people.
3. For each unique person, ask matches_department with the union of every role's departments and
   matches_seniority with the union of every role's seniorities.
4. Assign roles with the rules below.
5. When decision_maker is configured, ask is_decision_maker for each person who fills a role.

Role rules for one person and one role. Each answer gives one side:
- yes: the answer is yes and the returned details value is in the role's list (exact match after trimming);
- no: the answer is no, or the answer is yes and the returned value is not in the role's list;
- unclear: the answer is unknown, or the answer is yes without a returned value;
- error: the question has no answer because of an error or because the run stopped.
The person fills the role when both sides are yes. Either side no: the person cannot fill the role.
Otherwise an error side makes the role undecided for that person (error), and an unclear side makes the
person unclassified for the role (possible).

Role status per account, first that applies: filled, incomplete, possible, not_observed.
Account status, first that applies: no_people_found, committee_complete (every required role filled),
gaps_found (a required role not observed), incomplete (a required role undecided because of an error),
needs_review (a required role only possible).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, compare, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "buying-committee-map"
TITLE = "Buying committee map"
DESCRIPTION = ("Finds people at each account with Firmable people search, asks Decisions for each person's department "
               "and seniority, and maps them to the buying-committee roles you define. Roles with no one observed "
               "are listed as gaps.")

COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
# Seniority labels accepted by matches_seniority, from the decision tool catalogue of 24 Nov 2026.
SENIORITY_LABELS = ["Owner / Founder", "C-Suite / Partner", "Board Member / Company Director",
                    "VP/Director / Head of", "Manager", "Other"]
# People search ids and labels from the published people search reference.
SEARCH_SENIORITY = {"1": "Board Member / Company Director", "2": "Owner / Founder", "3": "C-Suite / Partner",
                    "4": "VP/Director / Head of", "5": "Manager", "6": "Other", "7": "No Data Available"}
SEARCH_DEPARTMENT = {"1": "General Management", "2": "Sales", "3": "Trades", "4": "Operations",
                     "5": "Engineering & Technical", "6": "Human Resources", "7": "Customer Service",
                     "8": "Medicine & Healthcare", "9": "Research & Analysis", "10": "Legal", "11": "Marketing",
                     "12": "Education & Training", "13": "Consulting", "14": "Finance", "15": "Product",
                     "16": "Other", "17": "No Data Available"}
MAX_LIST = 20  # maxItems for departments and seniorities in the decision tool input schemas
ROLE_STATUSES = ["filled", "incomplete", "possible", "not_observed"]
PERSON_STATUSES = ["assigned", "unclassified", "incomplete", "no_role", "person_not_found"]
STATUS_TONE = {"committee_complete": "good", "gaps_found": "bad", "needs_review": "review", "incomplete": "error",
               "no_people_found": "unknown", "company_not_found": "unknown", "invalid_input": "error",
               "error": "error", "not_processed": "unknown"}
ROLE_TONE = {"filled": "good", "incomplete": "error", "possible": "review", "not_observed": "bad"}
PERSON_TONE = {"assigned": "good", "unclassified": "review", "incomplete": "error", "no_role": "neutral",
               "person_not_found": "unknown"}
RESULT_TONE = {"pass": "good", "fail": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral",
               "person_not_found": "unknown"}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_COMPANY_ID = re.compile(r"^f\d+$")
_TOP_KEYS = {"roles", "search", "with_reasoning", "decision_maker"}
_ROLE_KEYS = {"id", "label", "departments", "seniorities", "required"}
_SEARCH_KEYS = {"selected_country", "position", "seniority", "department", "max_people"}
_DM_KEYS = {"seller", "buyer"}


def _whole(value: Any, low: int, high: int | None) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool) and value >= low
            and (high is None or value <= high))


def _code(value: Any, table_: dict[str, str]) -> str | None:
    """Maps a documented people search id or its label to the id string."""
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


def _union(roles: list[dict[str, Any]], key: str) -> list[str]:
    out: list[str] = []
    for r in roles:
        for v in r[key]:
            if v not in out:
                out.append(v)
    return out


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list) or not 1 <= len(roles_raw) <= 10:
        raise ConfigError("roles must be a list of 1 to 10 roles.")
    roles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, r in enumerate(roles_raw, start=1):
        if not isinstance(r, dict):
            raise ConfigError(f"roles[{i}] must be an object.")
        bad = sorted(set(r) - _ROLE_KEYS)
        if bad:
            raise ConfigError(f"roles[{i}] has unknown key(s): {', '.join(bad)}")
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID.match(rid):
            raise ConfigError(f"roles[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if rid in seen:
            raise ConfigError(f"roles[{i}].id '{rid}' is used twice.")
        seen.add(rid)
        label = r.get("label", rid)
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"roles[{i}].label must be non-empty text.")
        deps = r.get("departments")
        if not isinstance(deps, list) or not deps:
            raise ConfigError(f"roles[{i}].departments must be a non-empty list.")
        departments: list[str] = []
        for d in deps:
            if not isinstance(d, str) or not d.strip() or len(d.strip()) > 100:
                raise ConfigError(f"roles[{i}].departments must hold non-empty text of up to 100 characters.")
            if d.strip() not in departments:
                departments.append(d.strip())
        sens = r.get("seniorities")
        if not isinstance(sens, list) or not sens:
            raise ConfigError(f"roles[{i}].seniorities must be a non-empty list.")
        seniorities: list[str] = []
        for s in sens:
            if not isinstance(s, str) or s.strip() not in SENIORITY_LABELS:
                raise ConfigError(f"roles[{i}].seniorities must use Firmable's seniority labels: "
                                  f"{', '.join(SENIORITY_LABELS)}.")
            if s.strip() not in seniorities:
                seniorities.append(s.strip())
        required = r.get("required", True)
        if not isinstance(required, bool):
            raise ConfigError(f"roles[{i}].required must be true or false.")
        roles.append({"id": rid, "label": label.strip(), "departments": departments, "seniorities": seniorities,
                      "required": required})
    if not any(r["required"] for r in roles):
        raise ConfigError("At least one role must be required.")
    if len(_union(roles, "departments")) > MAX_LIST:
        raise ConfigError(f"The roles name more than {MAX_LIST} departments in total; matches_department accepts "
                          f"up to {MAX_LIST}.")

    s = raw.get("search", {})
    if not isinstance(s, dict):
        raise ConfigError("search must be an object.")
    bad = sorted(set(s) - _SEARCH_KEYS)
    if bad:
        raise ConfigError(f"search has unknown key(s): {', '.join(bad)}")
    country = s.get("selected_country", "AU")
    if country not in COUNTRIES:
        raise ConfigError(f"search.selected_country must be one of: {', '.join(COUNTRIES)}.")
    position = s.get("position", "")
    if not isinstance(position, str):
        raise ConfigError("search.position must be text.")
    seniority = None
    if s.get("seniority") not in (None, ""):
        seniority = _code(s["seniority"], SEARCH_SENIORITY)
        if seniority is None:
            raise ConfigError("search.seniority must be a seniority id 1 to 7 or its label.")
    department = None
    if s.get("department") not in (None, ""):
        department = _code(s["department"], SEARCH_DEPARTMENT)
        if department is None:
            raise ConfigError("search.department must be a department id 1 to 17 or its label.")
    max_people = s.get("max_people", 25)
    if not _whole(max_people, 1, 100):
        raise ConfigError("search.max_people must be a whole number from 1 to 100.")

    with_reasoning = raw.get("with_reasoning", False)
    if not isinstance(with_reasoning, bool):
        raise ConfigError("with_reasoning must be true or false.")
    dm = raw.get("decision_maker")
    decision_maker = None
    if dm is not None:
        if not isinstance(dm, dict):
            raise ConfigError("decision_maker must be an object.")
        bad = sorted(set(dm) - _DM_KEYS)
        if bad:
            raise ConfigError(f"decision_maker has unknown key(s): {', '.join(bad)}")
        seller = dm.get("seller")
        if not isinstance(seller, str) or not 3 <= len(seller.strip()) <= 2000:
            raise ConfigError("decision_maker.seller must be text of 3 to 2000 characters.")
        buyer = dm.get("buyer")
        if buyer is not None and (not isinstance(buyer, str) or len(buyer.strip()) > 2000):
            raise ConfigError("decision_maker.buyer must be text of up to 2000 characters.")
        decision_maker = {"seller": seller.strip(), "buyer": buyer.strip() if buyer and buyer.strip() else None}
    return {"roles": roles,
            "search": {"selected_country": country, "position": position.strip() or None, "seniority": seniority,
                       "department": department, "max_people": max_people},
            "with_reasoning": with_reasoning, "decision_maker": decision_maker}


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
    mp = config["search"]["max_people"]
    per_person = 3 if config["decision_maker"] else 2
    answers = n * mp * per_person
    return [
        PlannedCall("GET /decisions", "Read the decision tool list and check the tools this recipe uses", 1, []),
        PlannedCall("GET /company", "Company profile for each unique valid domain without a company_id", domains,
                    [("company", pricing.round4(pricing.COMPANY_PROFILE * domains))]),
        PlannedCall("POST /people/search", "One search for each unique account", n, []),
        PlannedCall("POST /decisions", f"{per_person} question(s) for up to {mp} person(s) per account", answers,
                    pricing.estimate_decision(answers, config["with_reasoning"])),
    ]


def _side(check: dict[str, Any], attr: str, values: list[str]) -> str:
    """One answer's side for one role: yes, no, unclear or error."""
    if check["result"] in ("error", "not_asked", "person_not_found"):
        return "error"
    if check["result"] == "unknown":
        return "unclear"
    if check["result"] == "fail":
        return "no"
    if check["value"] is None:
        return "unclear"
    return "yes" if check["value"] in values else "no"


def _value(ans: dict[str, Any], attr: str) -> str | None:
    details = ans.get("details")
    v = details.get(attr) if isinstance(details, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def _labels(roles: list[dict[str, Any]], ids: list[str]) -> str:
    by = {r["id"]: r["label"] for r in roles}
    return ", ".join(by[i] for i in ids)


def _classify(person: dict[str, Any], roles: list[dict[str, Any]], unions: dict[str, list[str]]) -> None:
    """Assigns roles to one person in place and writes the status, reason and notes."""
    dep, sen = person["department_check"], person["seniority_check"]
    person.update({"roles_filled": [], "roles_possible": [], "roles_undecided": [], "notes": []})
    for c, attr, tool in ((dep, "department", "matches_department"), (sen, "seniority", "matches_seniority")):
        if c["result"] == "pass" and c["value"] is not None and c["value"] not in unions[attr]:
            person["notes"].append(f"{tool} answered yes but returned {attr} '{c['value']}', which is not a "
                                   f"configured {attr}. No role is assigned on it. Both are shown as returned.")
        if c["result"] == "fail" and c["value"] is not None and c["value"] in unions[attr]:
            person["notes"].append(f"{tool} answered no but returned {attr} '{c['value']}', which is a configured "
                                   f"{attr}. The answer no is used. Both are shown as returned.")
    if dep["result"] == "person_not_found" or sen["result"] == "person_not_found":
        person["status"] = "person_not_found"
        person["reason"] = ("Decisions found no Firmable person for this ID, so the remaining questions were not "
                            "asked. No role is assigned.")
        return
    for role in roles:
        d = _side(dep, "department", role["departments"])
        s = _side(sen, "seniority", role["seniorities"])
        if d == "no" or s == "no":
            continue
        if d == "yes" and s == "yes":
            person["roles_filled"].append(role["id"])
        elif "error" in (d, s):
            person["roles_undecided"].append(role["id"])
        else:
            person["roles_possible"].append(role["id"])
    if person["roles_filled"]:
        person["status"] = "assigned"
        person["reason"] = f"Fills: {_labels(roles, person['roles_filled'])}."
    elif person["roles_undecided"]:
        person["status"] = "incomplete"
        person["reason"] = ("A question has no answer because of an error or because the run stopped, so these "
                            f"roles are undecided: {_labels(roles, person['roles_undecided'])}.")
    elif person["roles_possible"]:
        why = []
        for c, attr, tool in ((dep, "department", "matches_department"), (sen, "seniority", "matches_seniority")):
            if c["result"] == "unknown":
                why.append(f"{tool} answered unknown, so Firmable does not hold the {attr}.")
            elif c["result"] == "pass" and c["value"] is None:
                why.append(f"{tool} answered yes but returned no {attr} value, so the role cannot be decided.")
        person["status"] = "unclassified"
        person["reason"] = f"Could fill: {_labels(roles, person['roles_possible'])}. {' '.join(why)}"
    else:
        person["status"] = "no_role"
        person["reason"] = "Matches no configured role on the department and seniority returned."


def _account_status(roles: list[dict[str, Any]], role_results: list[dict[str, Any]]) -> tuple[str, str]:
    req = [rr for rr, role in zip(role_results, roles) if role["required"]]
    filled = sum(1 for rr in role_results if rr["status"] == "filled")

    def names(items: list[dict[str, Any]]) -> str:
        return ", ".join(rr["label"] for rr in items)

    gaps = [rr for rr in req if rr["status"] == "not_observed"]
    undecided = [rr for rr in req if rr["status"] == "incomplete"]
    possible = [rr for rr in req if rr["status"] == "possible"]
    if all(rr["status"] == "filled" for rr in req):
        return "committee_complete", (f"Every required role is filled. {filled} of {len(roles)} role(s) filled.")
    if gaps:
        return "gaps_found", (f"Required role(s) not observed among the people retrieved: {names(gaps)}. "
                              f"{filled} of {len(roles)} role(s) filled.")
    if undecided:
        return "incomplete", ("Required role(s) are undecided because a question has an error or was not asked: "
                              f"{names(undecided)}. {filled} of {len(roles)} role(s) filled.")
    return "needs_review", (f"Required role(s) could be filled only by unclassified people: {names(possible)}. "
                            f"Unknown is not a no. {filled} of {len(roles)} role(s) filled.")


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    s = cfg["search"]
    roles = cfg["roles"]
    dm = cfg["decision_maker"]
    unions = {"department": _union(roles, "departments"), "seniority": _union(roles, "seniorities")}
    coverage = [
        "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no "
        "or as a pass.",
        "Roles are inferred by Firmable's rules from each person's main title. This recipe assigns a role only from "
        "the department and seniority values the answers return, by exact text comparison after trimming.",
        f"Only people retrieved by one people search per account are checked: up to {s['max_people']} people in "
        f"country {s['selected_country']} (selectedCountry).",
        "A role marked not_observed has no one among the people retrieved. It is not observed in retrieved data; "
        "it does not mean no such person works there.",
        "People search accepts only companyId, selectedCountry, position, seniority, department, from and size. "
        "It has no name, location or date filter.",
    ]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    checks = [{"tool": "matches_department", "inputs": {"departments": unions["department"]}},
              {"tool": "matches_seniority", "inputs": {"seniorities": unions["seniority"]}}]
    dm_inputs: dict[str, Any] = {}
    if dm:
        dm_inputs = {"seller": dm["seller"]}
        if dm["buyer"]:
            dm_inputs["buyer"] = dm["buyer"]
        checks.append({"tool": "is_decision_maker", "inputs": dm_inputs})
    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no searches or questions were made.")
    problems = check_tools(catalogue, checks, "person")
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("The decision tools this recipe needs are not available with the inputs it sends.")
    model_tools = sorted({c["tool"] for c in checks if uses_model(catalogue, c["tool"])})
    if model_tools:
        coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                        f"{', '.join(model_tools)}. This code applies only the role rules in the configuration.")
    if dm:
        coverage.append("is_decision_maker is asked only for people who fill a role. Its answer is shown as "
                        "returned and does not change role assignment.")

    for r in ctx.rows:
        r["_key"] = _account_key(r)
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier account; their API responses were reused, not "
                        "requested again.")
    empty_search_seen = [False]

    def ask(row_id: str, tool: str, question: dict[str, Any]) -> dict[str, Any]:
        if ctx.stopped is not None:
            return {"tool": tool, "result": "not_asked", "answer": None, "error": None}
        try:
            resp = client.make_decision(tool, question, with_reasoning=cfg["with_reasoning"])
        except NotFoundError as e:
            if e.code == "person_not_found":
                return {"tool": tool, "result": "person_not_found", "answer": None, "error": None}
            return {"tool": tool, "result": "error", "answer": None, "error": ctx.record_error(row_id, e)}
        except FirmableError as e:
            return {"tool": tool, "result": "error", "answer": None, "error": ctx.record_error(row_id, e)}
        ans = answer_from(resp)
        result = compare(ans["answer"], "yes")
        return {"tool": tool, "result": result, **ans, "error": ans["answer_error"] if result == "error" else None}

    def one(row: dict[str, str]) -> dict[str, Any]:
        domain = normalise_domain(row.get("domain"))
        cid_in = (row.get("company_id") or "").strip()
        base = {"row_id": row["row_id"], "input": row, "domain": domain, "duplicate_of": dupes.get(row["row_id"]),
                "company_id": None, "company_id_source": None, "company": None, "search": None, "people": [],
                "roles": []}
        if cid_in and not _COMPANY_ID.match(cid_in):
            return {**base, "status": "invalid_input", "status_reason": f"'{cid_in}' is not a Firmable company ID."}
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

        body: dict[str, Any] = {"companyId": company_id, "selectedCountry": s["selected_country"]}
        for k in ("position", "seniority", "department"):
            if s[k]:
                body[k] = s[k]
        body.update({"from": "0", "size": str(s["max_people"])})
        resp = client.search_people(body)
        if resp.get("success") is False:
            empty_search_seen[0] = True
        records = [x for x in resp.get("records") or [] if isinstance(x, dict)]
        total = resp.get("total") if isinstance(resp.get("total"), int) and not isinstance(resp.get("total"), bool) \
            else None
        people: list[dict[str, Any]] = []
        seen: set[str] = set()
        no_id = 0
        for rec in records:
            pid = rec.get("person_id") or rec.get("id")
            if not isinstance(pid, str) or not pid:
                no_id += 1
                continue
            if pid in seen:
                continue
            seen.add(pid)
            people.append({"person_id": pid, "name": rec.get("name"), "position": rec.get("position")})
        not_checked = total - len(records) if total is not None and total > len(records) else 0
        base["search"] = {"filters": {k: v for k, v in body.items() if k not in ("companyId", "from", "size")},
                          "total": total, "retrieved": len(records), "people": len(people), "no_person_id": no_id,
                          "not_checked": not_checked}

        for p in people:
            q = {"person_id": p["person_id"]}
            dep = ask(row["row_id"], "matches_department", {**q, "departments": unions["department"]})
            if dep["result"] == "person_not_found":
                sen = {"tool": "matches_seniority", "result": "not_asked", "answer": None, "error": None}
            else:
                sen = ask(row["row_id"], "matches_seniority", {**q, "seniorities": unions["seniority"]})
            dep["value"] = _value(dep, "department")
            sen["value"] = _value(sen, "seniority")
            p["department_check"], p["seniority_check"] = dep, sen
            p["department"], p["seniority"] = dep["value"], sen["value"]
            _classify(p, roles, unions)
            p["decision_maker"] = None
            if dm and p["status"] == "assigned":
                p["decision_maker"] = ask(row["row_id"], "is_decision_maker", {**q, **dm_inputs})

        role_results = []
        for role in roles:
            filled = [p for p in people if role["id"] in p["roles_filled"]]
            undecided = [p for p in people if role["id"] in p["roles_undecided"]]
            possible = [p for p in people if role["id"] in p["roles_possible"]]
            status = ("filled" if filled else "incomplete" if undecided else "possible" if possible
                      else "not_observed")
            role_results.append({"role_id": role["id"], "label": role["label"], "required": role["required"],
                                 "status": status,
                                 "filled_by": [{"person_id": p["person_id"], "name": p["name"]} for p in filled],
                                 "possible": [{"person_id": p["person_id"], "name": p["name"]} for p in possible],
                                 "undecided": [{"person_id": p["person_id"], "name": p["name"]} for p in undecided]})
        if not people:
            status, reason = "no_people_found", "People search returned no people for this account."
        else:
            status, reason = _account_status(roles, role_results)
            reason += f" People checked: {len(people)}."
            missing = sum(1 for p in people if p["status"] == "person_not_found")
            if missing:
                reason += f" {missing} person(s) were not found by Decisions."
        if total is None:
            reason += " The API did not report a search total."
        elif not_checked:
            reason += (f" The search reported {total} people; {len(records)} were retrieved and {not_checked} were "
                       "not checked.")
        if no_id:
            reason += f" {no_id} search record(s) had no person ID and were left out."
        return {**base, "status": status, "status_reason": reason, "people": people, "roles": role_results}

    rows = ctx.run_rows(ctx.rows, one)
    if empty_search_seen[0]:
        coverage.append("A people search with no matches returns success false without a records list; it is "
                        "counted as zero people.")
    counts: dict[str, int] = {}
    role_counts = {role["id"]: {k: 0 for k in ROLE_STATUSES} for role in roles}
    person_counts = {k: 0 for k in PERSON_STATUSES}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        for rr in r.get("roles") or []:
            role_counts[rr["role_id"]][rr["status"]] += 1
        for p in r.get("people") or []:
            person_counts[p["status"]] += 1
    return {"rows": rows, "summary": {"status_counts": counts, "role_counts": role_counts,
                                      "person_counts": person_counts}, "coverage": coverage}


BASE_COLUMNS = ["row_id", "domain", "company_id", "company_name", "account_status", "account_status_reason",
                "search_total", "people_checked", "required_roles_not_observed", "required_roles_possible",
                "required_roles_undecided", "role_statuses"]


def csv_columns(result: dict[str, Any]) -> list[str]:
    roles = ((result.get("input") or {}).get("config") or {}).get("roles") or []
    return BASE_COLUMNS + [f"role_{r['id']}" for r in roles]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        search = r.get("search") or {}
        rr = r.get("roles") or []
        line = {
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "company_id": r.get("company_id") or r["input"].get("company_id"),
            "company_name": (r.get("company") or {}).get("name"), "account_status": r["status"],
            "account_status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "search_total": search.get("total"), "people_checked": search.get("people"),
            "required_roles_not_observed": [x["label"] for x in rr if x["required"] and x["status"] == "not_observed"],
            "required_roles_possible": [x["label"] for x in rr if x["required"] and x["status"] == "possible"],
            "required_roles_undecided": [x["label"] for x in rr if x["required"] and x["status"] == "incomplete"],
            "role_statuses": [f"{x['role_id']}={x['status']}" for x in rr],
        }
        for x in rr:
            line[f"role_{x['role_id']}"] = [p["name"] or p["person_id"] for p in x["filled_by"]]
        out.append(line)
    return out


def _who(items: list[dict[str, Any]]) -> str:
    return ", ".join(p["name"] or p["person_id"] for p in items)


def _check_text(c: dict[str, Any] | None) -> str:
    if not c:
        return "not asked"
    if c["result"] in ("pass", "fail", "unknown"):
        return c["answer"]
    return c["result"].replace("_", " ")


def render(report: Any, result: dict[str, Any]) -> None:
    summary = result["summary"]
    counts = summary.get("status_counts", {})
    roles = (result.get("input") or {}).get("config", {}).get("roles") or []
    report.add("Result", "Buying committee summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("Not observed means no one among the people retrieved fills the role. It does not mean no such "
                      "person works at the account. Unknown answers make a person <strong>unclassified</strong>, "
                      "never a no.", "unknown"))
    grid_rows = []
    for r in result["rows"]:
        title = (r.get("company") or {}).get("name") or r.get("domain") or r.get("company_id") or r["row_id"]
        cells: list[Any] = [title, Raw(badge(r["status"].replace("_", " "), STATUS_TONE.get(r["status"], "neutral")))]
        by = {x["role_id"]: x for x in r.get("roles") or []}
        for role in roles:
            x = by.get(role["id"])
            if x is None:
                cells.append("")
                continue
            who = _who(x["filled_by"] or x["undecided"] or x["possible"])
            cells.append(Raw(badge(x["status"].replace("_", " "), ROLE_TONE.get(x["status"], "neutral"))
                             + (f"<br>{esc(who)}" if who else "")))
        grid_rows.append(cells)
    headers = ["Account", "Status"] + [f"{role['label']}{' (required)' if role['required'] else ''}" for role in roles]
    report.add("Grid", "Roles by account", table(headers, grid_rows))

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
        search = r.get("search")
        if search:
            left += "<h3>Search coverage</h3>" + kv([
                ("Total reported", search["total"] if search["total"] is not None else "not reported"),
                ("Records retrieved", search["retrieved"]), ("People checked", search["people"]),
                ("Not checked", search["not_checked"])])
        if r.get("roles"):
            left += "<h3>Roles</h3>" + table(["Role", "Required", "Status", "People"], [
                [x["label"], "yes" if x["required"] else "no",
                 Raw(badge(x["status"].replace("_", " "), ROLE_TONE.get(x["status"], "neutral"))),
                 _who(x["filled_by"] or x["undecided"] or x["possible"]) or "not observed in retrieved data"]
                for x in r["roles"]])
        right = ""
        people = r.get("people") or []
        if people:
            right += "<h3>Facts returned by Decisions</h3>" + table(
                ["Name", "Position", "Department", "Seniority", "Classification", "Decision maker"], [
                    [p.get("name"), p.get("position"),
                     f"{_check_text(p['department_check'])}: {p['department'] or 'no value returned'}",
                     f"{_check_text(p['seniority_check'])}: {p['seniority'] or 'no value returned'}",
                     Raw(badge(p["status"].replace("_", " "), PERSON_TONE.get(p["status"], "neutral"))),
                     _check_text(p.get("decision_maker")) if p.get("decision_maker") else "not asked"]
                    for p in people])
            for p in people:
                extra = ""
                for c in (p["department_check"], p["seniority_check"], p.get("decision_maker")):
                    rs = (c or {}).get("reasoning") or {}
                    if rs.get("reason"):
                        extra += (f"<p class=\"muted\"><strong>{esc(c['tool'])}:</strong> {esc(rs.get('reason'))} "
                                  f"Source: {esc(rs.get('source'))}. Last refreshed: {esc(rs.get('last_refreshed'))}.</p>")
                if p["status"] == "assigned" and not p["notes"] and not extra:
                    continue
                right += f"<h3>{esc(p.get('name') or p['person_id'])}</h3><p>{esc(p['reason'])}</p>" + extra
                if p["notes"]:
                    right += note(bullet_list(p["notes"]), "review")
            right += details_json("People as JSON", people)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No people checked for this account.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its people", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, csv_columns, csv_rows, render)
