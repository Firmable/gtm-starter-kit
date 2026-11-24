"""Account qualification: checks a company list against a seller's qualification criteria.

Workflow for each row:
1. Normalise the domain. An invalid domain is reported and skipped.
2. Fetch the company profile (GET /company), unless fetch_company is false.
3. Ask each configured Decisions check (POST /decisions), with reasoning when configured.
4. Apply the aggregation policy below.

Aggregation policy, in order of precedence:
- disqualified: a mandatory check got the opposite of the wanted answer;
- incomplete: a mandatory check returned an error, so it has no answer;
- needs_review: a mandatory check answered unknown. Unknown is never a pass or a fail;
- qualified: every mandatory check passed and at least policy.min_optional_passes optional checks passed;
- not_qualified: every mandatory check passed but too few optional checks passed.
Rows with an invalid domain, no company profile, or an API error get their own status.
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

NAME = "account-qualification"
TITLE = "Account qualification"
DESCRIPTION = ("Checks each company against the qualification checks in the configuration, using Firmable "
               "company profiles and Decisions, and applies an explicit pass, fail and unknown policy.")

STATUS_TONE = {"qualified": "good", "not_qualified": "bad", "disqualified": "bad", "needs_review": "review",
               "incomplete": "error", "company_not_found": "unknown", "invalid_input": "error",
               "error": "error", "not_processed": "unknown"}
RESULT_TONE = {"pass": "good", "fail": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral"}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_TOP_KEYS = {"fetch_company", "with_reasoning", "checks", "policy"}


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ConfigError("checks must be a non-empty list.")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, c in enumerate(checks_raw, start=1):
        if not isinstance(c, dict):
            raise ConfigError(f"checks[{i}] must be an object.")
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise ConfigError(f"checks[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if cid in seen:
            raise ConfigError(f"checks[{i}].id {cid!r} is used twice.")
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
    if not isinstance(mop, int) or isinstance(mop, bool) or mop < 0:
        raise ConfigError("policy.min_optional_passes must be a whole number, 0 or more.")
    optional = sum(1 for c in checks if not c["mandatory"])
    if mop > optional:
        raise ConfigError(f"policy.min_optional_passes is {mop} but only {optional} optional check(s) exist.")
    for key in ("fetch_company", "with_reasoning"):
        if not isinstance(raw.get(key, True), bool):
            raise ConfigError(f"{key} must be true or false.")
    return {"fetch_company": raw.get("fetch_company", True), "with_reasoning": raw.get("with_reasoning", True),
            "checks": checks, "policy": {"min_optional_passes": mop}}


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
    checks = len(config["checks"])
    calls = [PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, [])]
    if config["fetch_company"]:
        calls.append(PlannedCall("GET /company", "Company profile for each unique valid domain", n,
                                 [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    calls.append(PlannedCall("POST /decisions", f"{checks} check(s) for each unique valid domain", n * checks,
                             pricing.estimate_decision(n * checks, config["with_reasoning"])))
    return calls


def _aggregate(checks: list[dict[str, Any]], mop: int) -> tuple[str, str]:
    def labels(items: list[dict[str, Any]]) -> str:
        return ", ".join(c["label"] for c in items)

    mandatory = [c for c in checks if c["mandatory"]]
    optional = [c for c in checks if not c["mandatory"]]
    failed = [c for c in mandatory if c["result"] == "fail"]
    errored = [c for c in mandatory if c["result"] in ("error", "not_asked")]
    unknown = [c for c in mandatory if c["result"] == "unknown"]
    if failed:
        return "disqualified", f"Mandatory check(s) failed: {labels(failed)}."
    if errored:
        return "incomplete", f"Mandatory check(s) have no answer because of an error or because the run stopped: {labels(errored)}."
    if unknown:
        return "needs_review", (f"Mandatory check(s) answered unknown: {labels(unknown)}. "
                                "Unknown means Firmable does not hold the fact; it is not a no.")
    passes = sum(1 for c in optional if c["result"] == "pass")
    opt_unknown = sum(1 for c in optional if c["result"] == "unknown")
    tail = f" {opt_unknown} optional check(s) answered unknown." if opt_unknown else ""
    if passes >= mop:
        return "qualified", (f"All mandatory checks passed; {passes} of {len(optional)} optional check(s) "
                             f"passed (minimum {mop}).{tail}")
    return "not_qualified", (f"All mandatory checks passed, but only {passes} of {len(optional)} optional "
                             f"check(s) passed (minimum {mop}).{tail}")


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = ["An unknown answer means Firmable does not hold the fact needed to answer. It is never "
                "counted as a no or as a pass."]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no checks were asked.")
    problems = check_tools(catalogue, cfg["checks"], "company")
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("The configuration names decision tools or inputs the API does not accept.")

    model_tools = sorted({c["tool"] for c in cfg["checks"] if uses_model(catalogue, c["tool"])})
    if model_tools:
        coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                        f"{', '.join(model_tools)}. This code applies only the rules in the configuration.")
    if not cfg["fetch_company"]:
        coverage.append("Company profiles were not fetched; checks were asked by domain.")

    for r in ctx.rows:
        r["_domain"] = normalise_domain(r.get("domain")) or ""
    dupes = find_duplicates(ctx.rows, "_domain")
    for r in ctx.rows:
        r.pop("_domain")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier domain; their API responses were reused, not "
                        "requested again.")
    mop = cfg["policy"]["min_optional_passes"]

    def one(row: dict[str, str]) -> dict[str, Any]:
        domain = normalise_domain(row.get("domain"))
        base = {"row_id": row["row_id"], "input": row, "domain": domain,
                "duplicate_of": dupes.get(row["row_id"]), "company": None, "checks": [],
                "missing_information": [], "errors": []}
        if not domain:
            return {**base, "status": "invalid_input",
                    "status_reason": f"{row.get('domain')!r} is not a valid domain."}
        company = None
        if cfg["fetch_company"]:
            try:
                company = client.get_company(fqdn=domain)
            except NotFoundError:
                return {**base, "status": "company_not_found",
                        "status_reason": "Firmable has no company profile for this domain, so no checks were asked."}
        base["company"] = company_facts(company)
        subject = {"company_id": company["id"]} if company else {"domain": domain}
        results: list[dict[str, Any]] = []
        for c in cfg["checks"]:
            entry = {"check_id": c["id"], "label": c["label"], "tool": c["tool"], "mandatory": c["mandatory"],
                     "want": c["want"]}
            if ctx.stopped is not None:
                results.append({**entry, "result": "not_asked", "answer": None, "error": None})
                continue
            try:
                resp = client.make_decision(c["tool"], {**subject, **c["inputs"]},
                                            with_reasoning=cfg["with_reasoning"])
            except FirmableError as e:
                err = ctx.record_error(row["row_id"], e)
                results.append({**entry, "result": "error", "answer": None, "error": err})
                continue
            ans = answer_from(resp)
            result = compare(ans["answer"], c["want"])
            results.append({**entry, "result": result, **ans,
                            "error": ans["answer_error"] if result == "error" else None})
        status, reason = _aggregate(results, mop)
        return {**base, "status": status, "status_reason": reason, "checks": results,
                "missing_information": [c["label"] for c in results if c["result"] == "unknown"],
                "errors": [c["label"] for c in results if c["result"] in ("error", "not_asked")]}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    check_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for c in r.get("checks") or []:
            cc = check_counts.setdefault(c["check_id"], {"pass": 0, "fail": 0, "unknown": 0, "error": 0, "not_asked": 0})
            cc[c["result"]] += 1
    return {"rows": rows, "summary": {"status_counts": counts, "check_counts": check_counts},
            "coverage": coverage}


CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
               "checks_passed", "checks_failed", "checks_unknown", "checks_error", "missing_information",
               "check_results"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        checks = r.get("checks") or []
        company = r.get("company") or {}
        out.append({
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_id": company.get("id"), "company_name": company.get("name"),
            "checks_passed": sum(1 for c in checks if c["result"] == "pass"),
            "checks_failed": sum(1 for c in checks if c["result"] == "fail"),
            "checks_unknown": sum(1 for c in checks if c["result"] == "unknown"),
            "checks_error": sum(1 for c in checks if c["result"] in ("error", "not_asked")),
            "missing_information": r.get("missing_information") or [],
            "check_results": [f"{c['check_id']}={c['result']}({c.get('answer') or '-'})" for c in checks],
        })
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Qualification summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("Unknown means Firmable does not hold the fact. A row with an unknown mandatory check is "
                      "<strong>needs review</strong>, never qualified.", "unknown"))
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
        if r.get("duplicate_of"):
            left += note(f"Same domain as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        checks = r.get("checks") or []
        if checks:
            right += table(["Check", "Mandatory", "Wanted", "Answer", "Result", "Confidence"], [
                [c["label"], "yes" if c["mandatory"] else "no", c["want"], c.get("answer") or "none",
                 Raw(badge(c["result"].replace("_", " "), RESULT_TONE.get(c["result"], "neutral"))),
                 c.get("confidence")] for c in checks])
            for c in checks:
                rs = c.get("reasoning") or {}
                if rs.get("reason"):
                    right += (f"<p><strong>{esc(c['label'])}:</strong> {esc(rs.get('reason'))}</p>"
                              f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                              f"{esc(rs.get('last_refreshed'))}."
                              + (f" Would change if: {esc(rs.get('would_change_if'))}." if rs.get("would_change_if") else "")
                              + "</p>")
                if c.get("error"):
                    right += note(f"{esc(c['label'])}: {esc((c['error'] or {}).get('message') if isinstance(c['error'], dict) else c['error'])}", "error")
        if r.get("missing_information"):
            right += "<h3>Missing information</h3>" + bullet_list(r["missing_information"])
        right += details_json("Check results as JSON", checks) if checks else ""
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No checks were asked for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its checks", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
