"""Contact health check: reviews whether each contact in a list is still current and reachable.

Workflow for each row:
1. Validate the person identifier, the expected employer and the optional expected title, email and mobile.
2. Resolve an expected employer domain to a Firmable company ID (GET /company), once per domain.
3. Fetch the person profile (GET /people), only when fetch_person is true.
4. Ask each configured Decisions check (POST /decisions).
5. Apply the health policy below.

Health policy, first status that applies:
- stale: the employment or role check answered no;
- incomplete: the employment or role check has no answer because of an error or because the run stopped;
- needs_review: the employment or role check answered unknown or could not be asked, or no
  employment or role check was asked at all;
- healthy: employment and role agree, and at least one channel is confirmed: the email check answered
  yes, or the mobile check answered yes and the Do Not Call check did not answer yes or unknown;
- incomplete: no channel is confirmed and a reachability check has no answer because of an error;
- reach_unconfirmed: employment and role agree, but no channel is confirmed;
- current: employment and role agree, and every reachability check is turned off.
Rows with an invalid identifier, no person profile, or an API error get their own status.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, compare, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import person_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "contact-health-check"
TITLE = "Contact health check"
DESCRIPTION = ("Checks whether each contact still works at the expected employer and holds the expected role, and "
               "what Firmable holds about their email deliverability, mobile and the Australian Do Not Call "
               "register. These are Firmable-held observations, not live mailbox, telephone or legal checks.")

CHECK_IDS = ["employment", "role", "email", "mobile", "dnc"]
TOOLS = {"employment": "works_at_company", "role": "is_contact_current", "email": "is_email_valid",
         "mobile": "is_mobile_valid", "dnc": "is_mobile_on_dnc"}
LABELS = {"employment": "Works at the expected employer", "role": "Still in the expected role",
          "email": "Work email likely to deliver", "mobile": "Mobile held by Firmable",
          "dnc": "Mobile on the Australian Do Not Call register"}
WANT = {"employment": "yes", "role": "yes", "email": "yes", "mobile": "yes", "dnc": "no"}
CURRENT = ("employment", "role")
REACH = ("email", "mobile", "dnc")
ID_COLUMNS = ["person_id", "work_email", "linkedin_url"]

STATUS_TONE = {"healthy": "good", "current": "good", "reach_unconfirmed": "review", "stale": "bad",
               "needs_review": "review", "incomplete": "error", "person_not_found": "unknown",
               "invalid_input": "error", "error": "error", "not_processed": "unknown"}
RESULT_TONE = {"pass": "good", "fail": "bad", "unknown": "unknown", "error": "error", "not_asked": "neutral",
               "not_checked": "unknown", "not_supplied": "neutral"}
_TOP_KEYS = {"fetch_person", "with_reasoning", "checks"}
_PERSON_ID = re.compile(r"^fp\d{12}$")
_COMPANY_ID = re.compile(r"^f\d{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINKEDIN = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.IGNORECASE)
_MOBILE = re.compile(r"^[\d\s()+.\-]+$")
_GET_PEOPLE_PARAM = {"person_id": "id", "work_email": "work_email", "linkedin_url": "ln_url"}


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    checks = raw.get("checks", list(CHECK_IDS))
    if not isinstance(checks, list) or not checks:
        raise ConfigError("checks must be a non-empty list.")
    seen: list[str] = []
    for i, c in enumerate(checks, start=1):
        if c not in CHECK_IDS:
            raise ConfigError(f"checks[{i}] must be one of: {', '.join(sorted(CHECK_IDS))}.")
        if c in seen:
            raise ConfigError(f"checks[{i}] {c!r} is listed twice.")
        seen.append(c)
    for key in ("fetch_person", "with_reasoning"):
        if not isinstance(raw.get(key, key == "with_reasoning"), bool):
            raise ConfigError(f"{key} must be true or false.")
    return {"fetch_person": raw.get("fetch_person", False), "with_reasoning": raw.get("with_reasoning", True),
            "checks": [c for c in CHECK_IDS if c in seen]}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=ID_COLUMNS)


def parse_row(row: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """Returns the checked values of one row, or a reason the row cannot be checked."""
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
    company_id = row.get("expected_company_id") or None
    if company_id and not _COMPANY_ID.match(company_id):
        return None, f"expected_company_id {company_id!r} is not a Firmable company ID (f followed by 12 digits)."
    domain = None
    if not company_id and row.get("expected_domain"):
        domain = normalise_domain(row.get("expected_domain"))
        if not domain:
            return None, f"expected_domain {row.get('expected_domain')!r} is not a valid domain."
    title = row.get("expected_title") or None
    if title is not None and not 2 <= len(title) <= 200:
        return None, "expected_title must be 2 to 200 characters."
    email = row.get("email_to_check") or None
    if email is not None and not _EMAIL.match(email):
        return None, f"email_to_check {email!r} is not an email address."
    mobile = row.get("mobile_to_check") or None
    if mobile is not None and not (6 <= len(mobile) <= 40 and _MOBILE.match(mobile)):
        return None, "mobile_to_check must be 6 to 40 digits, spaces, brackets, dots, dashes or a plus sign."
    return {"id_type": kind, "id_value": row[kind], "company_id": company_id, "domain": domain,
            "title": title, "email": email, "mobile": mobile}, None


def _question_keys(p: dict[str, Any], checks: list[str]) -> list[str]:
    subject = f"{p['id_type']}={p['id_value']}"
    employer = p["company_id"] or (f"domain={p['domain']}" if p["domain"] else "")
    keys = []
    for c in checks:
        if c == "employment" and employer:
            keys.append(f"employment|{subject}|{employer}")
        elif c == "role" and p["title"]:
            keys.append(f"role|{subject}|{employer}|{p['title']}")
        elif c == "email":
            keys.append(f"email|{subject}|{p['email'] or ''}")
        elif c in ("mobile", "dnc"):
            keys.append(f"{c}|{subject}|{p['mobile'] or ''}")
    return keys


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    persons: list[str] = []
    domains: list[str] = []
    questions: list[str] = []
    for r in rows:
        p, _ = parse_row(r)
        if p is None:
            continue
        key = f"{p['id_type']}={p['id_value']}"
        if key not in persons:
            persons.append(key)
        if p["domain"] and ("employment" in config["checks"] or "role" in config["checks"]) \
                and p["domain"] not in domains:
            domains.append(p["domain"])
        for q in _question_keys(p, config["checks"]):
            if q not in questions:
                questions.append(q)
    n = len(questions)
    calls = [PlannedCall("GET /decisions", "Read the decision tool list and check the tools this recipe uses", 1, []),
             PlannedCall("GET /company", "Resolve each unique expected employer domain to a company ID", len(domains),
                         [("company", pricing.round4(pricing.COMPANY_PROFILE * len(domains)))])]
    if config["fetch_person"]:
        calls.append(PlannedCall("GET /people", "Person profile for each unique valid identifier", len(persons),
                                 [("person", pricing.round4(pricing.PERSON_PROFILE * len(persons)))]))
    calls.append(PlannedCall("POST /decisions", "Each configured check that has the inputs it needs, once per "
                             "unique question", n, pricing.estimate_decision(n, config["with_reasoning"])))
    return calls


def _labels(items: list[dict[str, Any]]) -> str:
    return ", ".join(c["label"] for c in items)


def _phone_channel(by: dict[str, dict[str, Any]]) -> str:
    mobile = by.get("mobile")
    if mobile is None:
        return "not_checked"
    if mobile["result"] == "fail":
        return "not_held"
    if mobile["result"] == "unknown":
        return "unknown"
    if mobile["result"] != "pass":
        return "error"
    dnc = by.get("dnc")
    if dnc is None:
        return "held_dnc_not_checked"
    return {"pass": "held_not_on_dnc", "fail": "held_on_dnc", "unknown": "held_dnc_unknown"}.get(
        dnc["result"], "held_dnc_error")


def _aggregate(checks: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    by = {c["check_id"]: c for c in checks}
    current = [c for c in checks if c["check_id"] in CURRENT]
    failed = [c for c in current if c["result"] == "fail"]
    if failed:
        return "stale", f"Firmable data does not match the contact record: {_labels(failed)} answered no.", []
    errored = [c for c in current if c["result"] in ("error", "not_asked")]
    if errored:
        return "incomplete", ("Employment or role check(s) have no answer because of an error or because the run "
                              f"stopped: {_labels(errored)}."), []
    unknown = [c for c in current if c["result"] in ("unknown", "not_checked")]
    if unknown:
        reason = f"Employment or role check(s) could not be confirmed: {_labels(unknown)}."
        if any(c["result"] == "unknown" for c in unknown):
            reason += " Unknown means Firmable does not hold the fact; it is not a no."
        for c in unknown:
            if c["result"] == "not_checked":
                reason += f" {c['note']}"
        return "needs_review", reason, []
    passed = [c for c in current if c["result"] == "pass"]
    if not passed:
        return "needs_review", ("No employment or role check was asked, because the row has no expected employer "
                                "or title, or those checks are turned off."), []
    agrees = f"Firmable data agrees with the contact record: {_labels(passed)}."
    reach = [c for c in checks if c["check_id"] in REACH]
    if not reach:
        return "current", f"{agrees} Reachability checks are turned off.", []
    channels = []
    if (by.get("email") or {}).get("result") == "pass":
        channels.append("email")
    if _phone_channel(by) in ("held_not_on_dnc", "held_dnc_not_checked"):
        channels.append("mobile")
    if channels:
        return "healthy", f"{agrees} Channel(s) confirmed by Firmable data: {', '.join(channels)}.", channels
    reach_err = [c for c in reach if c["result"] in ("error", "not_asked")]
    if reach_err:
        return "incomplete", (f"{agrees} No channel is confirmed, and reachability check(s) have no answer: "
                              f"{_labels(reach_err)}."), []
    return "reach_unconfirmed", f"{agrees} No email or mobile channel is confirmed by Firmable data.", []


def _flags(by: dict[str, dict[str, Any]]) -> list[str]:
    flags = []
    emp, role = by.get("employment"), by.get("role")
    if emp and role and {emp.get("answer"), role.get("answer")} == {"yes", "no"}:
        flags.append(f"works_at_company answered {emp['answer']} and is_contact_current answered {role['answer']}. "
                     "Both answers are shown as returned; neither is corrected.")
    email = by.get("email")
    if email and email.get("answer") == "no":
        grade = (email.get("details") or {}).get("deliverability_grade")
        flags.append("The email check answered no. The tool describes a no as a low deliverability grade, not a "
                     "bounce." + (f" Grade returned: {grade}." if grade else ""))
    dnc = by.get("dnc")
    if dnc and dnc.get("answer") == "yes":
        as_at = (dnc.get("details") or {}).get("as_at")
        flags.append("Firmable records a mobile for this person on the Australian Do Not Call register"
                     + (f" as at {as_at}" if as_at else "") + ". This is not a live or legal check.")
    return flags


def _refresh(checks: list[dict[str, Any]]) -> list[str]:
    out = []
    for c in checks:
        parts = []
        if c.get("as_at"):
            parts.append(f"as at {c['as_at']}")
        if c.get("last_refreshed"):
            parts.append(f"last refreshed {c['last_refreshed']}")
        if c.get("fact_date"):
            parts.append(f"fact dated {c['fact_date']}")
        if parts:
            out.append(f"{c['label']}: {', '.join(parts)}")
    return out


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = [
        "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no "
        "or as a pass.",
        "Email, mobile and Do Not Call answers are Firmable-held observations as at Firmable's last refresh. They "
        "are not live mailbox or telephone checks, and they are not a legal Do Not Call compliance check.",
        "is_mobile_on_dnc covers Australian numbers only; other numbers answer unknown.",
        "This recipe sends no email or SMS and makes no calls.",
    ]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}}, "coverage": coverage}

    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no checks were asked.")
    parsed = [parse_row(r)[0] for r in ctx.rows]
    uses_email = any(p and p["email"] for p in parsed)
    uses_mobile = any(p and p["mobile"] for p in parsed)
    wanted = []
    for c in cfg["checks"]:
        inputs: dict[str, Any] = {}
        if c == "role":
            inputs["title"] = "Example title"
        if c == "email" and uses_email:
            inputs["email"] = "name@example.com"
        if c in ("mobile", "dnc") and uses_mobile:
            inputs["mobile"] = "0400000000"
        wanted.append({"tool": TOOLS[c], "inputs": inputs})
    problems = check_tools(catalogue, wanted, "person")
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("The decision tools this recipe needs are missing or have changed their inputs.")
    model_tools = sorted({TOOLS[c] for c in cfg["checks"] if uses_model(catalogue, TOOLS[c])})
    if model_tools:
        coverage.append(f"These decision tools use a decision model inside Firmable's service: "
                        f"{', '.join(model_tools)}. This code applies only the rules in the health policy.")
    if not cfg["fetch_person"]:
        coverage.append("Person profiles were not fetched; checks were asked with the identifier in each row.")
    coverage.append("A LinkedIn URL can match the wrong person when a record carries someone else's link. Rows "
                    "with a person_id are asked by person_id.")

    for r, p in zip(ctx.rows, parsed):
        r["_key"] = f"{p['id_type']}={p['id_value']}" if p else ""
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier person identifier; identical requests were "
                        "answered from memory, not sent again.")
    companies: dict[str, dict[str, Any]] = {}

    def resolve(domain: str, row_id: str) -> dict[str, Any]:
        if domain in companies:
            return companies[domain]
        try:
            company = client.get_company(fqdn=domain)
            out = {"id": company["id"], "name": company.get("name"), "error": None}
        except NotFoundError:
            out = {"id": None, "name": None, "error": None}
        except FirmableError as e:
            return {"id": None, "name": None, "error": ctx.record_error(row_id, e)}
        companies[domain] = out
        return out

    def one(row: dict[str, str]) -> dict[str, Any]:
        p, problem = parse_row(row)
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "duplicate_of": dupes.get(row["row_id"]),
                                "identifier": None, "expected_company_id": None, "expected_title": None,
                                "person": None, "checks": [], "phone_channel": None, "channels": [], "flags": [],
                                "last_refresh": [], "missing_information": []}
        if p is None:
            return {**base, "status": "invalid_input", "status_reason": problem}
        base["identifier"] = {"type": p["id_type"], "value": p["id_value"]}
        base["expected_title"] = p["title"]
        subject = {p["id_type"]: p["id_value"]}
        if cfg["fetch_person"]:
            try:
                person = client.get_person(**{_GET_PEOPLE_PARAM[p["id_type"]]: p["id_value"]})
            except NotFoundError:
                return {**base, "status": "person_not_found",
                        "status_reason": "Firmable has no person profile for this identifier, so no checks were asked."}
            base["person"] = person_facts(person)
            if _PERSON_ID.match(str(person["id"])):
                subject = {"person_id": person["id"]}
        company_id = p["company_id"]
        employer_problem: tuple[str, Any] | None = None
        needs_employer = "employment" in cfg["checks"] or "role" in cfg["checks"]
        if p["domain"] and needs_employer:
            company = resolve(p["domain"], row["row_id"])
            if company["error"]:
                employer_problem = ("error", company["error"])
            elif company["id"] is None:
                employer_problem = ("not_checked", "Firmable has no company profile for the expected employer domain.")
            company_id = company["id"]
        base["expected_company_id"] = company_id
        results: list[dict[str, Any]] = []
        for c in cfg["checks"]:
            entry: dict[str, Any] = {"check_id": c, "label": LABELS[c], "tool": TOOLS[c], "want": WANT[c],
                                     "answer": None, "note": None, "error": None}
            question: dict[str, Any] = dict(subject)
            if c == "employment":
                if employer_problem is not None:
                    kind, detail = employer_problem
                    results.append({**entry, "result": kind, "error": detail if kind == "error" else None,
                                    "note": detail if kind == "not_checked" else
                                    "The expected employer could not be resolved because of an error."})
                    continue
                if not company_id:
                    results.append({**entry, "result": "not_supplied", "note": "No expected employer was supplied."})
                    continue
                question["company_id"] = company_id
            elif c == "role":
                if not p["title"]:
                    results.append({**entry, "result": "not_supplied", "note": "No expected title was supplied."})
                    continue
                question["title"] = p["title"]
                if company_id:
                    question["company_id"] = company_id
            elif c == "email" and p["email"]:
                question["email"] = p["email"]
            elif c in ("mobile", "dnc") and p["mobile"]:
                question["mobile"] = p["mobile"]
            if ctx.stopped is not None:
                results.append({**entry, "result": "not_asked"})
                continue
            try:
                resp = client.make_decision(TOOLS[c], question, with_reasoning=cfg["with_reasoning"])
            except NotFoundError as e:
                if e.code == "person_not_found":
                    return {**base, "status": "person_not_found", "checks": results,
                            "status_reason": "Decisions found no Firmable person for this identifier, so the remaining "
                                             "checks were not asked."}
                results.append({**entry, "result": "error", "error": ctx.record_error(row["row_id"], e)})
                continue
            except FirmableError as e:
                results.append({**entry, "result": "error", "error": ctx.record_error(row["row_id"], e)})
                continue
            ans = answer_from(resp)
            result = compare(ans["answer"], WANT[c])
            reasoning = ans["reasoning"] or {}
            results.append({**entry, **ans, "result": result, "note": None,
                            "error": ans["answer_error"] if result == "error" else None,
                            "as_at": (ans["details"] or {}).get("as_at") if isinstance(ans["details"], dict) else None,
                            "last_refreshed": reasoning.get("last_refreshed"),
                            "fact_date": reasoning.get("fact_date")})
        status, reason, channels = _aggregate(results)
        by = {c["check_id"]: c for c in results}
        return {**base, "status": status, "status_reason": reason, "checks": results,
                "phone_channel": _phone_channel(by), "channels": channels, "flags": _flags(by),
                "last_refresh": _refresh(results),
                "missing_information": [c["label"] for c in results if c["result"] in ("unknown", "not_checked")]}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    check_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for c in r.get("checks") or []:
            cc = check_counts.setdefault(c["check_id"], {k: 0 for k in RESULT_TONE})
            cc[c["result"]] += 1
    return {"rows": rows, "summary": {"status_counts": counts, "check_counts": check_counts}, "coverage": coverage}


CSV_COLUMNS = ["row_id", "identifier", "duplicate_of", "status", "status_reason", "person_id", "expected_company_id",
               "expected_title", "employment", "role", "email", "mobile", "dnc", "phone_channel", "flags",
               "last_refresh"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        by = {c["check_id"]: c for c in r.get("checks") or []}
        ident = r.get("identifier")
        person = r.get("person") or {}
        row = {
            "row_id": r["row_id"], "identifier": f"{ident['type']}:{ident['value']}" if ident else None,
            "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "person_id": person.get("id"), "expected_company_id": r.get("expected_company_id"),
            "expected_title": r.get("expected_title"), "phone_channel": r.get("phone_channel"),
            "flags": r.get("flags") or [], "last_refresh": r.get("last_refresh") or [],
        }
        for c in CHECK_IDS:
            ch = by.get(c)
            row[c] = f"{ch['result']}({ch.get('answer') or '-'})" if ch else None
        out.append(row)
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Contact health summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("Unknown means Firmable does not hold the fact. A contact whose employment or role answered "
                      "unknown is <strong>needs review</strong>, never healthy.", "unknown")
               + note("Email, mobile and Do Not Call answers are Firmable-held observations as at its last refresh. "
                      "They are not live mailbox, telephone or legal checks.", "info"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        ident = r.get("identifier") or {}
        person = r.get("person") or {}
        title = person.get("name") or ident.get("value") or r["row_id"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        if ident:
            left += kv([("Identifier", f"{ident.get('type')}: {ident.get('value')}"),
                        ("Expected employer ID", r.get("expected_company_id")),
                        ("Expected title", r.get("expected_title")),
                        ("Phone channel", (r.get("phone_channel") or "").replace("_", " ") or None),
                        ("Firmable person ID", person.get("id")), ("Current position", person.get("position")),
                        ("Current employer", person.get("current_company_name"))])
        if r.get("duplicate_of"):
            left += note(f"Same identifier as row {esc(r['duplicate_of'])}; identical requests were reused.", "info")
        for f in r.get("flags") or []:
            left += note(esc(f), "review")
        right = ""
        checks = r.get("checks") or []
        if checks:
            right += table(["Check", "Tool", "Wanted", "Answer", "Result", "Confidence"], [
                [c["label"], c["tool"], c["want"], c.get("answer") or "none",
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
                if c.get("note"):
                    right += f"<p class=\"muted\">{esc(c['label'])}: {esc(c['note'])}</p>"
                if c.get("error"):
                    msg = c["error"].get("message") if isinstance(c["error"], dict) else c["error"]
                    right += note(f"{esc(c['label'])}: {esc(msg)}", "error")
        if r.get("last_refresh"):
            right += "<h3>Last refresh, as returned</h3>" + bullet_list(r["last_refresh"])
        if r.get("missing_information"):
            right += "<h3>Missing information</h3>" + bullet_list(r["missing_information"])
        right += details_json("Check results as JSON", checks) if checks else ""
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No checks were asked for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Contacts", "Each contact with its checks", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
