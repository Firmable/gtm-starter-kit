"""Campaign audience check: says include, exclude or review for each account in each campaign.

Workflow for each row:
1. Check the identifier. A row needs a valid company_id or domain; company_id wins when both are given.
2. Check existing_customer. When exclude_existing_customers is true, a row marked true is excluded from
   every campaign before any paid call.
3. Fetch the company profile (GET /company) for display facts, only when fetch_company is true.
4. Ask matches_campaign_audience (POST /decisions) once per configured campaign.
5. Summarise the account with the status policy below.

Per campaign result:
- include: Firmable answered yes;
- exclude: Firmable answered no;
- review: Firmable answered unknown. Unknown is never an include or an exclude;
- excluded_customer: the row is an existing customer and exclude_existing_customers is true;
- error: the request failed or the answer was not yes, no or unknown;
- not_asked: the run stopped before this campaign was asked.

Account status, first that applies:
- in_campaigns: at least one campaign answered include;
- incomplete: no include, and at least one campaign has an error or was not asked;
- needs_review: no include, and at least one campaign answered unknown;
- no_campaign: every campaign answered exclude.
Rows with invalid input, an excluded existing customer, a row-level API error, or a stopped run get their own
status.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client.decisions import answer_from, check_tools, tool_index, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client import pricing
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "campaign-audience-check"
TITLE = "Campaign audience check"
DESCRIPTION = ("Checks each account against one or more campaign audiences with Firmable's "
               "matches_campaign_audience decision tool, and says include, exclude or review for each campaign "
               "with Firmable's reason shown as returned.")
TOOL = "matches_campaign_audience"

STATUS_TONE = {"in_campaigns": "good", "no_campaign": "bad", "needs_review": "review", "excluded_customer": "neutral",
               "incomplete": "error", "invalid_input": "error", "error": "error", "not_processed": "unknown"}
RESULT_TONE = {"include": "good", "exclude": "bad", "review": "review", "excluded_customer": "neutral",
               "error": "error", "not_asked": "unknown"}
RESULTS = ["include", "exclude", "review", "excluded_customer", "error", "not_asked"]
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_COMPANY_ID = re.compile(r"^f\d{12}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_TOP_KEYS = {"campaigns", "exclude_existing_customers", "with_reasoning", "fetch_company"}
_CAMPAIGN_ITEM_KEYS = {"id", "label", "campaign"}
CAMPAIGN_KEYS = ["min_staff", "max_staff", "staff_in", "industries", "countries", "revenue_bands", "message"]
STAFF_IN = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"]
REVENUE_BANDS = ["<$1M", "$1M - $5M", "$5M - $10M", "$10M - $50M", "$50M - $100M", "$100M - $250M",
                 "$250M - $500M", "$500M - $1B", ">$1B+"]
MAX_CAMPAIGNS = 5


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_campaign(where: str, c: Any) -> dict[str, Any]:
    if not isinstance(c, dict):
        raise ConfigError(f"{where} must be an object.")
    unknown = sorted(set(c) - set(CAMPAIGN_KEYS))
    if unknown:
        raise ConfigError(f"{where} has key(s) the campaign schema does not list: {', '.join(unknown)}.")
    msg = c.get("message")
    if not isinstance(msg, str) or not msg.strip() or not 3 <= len(msg) <= 2000:
        raise ConfigError(f"{where}.message is required and must be 3 to 2000 characters.")
    for key in ("min_staff", "max_staff"):
        v = c.get(key)
        if v is not None and (not _is_int(v) or v < 0):
            raise ConfigError(f"{where}.{key} must be a whole number, 0 or more.")
    if _is_int(c.get("min_staff")) and _is_int(c.get("max_staff")) and c["min_staff"] > c["max_staff"]:
        raise ConfigError(f"{where}.min_staff is more than {where}.max_staff.")
    staff_in = c.get("staff_in")
    if staff_in is not None and staff_in not in STAFF_IN:
        raise ConfigError(f"{where}.staff_in must be one of {', '.join(STAFF_IN)}.")
    for key in ("industries", "countries", "revenue_bands"):
        v = c.get(key)
        if v is None:
            continue
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ConfigError(f"{where}.{key} must be a list of non-empty text values.")
    for code in c.get("countries") or []:
        if not _COUNTRY.match(code):
            raise ConfigError(f"{where}.countries has {code!r}; use two-letter upper-case ISO codes such as AU.")
    for band in c.get("revenue_bands") or []:
        if band not in REVENUE_BANDS:
            raise ConfigError(f"{where}.revenue_bands has {band!r}; use one of: {', '.join(REVENUE_BANDS)}.")
    return dict(c)


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    items = raw.get("campaigns")
    if not isinstance(items, list) or not items:
        raise ConfigError("campaigns must be a non-empty list.")
    if len(items) > MAX_CAMPAIGNS:
        raise ConfigError(f"campaigns has {len(items)} items; the most allowed is {MAX_CAMPAIGNS}.")
    campaigns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"campaigns[{i}] must be an object.")
        extra = sorted(set(item) - _CAMPAIGN_ITEM_KEYS)
        if extra:
            raise ConfigError(f"campaigns[{i}] has unknown key(s): {', '.join(extra)}.")
        cid = item.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise ConfigError(f"campaigns[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if cid in seen:
            raise ConfigError(f"campaigns[{i}].id {cid!r} is used twice.")
        seen.add(cid)
        label = item.get("label", cid)
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"campaigns[{i}].label must be text.")
        campaign = _check_campaign(f"campaigns[{i}].campaign", item.get("campaign"))
        campaigns.append({"id": cid, "label": label, "campaign": campaign})
    for key in ("exclude_existing_customers", "with_reasoning", "fetch_company"):
        if key in raw and not isinstance(raw[key], bool):
            raise ConfigError(f"{key} must be true or false.")
    return {"campaigns": campaigns,
            "exclude_existing_customers": raw.get("exclude_existing_customers", True),
            "with_reasoning": raw.get("with_reasoning", True),
            "fetch_company": raw.get("fetch_company", False)}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, any_of=["domain", "company_id"])


def _existing(row: dict[str, str]) -> tuple[bool | None, str | None]:
    """Returns (value, problem). Empty means not stated."""
    v = (row.get("existing_customer") or "").strip().lower()
    if v == "":
        return None, None
    if v in ("true", "false"):
        return v == "true", None
    return None, f"existing_customer is {row.get('existing_customer')!r}; use true, false or leave it empty."


def _subject(row: dict[str, str]) -> tuple[dict[str, str] | None, str | None]:
    """Returns (subject, problem). company_id wins over domain when both are given."""
    cid = (row.get("company_id") or "").strip()
    if cid:
        if not _COMPANY_ID.match(cid):
            return None, f"company_id {cid!r} is not a Firmable company ID (f followed by 12 digits)."
        return {"company_id": cid}, None
    domain = normalise_domain(row.get("domain"))
    if not domain:
        return None, f"{row.get('domain')!r} is not a valid domain."
    return {"domain": domain}, None


def _key(subject: dict[str, str] | None) -> str:
    if not subject:
        return ""
    return "company_id:" + subject["company_id"] if "company_id" in subject else "domain:" + subject["domain"]


def _paid_subjects(rows: list[dict[str, str]], config: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for r in rows:
        existing, problem = _existing(r)
        subject, bad = _subject(r)
        if problem or bad:
            continue
        if existing and config["exclude_existing_customers"]:
            continue
        k = _key(subject)
        if k not in out:
            out.append(k)
    return out


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    n = len(_paid_subjects(rows, config))
    k = len(config["campaigns"])
    calls = [PlannedCall("GET /decisions", "Read the decision tool list and check the campaign schema", 1, [])]
    if config["fetch_company"]:
        calls.append(PlannedCall("GET /company", "Company profile for each unique account that is asked", n,
                                 [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    calls.append(PlannedCall("POST /decisions", f"{k} campaign(s) for each unique account that is asked", n * k,
                             pricing.estimate_decision(n * k, config["with_reasoning"])))
    return calls


def _schema_problems(catalogue: dict[str, Any], campaigns: list[dict[str, Any]]) -> list[str]:
    """Checks the configured campaign keys against the live Campaign schema, when the catalogue has one."""
    tool = tool_index(catalogue).get(TOOL) or {}
    schema = tool.get("input_schema") or {}
    props = (((schema.get("$defs") or {}).get("Campaign") or {}).get("properties"))
    if not isinstance(props, dict):
        return []
    problems: list[str] = []
    for c in campaigns:
        extra = sorted(k for k in c["campaign"] if k not in props)
        if extra:
            problems.append(f"Campaign {c['id']!r} uses key(s) the live {TOOL} schema does not list: "
                            f"{', '.join(extra)}.")
    return problems


def _labels(items: list[dict[str, Any]]) -> str:
    return ", ".join(c["label"] for c in items)


def _aggregate(results: list[dict[str, Any]]) -> tuple[str, str]:
    n = len(results)
    inc = [c for c in results if c["result"] == "include"]
    err = [c for c in results if c["result"] in ("error", "not_asked")]
    rev = [c for c in results if c["result"] == "review"]
    err_text = (f"{len(err)} campaign(s) have no answer because of an error or because the run stopped: "
                f"{_labels(err)}.")
    rev_text = f"{len(rev)} campaign(s) answered unknown and need review: {_labels(rev)}."
    if inc:
        tail = (f" {rev_text}" if rev else "") + (f" {err_text}" if err else "")
        return "in_campaigns", f"Included in {len(inc)} of {n} campaign(s): {_labels(inc)}.{tail}"
    if err:
        tail = f" {rev_text}" if rev else ""
        return "incomplete", f"No campaign answered include. {err_text}{tail}"
    if rev:
        return "needs_review", (f"No campaign answered include. {rev_text} Unknown means Firmable does not hold "
                                "the facts needed to answer; it is not an exclude.")
    return "no_campaign", f"All {n} campaign(s) answered exclude."


def _outcome(answer: Any) -> str:
    return {"yes": "include", "no": "exclude", "unknown": "review"}.get(answer, "error")


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    campaigns = cfg["campaigns"]
    coverage = ["A review result means Firmable answered unknown because it does not hold the facts needed to "
                "answer. It is never counted as an include or an exclude.",
                "Reasons and filter details are shown as Firmable returned them."]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}, "campaign_counts": {}},
                "coverage": coverage}

    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no campaigns were asked.")
    problems = check_tools(catalogue, [{"tool": TOOL, "inputs": {"campaign": {}}}], "company")
    problems += _schema_problems(catalogue, campaigns) if not problems else []
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all("The decision tool list does not accept the configured campaigns.")

    if uses_model(catalogue, TOOL):
        coverage.append(f"These decision tools use a decision model inside Firmable's service: {TOOL}. It judges "
                        "each campaign message against the company. This code applies only the rules in the "
                        "configuration.")
    if cfg["fetch_company"]:
        coverage.append("Company profiles were fetched for display only. Campaigns were asked by Firmable company ID "
                        "when a profile was found.")
    else:
        coverage.append("Company profiles were not fetched; campaigns were asked by the company_id or domain given.")

    for r in ctx.rows:
        subject, _ = _subject(r)
        r["_key"] = _key(subject)
    dupes = find_duplicates(ctx.rows, "_key")
    for r in ctx.rows:
        r.pop("_key")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier account; their API responses were reused, not "
                        "requested again.")

    def one(row: dict[str, str]) -> dict[str, Any]:
        existing, existing_problem = _existing(row)
        subject, subject_problem = _subject(row)
        base = {"row_id": row["row_id"], "input": row, "account_name": row.get("account_name") or None,
                "subject": subject, "existing_customer": existing, "duplicate_of": dupes.get(row["row_id"]),
                "company_lookup": "not_requested", "asked_with": None, "company": None, "campaigns": [], "needs_review": [],
                "errors": []}
        problem = subject_problem or existing_problem
        if problem:
            return {**base, "status": "invalid_input", "status_reason": problem}
        if existing and cfg["exclude_existing_customers"]:
            results = [{"campaign_id": c["id"], "label": c["label"], "result": "excluded_customer"}
                       for c in campaigns]
            return {**base, "status": "excluded_customer", "campaigns": results,
                    "status_reason": "Marked as an existing customer, so it was excluded from every campaign before "
                                     "any paid call."}
        asked = dict(subject or {})
        if cfg["fetch_company"]:
            ident = {"id": asked["company_id"]} if "company_id" in asked else {"fqdn": asked["domain"]}
            try:
                company = client.get_company(**ident)
            except NotFoundError:
                base["company_lookup"] = "not_found"
            else:
                base["company_lookup"] = "found"
                base["company"] = company_facts(company)
                if company.get("id"):
                    asked = {"company_id": company["id"]}
        base["asked_with"] = asked
        results: list[dict[str, Any]] = []
        for c in campaigns:
            entry = {"campaign_id": c["id"], "label": c["label"]}
            if ctx.stopped is not None:
                results.append({**entry, "result": "not_asked", "answer": None, "filters": None, "error": None})
                continue
            try:
                resp = client.make_decision(TOOL, {**asked, "campaign": c["campaign"]},
                                            with_reasoning=cfg["with_reasoning"])
            except FirmableError as e:
                err = ctx.record_error(row["row_id"], e)
                results.append({**entry, "result": "error", "answer": None, "filters": None, "error": err})
                continue
            ans = answer_from(resp)
            result = _outcome(ans["answer"])
            details = ans["details"] if isinstance(ans["details"], dict) else {}
            results.append({**entry, "result": result, **ans, "filters": details.get("filters"),
                            "error": ans["answer_error"] if result == "error" else None})
        status, reason = _aggregate(results)
        return {**base, "status": status, "status_reason": reason, "campaigns": results,
                "needs_review": [c["label"] for c in results if c["result"] == "review"],
                "errors": [c["label"] for c in results if c["result"] in ("error", "not_asked")]}

    rows = ctx.run_rows(ctx.rows, one)
    excluded = sum(1 for r in rows if r["status"] == "excluded_customer")
    if excluded:
        coverage.append(f"{excluded} row(s) marked as existing customers were excluded from every campaign before "
                        "any paid call.")
    not_found = sum(1 for r in rows if r.get("company_lookup") == "not_found")
    if not_found:
        coverage.append(f"{not_found} row(s) have no Firmable company profile; their campaigns were still asked with "
                        "the identifier given.")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    campaign_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for c in r.get("campaigns") or []:
            cc = campaign_counts.setdefault(c["campaign_id"], {k: 0 for k in RESULTS})
            cc[c["result"]] += 1
    return {"rows": rows, "summary": {"status_counts": counts, "campaign_counts": campaign_counts},
            "coverage": coverage}


BASE_COLUMNS = ["row_id", "account_name", "company_id", "domain", "existing_customer", "duplicate_of", "status",
                "status_reason", "company_name", "included_in", "needs_review"]


def csv_columns(result: dict[str, Any]) -> list[str]:
    cols = list(BASE_COLUMNS)
    for c in result["input"]["config"]["campaigns"]:
        cols += [f"campaign_{c['id']}", f"campaign_{c['id']}_reason"]
    return cols


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        inp = r["input"]
        results = {c["campaign_id"]: c for c in r.get("campaigns") or []}
        company = r.get("company") or {}
        row = {
            "row_id": r["row_id"], "account_name": inp.get("account_name"),
            "company_id": company.get("id") or next((c["entity_id"] for c in r.get("campaigns") or []
                                                     if c.get("entity_id")), None) or inp.get("company_id"),
            "domain": inp.get("domain"),
            "existing_customer": r.get("existing_customer"), "duplicate_of": r.get("duplicate_of"),
            "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_name": company.get("name"),
            "included_in": [c["campaign_id"] for c in r.get("campaigns") or [] if c["result"] == "include"],
            "needs_review": [c["campaign_id"] for c in r.get("campaigns") or [] if c["result"] == "review"],
        }
        for c in result["input"]["config"]["campaigns"]:
            got = results.get(c["id"]) or {}
            row[f"campaign_{c['id']}"] = got.get("result")
            row[f"campaign_{c['id']}_reason"] = (got.get("reasoning") or {}).get("reason")
        out.append(row)
    return out


def _show(v: Any) -> str:
    return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)


def _title(r: dict[str, Any]) -> str:
    inp = r["input"]
    return ((r.get("company") or {}).get("name") or inp.get("account_name") or inp.get("domain")
            or inp.get("company_id") or r["row_id"])


def render(report: Any, result: dict[str, Any]) -> None:
    campaigns = result["input"]["config"]["campaigns"]
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Campaign audience summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("<strong>Review</strong> means Firmable answered unknown. It is not an exclude. An account "
                      "with no include and at least one review is <strong>needs review</strong>.", "unknown"))
    report.add("Campaigns", "Configured campaigns", table(
        ["Id", "Label", "Message", "Audience filters"],
        [[c["id"], c["label"], c["campaign"].get("message"),
          "; ".join(f"{k}: {_show(v)}" for k, v in c["campaign"].items() if k != "message") or "none"]
         for c in campaigns]))
    grid = []
    for r in result["rows"]:
        by_id = {c["campaign_id"]: c for c in r.get("campaigns") or []}
        cells: list[Any] = [_title(r), Raw(badge(r["status"].replace("_", " "), STATUS_TONE.get(r["status"], "neutral")))]
        for c in campaigns:
            got = by_id.get(c["id"])
            if not got:
                cells.append("none")
                continue
            reason = (got.get("reasoning") or {}).get("reason")
            cells.append(Raw(badge(got["result"].replace("_", " "), RESULT_TONE.get(got["result"], "neutral"))
                             + (f"<br><span class=\"muted\">{esc(reason)}</span>" if reason else "")))
        grid.append(cells)
    report.add("Grid", "Accounts by campaign", table(["Account", "Status"] + [c["label"] for c in campaigns], grid))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        left += kv([("Asked with", ", ".join(f"{k} {v}" for k, v in (r.get("asked_with") or {}).items()) or None),
                    ("Existing customer", {True: "yes", False: "no"}.get(r.get("existing_customer"))),
                    ("Company profile", (r.get("company_lookup") or "").replace("_", " ") or None)])
        company = r.get("company")
        if company:
            left += kv([("Firmable ID", company.get("id")), ("Name", company.get("name")),
                        ("Domain", company.get("fqdn")), ("Head office country", company.get("hq_country")),
                        ("Size", company.get("company_size")),
                        ("Industries", ", ".join(company.get("industries") or []) or None),
                        ("Revenue", company.get("revenue"))])
        if r.get("company_lookup") == "not_found":
            left += note("Firmable has no company profile for this identifier. Campaigns were still asked.", "unknown")
        if r.get("duplicate_of"):
            left += note(f"Same account as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        results = r.get("campaigns") or []
        for c in results:
            right += (f"<h3>{esc(c['label'])} "
                      f"{badge(c['result'].replace('_', ' '), RESULT_TONE.get(c['result'], 'neutral'))}</h3>")
            if c.get("answer") is not None:
                right += kv([("Answer", c.get("answer")), ("Confidence", c.get("confidence")),
                             ("Answered at", c.get("answered_at"))])
            rs = c.get("reasoning") or {}
            if rs.get("reason"):
                right += (f"<p><strong>Firmable's reason:</strong> {esc(rs.get('reason'))}</p>"
                          f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                          f"{esc(rs.get('last_refreshed'))}."
                          + (f" Would change if: {esc(rs.get('would_change_if'))}." if rs.get("would_change_if") else "")
                          + "</p>")
            if c.get("filters") is not None:
                right += details_json("Audience filter details as returned", c["filters"])
            if c.get("error"):
                msg = c["error"].get("message") if isinstance(c["error"], dict) else c["error"]
                right += note(esc(msg), "error")
        if r.get("needs_review"):
            right += "<h3>Needs review</h3>" + bullet_list(r["needs_review"])
        if results:
            right += details_json("Campaign results as JSON", results)
        cards.append(card(_title(r), badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No campaigns were asked for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its campaign answers", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, csv_columns, csv_rows, render)
