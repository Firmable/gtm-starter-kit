"""Territory routing: assigns accounts to sales teams with ordered, deterministic routing rules.

Workflow for each row:
1. Read the company identifier: a Firmable company ID, or a domain. An invalid identifier is reported.
2. Fetch the company profile (GET /company).
3. Evaluate the rules in configuration order. Profile conditions are evaluated first. A configured
   Decisions check (POST /decisions) is asked only when a rule still needs it, and at most once per row.
4. Apply the overlap policy and the review rules below.

Each condition is met, not_met, unknown (the profile or Decisions did not hold the fact), error, or
not_asked (the run stopped). A rule is no_match when any condition is not met, error when a condition
has an error, unknown when a condition is unknown, and match when every condition is met.

Overlap policy:
- first_match: rules are read in order and the first rule that is not no_match settles the row.
  Later rules are not evaluated.
- review: every rule is evaluated. Matching rules that route to different teams are a conflict.

Row status, first that applies:
- review: matching rules route to different teams;
- incomplete: a rule that could change the route has an error or was not asked;
- review: a rule that could change the route is unknown;
- review: the input domain differs from the profile's domain;
- review: a matching rule uses company size and the profile's size facts disagree;
- assigned: a rule matched;
- assigned_default: no rule matched and a default team is configured;
- unassigned: no rule matched and no default team is configured.
Rows with an invalid identifier, no company profile, or an API error get their own status.
Nothing is written to a CRM.
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

NAME = "territory-routing"
TITLE = "Territory routing"
DESCRIPTION = ("Assigns each company to a sales team by applying ordered routing rules to Firmable company "
               "profiles and optional Decisions answers, and sends ambiguous or incomplete records to a "
               "review queue. Nothing is written to a CRM.")

STATUS_TONE = {"assigned": "good", "assigned_default": "good", "unassigned": "neutral", "review": "review",
               "incomplete": "error", "company_not_found": "unknown", "invalid_input": "error",
               "error": "error", "not_processed": "unknown"}
OUTCOME_TONE = {"match": "good", "no_match": "neutral", "unknown": "unknown", "error": "error",
                "not_evaluated": "neutral"}
REVIEW_QUEUE = ("review", "incomplete", "company_not_found", "invalid_input", "error")

_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_COMPANY_ID = re.compile(r"^f\d{12}$")
_TOP_KEYS = {"with_reasoning", "teams", "decision_checks", "rules", "overlap", "default_team"}
_TEAM_KEYS = {"id", "name", "owner"}
_CHECK_KEYS = {"id", "label", "tool", "inputs"}
_RULE_KEYS = {"id", "label", "team", "when"}
# Profile conditions, in the order they are evaluated, and the profile field each reads.
_LIST_CONDS = {"hq_country_in": "hq_country", "states_any": "states", "industries_any": "industries",
               "company_size_in": "company_size"}
_NUM_CONDS = {"employee_count_min": "employee_count", "employee_count_max": "employee_count"}
_PROFILE_ORDER = ["hq_country_in", "states_any", "industries_any", "company_size_in", "employee_count_min",
                  "employee_count_max"]
_SIZE_CONDS = {"company_size_in", "employee_count_min", "employee_count_max"}
_COND_KEYS = set(_PROFILE_ORDER) | {"decisions"}
_BAND_RANGE = re.compile(r"^\s*([\d,]+)\s*-\s*([\d,]+)\s*$")
_BAND_PLUS = re.compile(r"^\s*([\d,]+)\s*\+\s*$")


def _unknown_keys(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ConfigError(f"{where} has unknown key(s): {', '.join(extra)}")


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _load_when(when: Any, where: str, check_ids: set[str]) -> dict[str, Any]:
    if not isinstance(when, dict) or not when:
        raise ConfigError(f"{where} must be an object with at least one condition.")
    _unknown_keys(when, _COND_KEYS, where)
    out: dict[str, Any] = {}
    for key in _PROFILE_ORDER:
        if key not in when:
            continue
        value = when[key]
        if key in _LIST_CONDS:
            if (not isinstance(value, list) or not value
                    or not all(isinstance(v, str) and v.strip() for v in value)):
                raise ConfigError(f"{where}.{key} must be a non-empty list of text values.")
        elif not _is_int(value) or value < 0:
            raise ConfigError(f"{where}.{key} must be a whole number, 0 or more.")
        out[key] = value
    if "employee_count_min" in out and "employee_count_max" in out \
            and out["employee_count_min"] > out["employee_count_max"]:
        raise ConfigError(f"{where}.employee_count_min is more than employee_count_max.")
    if "decisions" in when:
        dec = when["decisions"]
        if not isinstance(dec, dict) or not dec:
            raise ConfigError(f"{where}.decisions must be an object that maps a decision check id to yes or no.")
        for cid, want in dec.items():
            if cid not in check_ids:
                raise ConfigError(f"{where}.decisions names {cid!r}, which is not a decision_checks id.")
            if want not in ("yes", "no"):
                raise ConfigError(f"{where}.decisions.{cid} must be yes or no.")
        out["decisions"] = dict(dec)
    return out


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    _unknown_keys(raw, _TOP_KEYS, "The configuration")
    teams_raw = raw.get("teams")
    if not isinstance(teams_raw, list) or not teams_raw:
        raise ConfigError("teams must be a non-empty list.")
    teams: list[dict[str, Any]] = []
    team_ids: set[str] = set()
    for i, t in enumerate(teams_raw, start=1):
        if not isinstance(t, dict):
            raise ConfigError(f"teams[{i}] must be an object.")
        _unknown_keys(t, _TEAM_KEYS, f"teams[{i}]")
        tid = t.get("id")
        if not isinstance(tid, str) or not _ID.match(tid):
            raise ConfigError(f"teams[{i}].id must start with a lower-case letter and use only lower-case "
                              "letters, digits or underscores, up to 40 characters.")
        if tid in team_ids:
            raise ConfigError(f"teams[{i}].id {tid!r} is used twice.")
        team_ids.add(tid)
        name = t.get("name", tid)
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"teams[{i}].name must be text.")
        owner = t.get("owner")
        if owner is not None and not isinstance(owner, str):
            raise ConfigError(f"teams[{i}].owner must be text or null.")
        teams.append({"id": tid, "name": name, "owner": owner})

    checks_raw = raw.get("decision_checks", [])
    if not isinstance(checks_raw, list):
        raise ConfigError("decision_checks must be a list.")
    checks: list[dict[str, Any]] = []
    check_ids: set[str] = set()
    for i, c in enumerate(checks_raw, start=1):
        if not isinstance(c, dict):
            raise ConfigError(f"decision_checks[{i}] must be an object.")
        _unknown_keys(c, _CHECK_KEYS, f"decision_checks[{i}]")
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise ConfigError(f"decision_checks[{i}].id must start with a lower-case letter and use only "
                              "lower-case letters, digits or underscores, up to 40 characters.")
        if cid in check_ids:
            raise ConfigError(f"decision_checks[{i}].id {cid!r} is used twice.")
        check_ids.add(cid)
        tool = c.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ConfigError(f"decision_checks[{i}].tool must name a decision tool.")
        inputs = c.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ConfigError(f"decision_checks[{i}].inputs must be an object.")
        label = c.get("label", cid)
        if not isinstance(label, str):
            raise ConfigError(f"decision_checks[{i}].label must be text.")
        checks.append({"id": cid, "label": label, "tool": tool, "inputs": inputs})

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError("rules must be a non-empty list.")
    rules: list[dict[str, Any]] = []
    rule_ids: set[str] = set()
    used_checks: set[str] = set()
    for i, r in enumerate(rules_raw, start=1):
        if not isinstance(r, dict):
            raise ConfigError(f"rules[{i}] must be an object.")
        _unknown_keys(r, _RULE_KEYS, f"rules[{i}]")
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID.match(rid):
            raise ConfigError(f"rules[{i}].id must start with a lower-case letter and use only lower-case "
                              "letters, digits or underscores, up to 40 characters.")
        if rid in rule_ids:
            raise ConfigError(f"rules[{i}].id {rid!r} is used twice.")
        rule_ids.add(rid)
        team = r.get("team")
        if team not in team_ids:
            raise ConfigError(f"rules[{i}].team must be one of the team ids.")
        label = r.get("label", rid)
        if not isinstance(label, str):
            raise ConfigError(f"rules[{i}].label must be text.")
        when = _load_when(r.get("when"), f"rules[{i}].when", check_ids)
        used_checks |= set(when.get("decisions", {}))
        rules.append({"id": rid, "label": label, "team": team, "when": when})
    for c in checks:
        if c["id"] not in used_checks:
            raise ConfigError(f"decision_checks id {c['id']!r} is not used by any rule.")

    overlap = raw.get("overlap", "first_match")
    if overlap not in ("first_match", "review"):
        raise ConfigError("overlap must be first_match or review.")
    default_team = raw.get("default_team")
    if default_team is not None and default_team not in team_ids:
        raise ConfigError("default_team must be null or one of the team ids.")
    with_reasoning = raw.get("with_reasoning", True)
    if not isinstance(with_reasoning, bool):
        raise ConfigError("with_reasoning must be true or false.")
    return {"with_reasoning": with_reasoning, "teams": teams, "decision_checks": checks, "rules": rules,
            "overlap": overlap, "default_team": default_team}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=["domain", "company_id"])


def _lookup(row: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """Returns (by, value, problem). by is "id" or "fqdn"; problem explains an invalid identifier."""
    cid = (row.get("company_id") or "").strip()
    if cid:
        if _COMPANY_ID.match(cid):
            return "id", cid, None
        return None, None, f"company_id {cid!r} is not a Firmable company ID (f followed by 12 digits)."
    domain = normalise_domain(row.get("domain"))
    if domain:
        return "fqdn", domain, None
    return None, None, f"{row.get('domain')!r} is not a valid domain."


def _row_key(row: dict[str, str]) -> str:
    by, value, _ = _lookup(row)
    return f"{by}:{value}" if by else ""


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    keys: list[str] = []
    for r in rows:
        k = _row_key(r)
        if k and k not in keys:
            keys.append(k)
    n = len(keys)
    calls: list[PlannedCall] = []
    checks = len(config["decision_checks"])
    if checks:
        calls.append(PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []))
    calls.append(PlannedCall("GET /company", "Company profile for each unique valid identifier", n,
                             [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    if checks:
        calls.append(PlannedCall("POST /decisions", f"Up to {checks} decision check(s) for each unique valid identifier",
                                 n * checks, pricing.estimate_decision(n * checks, config["with_reasoning"])))
    return calls


def _norm(s: str) -> str:
    return s.strip().lower()


def _band(value: Any) -> tuple[int, int | None] | None:
    if not isinstance(value, str):
        return None
    m = _BAND_RANGE.match(value)
    if m:
        return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    m = _BAND_PLUS.match(value)
    if m:
        return int(m.group(1).replace(",", "")), None
    return None


def _size_conflict(facts: dict[str, Any]) -> str | None:
    count = facts.get("employee_count")
    band = _band(facts.get("company_size"))
    if not _is_int(count) or band is None:
        return None
    low, high = band
    if count < low or (high is not None and count > high):
        return (f"employee_count {count} is outside the company_size band {facts.get('company_size')}. "
                "employee_count counts staff in the requested country region, so the two can differ "
                "legitimately. This recipe does not correct either value.")
    return None


def _profile_condition(key: str, expected: Any, facts: dict[str, Any]) -> dict[str, Any]:
    if key in _LIST_CONDS:
        field = _LIST_CONDS[key]
        observed = facts.get(field)
        values = observed if isinstance(observed, list) else [observed]
        values = [v for v in values if isinstance(v, str) and v.strip()]
        if not values:
            result = "unknown"
        else:
            wanted = {_norm(e) for e in expected}
            result = "met" if any(_norm(v) in wanted for v in values) else "not_met"
    else:
        field = _NUM_CONDS[key]
        observed = facts.get(field)
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            result = "unknown"
        elif key == "employee_count_min":
            result = "met" if observed >= expected else "not_met"
        else:
            result = "met" if observed <= expected else "not_met"
    return {"condition": key, "field": field, "expected": expected, "observed": observed, "result": result}


def _outcome(conditions: list[dict[str, Any]]) -> str:
    results = [c["result"] for c in conditions]
    if "not_met" in results:
        return "no_match"
    if "error" in results or "not_asked" in results:
        return "error"
    if "unknown" in results:
        return "unknown"
    return "match"


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    teams = {t["id"]: t for t in cfg["teams"]}
    checks = {c["id"]: c for c in cfg["decision_checks"]}
    coverage = ["An unknown condition means the company profile or Decisions did not hold the fact. It is never "
                "treated as not met. A record whose route depends on an unknown goes to the review queue.",
                "Assignments are written to this run's output files only. Nothing is written to a CRM.",
                f"Overlap handling: {cfg['overlap']}."]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}, "team_counts": {},
                                          "rule_match_counts": {}, "review_queue": []}, "coverage": coverage}

    if cfg["decision_checks"]:
        try:
            catalogue = client.list_decision_tools()
        except FirmableError as e:
            ctx.record_error(None, e)
            return stop_all("The decision tool list could not be read, so no rows were routed.")
        problems = check_tools(catalogue, cfg["decision_checks"], "company")
        if problems:
            for p in problems:
                ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                                   "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                                   "retry_after": None})
            coverage.extend(problems)
            return stop_all("The configuration names decision tools or inputs the API does not accept.")
        model_tools = sorted({c["tool"] for c in cfg["decision_checks"] if uses_model(catalogue, c["tool"])})
        if model_tools:
            coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                            f"{', '.join(model_tools)}. This code applies only the rules in the configuration.")

    for r in ctx.rows:
        r["_key"] = _row_key(r)
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier company identifier; their API responses were "
                        "reused, not requested again.")

    def team_name(tid: str) -> str:
        return teams[tid]["name"]

    def one(row: dict[str, str]) -> dict[str, Any]:
        by, value, problem = _lookup(row)
        base: dict[str, Any] = {
            "row_id": row["row_id"], "input": row, "lookup": {"by": by, "value": value} if by else None,
            "duplicate_of": dupes.get(row["row_id"]), "company": None, "decisions": [], "rules": [],
            "matched_rules": [], "team_id": None, "team_name": None, "owner": None, "candidate_teams": [],
            "review_reasons": [], "data_conflicts": []}
        if not by:
            return {**base, "status": "invalid_input", "status_reason": problem}
        try:
            company = client.get_company(**{by: value})
        except NotFoundError:
            return {**base, "status": "company_not_found",
                    "status_reason": "Firmable has no company profile for this identifier, so no rules were evaluated."}
        facts = company_facts(company) or {}
        facts["states"] = company.get("states")
        base["company"] = facts

        identifier_conflict = None
        input_domain = normalise_domain(row.get("domain"))
        profile_domain = normalise_domain(company.get("fqdn") if isinstance(company.get("fqdn"), str) else None)
        if input_domain and profile_domain and input_domain != profile_domain:
            identifier_conflict = (f"The input domain {input_domain} differs from the profile's fqdn "
                                   f"{profile_domain}.")
        size_conflict = _size_conflict(facts)
        base["data_conflicts"] = [c for c in (identifier_conflict, size_conflict) if c]

        answers: dict[str, dict[str, Any]] = {}

        def ask(cid: str) -> dict[str, Any]:
            if cid in answers:
                return answers[cid]
            c = checks[cid]
            entry = {"check_id": cid, "label": c["label"], "tool": c["tool"]}
            if ctx.stopped is not None:
                rec = {**entry, "asked": False, "answer": None, "error": None}
            else:
                try:
                    resp = client.make_decision(c["tool"], {"company_id": company["id"], **c["inputs"]},
                                                with_reasoning=cfg["with_reasoning"])
                except FirmableError as e:
                    rec = {**entry, "asked": True, "answer": None, "error": ctx.record_error(row["row_id"], e)}
                else:
                    ans = answer_from(resp)
                    bad = ans["answer"] not in ("yes", "no", "unknown")
                    rec = {**entry, "asked": True, **ans, "error": ans["answer_error"] if bad else None}
            answers[cid] = rec
            return rec

        evaluated: list[dict[str, Any]] = []
        settled = False
        for rule in cfg["rules"]:
            info = {"rule_id": rule["id"], "label": rule["label"], "team_id": rule["team"]}
            if settled:
                evaluated.append({**info, "outcome": "not_evaluated", "conditions": []})
                continue
            when = rule["when"]
            conds = [_profile_condition(k, when[k], facts) for k in _PROFILE_ORDER if k in when]
            profile_failed = any(c["result"] == "not_met" for c in conds)
            for cid, want in (when.get("decisions") or {}).items():
                cond = {"condition": "decision", "field": cid, "expected": want, "observed": None}
                if profile_failed:
                    conds.append({**cond, "result": "skipped"})
                    continue
                rec = ask(cid)
                if not rec["asked"]:
                    conds.append({**cond, "result": "not_asked"})
                    continue
                cond["observed"] = rec["answer"]
                cmp = compare(rec["answer"], want) if rec["error"] is None else "error"
                conds.append({**cond, "result": {"pass": "met", "fail": "not_met"}.get(cmp, cmp)})
            outcome = _outcome(conds)
            evaluated.append({**info, "outcome": outcome, "conditions": conds})
            if cfg["overlap"] == "first_match" and outcome != "no_match":
                settled = True

        matched = [r for r in evaluated if r["outcome"] == "match"]
        match_teams: list[str] = []
        for r in matched:
            if r["team_id"] not in match_teams:
                match_teams.append(r["team_id"])
        unsettled = [r for r in evaluated if r["outcome"] in ("unknown", "error")
                     and not (len(match_teams) == 1 and r["team_id"] == match_teams[0])]
        candidates = list(match_teams)
        for r in unsettled:
            if r["team_id"] not in candidates:
                candidates.append(r["team_id"])

        def labels(items: list[dict[str, Any]]) -> str:
            return ", ".join(r["label"] for r in items)

        reasons: list[tuple[str, str]] = []
        if len(match_teams) > 1:
            reasons.append(("review", f"Matching rules route to different teams: "
                                      f"{', '.join(r['label'] + ' (' + team_name(r['team_id']) + ')' for r in matched)}."))
        errored = [r for r in unsettled if r["outcome"] == "error"]
        if errored:
            reasons.append(("incomplete", f"Rule(s) that could change the route have an error or were not asked "
                                          f"because the run stopped: {labels(errored)}."))
        unknown = [r for r in unsettled if r["outcome"] == "unknown"]
        if unknown:
            reasons.append(("review", f"Rule(s) that could change the route depend on facts Firmable did not "
                                      f"return: {labels(unknown)}. Unknown is not treated as not met."))
        if identifier_conflict:
            reasons.append(("review", identifier_conflict))
        size_rules = [r for r in matched if any(c["condition"] in _SIZE_CONDS for c in r["conditions"])]
        if size_conflict and size_rules:
            reasons.append(("review", f"Matching rule(s) use company size, but the profile's size facts "
                                      f"disagree: {labels(size_rules)}."))
        out = {**base, "decisions": list(answers.values()), "rules": evaluated,
               "matched_rules": [r["rule_id"] for r in matched], "candidate_teams": candidates,
               "review_reasons": [text for _, text in reasons]}
        if reasons:
            return {**out, "status": reasons[0][0], "status_reason": reasons[0][1]}
        if match_teams:
            tid = match_teams[0]
            reason = f"Rule {matched[0]['label']} matched; routed to {team_name(tid)}."
            if len(matched) > 1:
                reason += f" {len(matched) - 1} other matching rule(s) route to the same team."
            return {**out, "status": "assigned", "status_reason": reason, "team_id": tid,
                    "team_name": team_name(tid), "owner": teams[tid]["owner"]}
        if cfg["default_team"]:
            tid = cfg["default_team"]
            return {**out, "status": "assigned_default", "candidate_teams": [],
                    "status_reason": f"No rule matched; routed to the default team, {team_name(tid)}.",
                    "team_id": tid, "team_name": team_name(tid), "owner": teams[tid]["owner"]}
        return {**out, "status": "unassigned", "status_reason": "No rule matched and no default team is configured."}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    team_counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {r["id"]: 0 for r in cfg["rules"]}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r.get("team_id"):
            team_counts[r["team_id"]] = team_counts.get(r["team_id"], 0) + 1
        for rid in r.get("matched_rules") or []:
            rule_counts[rid] += 1
    conflicts = sum(1 for r in rows if r.get("data_conflicts"))
    if conflicts:
        coverage.append(f"{conflicts} row(s) have company data that disagrees with the input or with itself. "
                        "They are listed under data conflicts. This recipe reports them and does not correct "
                        "upstream data.")
    queue = [r["row_id"] for r in rows if r["status"] in REVIEW_QUEUE]
    return {"rows": rows, "summary": {"status_counts": counts, "team_counts": team_counts,
                                      "rule_match_counts": rule_counts, "review_queue": queue},
            "coverage": coverage}


CSV_COLUMNS = ["row_id", "input_domain", "input_company_id", "duplicate_of", "status", "status_reason",
               "review_queue", "team_id", "team_name", "owner", "matched_rules", "candidate_teams",
               "review_reasons", "company_id", "company_name", "hq_country", "states", "employee_count",
               "company_size", "industries", "data_conflicts", "rule_results"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        out.append({
            "row_id": r["row_id"], "input_domain": r["input"].get("domain"),
            "input_company_id": r["input"].get("company_id"), "duplicate_of": r.get("duplicate_of"),
            "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "review_queue": r["status"] in REVIEW_QUEUE, "team_id": r.get("team_id"),
            "team_name": r.get("team_name"), "owner": r.get("owner"),
            "matched_rules": r.get("matched_rules") or [], "candidate_teams": r.get("candidate_teams") or [],
            "review_reasons": r.get("review_reasons") or [], "company_id": company.get("id"),
            "company_name": company.get("name"), "hq_country": company.get("hq_country"),
            "states": company.get("states") if isinstance(company.get("states"), list) else [],
            "employee_count": company.get("employee_count"), "company_size": company.get("company_size"),
            "industries": company.get("industries") if isinstance(company.get("industries"), list) else [],
            "data_conflicts": r.get("data_conflicts") or [],
            "rule_results": [f"{x['rule_id']}={x['outcome']}" for x in r.get("rules") or []],
        })
    return out


def _cond_text(c: dict[str, Any]) -> str:
    observed = c.get("observed")
    if isinstance(observed, list):
        observed = ", ".join(str(v) for v in observed) or "none"
    expected = c.get("expected")
    if isinstance(expected, list):
        expected = ", ".join(expected)
    seen = "not returned" if observed is None else observed
    return f"{c['condition']} {c['field']}: wanted {expected}, observed {seen}, {c['result'].replace('_', ' ')}"


def render(report: Any, result: dict[str, Any]) -> None:
    summary = result["summary"]
    counts = summary.get("status_counts", {})
    body = ("<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                             for k, v in counts.items()) + "</p>")
    teams = {t["id"]: t for t in result["input"]["config"].get("teams", [])}
    if summary.get("team_counts"):
        body += table(["Team", "Owner", "Accounts"], [[teams.get(k, {}).get("name", k), teams.get(k, {}).get("owner"), v]
                                                    for k, v in summary["team_counts"].items()])
    if summary.get("rule_match_counts"):
        body += table(["Rule", "Rows matched"], [[k, v] for k, v in summary["rule_match_counts"].items()])
    body += note("Unknown means the company profile or Decisions did not hold the fact. A record whose route "
                 "depends on an unknown goes to the <strong>review queue</strong>, never to a team by default.",
                 "unknown")
    report.add("Result", "Routing summary", body)

    queue = [r for r in result["rows"] if r["status"] in REVIEW_QUEUE]
    if queue:
        report.add("Review queue", "Records a person needs to route", table(
            ["Row", "Company", "Status", "Reason", "Candidate teams"],
            [[r["row_id"], (r.get("company") or {}).get("name") or r["input"].get("domain") or r["input"].get("company_id"),
              r["status"].replace("_", " "),
              r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
              ", ".join(r.get("candidate_teams") or []) or None] for r in queue]))

    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = ((r.get("company") or {}).get("name") or r["input"].get("domain") or r["input"].get("company_id")
                 or r["row_id"])
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        if r.get("team_name"):
            left += kv([("Team", r["team_name"]), ("Owner", r.get("owner"))])
        company = r.get("company")
        if company:
            left += kv([("Firmable ID", company.get("id")), ("Domain", company.get("fqdn")),
                        ("Head office country", company.get("hq_country")),
                        ("States", ", ".join(company.get("states") or []) or None),
                        ("Employee count", company.get("employee_count")), ("Size", company.get("company_size")),
                        ("Industries", ", ".join(company.get("industries") or []) or None)])
        if r.get("data_conflicts"):
            left += "<h3>Data conflicts</h3>" + bullet_list(r["data_conflicts"])
        if len(r.get("review_reasons") or []) > 1:
            left += "<h3>All review reasons</h3>" + bullet_list(r["review_reasons"])
        if r.get("duplicate_of"):
            left += note(f"Same identifier as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        rules = r.get("rules") or []
        if rules:
            right += table(["Rule", "Team", "Outcome", "Conditions"], [
                [x["label"], teams.get(x["team_id"], {}).get("name", x["team_id"]),
                 Raw(badge(x["outcome"].replace("_", " "), OUTCOME_TONE.get(x["outcome"], "neutral"))),
                 Raw(bullet_list([_cond_text(c) for c in x["conditions"]]) or "<span class=\"muted\">Not evaluated</span>")]
                for x in rules])
        for d in r.get("decisions") or []:
            rs = d.get("reasoning") or {}
            right += (f"<p><strong>{esc(d['label'])}</strong> ({esc(d['tool'])}): "
                      f"{esc(d.get('answer') or ('not asked' if not d.get('asked') else 'no answer'))}"
                      + (f", confidence {esc(d.get('confidence'))}" if d.get("confidence") is not None else "")
                      + "</p>")
            if rs.get("reason"):
                right += (f"<p>{esc(rs.get('reason'))}</p><p class=\"muted\">Source: {esc(rs.get('source'))}. "
                          f"Last refreshed: {esc(rs.get('last_refreshed'))}."
                          + (f" Would change if: {esc(rs.get('would_change_if'))}." if rs.get("would_change_if") else "")
                          + "</p>")
            if d.get("error"):
                err = d["error"]
                right += note(f"{esc(d['label'])}: {esc(err.get('message') if isinstance(err, dict) else err)}", "error")
        if rules:
            right += details_json("Rule results as JSON", rules)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No rules were evaluated for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its routing", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
