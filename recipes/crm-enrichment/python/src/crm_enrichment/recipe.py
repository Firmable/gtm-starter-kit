"""CRM enrichment: fills empty fields in an existing company or contact file from Firmable profiles.

Workflow for each row:
1. Read the configured identifier columns. A company identifier is looked up with GET /company and a
   person identifier with GET /people. Rows are never matched by name.
2. For each mapping, read the Firmable value at the mapping's dotted field path.
3. Apply the merge policy below, field by field.

Merge policy for one mapped field:
- source_unavailable: the profile for this mapping's source was not retrieved; the cell is unchanged;
- no_api_value: the profile has no value at that path; the cell is unchanged;
- skipped_dnd: a person phone flagged do-not-call, with skip_dnd_phones true; the cell is unchanged;
- filled: the cell was empty, so it takes the Firmable value;
- unchanged: the cell equals the Firmable value under the comparison rules;
- conflict_kept: the cell differs and merge_policy is fill_empty, so the existing value is kept;
- overwritten: the cell differs and merge_policy is overwrite, so the Firmable value replaces it.

Row status, first that applies: invalid_input (no usable identifier), incomplete (a lookup failed or
was not made because the run stopped), not_matched (no profile found), enriched (a field was filled or
overwritten), no_change.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from firmable_client import pricing
from firmable_client.errors import ConfigError, FirmableError, NotFoundError
from firmable_client.facts import company_facts, person_facts
from firmable_client.io import normalise_domain, read_rows
from firmable_client.recipe import PlannedCall, Recipe, RunContext
from firmable_client.report import Raw, badge, card, details_json, esc, kv, note, table

NAME = "crm-enrichment"
TITLE = "CRM enrichment"
DESCRIPTION = ("Fills empty fields in an existing company or contact file from Firmable company and person "
               "profiles, keeps existing values that differ, and lists every field-level change and conflict.")

SOURCES = ("company", "person")
SOURCE_LABEL = {"company": "Company", "person": "Person"}
ID_TYPES = {
    "company": ["fqdn", "id", "website", "abn", "ln_slug", "ln_url"],
    "person": ["id", "work_email", "personal_email", "ln_slug", "ln_url"],
}
ID_LABEL = {"fqdn": "domain", "id": "Firmable ID", "website": "website", "abn": "ABN", "ln_slug": "LinkedIn slug",
            "ln_url": "LinkedIn URL", "work_email": "work email", "personal_email": "personal email"}
# Top-level response fields from the published API reference. Person gender is left out on purpose.
DOCUMENTED_FIELDS = {
    "company": ["id", "name", "website", "fqdn", "description", "tagline", "linkedin", "employee_count",
                "year_founded", "hq_location", "hq_country", "company_size", "company_type",
                "abn_operation_status", "locations", "industries", "revenue", "phones", "emails", "social_media",
                "industry_codes", "keywords", "org_registers", "states"],
    "person": ["id", "name", "first_name", "last_name", "middle_name", "headline", "description", "position",
               "current_company", "time_in_current_role", "month_joined", "year_joined", "emails", "phones",
               "linkedin", "social_media", "location", "department", "seniority", "skills", "education",
               "keywords", "secondary_position", "country", "experience", "slug"],
}
STATUS_COLUMNS = ["enrichment_status", "enrichment_reason", "company_match", "person_match", "fields_filled",
                  "fields_overwritten", "conflicts", "duplicate_of"]
STATUS_TONE = {"enriched": "good", "no_change": "neutral", "not_matched": "unknown", "incomplete": "error",
               "invalid_input": "error", "error": "error", "not_processed": "unknown"}
ACTION_TONE = {"filled": "good", "overwritten": "review", "conflict_kept": "review", "unchanged": "neutral",
               "no_api_value": "unknown", "source_unavailable": "unknown", "skipped_dnd": "info"}
ACTIONS = ["filled", "overwritten", "conflict_kept", "unchanged", "no_api_value", "source_unavailable", "skipped_dnd"]
_TOP_KEYS = {"identifiers", "mappings", "merge_policy", "comparison", "skip_dnd_phones"}
_MAPPING_KEYS = {"column", "source", "field", "compare_as"}
_PATH = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_PATH = re.compile(r"^phones\.(\d+)(\.|$)")
_WS = re.compile(r"\s+")


def q(value: Any) -> str:
    """Quotes a value the same way in Python and TypeScript."""
    return f"'{value}'"


def load_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"Unknown configuration key(s): {', '.join(unknown)}")
    ids_raw = raw.get("identifiers")
    if not isinstance(ids_raw, dict) or not ids_raw:
        raise ConfigError("identifiers must be an object naming a company and/or person identifier column.")
    bad = sorted(set(ids_raw) - set(SOURCES))
    if bad:
        raise ConfigError(f"identifiers has unknown key(s): {', '.join(bad)}")
    identifiers: dict[str, Any] = {}
    for src in SOURCES:
        if src not in ids_raw:
            continue
        spec = ids_raw[src]
        if not isinstance(spec, dict) or sorted(spec) != ["column", "type"]:
            raise ConfigError(f"identifiers.{src} must be an object with column and type.")
        if not isinstance(spec["column"], str) or not spec["column"].strip():
            raise ConfigError(f"identifiers.{src}.column must be a column name.")
        if spec["type"] not in ID_TYPES[src]:
            raise ConfigError(f"identifiers.{src}.type must be one of: {', '.join(ID_TYPES[src])}.")
        identifiers[src] = {"column": spec["column"].strip(), "type": spec["type"]}
    id_columns = {v["column"] for v in identifiers.values()}

    maps_raw = raw.get("mappings")
    if not isinstance(maps_raw, list) or not maps_raw:
        raise ConfigError("mappings must be a non-empty list.")
    mappings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, m in enumerate(maps_raw, start=1):
        if not isinstance(m, dict):
            raise ConfigError(f"mappings[{i}] must be an object.")
        extra = sorted(set(m) - _MAPPING_KEYS)
        if extra:
            raise ConfigError(f"mappings[{i}] has unknown key(s): {', '.join(extra)}")
        col = m.get("column")
        if not isinstance(col, str) or not col.strip():
            raise ConfigError(f"mappings[{i}].column must be a column name.")
        col = col.strip()
        if col == "row_id" or col in STATUS_COLUMNS:
            raise ConfigError(f"mappings[{i}].column {q(col)} is reserved by this recipe.")
        if col in id_columns:
            raise ConfigError(f"mappings[{i}].column {q(col)} is an identifier column and cannot be enriched.")
        if col in seen:
            raise ConfigError(f"mappings[{i}].column {q(col)} is mapped twice.")
        seen.add(col)
        src = m.get("source")
        if src not in SOURCES:
            raise ConfigError(f"mappings[{i}].source must be company or person.")
        field = m.get("field")
        if not isinstance(field, str) or not _PATH.match(field):
            raise ConfigError(f"mappings[{i}].field must be a dotted path such as name or current_company.name.")
        if field.split(".")[0] not in DOCUMENTED_FIELDS[src]:
            raise ConfigError(f"mappings[{i}].field {q(field)} does not start with a documented {src} field.")
        compare_as = m.get("compare_as", "text")
        if compare_as not in ("text", "domain"):
            raise ConfigError(f"mappings[{i}].compare_as must be text or domain.")
        if src not in identifiers:
            raise ConfigError(f"mappings[{i}] uses source {src} but identifiers.{src} is not set.")
        mappings.append({"column": col, "source": src, "field": field, "compare_as": compare_as})
    for src in identifiers:
        if not any(m["source"] == src for m in mappings):
            raise ConfigError(f"identifiers.{src} is set but no mapping uses source {src}.")

    policy = raw.get("merge_policy", "fill_empty")
    if policy not in ("fill_empty", "overwrite"):
        raise ConfigError("merge_policy must be fill_empty or overwrite.")
    cmp_raw = raw.get("comparison", {})
    if not isinstance(cmp_raw, dict):
        raise ConfigError("comparison must be an object.")
    bad = sorted(set(cmp_raw) - {"case_insensitive", "collapse_whitespace"})
    if bad:
        raise ConfigError(f"comparison has unknown key(s): {', '.join(bad)}")
    comparison = {}
    for k in ("case_insensitive", "collapse_whitespace"):
        v = cmp_raw.get(k, True)
        if not isinstance(v, bool):
            raise ConfigError(f"comparison.{k} must be true or false.")
        comparison[k] = v
    skip_dnd = raw.get("skip_dnd_phones", True)
    if not isinstance(skip_dnd, bool):
        raise ConfigError("skip_dnd_phones must be true or false.")
    return {"identifiers": identifiers, "mappings": mappings, "merge_policy": policy, "comparison": comparison,
            "skip_dnd_phones": skip_dnd}


def read_input(path: Path) -> list[dict[str, str]]:
    return read_rows(path)


def normalise_identifier(kind: str, value: str | None) -> str | None:
    """Returns the identifier to send, or None when the value cannot be one."""
    v = (value or "").strip()
    if not v:
        return None
    if kind == "fqdn":
        return normalise_domain(v)
    if kind in ("work_email", "personal_email"):
        v = v.lower()
        return v if _EMAIL.match(v) else None
    if kind == "abn":
        digits = re.sub(r"\s", "", v)
        return digits if re.fullmatch(r"\d{11}", digits) else None
    return None if re.search(r"\s", v) else v


def _unique_valid(rows: list[dict[str, str]], spec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for r in rows:
        v = normalise_identifier(spec["type"], r.get(spec["column"]))
        if v and v not in out:
            out.append(v)
    return out


def plan(rows: list[dict[str, str]], config: dict[str, Any]) -> list[PlannedCall]:
    calls = []
    ids = config["identifiers"]
    if "company" in ids:
        n = len(_unique_valid(rows, ids["company"]))
        calls.append(PlannedCall("GET /company", f"Company profile for each unique valid "
                                 f"{ID_LABEL[ids['company']['type']]} in column {q(ids['company']['column'])}",
                                 n, [("company", pricing.round4(pricing.COMPANY_PROFILE * n))]))
    if "person" in ids:
        n = len(_unique_valid(rows, ids["person"]))
        calls.append(PlannedCall("GET /people", f"Person profile for each unique valid "
                                 f"{ID_LABEL[ids['person']['type']]} in column {q(ids['person']['column'])}",
                                 n, [("person", pricing.round4(pricing.PERSON_PROFILE * n))]))
    return calls


def number_text(v: int | float) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def canonical_json(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return number_text(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(canonical_json(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + canonical_json(x)
                              for k, x in v.items()) + "}"
    return json.dumps(str(v), ensure_ascii=False)


def value_text(v: Any) -> str:
    """The text written to a cell for an API value. Missing and empty values become ""."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return number_text(v)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        if all(x is None or isinstance(x, (str, int, float, bool)) for x in v):
            return "; ".join(t for t in (value_text(x) for x in v) if t)
        return canonical_json(v)
    if isinstance(v, dict):
        return canonical_json(v) if v else ""
    return str(v)


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            idx = int(seg)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def comparable(text: str, compare_as: str, cmp: dict[str, bool]) -> str:
    if compare_as == "domain":
        d = normalise_domain(text)
        if d:
            return d
    if cmp["collapse_whitespace"]:
        text = " ".join(_WS.split(text.strip())).strip()
    if cmp["case_insensitive"]:
        text = text.lower()
    return text


