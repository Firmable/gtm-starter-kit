"""Duplicate account review: reviews candidate duplicate pairs of company records.

Each input row is one pair that you supply. The recipe never builds pairs of its own, so it never
compares every row with every other row.

Workflow for each pair:
1. Read each side: a Firmable company ID, or a domain. An invalid side is reported.
2. Fetch both company profiles (GET /company).
3. Compare the identifiers the profiles hold: ABN, ACN, NZBN, domain, LinkedIn handle, website domain,
   name and head office country.
4. Ask is_same_business (POST /decisions) with the left company ID and the right as other_company_id.
5. Apply the outcome rules below.

Outcome rules, in order:
- invalid_input: a side has no valid identifier;
- needs_review: a side has no Firmable profile;
- needs_review: a side's input domain differs from its profile's domain;
- likely_duplicate: both sides resolve to the same Firmable company record;
- incomplete: is_same_business returned an error or was not asked because the run stopped;
- needs_review: is_same_business answered unknown;
- needs_review: is_same_business answered yes, but the profiles hold different ABN, ACN or NZBN values;
- needs_review: is_same_business answered no, but the profiles share an ABN, ACN, NZBN, domain or LinkedIn handle;
- likely_duplicate: is_same_business answered yes;
- distinct: is_same_business answered no.
Nothing is merged or deleted.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, uses_model
from firmable_client.errors import ConfigError, FirmableError, InputError, NotFoundError
from firmable_client.facts import company_facts
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "duplicate-account-review"
TITLE = "Duplicate account review"
DESCRIPTION = ("Reviews candidate duplicate pairs of company records that you supply, using Firmable company "
               "profiles and the is_same_business decision tool, and explains which identifiers agree or "
               "conflict. Nothing is merged or deleted.")
TOOL = "is_same_business"

STATUS_TONE = {"likely_duplicate": "good", "distinct": "neutral", "needs_review": "review", "incomplete": "error",
               "invalid_input": "error", "error": "error", "not_processed": "unknown"}
COMPARISON_TONE = {"same": "good", "different": "bad", "missing": "unknown"}
_COMPANY_ID = re.compile(r"^f\d{12}$")
_TOP_KEYS = {"with_reasoning"}
# Identifiers compared, in report order.
IDENTIFIERS = ["abn", "acn", "nzbn", "fqdn", "linkedin", "website_domain", "name", "hq_country"]
REGISTRY = ["abn", "acn", "nzbn"]
STRONG_SAME = ["abn", "acn", "nzbn", "fqdn", "linkedin"]
SIDES = ("left", "right")


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    with_reasoning = raw.get("with_reasoning", True)
    if not isinstance(with_reasoning, bool):
        raise ConfigError("with_reasoning must be true or false.")
    return {"with_reasoning": with_reasoning}


def read_input(path: Path) -> list[dict[str, str]]:
    rows = read_rows(path, any_of=["left_domain", "left_company_id"])
    problems = [f"row {n} ({r['row_id']}): needs one of right_domain, right_company_id"
                for n, r in enumerate(rows, start=1) if not (r.get("right_domain") or r.get("right_company_id"))]
    if problems:
        raise InputError(f"{path.name} has problems:\n  " + "\n  ".join(problems))
    return rows


def _side(row: dict[str, str], side: str) -> dict[str, Any]:
    """The lookup for one side: {by, value, input_domain, problem}."""
    cid = (row.get(f"{side}_company_id") or "").strip()
    raw_domain = row.get(f"{side}_domain")
    domain = normalise_domain(raw_domain)
    if cid:
        if _COMPANY_ID.match(cid):
            return {"by": "id", "value": cid, "input_domain": domain, "problem": None}
        return {"by": None, "value": None, "input_domain": domain,
                "problem": f"{side}_company_id {cid!r} is not a Firmable company ID (f followed by 12 digits)."}
    if domain:
        return {"by": "fqdn", "value": domain, "input_domain": domain, "problem": None}
    return {"by": None, "value": None, "input_domain": None,
            "problem": f"{side}_domain {raw_domain!r} is not a valid domain."}


def _side_key(s: dict[str, Any]) -> str:
    return f"{s['by']}:{s['value']}" if s["by"] else ""


def _pair_key(row: dict[str, str]) -> str:
    keys = [_side_key(_side(row, side)) for side in SIDES]
    if not all(keys):
        return ""
    return "|".join(sorted(keys))


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    sides: list[str] = []
    pairs: list[str] = []
    for r in rows:
        keys = [_side_key(_side(r, side)) for side in SIDES]
        if not all(keys):
            continue
        for k in keys:
            if k not in sides:
                sides.append(k)
        pk = "|".join(sorted(keys))
        if keys[0] != keys[1] and pk not in pairs:
            pairs.append(pk)
    return [
        PlannedCall("GET /decisions", f"Read the decision tool list and check that {TOOL} is available", 1, []),
        PlannedCall("GET /company", "Company profile for each unique valid side", len(sides),
                    [("company", pricing.round4(pricing.COMPANY_PROFILE * len(sides)))]),
        PlannedCall("POST /decisions", f"{TOOL} for each unique valid pair of different identifiers", len(pairs),
                    pricing.estimate_decision(len(pairs), config["with_reasoning"])),
    ]


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    s = str(value).strip()
    return s or None


def _identifier_values(c: dict[str, Any]) -> dict[str, Any]:
    codes = c.get("industry_codes") if isinstance(c.get("industry_codes"), dict) else {}
    return {"abn": codes.get("abn"), "acn": codes.get("acn"), "nzbn": codes.get("nzbn"), "fqdn": c.get("fqdn"),
            "linkedin": c.get("linkedin"), "website_domain": c.get("website"), "name": c.get("name"),
            "hq_country": c.get("hq_country")}


def _comparable(identifier: str, value: Any) -> str | None:
    s = _text(value)
    if s is None:
        return None
    if identifier in REGISTRY:
        s = re.sub(r"\s+", "", s)
    if identifier in ("fqdn", "website_domain"):
        return normalise_domain(s)
    return s.lower()


def _compare_identifiers(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    lv, rv = _identifier_values(left), _identifier_values(right)
    out = []
    for ident in IDENTIFIERS:
        a, b = _comparable(ident, lv[ident]), _comparable(ident, rv[ident])
        comparison = "missing" if a is None or b is None else ("same" if a == b else "different")
        out.append({"identifier": ident, "left": lv[ident], "right": rv[ident], "comparison": comparison})
    return out


def _names(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    coverage = ["Only the pairs in the input file are compared. The recipe does not compare every row with every "
                "other row.",
                "Nothing is merged or deleted. Each outcome is a recommendation for a person to act on.",
                "A missing identifier was not observed in retrieved data. It does not mean the company has none.",
                "An unknown answer from is_same_business is never treated as distinct."]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}, "answer_counts": {}},
                "coverage": coverage}

    try:
        catalogue = client.list_decision_tools()
    except FirmableError as e:
        ctx.record_error(None, e)
        return stop_all("The decision tool list could not be read, so no pairs were reviewed.")
    problems = check_tools(catalogue, [{"tool": TOOL, "inputs": {"other_company_id": ""}}], "company")
    if problems:
        for p in problems:
            ctx.errors.append({"row_id": None, "kind": "config", "status": None, "code": None,
                               "endpoint": "GET /decisions", "message": p, "charge_uncertain": False,
                               "retry_after": None})
        coverage.extend(problems)
        return stop_all(f"The decision tool list does not offer {TOOL} as this recipe uses it.")
    if uses_model(catalogue, TOOL):
        coverage.append(f"{TOOL} uses a decision model inside Firmable's service, according to GET /decisions. "
                        "This code applies only the rules in this recipe.")
    else:
        coverage.append(f"{TOOL} does not use a decision model, according to GET /decisions. Other Firmable tools "
                        "may.")

    for r in ctx.rows:
        r["_pair"] = _pair_key(r)
    dupes = find_duplicates(ctx.rows, "_pair")
    for r in ctx.rows:
        r.pop("_pair")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier pair, in either order; their API responses were "
                        "reused, not requested again.")
    asked: dict[str, dict[str, Any]] = {}

    def one(row: dict[str, str]) -> dict[str, Any]:
        sides = {side: _side(row, side) for side in SIDES}
        base: dict[str, Any] = {
            "row_id": row["row_id"], "input": row, "left_row_id": row.get("left_row_id") or None,
            "right_row_id": row.get("right_row_id") or None, "duplicate_of": dupes.get(row["row_id"]),
            "left": {"lookup": None, "found": None, "company": None},
            "right": {"lookup": None, "found": None, "company": None},
            "identifiers": [], "supporting_identifiers": [], "conflicting_identifiers": [],
            "missing_identifiers": [], "contradictions": [], "notes": [], "decision": None}
        for side in SIDES:
            s = sides[side]
            base[side] = {"lookup": {"by": s["by"], "value": s["value"]} if s["by"] else None, "found": None,
                          "company": None}
        problems = [sides[side]["problem"] for side in SIDES if sides[side]["problem"]]
        if problems:
            return {**base, "status": "invalid_input", "status_reason": " ".join(problems)}

        profiles: dict[str, dict[str, Any] | None] = {}
        for side in SIDES:
            s = sides[side]
            try:
                profiles[side] = client.get_company(**{s["by"]: s["value"]})
            except NotFoundError:
                profiles[side] = None
            c = profiles[side]
            facts = company_facts(c)
            if facts is not None:
                facts["industry_codes"] = c.get("industry_codes") if isinstance(c.get("industry_codes"), dict) else None
            base[side] = {**base[side], "found": c is not None, "company": facts}
        missing = [side for side in SIDES if profiles[side] is None]
        if missing:
            which = "either side" if len(missing) == 2 else f"the {missing[0]} side ({sides[missing[0]]['value']})"
            return {**base, "status": "needs_review",
                    "status_reason": f"Firmable has no company profile for {which}, so the pair was not compared."}

        left, right = profiles["left"], profiles["right"]
        assert left is not None and right is not None
        idents = _compare_identifiers(left, right)
        base["identifiers"] = idents
        base["supporting_identifiers"] = [i["identifier"] for i in idents if i["comparison"] == "same"]
        base["conflicting_identifiers"] = [i["identifier"] for i in idents if i["comparison"] == "different"]
        base["missing_identifiers"] = [i["identifier"] for i in idents if i["comparison"] == "missing"]
        agree = (f"Identifiers that agree: {_names(base['supporting_identifiers'])}. "
                 f"Identifiers that differ: {_names(base['conflicting_identifiers'])}.")

        mismatches = []
        for side in SIDES:
            given = sides[side]["input_domain"]
            held = normalise_domain(_text(profiles[side].get("fqdn")))  # type: ignore[union-attr]
            if given and held and given != held:
                mismatches.append(f"The {side} input domain {given} differs from the profile's fqdn {held}.")
        base["notes"] = mismatches
        if mismatches:
            return {**base, "status": "needs_review",
                    "status_reason": " ".join(mismatches) + f" {TOOL} was not asked."}

        if left["id"] == right["id"]:
            return {**base, "status": "likely_duplicate",
                    "status_reason": f"Both sides resolve to the same Firmable company record, {left['id']}, so "
                                     f"{TOOL} was not asked."}

        pair = "|".join(sorted([left["id"], right["id"]]))
        if pair in asked:
            dec = {**asked[pair], "reused_from_row": asked[pair]["row_id"]}
        elif ctx.stopped is not None:
            dec = {"row_id": row["row_id"], "asked": False, "answer": None, "error": None}
        else:
            entry = {"row_id": row["row_id"], "asked": True, "company_id": left["id"],
                     "other_company_id": right["id"]}
            try:
                resp = client.make_decision(TOOL, {"company_id": left["id"], "other_company_id": right["id"]},
                                            with_reasoning=cfg["with_reasoning"])
            except FirmableError as e:
                dec = {**entry, "answer": None, "error": ctx.record_error(row["row_id"], e)}
            else:
                ans = answer_from(resp)
                bad = ans["answer"] not in ("yes", "no", "unknown")
                dec = {**entry, **ans, "error": ans["answer_error"] if bad else None}
                asked[pair] = dec
        dec = {k: v for k, v in dec.items() if k != "row_id"}
        base["decision"] = dec
        answer = dec.get("answer")
        if not dec["asked"]:
            return {**base, "status": "incomplete", "status_reason": f"The run stopped before {TOOL} was asked."}
        if dec.get("error") is not None or answer not in ("yes", "no", "unknown"):
            return {**base, "status": "incomplete",
                    "status_reason": f"{TOOL} returned an error, so the pair has no answer."}
        if answer == "unknown":
            return {**base, "status": "needs_review",
                    "status_reason": f"{TOOL} answered unknown. Unknown means Firmable could not settle the "
                                     f"question; it is not a no. {agree}"}
        by_id = {i["identifier"]: i["comparison"] for i in idents}
        if answer == "yes":
            contradictions = [f"{i} differs" for i in REGISTRY if by_id[i] == "different"]
        else:
            contradictions = [f"{i} is the same" for i in STRONG_SAME if by_id[i] == "same"]
        base["contradictions"] = contradictions
        if contradictions:
            return {**base, "status": "needs_review",
                    "status_reason": f"{TOOL} answered {answer}, but profile identifiers contradict it: "
                                     f"{', '.join(contradictions)}."}
        if answer == "yes":
            return {**base, "status": "likely_duplicate", "status_reason": f"{TOOL} answered yes. {agree}"}
        return {**base, "status": "distinct", "status_reason": f"{TOOL} answered no. {agree}"}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    answers: dict[str, int] = {"yes": 0, "no": 0, "unknown": 0, "error": 0, "not_asked": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        d = r.get("decision")
        if d:
            if not d["asked"]:
                answers["not_asked"] += 1
            elif d.get("error") is not None or d.get("answer") not in ("yes", "no", "unknown"):
                answers["error"] += 1
            else:
                answers[d["answer"]] += 1
    return {"rows": rows, "summary": {"status_counts": counts, "answer_counts": answers}, "coverage": coverage}


CSV_COLUMNS = ["row_id", "left_row_id", "right_row_id", "left_input", "right_input", "duplicate_of", "status",
               "status_reason", "left_company_id", "left_company_name", "right_company_id", "right_company_name",
               "is_same_business", "confidence", "supporting_identifiers", "conflicting_identifiers",
               "missing_identifiers", "contradictions", "notes"]


def _input_text(row: dict[str, Any], side: str) -> str | None:
    return row.get(f"{side}_company_id") or row.get(f"{side}_domain") or None


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        left = (r.get("left") or {}).get("company") or {}
        right = (r.get("right") or {}).get("company") or {}
        d = r.get("decision") or {}
        out.append({
            "row_id": r["row_id"], "left_row_id": r["input"].get("left_row_id") or None,
            "right_row_id": r["input"].get("right_row_id") or None,
            "left_input": _input_text(r["input"], "left"), "right_input": _input_text(r["input"], "right"),
            "duplicate_of": r.get("duplicate_of"), "status": r["status"],
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "left_company_id": left.get("id"), "left_company_name": left.get("name"),
            "right_company_id": right.get("id"), "right_company_name": right.get("name"),
            "is_same_business": d.get("answer"), "confidence": d.get("confidence"),
            "supporting_identifiers": r.get("supporting_identifiers") or [],
            "conflicting_identifiers": r.get("conflicting_identifiers") or [],
            "missing_identifiers": r.get("missing_identifiers") or [],
            "contradictions": r.get("contradictions") or [], "notes": r.get("notes") or [],
        })
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    counts = result["summary"].get("status_counts", {})
    report.add("Result", "Pair review summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + note("These are recommendations. Nothing was merged or deleted. An unknown answer is "
                      "<strong>needs review</strong>, never distinct.", "unknown"))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        lc = (r.get("left") or {}).get("company") or {}
        rc = (r.get("right") or {}).get("company") or {}
        lname = lc.get("name") or _input_text(r["input"], "left") or "left"
        rname = rc.get("name") or _input_text(r["input"], "right") or "right"
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left_html = f"<p>{esc(reason)}</p>"
        left_html += kv([("Left input", _input_text(r["input"], "left")), ("Left CRM row", r["input"].get("left_row_id")),
                         ("Left Firmable ID", lc.get("id")),
                         ("Right input", _input_text(r["input"], "right")), ("Right CRM row", r["input"].get("right_row_id")),
                         ("Right Firmable ID", rc.get("id"))])
        if r.get("contradictions"):
            left_html += "<h3>Contradictions</h3>" + bullet_list(r["contradictions"])
        if r.get("notes"):
            left_html += "<h3>Notes</h3>" + bullet_list(r["notes"])
        if r.get("duplicate_of"):
            left_html += note(f"Same pair as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right_html = ""
        idents = r.get("identifiers") or []
        if idents:
            right_html += table(["Identifier", "Left", "Right", "Comparison"], [
                [i["identifier"], i.get("left"), i.get("right"),
                 Raw(badge(i["comparison"] if i["comparison"] != "missing" else "not observed",
                           COMPARISON_TONE.get(i["comparison"], "neutral")))] for i in idents])
            right_html += ("<p class=\"muted\">Not observed means the identifier was not in the retrieved profile. "
                           "It does not mean the company has none.</p>")
        d = r.get("decision")
        if d:
            rs = d.get("reasoning") or {}
            right_html += (f"<p><strong>{esc(TOOL)}:</strong> "
                           f"{esc(d.get('answer') or ('not asked' if not d.get('asked') else 'no answer'))}"
                           + (f", confidence {esc(d.get('confidence'))}" if d.get("confidence") is not None else "")
                           + "</p>")
            if d.get("details"):
                right_html += kv([(k, v) for k, v in d["details"].items()])
            if rs.get("reason"):
                right_html += (f"<p>{esc(rs.get('reason'))}</p><p class=\"muted\">Source: {esc(rs.get('source'))}. "
                               f"Last refreshed: {esc(rs.get('last_refreshed'))}."
                               + (f" Would change if: {esc(rs.get('would_change_if'))}." if rs.get("would_change_if") else "")
                               + "</p>")
            if d.get("error"):
                err = d["error"]
                right_html += note(esc(err.get("message") if isinstance(err, dict) else err), "error")
            right_html += details_json("Decision as JSON", d)
        cards.append(card(f"{lname} and {rname}", badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")),
                          left_html, right_html or "<p class=\"muted\">The pair was not compared.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Pairs", "Each candidate pair with its evidence", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
