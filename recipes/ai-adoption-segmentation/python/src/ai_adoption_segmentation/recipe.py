"""AI adoption segmentation: assigns each company to one configured campaign segment.

Workflow for each row:
1. Normalise the domain. An invalid domain is reported and skipped.
2. Fetch AI Adoption (POST /ai-adoption), with evidence when configured.
3. Check every level the segment rules read against the configured level list.
4. Try the segments in order. Tech Intel and Decisions are requested only when a segment being tried
   has a condition that needs them.

Assignment policy:
- Each condition is met, not_met, unknown (data missing) or error (a request failed).
- match "all": not_met if any condition is not met; else error, else unknown, else matched.
  match "any": matched if any condition is met; else error, else unknown, else not_matched.
- Segments are tried in order and the first matched segment wins.
- A segment that is unknown or error stops the walk. The account goes to the review segment, never to
  a lower-priority segment, so missing data cannot silently move an account down the list.
- A level that the rules read but that is not in `levels` sends the account to review.
- When every segment is not_matched, the account goes to the unmatched segment.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.decisions import answer_from, check_tools, compare, uses_model
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.io import find_duplicates, normalise_domain, read_rows
from firmable_client.recipe import FATAL_KINDS, PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, bullet_list, card, details_json, esc, kv, note, table

NAME = "ai-adoption-segmentation"
TITLE = "AI adoption segmentation"
DESCRIPTION = ("Assigns each company to one AI-related campaign segment using Firmable AI Adoption levels, "
               "dimensions and momentum, optional Tech Intel and Decisions, and explicit ordered rules.")

DIMENSIONS = ["architecture", "footprint", "hiring", "initiatives", "workforce"]
DEFAULT_LEVELS = ["NONE", "BASIC", "MEANINGFUL", "MODERATE", "STRONG", "ADVANCED"]
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
TECH_KEYS = ["category_slug", "filter_slug", "product_slug", "vendor_slug"]
CONDITION_KEYS = {
    "overall_level": {"type", "at_least", "at_most", "in"},
    "dimension_level": {"type", "dimension", "at_least", "at_most", "in"},
    "momentum": {"type", "in"},
    "momentum_signal": {"type", "kind", "min_count"},
    "technology": {"type", "category_slug", "filter_slug", "product_slug", "vendor_slug", "min_confidence",
                   "min_count"},
    "decision": {"type", "check"},
}
STATUS_TONE = {"assigned": "good", "needs_review": "review", "incomplete": "error", "no_segment_matched": "unknown",
               "invalid_input": "error", "error": "error", "not_processed": "unknown"}
OUTCOME_TONE = {"met": "good", "not_met": "bad", "unknown": "unknown", "error": "error", "not_evaluated": "neutral",
                "matched": "good", "not_matched": "bad"}
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_TOP_KEYS = {"levels", "with_evidence", "tech_intel_max_pages", "with_reasoning", "decision_checks", "segments",
             "review_segment", "unmatched_segment"}


# ----- configuration --------------------------------------------------------------------------

def _str_list(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) and x for x in v)


def _whole(v: Any, minimum: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def _special(raw: dict[str, Any], key: str, default_id: str, default_label: str) -> dict[str, str]:
    s = raw.get(key, {}) or {}
    if not isinstance(s, dict) or set(s) - {"id", "label"}:
        raise ConfigError(f"{key} must be an object with id and label.")
    sid = s.get("id", default_id)
    label = s.get("label", default_label)
    if not isinstance(sid, str) or not _ID.match(sid):
        raise ConfigError(f"{key}.id must be 1 to 40 lower-case letters, digits or underscores.")
    if not isinstance(label, str) or not label:
        raise ConfigError(f"{key}.label must be text.")
    return {"id": sid, "label": label}


def _condition(c: Any, where: str, levels: list[str], checks: set[str]) -> dict[str, Any]:
    if not isinstance(c, dict):
        raise ConfigError(f"{where} must be an object.")
    ctype = c.get("type")
    if ctype not in CONDITION_KEYS:
        raise ConfigError(f"{where}.type must be one of: {', '.join(CONDITION_KEYS)}.")
    extra = sorted(set(c) - CONDITION_KEYS[ctype])
    if extra:
        raise ConfigError(f"{where} has unknown key(s) for type {ctype}: {', '.join(extra)}")
    out: dict[str, Any] = {"type": ctype}
    if ctype in ("overall_level", "dimension_level"):
        if ctype == "dimension_level":
            if c.get("dimension") not in DIMENSIONS:
                raise ConfigError(f"{where}.dimension must be one of: {', '.join(DIMENSIONS)}.")
            out["dimension"] = c["dimension"]
        ops = [k for k in ("at_least", "at_most", "in") if k in c]
        if len(ops) != 1:
            raise ConfigError(f"{where} needs exactly one of at_least, at_most or in.")
        op = ops[0]
        value = c[op]
        wanted = value if op == "in" else [value]
        if (op == "in" and not _str_list(value)) or (op != "in" and not isinstance(value, str)):
            raise ConfigError(f"{where}.{op} must be {'a list of levels' if op == 'in' else 'a level'}.")
        missing = [v for v in wanted if v not in levels]
        if missing:
            raise ConfigError(f"{where}.{op} uses level(s) not in levels: {', '.join(missing)}")
        out[op] = value
    elif ctype == "momentum":
        if not _str_list(c.get("in")):
            raise ConfigError(f"{where}.in must be a non-empty list of momentum labels.")
        out["in"] = c["in"]
    elif ctype == "momentum_signal":
        if not isinstance(c.get("kind"), str) or not c["kind"]:
            raise ConfigError(f"{where}.kind must name a momentum signal kind.")
        mc = c.get("min_count", 1)
        if not _whole(mc, 1):
            raise ConfigError(f"{where}.min_count must be a whole number, 1 or more.")
        out.update(kind=c["kind"], min_count=mc)
    elif ctype == "technology":
        given = [k for k in TECH_KEYS if k in c]
        if not given:
            raise ConfigError(f"{where} needs at least one of: {', '.join(TECH_KEYS)}.")
        for k in given:
            if not _str_list(c[k]):
                raise ConfigError(f"{where}.{k} must be a non-empty list of slugs.")
            out[k] = c[k]
        mconf = c.get("min_confidence", "low")
        if mconf not in CONFIDENCE_RANK:
            raise ConfigError(f"{where}.min_confidence must be low, medium or high.")
        mc = c.get("min_count", 1)
        if not _whole(mc, 1):
            raise ConfigError(f"{where}.min_count must be a whole number, 1 or more.")
        out.update(min_confidence=mconf, min_count=mc)
    else:
        if c.get("check") not in checks:
            raise ConfigError(f"{where}.check must name an id from decision_checks.")
        out["check"] = c["check"]
    return out


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    levels = raw.get("levels", DEFAULT_LEVELS)
    if not _str_list(levels) or len(set(levels)) != len(levels):
        raise ConfigError("levels must be a non-empty list of distinct level names, lowest first.")
    for key in ("with_evidence", "with_reasoning"):
        if not isinstance(raw.get(key, key == "with_reasoning"), bool):
            raise ConfigError(f"{key} must be true or false.")
    max_pages = raw.get("tech_intel_max_pages", 1)
    if not _whole(max_pages, 1) or max_pages > 20:
        raise ConfigError("tech_intel_max_pages must be a whole number from 1 to 20.")

    checks_raw = raw.get("decision_checks", [])
    if not isinstance(checks_raw, list):
        raise ConfigError("decision_checks must be a list.")
    checks: list[dict[str, Any]] = []
    for i, c in enumerate(checks_raw, start=1):
        if not isinstance(c, dict):
            raise ConfigError(f"decision_checks[{i}] must be an object.")
        extra = sorted(set(c) - {"id", "label", "tool", "inputs", "want"})
        if extra:
            raise ConfigError(f"decision_checks[{i}] has unknown key(s): {', '.join(extra)}")
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise ConfigError(f"decision_checks[{i}].id must be 1 to 40 lower-case letters, digits or underscores.")
        if any(x["id"] == cid for x in checks):
            raise ConfigError(f"decision_checks[{i}].id {cid!r} is used twice.")
        if not isinstance(c.get("tool"), str) or not c["tool"]:
            raise ConfigError(f"decision_checks[{i}].tool must name a decision tool.")
        inputs = c.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ConfigError(f"decision_checks[{i}].inputs must be an object.")
        want = c.get("want", "yes")
        if want not in ("yes", "no"):
            raise ConfigError(f"decision_checks[{i}].want must be yes or no.")
        label = c.get("label", cid)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"decision_checks[{i}].label must be text.")
        checks.append({"id": cid, "label": label, "tool": c["tool"], "inputs": inputs, "want": want})
    check_ids = {c["id"] for c in checks}

    segs_raw = raw.get("segments")
    if not isinstance(segs_raw, list) or not segs_raw:
        raise ConfigError("segments must be a non-empty list.")
    review = _special(raw, "review_segment", "review", "Needs review")
    unmatched = _special(raw, "unmatched_segment", "unmatched", "No segment matched")
    if review["id"] == unmatched["id"]:
        raise ConfigError("review_segment.id and unmatched_segment.id must differ.")
    segments: list[dict[str, Any]] = []
    seen = {review["id"], unmatched["id"]}
    for i, s in enumerate(segs_raw, start=1):
        where = f"segments[{i}]"
        if not isinstance(s, dict):
            raise ConfigError(f"{where} must be an object.")
        extra = sorted(set(s) - {"id", "label", "description", "match", "conditions"})
        if extra:
            raise ConfigError(f"{where} has unknown key(s): {', '.join(extra)}")
        sid = s.get("id")
        if not isinstance(sid, str) or not _ID.match(sid):
            raise ConfigError(f"{where}.id must be 1 to 40 lower-case letters, digits or underscores.")
        if sid in seen:
            raise ConfigError(f"{where}.id {sid!r} is used twice or clashes with the review or unmatched segment.")
        seen.add(sid)
        label = s.get("label", sid)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"{where}.label must be text.")
        desc = s.get("description", "")
        if not isinstance(desc, str):
            raise ConfigError(f"{where}.description must be text.")
        match = s.get("match", "all")
        if match not in ("all", "any"):
            raise ConfigError(f"{where}.match must be all or any.")
        conds = s.get("conditions")
        if not isinstance(conds, list) or not conds:
            raise ConfigError(f"{where}.conditions must be a non-empty list.")
        segments.append({"id": sid, "label": label, "description": desc, "match": match,
                         "conditions": [_condition(c, f"{where}.conditions[{j}]", levels, check_ids)
                                        for j, c in enumerate(conds, start=1)]})
    used = {c["check"] for s in segments for c in s["conditions"] if c["type"] == "decision"}
    for c in checks:
        if c["id"] not in used:
            raise ConfigError(f"decision_checks {c['id']!r} is not used by any segment. Remove it so no "
                              "answer is paid for without a reason.")
    return {"levels": levels, "with_evidence": raw.get("with_evidence", False),
            "tech_intel_max_pages": max_pages, "with_reasoning": raw.get("with_reasoning", True),
            "decision_checks": checks, "segments": segments, "review_segment": review,
            "unmatched_segment": unmatched}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path, required=["domain"])


def _uses(config: dict[str, Any], ctype: str) -> bool:
    return any(c["type"] == ctype for s in config["segments"] for c in s["conditions"])


def _valid_unique_domains(rows: list[dict[str, str]]) -> list[str]:
    out: list[str] = []
    for r in rows:
        d = normalise_domain(r.get("domain"))
        if d and d not in out:
            out.append(d)
    return out


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    n = len(_valid_unique_domains(rows))
    calls: list[PlannedCall] = []
    checks = len(config["decision_checks"])
    if checks:
        calls.append(PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []))
    calls.append(PlannedCall("POST /ai-adoption", "AI Adoption for each unique valid domain"
                             + (", with evidence" if config["with_evidence"] else ""), n,
                             pricing.estimate_ai_adoption(n, config["with_evidence"])))
    if _uses(config, "technology"):
        mp = config["tech_intel_max_pages"]
        calls.append(PlannedCall("GET /tech-intel", f"Up to {mp} page(s) of technologies for each unique valid "
                                 "domain, only when a technology condition is tried", n * mp,
                                 [("tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n))]))
    if checks:
        calls.append(PlannedCall("POST /decisions", f"{checks} check(s) for each unique valid domain, only when a "
                                 "segment that uses them is tried", n * checks,
                                 pricing.estimate_decision(n * checks, config["with_reasoning"])))
    return calls


# ----- evaluation -----------------------------------------------------------------------------

def _rank_text(subject: str, level: str, op: str, value: Any, levels: list[str]) -> tuple[str, str]:
    """Compares a familiar level. Returns (outcome, explanation)."""
    if op == "in":
        if level in value:
            return "met", f"{subject} {level} is one of {', '.join(value)}."
        return "not_met", f"{subject} {level} is not one of {', '.join(value)}."
    a, b = levels.index(level), levels.index(value)
    if op == "at_least":
        return ("met", f"{subject} {level} is at least {value}.") if a >= b else \
            ("not_met", f"{subject} {level} is below {value}.")
    return ("met", f"{subject} {level} is at most {value}.") if a <= b else \
        ("not_met", f"{subject} {level} is above {value}.")


def _describe(c: dict[str, Any]) -> str:
    t = c["type"]
    if t in ("overall_level", "dimension_level"):
        subject = "overall level" if t == "overall_level" else f"{c['dimension']} level"
        op = next(k for k in ("at_least", "at_most", "in") if k in c)
        value = ", ".join(c[op]) if op == "in" else c[op]
        return f"{subject} {op.replace('_', ' ')} {value}"
    if t == "momentum":
        return f"momentum in {', '.join(c['in'])}"
    if t == "momentum_signal":
        return f"momentum signal {c['kind']} with count at least {c['min_count']}"
    if t == "technology":
        return f"at least {c['min_count']} technology(ies) with {_tech_filter(c)}"
    return f"decision check {c['check']}"


def _tech_filter(c: dict[str, Any]) -> str:
    parts = [f"{k} {', '.join(c[k])}" for k in TECH_KEYS if k in c]
    return f"{'; '.join(parts)}; confidence {c['min_confidence']} or higher"


def _sentence(text: Any) -> str:
    return str(text).rstrip(".") + "."


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _tech_match(item: dict[str, Any], c: dict[str, Any]) -> bool:
    for k in TECH_KEYS:
        if k in c and item.get(k) not in c[k]:
            return False
    rank = CONFIDENCE_RANK.get(str(item.get("confidence")))
    return rank is not None and rank >= CONFIDENCE_RANK[c["min_confidence"]]


def _ai_summary(comp: dict[str, Any]) -> dict[str, Any]:
    mom = comp.get("momentum") if isinstance(comp.get("momentum"), dict) else None
    dims = comp.get("dimensions") if isinstance(comp.get("dimensions"), dict) else {}
    return {
        "firmable_id": comp.get("firmable_id"), "company_name": comp.get("company_name"),
        "country": comp.get("country"), "industry_group": comp.get("industry_group"), "size_band": comp.get("size_band"),
        "overall_level": comp.get("overall_level"), "explanation": comp.get("explanation"),
        "momentum_label": (mom or {}).get("label"),
        "momentum_signals": [s for s in ((mom or {}).get("signals") or []) if isinstance(s, dict)],
        "dimensions": {d: ({k: dims[d].get(k) for k in ("level", "headline", "observability", "counts_toward_overall")}
                           if isinstance(dims.get(d), dict) else None) for d in DIMENSIONS},
        "evidence_available": comp.get("evidence_available"), "as_of": comp.get("as_of"),
        "framework_version": comp.get("framework_version"),
    }


def _evidence_summary(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, dict):
        return None
    out: dict[str, Any] = {}
    for area in DIMENSIONS:
        e = evidence.get(area)
        if not isinstance(e, dict):
            continue
        items = [i for i in (e.get("items") or []) if isinstance(i, dict)]
        out[area] = {"item_count": e.get("item_count"), "items_returned": len(items),
                     "qualifying": sum(1 for i in items if i.get("qualifies") is True),
                     "not_qualifying": sum(1 for i in items if i.get("qualifies") is False),
                     "truncated": e.get("truncated") is True, "as_of": e.get("as_of")}
    return out


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    levels: list[str] = cfg["levels"]
    review, unmatched = cfg["review_segment"], cfg["unmatched_segment"]
    checks = {c["id"]: c for c in cfg["decision_checks"]}
    coverage = [
        "Segments are campaign groupings produced by the configured rules. An AI adoption level, a momentum label or "
        "a detected technology does not prove buying intent.",
        "Segments are tried in order and the first match wins. When a segment cannot be evaluated because data is "
        "missing or a request failed, the account goes to the review segment, not to a lower-priority segment.",
        f"Levels are ranked by the configured list, lowest first: {', '.join(levels)}. A level the rules read that is "
        "not in this list sends the account to review.",
        "AI Adoption explanations and headlines, and decision answers and reasons, are shown as returned.",
    ]

    def stop_all(reason: str) -> dict[str, Any]:
        rows = [{"row_id": r["row_id"], "input": r, "status": "not_processed", "reason": reason} for r in ctx.rows]
        return {"rows": rows, "summary": {"status_counts": {"not_processed": len(rows)}, "segment_counts": {}},
                "coverage": coverage}

    catalogue = None
    if checks:
        try:
            catalogue = client.list_decision_tools()
        except FirmableError as e:
            ctx.record_error(None, e)
            return stop_all("The decision tool list could not be read, so no account was segmented.")
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
    if _uses(cfg, "technology"):
        coverage.append("Technology conditions use the Tech Intel list without evidence. A technology missing from an "
                        "incomplete list is unknown, not absent.")
    if cfg["with_evidence"]:
        coverage.append("AI Adoption evidence items with qualifies false are counted separately; they do not count "
                        "towards a level. Evidence is shown as returned and is not used by the segment rules.")

    for r in ctx.rows:
        r["_domain"] = normalise_domain(r.get("domain")) or ""
    dupes = find_duplicates(ctx.rows, "_domain")
    for r in ctx.rows:
        r.pop("_domain")
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier domain; their API responses were reused, not "
                        "requested again.")
    referenced = {"overall_level" if c["type"] == "overall_level" else f"dimensions.{c['dimension']}.level"
                  for s in cfg["segments"] for c in s["conditions"] if c["type"] in ("overall_level", "dimension_level")}

    def one(row: dict[str, str]) -> dict[str, Any]:
        domain = normalise_domain(row.get("domain"))
        base: dict[str, Any] = {"row_id": row["row_id"], "input": row, "domain": domain,
                                "duplicate_of": dupes.get(row["row_id"]), "segment_id": None, "segment_label": None,
                                "ai_adoption": None, "ai_adoption_found": None, "unfamiliar_levels": [],
                                "evidence_summary": None, "technologies": None, "decisions": [], "trace": [],
                                "unknowns": [], "errors": []}
        if not domain:
            return {**base, "status": "invalid_input", "status_reason": f"{row.get('domain')!r} is not a valid domain."}

        state: dict[str, Any] = {"ai": None, "ai_error": None, "tech": None, "tech_error": None, "decisions": {}}
        try:
            resp = client.get_ai_adoption({"domain": domain}, with_evidence=cfg["with_evidence"])
            comp = resp.get("company")
            err = resp.get("error") if isinstance(resp.get("error"), dict) else None
            if isinstance(comp, dict):
                state["ai"] = _ai_summary(comp)
                base["ai_adoption_found"] = True
                base["evidence_summary"] = _evidence_summary(resp.get("evidence")) if cfg["with_evidence"] else None
            elif err and err.get("code") not in (None, "not_found"):
                state["ai_error"] = f"{err.get('code')}: {err.get('message')}"
                ctx.errors.append({"row_id": row["row_id"], "kind": "answer_error", "status": 200,
                                   "code": err.get("code"), "endpoint": "POST /ai-adoption",
                                   "message": str(err.get("message") or err.get("code")), "charge_uncertain": False,
                                   "retry_after": None})
            else:
                base["ai_adoption_found"] = False
        except FirmableError as e:
            entry = ctx.record_error(row["row_id"], e)
            state["ai_error"] = entry["message"]
        base["ai_adoption"] = state["ai"]
        ai = state["ai"]
        firmable_id = (ai or {}).get("firmable_id")

        if ai:
            found = []
            for path in ["overall_level"] + [f"dimensions.{d}.level" for d in DIMENSIONS]:
                lvl = ai["overall_level"] if path == "overall_level" else (ai["dimensions"].get(path.split(".")[1]) or {}).get("level")
                if isinstance(lvl, str) and lvl not in levels:
                    found.append((path, lvl))
            base["unfamiliar_levels"] = [f"{p}={lvl}" for p, lvl in found]
            blocking = [f"{p} {lvl}" for p, lvl in found if p in referenced]
            if blocking:
                reason = (f"AI Adoption returned level(s) not in the configured level list: {', '.join(blocking)}. "
                          "The account goes to review instead of being ranked.")
                return {**base, "status": "needs_review", "segment_id": review["id"], "segment_label": review["label"],
                        "status_reason": reason, "unknowns": [reason]}

        def ai_missing() -> tuple[str, str]:
            if state["ai_error"] is not None:
                return "error", _sentence(f"The AI Adoption request failed: {state['ai_error']}")
            return "unknown", "Firmable has no AI Adoption data for this company (not found)."

        def tech() -> None:
            if state["tech"] is not None or state["tech_error"] is not None:
                return
            if ctx.stopped is not None:
                state["tech_error"] = "not requested because the run stopped"
                return
            ident = {"id": firmable_id} if firmable_id else {"fqdn": domain}
            res = client.get_tech_intel(ident, max_pages=cfg["tech_intel_max_pages"])
            if res.get("error"):
                entry = {"row_id": row["row_id"], **res["error"]}
                ctx.errors.append(entry)
                if entry.get("kind") in FATAL_KINDS and ctx.stopped is None:
                    ctx.stopped = entry
            if res.get("pages_fetched", 0) == 0:
                state["tech_error"] = (res.get("error") or {}).get("message") or "no page was returned"
                return
            state["tech"] = res

        def decide(check_id: str) -> dict[str, Any]:
            if check_id in state["decisions"]:
                return state["decisions"][check_id]
            c = checks[check_id]
            entry = {"check_id": c["id"], "label": c["label"], "tool": c["tool"], "want": c["want"]}
            if ctx.stopped is not None:
                rec = {**entry, "result": "not_asked", "answer": None, "error": None}
            else:
                subject = {"company_id": firmable_id} if firmable_id else {"domain": domain}
                try:
                    resp = client.make_decision(c["tool"], {**subject, **c["inputs"]},
                                                with_reasoning=cfg["with_reasoning"])
                    ans = answer_from(resp)
                    result = compare(ans["answer"], c["want"])
                    rec = {**entry, "result": result, **ans,
                           "error": ans["answer_error"] if result == "error" else None}
                except FirmableError as e:
                    rec = {**entry, "result": "error", "answer": None, "error": ctx.record_error(row["row_id"], e)}
            state["decisions"][check_id] = rec
            return rec

        def evaluate(c: dict[str, Any]) -> tuple[str, str]:
            t = c["type"]
            if t in ("overall_level", "dimension_level", "momentum", "momentum_signal"):
                if ai is None:
                    return ai_missing()
            if t in ("overall_level", "dimension_level"):
                op = next(k for k in ("at_least", "at_most", "in") if k in c)
                if t == "overall_level":
                    subject, level, extra = "Overall level", ai["overall_level"], ""
                else:
                    dim = ai["dimensions"].get(c["dimension"])
                    name = c["dimension"].capitalize()
                    if dim is None:
                        return "unknown", f"The {c['dimension']} area was not returned."
                    subject, level = f"{name} level", dim.get("level")
                    extra = (f" Firmable's observability for this area is {dim.get('observability')}."
                             if dim.get("observability") == "LOW" else "")
                if not isinstance(level, str) or not level:
                    return "unknown", f"{subject} was not returned."
                if level not in levels:
                    return "unknown", f"{subject} {level} is not in the configured level list, so it cannot be ranked."
                outcome, text = _rank_text(subject, level, op, c[op], levels)
                return outcome, text + extra
            if t == "momentum":
                label = ai["momentum_label"]
                if not isinstance(label, str) or not label:
                    return "unknown", "Momentum was not returned."
                if label in c["in"]:
                    return "met", f"Momentum {label} is one of {', '.join(c['in'])}."
                return "not_met", f"Momentum {label} is not one of {', '.join(c['in'])}."
            if t == "momentum_signal":
                if ai["momentum_label"] is None and not ai["momentum_signals"]:
                    return "unknown", "Momentum was not returned."
                sig = next((s for s in ai["momentum_signals"] if s.get("kind") == c["kind"]), None)
                if sig is None:
                    return "not_met", f"Momentum signal {c['kind']} was not observed in retrieved data."
                count = sig.get("count")
                if not isinstance(count, int) or isinstance(count, bool):
                    return "unknown", f"Momentum signal {c['kind']} has no count."
                latest = f" (latest {sig.get('latest')})" if sig.get("latest") else ""
                if count >= c["min_count"]:
                    return "met", f"Momentum signal {c['kind']} has count {count}, at least {c['min_count']}{latest}."
                return "not_met", f"Momentum signal {c['kind']} has count {count}, below {c['min_count']}{latest}."
            if t == "technology":
                tech()
                if state["tech"] is None:
                    return "error", _sentence(f"The Tech Intel request failed: {state['tech_error']}")
                res = state["tech"]
                items = [i for i in res.get("items") or [] if isinstance(i, dict)]
                matched = [i for i in items if _tech_match(i, c)]
                names = sorted({str(i.get("product") or "-") for i in matched})
                desc = _tech_filter(c)
                n = len(matched)
                if n >= c["min_count"]:
                    return "met", f"{n} technology(ies) with {desc} observed: {', '.join(names[:5])}."
                total = res.get("total")
                total_text = str(total) if isinstance(total, int) and not isinstance(total, bool) else "unknown"
                if res.get("complete"):
                    return "not_met", (f"{n} technology(ies) with {desc} observed in retrieved data, fewer than "
                                       f"{c['min_count']}. The list of {len(items)} technologies is complete.")
                return "unknown", (f"{n} technology(ies) with {desc} observed in retrieved data, fewer than "
                                   f"{c['min_count']}, but only {len(items)} of {total_text} technologies were retrieved.")
            rec = decide(c["check"])
            if rec["result"] == "pass":
                return "met", f"Decision {rec['label']}: answered {rec['answer']}, wanted {rec['want']}."
            if rec["result"] == "fail":
                return "not_met", f"Decision {rec['label']}: answered {rec['answer']}, wanted {rec['want']}."
            if rec["result"] == "unknown":
                return "unknown", (f"Decision {rec['label']}: answered unknown. Firmable does not hold the fact; "
                                   "it is not a no.")
            if rec["result"] == "not_asked":
                return "error", f"Decision {rec['label']}: not asked because the run stopped."
            err = rec.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            return "error", _sentence(f"Decision {rec['label']}: no answer because of an error: {msg}")

        trace: list[dict[str, Any]] = []
        final: tuple[str, dict[str, Any] | None] = ("not_matched", None)
        for seg in cfg["segments"]:
            results: list[dict[str, Any]] = []
            stop = False
            for c in seg["conditions"]:
                if stop:
                    results.append({"condition": c, "description": _describe(c), "outcome": "not_evaluated",
                                    "explanation": None})
                    continue
                outcome, text = evaluate(c)
                results.append({"condition": c, "description": _describe(c), "outcome": outcome, "explanation": text})
                if (seg["match"] == "all" and outcome == "not_met") or (seg["match"] == "any" and outcome == "met"):
                    stop = True
            outs = [x["outcome"] for x in results]
            if seg["match"] == "all":
                seg_out = ("not_matched" if "not_met" in outs else "error" if "error" in outs
                           else "unknown" if "unknown" in outs else "matched")
            else:
                seg_out = ("matched" if "met" in outs else "error" if "error" in outs
                           else "unknown" if "unknown" in outs else "not_matched")
            trace.append({"segment_id": seg["id"], "label": seg["label"], "match": seg["match"], "outcome": seg_out,
                          "conditions": results})
            if seg_out != "not_matched":
                final = (seg_out, seg)
                break

        tech_res = state["tech"]
        base["technologies"] = None if tech_res is None else {
            "total": tech_res.get("total"), "retrieved": len(tech_res.get("items") or []),
            "complete": bool(tech_res.get("complete")), "pages_fetched": tech_res.get("pages_fetched")}
        base["decisions"] = list(state["decisions"].values())
        unknowns = _unique([x["explanation"] for t in trace for x in t["conditions"] if x["outcome"] in ("unknown", "error")])
        errors = _unique([x["explanation"] for t in trace for x in t["conditions"] if x["outcome"] == "error"])
        base.update(trace=trace, unknowns=unknowns, errors=errors)
        outcome, seg = final
        if outcome == "matched" and seg is not None:
            met = [x["explanation"] for x in trace[-1]["conditions"] if x["outcome"] == "met"]
            return {**base, "status": "assigned", "segment_id": seg["id"], "segment_label": seg["label"],
                    "status_reason": f"Matched segment {seg['label']}. " + " ".join(met)}
        if outcome in ("unknown", "error") and seg is not None:
            gaps = _unique([x["explanation"] for x in trace[-1]["conditions"] if x["outcome"] in ("unknown", "error")])
            why = "an API error" if outcome == "error" else "missing data"
            return {**base, "status": "incomplete" if outcome == "error" else "needs_review",
                    "segment_id": review["id"], "segment_label": review["label"],
                    "status_reason": (f"Segment {seg['label']} could not be evaluated because of {why}. "
                                      + " ".join(gaps) + " Later segments were not tried.")}
        return {**base, "status": "no_segment_matched", "segment_id": unmatched["id"],
                "segment_label": unmatched["label"],
                "status_reason": "No configured segment matched. Each segment had a condition that was not met."}

    rows = ctx.run_rows(ctx.rows, one)
    not_found = sum(1 for r in rows if r.get("ai_adoption_found") is False and not r.get("duplicate_of"))
    if not_found:
        coverage.append(f"{not_found} company(ies) have no AI Adoption data in Firmable (not found). This means no "
                        "data was retrieved, not that the company has no AI adoption.")
    unfamiliar = sorted({u for r in rows for u in r.get("unfamiliar_levels") or []})
    if unfamiliar:
        coverage.append(f"AI Adoption returned level(s) not in the configured list: {', '.join(unfamiliar)}. Add "
                        "them to levels in the right order once you know where they rank.")
    truncated = sum(1 for r in rows if not r.get("duplicate_of")
                    for e in (r.get("evidence_summary") or {}).values() if e.get("truncated"))
    if truncated:
        coverage.append(f"{truncated} evidence area(s) are truncated: the response holds fewer items than "
                        "item_count.")
    counts: dict[str, int] = {}
    seg_counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r.get("segment_id"):
            seg_counts[r["segment_id"]] = seg_counts.get(r["segment_id"], 0) + 1
    return {"rows": rows, "summary": {"status_counts": counts, "segment_counts": seg_counts}, "coverage": coverage}


# ----- output ---------------------------------------------------------------------------------

CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "segment_id", "segment_label", "status_reason",
               "company_id", "company_name", "overall_level", "momentum", "architecture_level", "footprint_level",
               "hiring_level", "initiatives_level", "workforce_level", "unfamiliar_levels", "unknowns", "trace"]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        ai = r.get("ai_adoption") or {}
        dims = ai.get("dimensions") or {}
        row = {
            "row_id": r["row_id"], "domain": r.get("domain") or r["input"].get("domain"),
            "duplicate_of": r.get("duplicate_of"), "status": r["status"], "segment_id": r.get("segment_id"),
            "segment_label": r.get("segment_label"),
            "status_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_id": ai.get("firmable_id"), "company_name": ai.get("company_name"),
            "overall_level": ai.get("overall_level"), "momentum": ai.get("momentum_label"),
            "unfamiliar_levels": r.get("unfamiliar_levels") or [], "unknowns": r.get("unknowns") or [],
            "trace": [f"{t['segment_id']}={t['outcome']}" for t in r.get("trace") or []],
        }
        for d in DIMENSIONS:
            row[f"{d}_level"] = (dims.get(d) or {}).get("level")
        out.append(row)
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    s = result["summary"]
    cfg = result.get("input", {}).get("config") or {}
    report.add("Result", "Segment summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in (s.get("status_counts") or {}).items()) + "</p>"
               + table(["Segment", "Accounts"], [[k, v] for k, v in (s.get("segment_counts") or {}).items()])
               + note("A segment is a grouping from your rules. An AI adoption level or a detected technology does "
                      "<strong>not prove buying intent</strong>. Unknown data sends an account to review, never to a "
                      "lower segment.", "unknown"))
    if cfg.get("segments"):
        report.add("Rules", "Segments in the order they are tried", table(
            ["#", "Segment", "Match", "Conditions"],
            [[i, f"{sg['label']} ({sg['id']})", sg["match"], "; ".join(_describe(c) for c in sg["conditions"])]
             for i, sg in enumerate(cfg["segments"], start=1)]))
    cards = []
    for r in result["rows"]:
        status = r["status"]
        ai = r.get("ai_adoption")
        title = (ai or {}).get("company_name") or r.get("domain") or r["input"].get("domain") or r["row_id"]
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = ""
        if r.get("segment_label"):
            left += f"<p><strong>Segment:</strong> {esc(r['segment_label'])}</p>"
        left += f"<p>{esc(reason)}</p>"
        if ai:
            left += "<h3>AI Adoption, as returned</h3>" + kv([
                ("Firmable ID", ai.get("firmable_id")), ("Overall level", ai.get("overall_level")),
                ("Explanation", ai.get("explanation")), ("Momentum", ai.get("momentum_label")),
                ("Data as of", ai.get("as_of")), ("Framework version", ai.get("framework_version"))])
            left += table(["Area", "Level", "Observability", "Counts toward overall", "Headline"], [
                [d, (dim or {}).get("level"), (dim or {}).get("observability"), (dim or {}).get("counts_toward_overall"),
                 (dim or {}).get("headline")] for d, dim in (ai.get("dimensions") or {}).items()])
        elif r.get("ai_adoption_found") is False:
            left += note("Firmable has no AI Adoption data for this company. This is not evidence of no adoption.",
                         "unknown")
        if r.get("duplicate_of"):
            left += note(f"Same domain as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        right = ""
        for t in r.get("trace") or []:
            right += (f"<h3>{esc(t['label'])} {badge(t['outcome'].replace('_', ' '), OUTCOME_TONE.get(t['outcome'], 'neutral'))}"
                      f"</h3><p class=\"muted\">Match {esc(t['match'])}</p>")
            right += table(["Condition", "Outcome", "Explanation"], [
                [x["description"], Raw(badge(x["outcome"].replace("_", " "), OUTCOME_TONE.get(x["outcome"], "neutral"))),
                 x.get("explanation")] for x in t["conditions"]])
        if r.get("unknowns"):
            right += "<h3>Unknowns and errors</h3>" + bullet_list(r["unknowns"])
        for d in r.get("decisions") or []:
            rs = d.get("reasoning") or {}
            if rs.get("reason"):
                right += (f"<p><strong>{esc(d['label'])}:</strong> {esc(rs.get('reason'))}</p>"
                          f"<p class=\"muted\">Source: {esc(rs.get('source'))}. Last refreshed: "
                          f"{esc(rs.get('last_refreshed'))}.</p>")
        ev = r.get("evidence_summary")
        if ev:
            right += "<h3>Evidence returned</h3>" + table(
                ["Area", "Items held", "Items returned", "Qualifying", "Not qualifying", "Truncated"],
                [[a, e.get("item_count"), e.get("items_returned"), e.get("qualifying"), e.get("not_qualifying"),
                  "yes" if e.get("truncated") else "no"] for a, e in ev.items()])
        if r.get("trace"):
            right += details_json("Rule trace as JSON", r["trace"])
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No segment was tried for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Accounts", "Each account with its segment and rule trace", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, CSV_COLUMNS, csv_rows, render)