def _source_note(src: dict[str, Any]) -> str:
    status = src["status"]
    if status == "no_identifier":
        return f"no value in column {q(src['column'])}"
    if status == "invalid_identifier":
        return f"{q(src['input_value'])} is not a valid {ID_LABEL[src['type']]}"
    if status == "not_found":
        return "Firmable has no matching profile"
    if status == "error":
        return f"the lookup failed: {src['message'].rstrip('.')}"
    if status == "not_requested":
        return "not looked up because the run stopped"
    return "matched"


def _status(sources: dict[str, dict[str, Any]], filled: int, overwritten: int, conflicts_kept: int) -> tuple[str, str]:
    states = [s["status"] for s in sources.values()]
    changed = f"{filled} field(s) filled and {overwritten} overwritten."
    if all(st in ("no_identifier", "invalid_identifier") for st in states):
        status, base = "invalid_input", "No usable identifier, so nothing was looked up and the row is unchanged."
    elif any(st in ("error", "not_requested") for st in states):
        status = "incomplete"
        base = ("A lookup has no result because of an error or because the run stopped; its fields were not "
                f"changed. {changed}")
    elif not any(st == "matched" for st in states):
        status, base = "not_matched", "Firmable has no matching profile, so the row is unchanged."
    elif filled + overwritten:
        status, base = "enriched", changed
    else:
        status, base = "no_change", "No field was changed."
    parts = [base]
    if conflicts_kept:
        parts.append(f"{conflicts_kept} conflict(s) kept the existing value.")
    for name, s in sources.items():
        if s["status"] != "matched":
            parts.append(f"{SOURCE_LABEL[name]}: {_source_note(s)}.")
    return status, " ".join(parts)


