"""Expansion signals: flags land-and-expand signals for each account and assigns an ordered tier.

Workflow for each row:
1. Pick the company identifier: a valid company_id, otherwise a valid domain. Anything else is invalid input.
2. Fetch the company profile (GET /company) when fetch_company is true.
3. Ask every configured signal with Decisions (POST /decisions):
   - multinational: is_multinational;
   - multiple_sites: has_multiple_sites with min_sites;
   - local_team:<CC>: has_local_team with country and min_staff (answers only for AU and NZ);
   - office_in:<CC>: is_located_in with countries [CC] and where any_office.
4. Evaluate the tiers in order.

Tier policy:
- A signal counts only on a yes. A no means Firmable has not recorded it, not that it is absent.
- A tier matches when every `all` signal answered yes and, if `any` is given, at least one `any` signal answered yes.
- A tier is not matched when an `all` signal answered no, or every `any` signal answered no.
- Otherwise the tier depends on signals without a yes or no. If any of them has an error or was not asked, the tier
  is incomplete; if they all answered unknown, the tier needs review.
- Tiers are tried in order. The first tier that is matched, needs review or is incomplete decides the account.
  Missing data never moves an account down to a lower tier.
- When every tier is not matched, the account has no tier.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "expansion-signals"
TITLE = "Expansion signals"
DESCRIPTION = ("Flags land-and-expand signals for each account (recorded in several countries, several sites, "
               "local teams and offices in named markets) with Firmable Decisions, and assigns the first "
               "configured tier whose signals answered yes.")

STATUS_TONE = {"tier_assigned": "good", "no_tier": "neutral", "needs_review": "review", "incomplete": "error",
               "company_not_found": "unknown", "invalid_input": "error", "error": "error",
               "not_processed": "unknown"}
RESULT_TONE = {"yes": "good", "no": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral"}
OUTCOME_TONE = {"matched": "good", "not_matched": "neutral", "needs_review": "review", "incomplete": "error",
                "not_considered": "neutral"}
LOCAL_TEAM_COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
LOCAL_TEAM_ANSWERS = ["AU", "NZ"]
FACT_KEYS = {"is_multinational": "countries", "has_multiple_sites": "locations_held",
             "has_local_team": "staff_in_country", "is_located_in": "location_held"}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_CC = re.compile(r"^[A-Z]{2}$")
_COMPANY_ID = re.compile(r"^f\d{12}$")
_TOP_KEYS = {"checks", "tiers", "with_reasoning", "fetch_company"}
_CHECK_KEYS = {"multinational", "multiple_sites", "local_teams", "offices_in"}


def _whole(v: Any, minimum: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def _load_checks(raw: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if not isinstance(raw, dict):
        raise ConfigError("checks must be an object with at least one of multinational, multiple_sites, "
                          "local_teams, offices_in.")
    unknown = sorted(set(raw) - _CHECK_KEYS)
    if unknown:
        raise ConfigError(f"Unknown key(s) in checks: {', '.join(unknown)}")
    signals: list[dict[str, Any]] = []
    warnings: list[str] = []

    multinational = raw.get("multinational", False)
    if not isinstance(multinational, bool):
        raise ConfigError("checks.multinational must be true or false.")
    if multinational:
        signals.append({"id": "multinational", "label": "Recorded in two or more countries",
                        "tool": "is_multinational", "inputs": {}})

    ms = raw.get("multiple_sites", False)
    multiple_sites = None
    if isinstance(ms, bool):
        if ms:
            multiple_sites = {"min_sites": 2}
    elif isinstance(ms, dict):
        extra = sorted(set(ms) - {"min_sites"})
        if extra:
            raise ConfigError(f"checks.multiple_sites accepts only min_sites, not: {', '.join(extra)}")
        min_sites = ms.get("min_sites", 2)
        if not _whole(min_sites, 2):
            raise ConfigError("checks.multiple_sites.min_sites must be a whole number, 2 or more.")
        multiple_sites = {"min_sites": min_sites}
    else:
        raise ConfigError("checks.multiple_sites must be true, false or an object with min_sites.")
    if multiple_sites:
        signals.append({"id": "multiple_sites", "label": f"At least {multiple_sites['min_sites']} sites recorded",
                        "tool": "has_multiple_sites", "inputs": {"min_sites": multiple_sites["min_sites"]}})

    lt_raw = raw.get("local_teams", [])
    if not isinstance(lt_raw, list):
        raise ConfigError("checks.local_teams must be a list of {country, min_staff}.")
    local_teams: list[dict[str, Any]] = []
    for i, t in enumerate(lt_raw, start=1):
        if not isinstance(t, dict):
            raise ConfigError(f"checks.local_teams[{i}] must be an object.")
        extra = sorted(set(t) - {"country", "min_staff"})
        if extra:
            raise ConfigError(f"checks.local_teams[{i}] has unknown key(s): {', '.join(extra)}")
        country = t.get("country")
        if country not in LOCAL_TEAM_COUNTRIES:
            raise ConfigError(f"checks.local_teams[{i}].country must be one of {', '.join(LOCAL_TEAM_COUNTRIES)}.")
        if any(x["country"] == country for x in local_teams):
            raise ConfigError(f"checks.local_teams[{i}].country {country!r} is listed twice.")
        min_staff = t.get("min_staff", 5)
        if not _whole(min_staff, 1):
            raise ConfigError(f"checks.local_teams[{i}].min_staff must be a whole number, 1 or more.")
        local_teams.append({"country": country, "min_staff": min_staff})
        signals.append({"id": f"local_team:{country}", "label": f"At least {min_staff} staff in {country}",
                        "tool": "has_local_team", "inputs": {"country": country, "min_staff": min_staff}})
    unanswered = [t["country"] for t in local_teams if t["country"] not in LOCAL_TEAM_ANSWERS]
    if unanswered:
        warnings.append(f"has_local_team answers only for AU and NZ. These configured countries will answer "
                        f"unknown: {', '.join(unanswered)}.")

    oi_raw = raw.get("offices_in", [])
    if not isinstance(oi_raw, list):
        raise ConfigError("checks.offices_in must be a list of two-letter country codes.")
    offices_in: list[str] = []
    for i, cc in enumerate(oi_raw, start=1):
        if not isinstance(cc, str) or not _CC.match(cc):
            raise ConfigError(f"checks.offices_in[{i}] must be a two-letter upper-case country code such as AU.")
        if cc in offices_in:
            raise ConfigError(f"checks.offices_in[{i}] {cc!r} is listed twice.")
        offices_in.append(cc)
        signals.append({"id": f"office_in:{cc}", "label": f"Office or staff recorded in {cc}",
                        "tool": "is_located_in", "inputs": {"countries": [cc], "where": "any_office"}})

    if not signals:
        raise ConfigError("checks must turn on at least one signal.")
    checks = {"multinational": multinational, "multiple_sites": multiple_sites, "local_teams": local_teams,
              "offices_in": offices_in}
    return checks, signals, warnings


def _load_tiers(raw: Any, signal_ids: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or len(raw) > 20:
        raise ConfigError("tiers must be a list of 1 to 20 tiers.")
    tiers: list[dict[str, Any]] = []
    for i, t in enumerate(raw, start=1):
        if not isinstance(t, dict):
            raise ConfigError(f"tiers[{i}] must be an object.")
        extra = sorted(set(t) - {"id", "label", "when"})
        if extra:
            raise ConfigError(f"tiers[{i}] has unknown key(s): {', '.join(extra)}")
        tid = t.get("id")
        if not isinstance(tid, str) or not _ID.match(tid):
            raise ConfigError(f"tiers[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if any(x["id"] == tid for x in tiers):
            raise ConfigError(f"tiers[{i}].id {tid!r} is used twice.")
        label = t.get("label", tid)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"tiers[{i}].label must be text.")
        when = t.get("when")
        if not isinstance(when, dict):
            raise ConfigError(f"tiers[{i}].when must be an object with all and/or any.")
        extra = sorted(set(when) - {"all", "any"})
        if extra:
            raise ConfigError(f"tiers[{i}].when has unknown key(s): {', '.join(extra)}")
        parts: dict[str, list[str]] = {}
        for key in ("all", "any"):
            ids = when.get(key, [])
            if not isinstance(ids, list) or not all(isinstance(s, str) for s in ids):
                raise ConfigError(f"tiers[{i}].when.{key} must be a list of signal ids.")
            for s in ids:
                if s not in signal_ids:
                    raise ConfigError(f"tiers[{i}].when.{key} names signal {s!r}, which checks does not turn on. "
                                      f"Configured signals: {', '.join(signal_ids)}.")
            if len(set(ids)) != len(ids):
                raise ConfigError(f"tiers[{i}].when.{key} repeats a signal.")
            parts[key] = list(ids)
        if not parts["all"] and not parts["any"]:
            raise ConfigError(f"tiers[{i}].when needs at least one signal in all or any.")
        tiers.append({"id": tid, "label": label, "when": parts})
    return tiers


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    checks, signals, warnings = _load_checks(raw.get("checks"))
    tiers = _load_tiers(raw.get("tiers"), [s["id"] for s in signals])
    for key in ("with_reasoning", "fetch_company"):
        if not isinstance(raw.get(key, False), bool):
            raise ConfigError(f"{key} must be true or false.")
    return {"checks": checks, "signals": signals, "tiers": tiers,
            "with_reasoning": raw.get("with_reasoning", False), "fetch_company": raw.get("fetch_company", False),
            "warnings": warnings}


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
    signals = len(config["signals"])
    calls = [PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, [])]
    if config["fetch_company"]:
        calls.append(PlannedCall("GET /company", "Company profile for each unique valid identifier", n,
                                 [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    purpose = f"{signals} signal(s) for each unique valid identifier"
    if config["warnings"]:
        purpose += ". Config warning: " + " ".join(config["warnings"])
    calls.append(PlannedCall("POST /decisions", purpose, n * signals,
                             pricing.estimate_decision(n * signals, config["with_reasoning"])))
    return calls


def _fact_text(v: Any) -> str:
    if v is None:
        return "not returned"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list):
        return ", ".join(_fact_text(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def _evaluate_tier(tier: dict[str, Any], res: dict[str, str]) -> tuple[str, str]:
    all_ids, any_ids = tier["when"]["all"], tier["when"]["any"]
    all_no = [s for s in all_ids if res[s] == "no"]
    any_yes = [s for s in any_ids if res[s] == "yes"]
    any_all_no = bool(any_ids) and all(res[s] == "no" for s in any_ids)
    if all_no or any_all_no:
        parts = []
        if all_no:
            parts.append(f"required signal(s) answered no: {', '.join(all_no)}")
        if any_all_no:
            parts.append(f"none of the signal(s) in any answered yes: {', '.join(any_ids)}")
        return "not_matched", "Not matched: " + "; ".join(parts) + "."
    blockers = [s for s in all_ids if res[s] != "yes"]
    if any_ids and not any_yes:
        blockers += [s for s in any_ids if res[s] != "no"]
    if not blockers:
        return "matched", f"Matched: {', '.join(all_ids + any_yes)} answered yes."
    errored = [s for s in blockers if res[s] in ("error", "not_asked")]
    if errored:
        return "incomplete", ("Cannot be decided: signal(s) have no answer because of an error or because the run "
                              f"stopped: {', '.join(errored)}.")
    return "needs_review", f"Would match if these signal(s) answered yes, but they answered unknown: {', '.join(blockers)}."


def _assign(tiers: list[dict[str, Any]], res: dict[str, str]) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None, str]:
    trace: list[dict[str, Any]] = []
    decided: dict[str, Any] | None = None
    status = "no_tier"
    for t in tiers:
        info = {"tier_id": t["id"], "label": t["label"], "all": t["when"]["all"], "any": t["when"]["any"]}
        if decided is not None:
            trace.append({**info, "outcome": "not_considered", "reason": "An earlier tier decided the outcome."})
            continue
        outcome, reason = _evaluate_tier(t, res)
        trace.append({**info, "outcome": outcome, "reason": reason})
        if outcome != "not_matched":
            decided = {"tier_id": t["id"], "label": t["label"], "reason": reason}
            status = {"matched": "tier_assigned", "needs_review": "needs_review", "incomplete": "incomplete"}[outcome]
    unknown = sum(1 for v in res.values() if v == "unknown")
    if status == "tier_assigned" and decided:
        text = f"First matching tier: {decided['label']}. {decided['reason']}"
    elif status == "needs_review" and decided:
        text = (f"Tier {decided['label']} depends on unknown answers. {decided['reason']} Unknown means Firmable "
                "does not hold the fact; it is not a no.")
    elif status == "incomplete" and decided:
        text = f"Tier {decided['label']} could not be decided. {decided['reason']}"
    else:
        text = "No tier matched."
        if unknown:
            text += f" {unknown} signal(s) answered unknown, and no tier's outcome depended on them."
    return trace, status, decided, text


def _conflicts(signals: list[dict[str, Any]]) -> list[str]:
    res = {s["signal_id"]: s["result"] for s in signals}
    offices_yes = [s["signal_id"].split(":", 1)[1] for s in signals
                   if s["signal_id"].startswith("office_in:") and s["result"] == "yes"]
    out: list[str] = []
    if res.get("multinational") == "no" and len(offices_yes) >= 2:
        out.append(f"is_multinational answered no, but is_located_in (any office) answered yes for "
                   f"{len(offices_yes)} countries: {', '.join(offices_yes)}. Shown as returned; not corrected.")
    if res.get("multiple_sites") == "no" and len(offices_yes) >= 2:
        out.append(f"has_multiple_sites answered no, but is_located_in (any office) answered yes for "
                   f"{len(offices_yes)} countries: {', '.join(offices_yes)}. Shown as returned; not corrected.")
    return out


_EMPTY_ANSWER = {"answer": None, "confidence": None, "details": None, "reasoning": None, "answered_at": None,
                 "credits": None, "answer_error": None, "entity_id": None, "request_id": None}


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = ["An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a "
                "no or as a yes.",
                "A no answer means the presence is not recorded in the data Firmable holds. It is not proof that the "
                "company has no presence there.",
                "Expansion signals describe where a company is recorded. They do not show buying intent."]
    if cfg["warnings"]:
        coverage.extend(cfg["warnings"])
    elif cfg["checks"]["local_teams"]:
        coverage.append("has_local_team counts staff only in AU and NZ. Other countries answer unknown.")

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}, "tier_counts": {},
                                          "signal_counts": {}}, "coverage": coverage}

    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no signals were asked.")
    problems = list(dict.fromkeys(check_tools(catalogue, cfg["signals"], "company")))
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("The configuration needs decision tools or inputs the API does not accept.")

    model_tools = sorted({s["tool"] for s in cfg["signals"] if uses_model(catalogue, s["tool"])})
    if model_tools:
        coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                        f"{', '.join(model_tools)}. This code applies only the tier rules in the configuration.")
    if not cfg["fetch_company"]:
        coverage.append("Company profiles were not fetched; signals were asked by the company_id or domain given.")

    for r in ctx.rows:
        r["_key"] = _row_key(r)
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier company identifier; their API responses were "
                        "reused, not requested again.")

    def one(row: dict[str, str]) -> dict[str, Any]:
        by, value, problem = _lookup(row)
        base: dict[str, Any] = {
            "row_id": row["row_id"], "input": row, "lookup": {"by": by, "value": value} if by else None,
            "duplicate_of": dupes.get(row["row_id"]), "company": None, "signals": [], "tiers": [],
            "tier_id": None, "tier_label": None, "deciding_tier_id": None, "data_conflicts": []}
        if not by:
            return {**base, "status": "invalid_input", "status_reason": problem}
        subject: dict[str, Any] = {"company_id": value} if by == "id" else {"domain": value}
        if cfg["fetch_company"]:
            try:
                company = client.get_company(**{by: value})
            except NotFoundError:
                return {**base, "status": "company_not_found",
                        "status_reason": "Firmable has no company profile for this identifier, so no signals were asked."}
            base["company"] = company_facts(company)
            subject = {"company_id": company["id"]}
            input_domain = normalise_domain(row.get("domain"))
            fqdn = company.get("fqdn")
            profile_domain = normalise_domain(fqdn if isinstance(fqdn, str) else None)
            if input_domain and profile_domain and input_domain != profile_domain:
                base["data_conflicts"].append(f"The input domain {input_domain} differs from the profile's fqdn "
                                              f"{profile_domain}.")
        results: list[dict[str, Any]] = []
        for s in cfg["signals"]:
            entry = {"signal_id": s["id"], "label": s["label"], "tool": s["tool"], "inputs": s["inputs"]}
            if ctx.stopped is not None:
                results.append({**entry, "result": "not_asked", **_EMPTY_ANSWER, "fact": None, "error": None})
                continue
            try:
                resp = client.make_decision(s["tool"], {**subject, **s["inputs"]}, with_reasoning=cfg["with_reasoning"])
            except NotFoundError as e:
                if e.code == "company_not_found":
                    return {**base, "status": "company_not_found", "signals": results,
                            "status_reason": "Decisions found no Firmable company for this identifier, so the "
                                             "remaining signals were not asked."}
                results.append({**entry, "result": "error", **_EMPTY_ANSWER, "fact": None,
                                "error": ctx.record_error(row["row_id"], e)})
                continue
            except FirmableError as e:
                results.append({**entry, "result": "error", **_EMPTY_ANSWER, "fact": None,
                                "error": ctx.record_error(row["row_id"], e)})
                continue
            ans = answer_from(resp)
            result = ans["answer"] if ans["answer"] in ("yes", "no", "unknown") else "error"
            details = ans["details"] if isinstance(ans["details"], dict) else {}
            key = FACT_KEYS[s["tool"]]
            fact = {"key": key, "value": details.get(key)} if key in details else None
            results.append({**entry, "result": result, **ans, "fact": fact,
                            "error": ans["answer_error"] if result == "error" else None})
        res = {s["signal_id"]: s["result"] for s in results}
        trace, status, decided, reason = _assign(cfg["tiers"], res)
        base["data_conflicts"] += _conflicts(results)
        return {**base, "status": status, "status_reason": reason, "signals": results, "tiers": trace,
                "tier_id": decided["tier_id"] if status == "tier_assigned" and decided else None,
                "tier_label": decided["label"] if status == "tier_assigned" and decided else None,
                "deciding_tier_id": decided["tier_id"] if decided else None}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    signal_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r.get("tier_id"):
            tier_counts[r["tier_id"]] = tier_counts.get(r["tier_id"], 0) + 1
        for s in r.get("signals") or []:
            sc = signal_counts.setdefault(s["signal_id"], {"yes": 0, "no": 0, "unknown": 0, "error": 0, "not_asked": 0})
            sc[s["result"]] += 1
    conflicts = sum(1 for r in rows if r.get("data_conflicts"))
    if conflicts:
        coverage.append(f"{conflicts} row(s) have answers or identifiers that disagree. They are shown as returned "
                        "and are not corrected.")
    return {"rows": rows, "summary": {"status_counts": counts, "tier_counts": tier_counts,
                                      "signal_counts": signal_counts}, "coverage": coverage}


BASE_COLUMNS = ["row_id", "input_domain", "input_company_id", "duplicate_of", "status", "status_reason", "tier_id",
                "tier_label", "deciding_tier_id", "company_id", "company_name"]
TAIL_COLUMNS = ["signal_facts", "data_conflicts"]


def csv_columns(result: dict[str, Any]) -> list[str]:
    cfg = (result.get("input") or {}).get("config") or {}
    return BASE_COLUMNS + [s["id"] for s in cfg.get("signals") or []] + TAIL_COLUMNS


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = (result.get("input") or {}).get("config") or {}
    out = []
    for r in result["rows"]:
        company = r.get("company") or {}
        by_id = {s["signal_id"]: s for s in r.get("signals") or []}
        row = {
            "row_id": r["row_id"], "input_domain": r["input"].get("domain"),
            "input_company_id": r["input"].get("company_id"), "duplicate_of": r.get("duplicate_of"),
            "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "tier_id": r.get("tier_id"), "tier_label": r.get("tier_label"),
            "deciding_tier_id": r.get("deciding_tier_id"), "company_id": company.get("id"),
            "company_name": company.get("name"),
            "signal_facts": [f"{s['signal_id']} {s['fact']['key']}={_fact_text(s['fact']['value'])}"
                             for s in r.get("signals") or [] if s.get("fact")],
            "data_conflicts": r.get("data_conflicts") or [],
        }
        for s in cfg.get("signals") or []:
            row[s["id"]] = by_id[s["id"]]["result"] if s["id"] in by_id else None
        out.append(row)
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    s = result["summary"]
    cfg = result.get("input", {}).get("config") or {}
    report.add("Result", "Expansion signal summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in (s.get("status_counts") or {}).items()) + "</p>"
               + table(["Tier", "Accounts"], [[k, v] for k, v in (s.get("tier_counts") or {}).items()])
               + table(["Signal", "Yes", "No", "Unknown", "Error", "Not asked"],
                       [[k, v["yes"], v["no"], v["unknown"], v["error"], v["not_asked"]]
                        for k, v in (s.get("signal_counts") or {}).items()])
               + note("A signal counts only on a yes. <strong>No</strong> means not recorded in the data Firmable "
                      "holds, not proof of no presence. <strong>Unknown</strong> sends an account to review, never to "
                      "a lower tier. These signals do not show buying intent.", "unknown"))
    if cfg.get("tiers"):
        report.add("Rules", "Tiers in the order they are tried", table(
            ["#", "Tier", "All of", "At least one of"],
            [[i, f"{t['label']} ({t['id']})", ", ".join(t["when"]["all"]) or "-", ", ".join(t["when"]["any"]) or "-"]
             for i, t in enumerate(cfg["tiers"], start=1)])
            + table(["Signal", "Meaning", "Decision tool", "Inputs"],
                    [[x["id"], x["label"], x["tool"], json.dumps(x["inputs"])] for x in cfg.get("signals") or []])
            + "".join(note(esc(w), "unknown") for w in cfg.get("warnings") or []))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        company = r.get("company")
        title = ((company or {}).get("name") or r["input"].get("domain") or r["input"].get("company_id")
                 or r["row_id"])
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = ""
        if r.get("tier_label"):
            left += f"<p><strong>Tier:</strong> {esc(r['tier_label'])}</p>"
        left += f"<p>{esc(reason)}</p>"
        if company:
            left += kv([("Firmable ID", company.get("id")), ("Domain", company.get("fqdn")),
                        ("Head office country", company.get("hq_country")), ("Size", company.get("company_size"))])
        elif r.get("lookup"):
            left += kv([("Asked by", r["lookup"]["by"]), ("Identifier", r["lookup"]["value"])])
        if r.get("duplicate_of"):
            left += note(f"Same company identifier as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        for c in r.get("data_conflicts") or []:
            left += note(esc(c), "review")
        right = ""
        signals = r.get("signals") or []
        if signals:
            right += "<h3>Signals, as returned</h3>" + table(
                ["Signal", "Answer", "Fact returned", "Confidence"],
                [[x["label"], Raw(badge(x["result"].replace("_", " "), RESULT_TONE.get(x["result"], "neutral"))),
                  f"{x['fact']['key']}: {_fact_text(x['fact']['value'])}" if x.get("fact") else None,
                  x.get("confidence")] for x in signals])
            for x in signals:
                rs = x.get("reasoning") or {}
                if rs.get("reason"):
                    right += (f"<p><strong>{esc(x['label'])}:</strong> {esc(rs.get('reason'))}</p>"
                              f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                              f"{esc(rs.get('last_refreshed'))}.</p>")
                if x.get("error"):
                    err = x["error"]
                    right += note(f"{esc(x['label'])}: {esc(err.get('message') if isinstance(err, dict) else err)}",
                                  "error")
            unknown = [x["label"] for x in signals if x["result"] == "unknown"]
            if unknown:
                right += "<h3>Not held by Firmable (unknown)</h3>" + bullet_list(unknown)
        if r.get("tiers"):
            right += "<h3>Tier trace</h3>" + table(["Tier", "Outcome", "Explanation"], [
                [t["label"], Raw(badge(t["outcome"].replace("_", " "), OUTCOME_TONE.get(t["outcome"], "neutral"))),
                 t["reason"]] for t in r["tiers"]])
        if signals:
            right += details_json("Signals as JSON, with details as returned", signals)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No signals were asked for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its signals and tier", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, csv_columns, csv_rows, render)