def run(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.config
    client = ctx.client
    ids = cfg["identifiers"]
    cmp = cfg["comparison"]
    overwrite = cfg["merge_policy"] == "overwrite"
    id_desc = " and ".join(f"{q(ids[s]['column'])} ({ID_LABEL[ids[s]['type']]})" for s in SOURCES if s in ids)
    coverage = [
        f"Rows are matched only by the identifier column(s) {id_desc}. Rows are never matched by name.",
        ("Merge policy overwrite: a non-empty cell that differs from Firmable is replaced, and the previous "
         "value is listed in the conflict report.") if overwrite else
        ("Merge policy fill_empty: empty cells are filled. A non-empty cell that differs from Firmable keeps its "
         "value and is listed in the conflict report."),
        f"Values are compared {'ignoring' if cmp['case_insensitive'] else 'respecting'} letter case and "
        f"{'ignoring' if cmp['collapse_whitespace'] else 'respecting'} extra spaces.",
        "A field Firmable did not return is reported as no_api_value. It means the value was not in the retrieved "
        "profile, not that it does not exist.",
        "No CRM is connected. The recipe reads one file and writes files; nothing is sent to Salesforce, HubSpot "
        "or another CRM.",
    ]
    if cfg["skip_dnd_phones"] and any(m["source"] == "person" and _PHONE_PATH.match(m["field"])
                                      for m in cfg["mappings"]):
        coverage.append("Person phone numbers flagged do-not-call (is_dnd true) are not written to the file.")
    for src in SOURCES:
        if src in ids and not any(ids[src]["column"] in r for r in ctx.rows):
            coverage.append(f"Identifier column {q(ids[src]['column'])} is not in the input file, so no {src} "
                            "lookups were made.")

    keys: dict[str, str] = {}
    first: dict[str, str] = {}
    dupes: dict[str, str] = {}
    for r in ctx.rows:
        parts = [normalise_identifier(ids[s]["type"], r.get(ids[s]["column"])) or "" for s in SOURCES if s in ids]
        key = "|".join(parts) if any(parts) else ""
        keys[r["row_id"]] = key
        if key and key in first:
            dupes[r["row_id"]] = first[key]
        elif key:
            first[key] = r["row_id"]
    if dupes:
        coverage.append(f"{len(dupes)} row(s) repeat an earlier row's identifiers; their API responses were "
                        "reused, not requested again.")

    def lookup(row: dict[str, str], src: str) -> dict[str, Any]:
        spec = ids[src]
        raw_value = row.get(spec["column"]) or ""
        out = {"column": spec["column"], "type": spec["type"], "input_value": raw_value, "value": None,
               "status": "", "message": None, "profile": None, "error": None}
        value = normalise_identifier(spec["type"], raw_value)
        out["value"] = value
        if not raw_value.strip():
            return {**out, "status": "no_identifier"}
        if not value:
            return {**out, "status": "invalid_identifier"}
        if ctx.stopped is not None:
            return {**out, "status": "not_requested"}
        try:
            if src == "company":
                profile = client.get_company(**{spec["type"]: value})
            else:
                profile = client.get_person(**{spec["type"]: value})
        except NotFoundError:
            return {**out, "status": "not_found"}
        except FirmableError as e:
            err = ctx.record_error(row["row_id"], e)
            return {**out, "status": "error", "message": e.message, "error": err}
        return {**out, "status": "matched", "profile": profile}

    def one(row: dict[str, str]) -> dict[str, Any]:
        sources = {src: lookup(row, src) for src in SOURCES if src in ids}
        changes: list[dict[str, Any]] = []
        values: dict[str, str] = {}
        for m in cfg["mappings"]:
            existing = row.get(m["column"]) or ""
            src = sources[m["source"]]
            entry = {"column": m["column"], "source": m["source"], "field": m["field"], "existing": existing,
                     "api_value": None, "is_dnd": None}
            if src["status"] != "matched":
                action, output = "source_unavailable", existing
            else:
                api_text = value_text(get_path(src["profile"], m["field"]))
                entry["api_value"] = api_text or None
                phone = _PHONE_PATH.match(m["field"]) if m["source"] == "person" else None
                if phone:
                    item = get_path(src["profile"], f"phones.{phone.group(1)}")
                    entry["is_dnd"] = item.get("is_dnd") if isinstance(item, dict) else None
                if not api_text:
                    action, output = "no_api_value", existing
                elif phone and cfg["skip_dnd_phones"] and entry["is_dnd"] is True:
                    action, output = "skipped_dnd", existing
                elif not existing:
                    action, output = "filled", api_text
                elif comparable(existing, m["compare_as"], cmp) == comparable(api_text, m["compare_as"], cmp):
                    action, output = "unchanged", existing
                elif overwrite:
                    action, output = "overwritten", api_text
                else:
                    action, output = "conflict_kept", existing
            values[m["column"]] = output
            changes.append({**entry, "action": action, "output": output})
        count = {a: sum(1 for c in changes if c["action"] == a) for a in ACTIONS}
        status, reason = _status(sources, count["filled"], count["overwritten"], count["conflict_kept"])
        lookups = {}
        for name, s in sources.items():
            lookups[name] = {k: s[k] for k in ("column", "type", "input_value", "value", "status", "message", "error")}
        company = sources.get("company", {}).get("profile")
        person = sources.get("person", {}).get("profile")
        return {"row_id": row["row_id"], "input": row, "duplicate_of": dupes.get(row["row_id"]),
                "status": status, "status_reason": reason, "lookups": lookups,
                "company": company_facts(company), "person": person_facts(person),
                "values": values, "changes": changes,
                "fields_filled": count["filled"], "fields_overwritten": count["overwritten"],
                "conflicts": count["conflict_kept"] + count["overwritten"]}

    rows = ctx.run_rows(ctx.rows, one)
    counts: dict[str, int] = {}
    actions = {a: 0 for a in ACTIONS}
    conflicts = []
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        for c in r.get("changes") or []:
            actions[c["action"]] += 1
            if c["action"] in ("conflict_kept", "overwritten"):
                conflicts.append({"row_id": r["row_id"], "column": c["column"], "source": c["source"],
                                  "field": c["field"], "existing": c["existing"], "api_value": c["api_value"],
                                  "action": c["action"], "output": c["output"]})
    return {"rows": rows, "summary": {"status_counts": counts, "action_counts": actions, "conflicts": conflicts},
            "coverage": coverage}


# The enriched CSV keeps every input column, so its header is computed from the result.


def csv_header(result: dict[str, Any]) -> list[str]:
    header = ["row_id"]
    for r in result["rows"]:
        for k in (r.get("input") or {}):
            if k not in header:
                header.append(k)
    for m in result["input"]["config"]["mappings"]:
        if m["column"] not in header:
            header.append(m["column"])
    return header + [c for c in STATUS_COLUMNS if c not in header]


def csv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in result["rows"]:
        row: dict[str, Any] = dict(r.get("input") or {})
        row.update(r.get("values") or {})
        lookups = r.get("lookups") or {}
        row.update({
            "row_id": r["row_id"], "enrichment_status": r["status"],
            "enrichment_reason": r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message"),
            "company_match": (lookups.get("company") or {}).get("status"),
            "person_match": (lookups.get("person") or {}).get("status"),
            "fields_filled": r.get("fields_filled"), "fields_overwritten": r.get("fields_overwritten"),
            "conflicts": r.get("conflicts"), "duplicate_of": r.get("duplicate_of"),
        })
        out.append(row)
    return out


def render(report: Any, result: dict[str, Any]) -> None:
    summary = result["summary"]
    counts = summary.get("status_counts", {})
    actions = summary.get("action_counts", {})
    report.add("Result", "Enrichment summary",
               "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", STATUS_TONE.get(k, "neutral"))
                                for k, v in counts.items()) + "</p>"
               + "<p>" + " ".join(badge(f"{k.replace('_', ' ')}: {v}", ACTION_TONE.get(k, "neutral"))
                                  for k, v in actions.items() if v) + "</p>"
               + note("A field Firmable did not return is shown as <strong>no API value</strong>: not observed in "
                      "the retrieved profile, which is not the same as not existing.", "unknown"))
    conflicts = summary.get("conflicts") or []
    if conflicts:
        body = table(["Row", "Column", "Existing value", "Firmable value", "Action", "Value in file"],
                     [[c["row_id"], c["column"], c["existing"], c["api_value"],
                       Raw(badge(c["action"].replace("_", " "), ACTION_TONE.get(c["action"], "neutral"))),
                       c["output"]] for c in conflicts])
    else:
        body = "<p>No existing value differed from Firmable.</p>"
    report.add("Conflicts", "Conflict report", body)
    cards = []
    for r in result["rows"]:
        status = r["status"]
        title = ((r.get("company") or {}).get("name") or (r.get("person") or {}).get("name") or r["row_id"])
        reason = r.get("status_reason") or r.get("reason") or (r.get("error") or {}).get("message") or ""
        left = f"<p>{esc(reason)}</p>"
        lookups = r.get("lookups") or {}
        if lookups:
            left += table(["Lookup", "Column", "Identifier", "Result"],
                          [[SOURCE_LABEL[k], v["column"], v["input_value"], v["status"].replace("_", " ")]
                           for k, v in lookups.items()])
        if r.get("company"):
            c = r["company"]
            left += kv([("Firmable company ID", c.get("id")), ("Company", c.get("name")), ("Domain", c.get("fqdn"))])
        if r.get("person"):
            p = r["person"]
            left += kv([("Firmable person ID", p.get("id")), ("Person", p.get("name")), ("Position", p.get("position"))])
        if r.get("duplicate_of"):
            left += note(f"Same identifiers as row {esc(r['duplicate_of'])}; responses were reused.", "info")
        changes = r.get("changes") or []
        right = ""
        if changes:
            right += table(["Column", "Firmable field", "Existing", "Firmable value", "Action", "Value in file"],
                           [[c["column"], f"{c['source']}.{c['field']}", c["existing"], c["api_value"],
                             Raw(badge(c["action"].replace("_", " "), ACTION_TONE.get(c["action"], "neutral"))),
                             c["output"]] for c in changes])
            right += details_json("Field changes as JSON", changes)
        cards.append(card(title, badge(status.replace("_", " "), STATUS_TONE.get(status, "neutral")), left,
                          right or "<p class=\"muted\">No fields were processed for this row.</p>",
                          sub=f"Row {r['row_id']}"))
    report.add("Rows", "Each row with its field changes", "".join(cards))


RECIPE = Recipe(NAME, TITLE, DESCRIPTION, load_config, read_input, plan, run, csv_header, csv_rows, render)
