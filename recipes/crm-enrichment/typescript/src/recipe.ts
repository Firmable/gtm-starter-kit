/**
 * CRM enrichment: fills empty fields in an existing company or contact file from Firmable profiles.
 * Mirrors recipes/crm-enrichment/python/src/crm_enrichment/recipe.py; see that file and the recipe
 * README for the merge policy.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, card, detailsJson, esc, facts, kv, normaliseDomain, note, pricing, readRows, table,
} from "firmable-gtm-client";

export const NAME = "crm-enrichment";
export const TITLE = "CRM enrichment";
export const DESCRIPTION = "Fills empty fields in an existing company or contact file from Firmable company and person " +
  "profiles, keeps existing values that differ, and lists every field-level change and conflict.";

const SOURCES = ["company", "person"] as const;
type Source = (typeof SOURCES)[number];
const SOURCE_LABEL: Record<string, string> = { company: "Company", person: "Person" };
const ID_TYPES: Record<Source, string[]> = {
  company: ["fqdn", "id", "website", "abn", "ln_slug", "ln_url"],
  person: ["id", "work_email", "personal_email", "ln_slug", "ln_url"],
};
const ID_LABEL: Record<string, string> = {
  fqdn: "domain", id: "Firmable ID", website: "website", abn: "ABN", ln_slug: "LinkedIn slug",
  ln_url: "LinkedIn URL", work_email: "work email", personal_email: "personal email",
};
// Top-level response fields from the published API reference. Person gender is left out on purpose.
const DOCUMENTED_FIELDS: Record<Source, string[]> = {
  company: ["id", "name", "website", "fqdn", "description", "tagline", "linkedin", "employee_count",
    "year_founded", "hq_location", "hq_country", "company_size", "company_type",
    "abn_operation_status", "locations", "industries", "revenue", "phones", "emails", "social_media",
    "industry_codes", "keywords", "org_registers", "states"],
  person: ["id", "name", "first_name", "last_name", "middle_name", "headline", "description", "position",
    "current_company", "time_in_current_role", "month_joined", "year_joined", "emails", "phones",
    "linkedin", "social_media", "location", "department", "seniority", "skills", "education",
    "keywords", "secondary_position", "country", "experience", "slug"],
};
const STATUS_COLUMNS = ["enrichment_status", "enrichment_reason", "company_match", "person_match", "fields_filled",
  "fields_overwritten", "conflicts", "duplicate_of"];
const STATUS_TONE: Record<string, string> = {
  enriched: "good", no_change: "neutral", not_matched: "unknown", incomplete: "error", invalid_input: "error",
  error: "error", not_processed: "unknown",
};
const ACTION_TONE: Record<string, string> = {
  filled: "good", overwritten: "review", conflict_kept: "review", unchanged: "neutral", no_api_value: "unknown",
  source_unavailable: "unknown", skipped_dnd: "info",
};
const ACTIONS = ["filled", "overwritten", "conflict_kept", "unchanged", "no_api_value", "source_unavailable", "skipped_dnd"];
const TOP_KEYS = new Set(["identifiers", "mappings", "merge_policy", "comparison", "skip_dnd_phones"]);
const MAPPING_KEYS = new Set(["column", "source", "field", "compare_as"]);
const PATH = /^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$/;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE_PATH = /^phones\.(\d+)(\.|$)/;

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);

/** Quotes a value the same way in Python and TypeScript. */
export const q = (value: unknown): string => `'${value}'`;

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const idsRaw = raw.identifiers;
  if (!isObj(idsRaw) || !Object.keys(idsRaw).length) {
    throw new ConfigError("identifiers must be an object naming a company and/or person identifier column.");
  }
  let bad = Object.keys(idsRaw).filter((k) => !(SOURCES as readonly string[]).includes(k)).sort();
  if (bad.length) throw new ConfigError(`identifiers has unknown key(s): ${bad.join(", ")}`);
  const identifiers: Json = {};
  for (const src of SOURCES) {
    if (!(src in idsRaw)) continue;
    const spec = idsRaw[src];
    if (!isObj(spec) || Object.keys(spec).sort().join(",") !== "column,type") {
      throw new ConfigError(`identifiers.${src} must be an object with column and type.`);
    }
    if (typeof spec.column !== "string" || !spec.column.trim()) throw new ConfigError(`identifiers.${src}.column must be a column name.`);
    if (typeof spec.type !== "string" || !ID_TYPES[src].includes(spec.type)) {
      throw new ConfigError(`identifiers.${src}.type must be one of: ${ID_TYPES[src].join(", ")}.`);
    }
    identifiers[src] = { column: spec.column.trim(), type: spec.type };
  }
  const idColumns = new Set(Object.values(identifiers).map((v: Json) => v.column));

  const mapsRaw = raw.mappings;
  if (!Array.isArray(mapsRaw) || !mapsRaw.length) throw new ConfigError("mappings must be a non-empty list.");
  const mappings: Json[] = [];
  const seen = new Set<string>();
  mapsRaw.forEach((m: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(m)) throw new ConfigError(`mappings[${i}] must be an object.`);
    const extra = Object.keys(m).filter((k) => !MAPPING_KEYS.has(k)).sort();
    if (extra.length) throw new ConfigError(`mappings[${i}] has unknown key(s): ${extra.join(", ")}`);
    const colRaw = m.column;
    if (typeof colRaw !== "string" || !colRaw.trim()) throw new ConfigError(`mappings[${i}].column must be a column name.`);
    const col = colRaw.trim();
    if (col === "row_id" || STATUS_COLUMNS.includes(col)) throw new ConfigError(`mappings[${i}].column ${q(col)} is reserved by this recipe.`);
    if (idColumns.has(col)) throw new ConfigError(`mappings[${i}].column ${q(col)} is an identifier column and cannot be enriched.`);
    if (seen.has(col)) throw new ConfigError(`mappings[${i}].column ${q(col)} is mapped twice.`);
    seen.add(col);
    const src = m.source;
    if (src !== "company" && src !== "person") throw new ConfigError(`mappings[${i}].source must be company or person.`);
    const field = m.field;
    if (typeof field !== "string" || !PATH.test(field)) {
      throw new ConfigError(`mappings[${i}].field must be a dotted path such as name or current_company.name.`);
    }
    if (!DOCUMENTED_FIELDS[src as Source].includes(field.split(".")[0])) {
      throw new ConfigError(`mappings[${i}].field ${q(field)} does not start with a documented ${src} field.`);
    }
    const compareAs = m.compare_as ?? "text";
    if (compareAs !== "text" && compareAs !== "domain") throw new ConfigError(`mappings[${i}].compare_as must be text or domain.`);
    if (!(src in identifiers)) throw new ConfigError(`mappings[${i}] uses source ${src} but identifiers.${src} is not set.`);
    mappings.push({ column: col, source: src, field, compare_as: compareAs });
  });
  for (const src of Object.keys(identifiers)) {
    if (!mappings.some((m) => m.source === src)) throw new ConfigError(`identifiers.${src} is set but no mapping uses source ${src}.`);
  }

  const policy = raw.merge_policy ?? "fill_empty";
  if (policy !== "fill_empty" && policy !== "overwrite") throw new ConfigError("merge_policy must be fill_empty or overwrite.");
  const cmpRaw = raw.comparison ?? {};
  if (!isObj(cmpRaw)) throw new ConfigError("comparison must be an object.");
  bad = Object.keys(cmpRaw).filter((k) => k !== "case_insensitive" && k !== "collapse_whitespace").sort();
  if (bad.length) throw new ConfigError(`comparison has unknown key(s): ${bad.join(", ")}`);
  const comparison: Json = {};
  for (const k of ["case_insensitive", "collapse_whitespace"]) {
    const v = cmpRaw[k] ?? true;
    if (typeof v !== "boolean") throw new ConfigError(`comparison.${k} must be true or false.`);
    comparison[k] = v;
  }
  const skipDnd = raw.skip_dnd_phones ?? true;
  if (typeof skipDnd !== "boolean") throw new ConfigError("skip_dnd_phones must be true or false.");
  return { identifiers, mappings, merge_policy: policy, comparison, skip_dnd_phones: skipDnd };
}

export function readInput(path: string): Row[] {
  return readRows(path);
}

/** Returns the identifier to send, or null when the value cannot be one. */
export function normaliseIdentifier(kind: string, value: string | null | undefined): string | null {
  let v = (value ?? "").trim();
  if (!v) return null;
  if (kind === "fqdn") return normaliseDomain(v);
  if (kind === "work_email" || kind === "personal_email") {
    v = v.toLowerCase();
    return EMAIL.test(v) ? v : null;
  }
  if (kind === "abn") {
    const digits = v.replace(/\s/g, "");
    return /^\d{11}$/.test(digits) ? digits : null;
  }
  return /\s/.test(v) ? null : v;
}

function uniqueValid(rows: Row[], spec: Json): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const v = normaliseIdentifier(spec.type, r[spec.column]);
    if (v && !out.includes(v)) out.push(v);
  }
  return out;
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const calls: PlannedCall[] = [];
  const ids = config.identifiers;
  if (ids.company) {
    const n = uniqueValid(rows, ids.company).length;
    calls.push(new PlannedCall("GET /company", `Company profile for each unique valid ${ID_LABEL[ids.company.type]} in column ${q(ids.company.column)}`,
      n, [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  }
  if (ids.person) {
    const n = uniqueValid(rows, ids.person).length;
    calls.push(new PlannedCall("GET /people", `Person profile for each unique valid ${ID_LABEL[ids.person.type]} in column ${q(ids.person.column)}`,
      n, [["person", pricing.round4(pricing.PERSON_PROFILE * n)]]));
  }
  return calls;
}

const numberText = (v: number): string => String(v);

export function canonicalJson(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return numberText(v);
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonicalJson).join(",") + "]";
  if (isObj(v)) return "{" + Object.entries(v).map(([k, x]) => JSON.stringify(k) + ":" + canonicalJson(x)).join(",") + "}";
  return JSON.stringify(String(v));
}

/** The text written to a cell for an API value. Missing and empty values become "". */
export function valueText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return numberText(v);
  if (typeof v === "string") return v.trim();
  if (Array.isArray(v)) {
    if (v.every((x) => x === null || ["string", "number", "boolean"].includes(typeof x))) {
      return v.map(valueText).filter((t) => t).join("; ");
    }
    return canonicalJson(v);
  }
  if (isObj(v)) return Object.keys(v).length ? canonicalJson(v) : "";
  return String(v);
}

export function getPath(obj: unknown, path: string): unknown {
  let cur: unknown = obj;
  for (const seg of path.split(".")) {
    if (isObj(cur)) cur = cur[seg];
    else if (Array.isArray(cur) && /^\d+$/.test(seg)) {
      const idx = Number(seg);
      cur = idx < cur.length ? cur[idx] : null;
    } else return null;
    if (cur === null || cur === undefined) return null;
  }
  return cur;
}

function comparable(text: string, compareAs: string, cmp: Json): string {
  if (compareAs === "domain") {
    const d = normaliseDomain(text);
    if (d) return d;
  }
  if (cmp.collapse_whitespace) text = text.trim().split(/\s+/).join(" ").trim();
  if (cmp.case_insensitive) text = text.toLowerCase();
  return text;
}

function sourceNote(src: Json): string {
  switch (src.status) {
    case "no_identifier": return `no value in column ${q(src.column)}`;
    case "invalid_identifier": return `${q(src.input_value)} is not a valid ${ID_LABEL[src.type]}`;
    case "not_found": return "Firmable has no matching profile";
    case "error": return `the lookup failed: ${String(src.message).replace(/\.+$/, "")}`;
    case "not_requested": return "not looked up because the run stopped";
    default: return "matched";
  }
}

function statusOf(sources: Record<string, Json>, filled: number, overwritten: number, conflictsKept: number): [string, string] {
  const states = Object.values(sources).map((s) => s.status as string);
  const changed = `${filled} field(s) filled and ${overwritten} overwritten.`;
  let status: string;
  let base: string;
  if (states.every((st) => st === "no_identifier" || st === "invalid_identifier")) {
    [status, base] = ["invalid_input", "No usable identifier, so nothing was looked up and the row is unchanged."];
  } else if (states.some((st) => st === "error" || st === "not_requested")) {
    status = "incomplete";
    base = "A lookup has no result because of an error or because the run stopped; its fields were not " +
      `changed. ${changed}`;
  } else if (!states.includes("matched")) {
    [status, base] = ["not_matched", "Firmable has no matching profile, so the row is unchanged."];
  } else if (filled + overwritten) {
    [status, base] = ["enriched", changed];
  } else {
    [status, base] = ["no_change", "No field was changed."];
  }
  const parts = [base];
  if (conflictsKept) parts.push(`${conflictsKept} conflict(s) kept the existing value.`);
  for (const [name, s] of Object.entries(sources)) {
    if (s.status !== "matched") parts.push(`${SOURCE_LABEL[name]}: ${sourceNote(s)}.`);
  }
  return [status, parts.join(" ")];
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const ids = cfg.identifiers;
  const cmp = cfg.comparison;
  const overwrite = cfg.merge_policy === "overwrite";
  const active = SOURCES.filter((s) => ids[s]);
  const idDesc = active.map((s) => `${q(ids[s].column)} (${ID_LABEL[ids[s].type]})`).join(" and ");
  const coverage: string[] = [
    `Rows are matched only by the identifier column(s) ${idDesc}. Rows are never matched by name.`,
    overwrite
      ? "Merge policy overwrite: a non-empty cell that differs from Firmable is replaced, and the previous " +
        "value is listed in the conflict report."
      : "Merge policy fill_empty: empty cells are filled. A non-empty cell that differs from Firmable keeps its " +
        "value and is listed in the conflict report.",
    `Values are compared ${cmp.case_insensitive ? "ignoring" : "respecting"} letter case and ` +
      `${cmp.collapse_whitespace ? "ignoring" : "respecting"} extra spaces.`,
    "A field Firmable did not return is reported as no_api_value. It means the value was not in the retrieved " +
      "profile, not that it does not exist.",
    "No CRM is connected. The recipe reads one file and writes files; nothing is sent to Salesforce, HubSpot " +
      "or another CRM.",
  ];
  if (cfg.skip_dnd_phones && cfg.mappings.some((m: Json) => m.source === "person" && PHONE_PATH.test(m.field))) {
    coverage.push("Person phone numbers flagged do-not-call (is_dnd true) are not written to the file.");
  }
  for (const src of active) {
    if (!ctx.rows.some((r) => ids[src].column in r)) {
      coverage.push(`Identifier column ${q(ids[src].column)} is not in the input file, so no ${src} lookups were made.`);
    }
  }

  const first = new Map<string, string>();
  const dupes: Record<string, string> = {};
  for (const r of ctx.rows) {
    const parts = active.map((s) => normaliseIdentifier(ids[s].type, r[ids[s].column]) || "");
    const key = parts.some((p) => p) ? parts.join("|") : "";
    if (key && first.has(key)) dupes[r.row_id] = first.get(key)!;
    else if (key) first.set(key, r.row_id);
  }
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier row's identifiers; their API responses were reused, not requested again.`);

  const lookup = async (row: Row, src: Source): Promise<Json> => {
    const spec = ids[src];
    const rawValue: string = row[spec.column] || "";
    const value = normaliseIdentifier(spec.type, rawValue);
    const out: Json = { column: spec.column, type: spec.type, input_value: rawValue, value, status: "",
      message: null, profile: null, error: null };
    if (!rawValue.trim()) return { ...out, status: "no_identifier" };
    if (!value) return { ...out, status: "invalid_identifier" };
    if (ctx.stopped !== null) return { ...out, status: "not_requested" };
    let profile: Json;
    try {
      profile = src === "company" ? await client.getCompany({ [spec.type]: value }) : await client.getPerson({ [spec.type]: value });
    } catch (e) {
      if (e instanceof NotFoundError) return { ...out, status: "not_found" };
      if (!(e instanceof FirmableError)) throw e;
      const err = ctx.recordError(row.row_id, e);
      return { ...out, status: "error", message: e.message, error: err };
    }
    return { ...out, status: "matched", profile };
  };

  const one = async (row: Row): Promise<Json> => {
    const sources: Record<string, Json> = {};
    for (const src of active) sources[src] = await lookup(row, src);
    const changes: Json[] = [];
    const values: Record<string, string> = {};
    for (const m of cfg.mappings) {
      const existing: string = row[m.column] || "";
      const src = sources[m.source];
      const entry: Json = { column: m.column, source: m.source, field: m.field, existing, api_value: null, is_dnd: null };
      let action: string;
      let output: string;
      if (src.status !== "matched") {
        [action, output] = ["source_unavailable", existing];
      } else {
        const apiText = valueText(getPath(src.profile, m.field));
        entry.api_value = apiText || null;
        const phone = m.source === "person" ? PHONE_PATH.exec(m.field) : null;
        if (phone) {
          const item = getPath(src.profile, `phones.${phone[1]}`);
          entry.is_dnd = isObj(item) ? (item.is_dnd ?? null) : null;
        }
        if (!apiText) [action, output] = ["no_api_value", existing];
        else if (phone && cfg.skip_dnd_phones && entry.is_dnd === true) [action, output] = ["skipped_dnd", existing];
        else if (!existing) [action, output] = ["filled", apiText];
        else if (comparable(existing, m.compare_as, cmp) === comparable(apiText, m.compare_as, cmp)) [action, output] = ["unchanged", existing];
        else if (overwrite) [action, output] = ["overwritten", apiText];
        else [action, output] = ["conflict_kept", existing];
      }
      values[m.column] = output;
      changes.push({ ...entry, action, output });
    }
    const count: Record<string, number> = {};
    for (const a of ACTIONS) count[a] = changes.filter((c) => c.action === a).length;
    const [status, reason] = statusOf(sources, count.filled, count.overwritten, count.conflict_kept);
    const lookups: Json = {};
    for (const [name, s] of Object.entries(sources)) {
      lookups[name] = { column: s.column, type: s.type, input_value: s.input_value, value: s.value, status: s.status,
        message: s.message, error: s.error };
    }
    return { row_id: row.row_id, input: row, duplicate_of: dupes[row.row_id] ?? null, status, status_reason: reason,
      lookups, company: facts.companyFacts(sources.company?.profile ?? null),
      person: facts.personFacts(sources.person?.profile ?? null), values, changes,
      fields_filled: count.filled, fields_overwritten: count.overwritten,
      conflicts: count.conflict_kept + count.overwritten };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  const actions: Record<string, number> = {};
  for (const a of ACTIONS) actions[a] = 0;
  const conflicts: Json[] = [];
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    for (const c of r.changes ?? []) {
      actions[c.action] += 1;
      if (c.action === "conflict_kept" || c.action === "overwritten") {
        conflicts.push({ row_id: r.row_id, column: c.column, source: c.source, field: c.field, existing: c.existing,
          api_value: c.api_value, action: c.action, output: c.output });
      }
    }
  }
  return { rows, summary: { status_counts: counts, action_counts: actions, conflicts }, coverage };
}

// The enriched CSV keeps every input column, so its header is computed from the result.

export function csvHeader(result: Json): string[] {
  const header = ["row_id"];
  for (const r of result.rows) {
    for (const k of Object.keys(r.input ?? {})) if (!header.includes(k)) header.push(k);
  }
  for (const m of result.input.config.mappings) if (!header.includes(m.column)) header.push(m.column);
  return [...header, ...STATUS_COLUMNS.filter((c) => !header.includes(c))];
}

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const lookups = r.lookups ?? {};
    return {
      ...(r.input ?? {}), ...(r.values ?? {}),
      row_id: r.row_id, enrichment_status: r.status,
      enrichment_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_match: lookups.company?.status ?? null, person_match: lookups.person?.status ?? null,
      fields_filled: r.fields_filled ?? null, fields_overwritten: r.fields_overwritten ?? null,
      conflicts: r.conflicts ?? null, duplicate_of: r.duplicate_of ?? null,
    };
  });
}

export function render(report: Report, result: Json): void {
  const summary = result.summary;
  const counts: Record<string, number> = summary.status_counts ?? {};
  const actions: Record<string, number> = summary.action_counts ?? {};
  report.add("Result", "Enrichment summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    "<p>" + Object.entries(actions).filter(([, v]) => v).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, ACTION_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("A field Firmable did not return is shown as <strong>no API value</strong>: not observed in " +
      "the retrieved profile, which is not the same as not existing.", "unknown"));
  const conflicts: Json[] = summary.conflicts ?? [];
  const body = conflicts.length
    ? table(["Row", "Column", "Existing value", "Firmable value", "Action", "Value in file"], conflicts.map((c) => [
      c.row_id, c.column, c.existing, c.api_value, new Raw(badge(c.action.replace(/_/g, " "), ACTION_TONE[c.action] ?? "neutral")), c.output]))
    : "<p>No existing value differed from Firmable.</p>";
  report.add("Conflicts", "Conflict report", body);
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.person?.name || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    const lookups: Record<string, Json> = r.lookups ?? {};
    if (Object.keys(lookups).length) {
      left += table(["Lookup", "Column", "Identifier", "Result"], Object.entries(lookups).map(([k, v]) => [
        SOURCE_LABEL[k], v.column, v.input_value, String(v.status).replace(/_/g, " ")]));
    }
    if (r.company) left += kv([["Firmable company ID", r.company.id], ["Company", r.company.name], ["Domain", r.company.fqdn]]);
    if (r.person) left += kv([["Firmable person ID", r.person.id], ["Person", r.person.name], ["Position", r.person.position]]);
    if (r.duplicate_of) left += note(`Same identifiers as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    const changes: Json[] = r.changes ?? [];
    let right = "";
    if (changes.length) {
      right += table(["Column", "Firmable field", "Existing", "Firmable value", "Action", "Value in file"], changes.map((c) => [
        c.column, `${c.source}.${c.field}`, c.existing, c.api_value,
        new Raw(badge(c.action.replace(/_/g, " "), ACTION_TONE[c.action] ?? "neutral")), c.output]));
      right += detailsJson("Field changes as JSON", changes);
    }
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No fields were processed for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Rows", "Each row with its field changes", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: csvHeader, csvRows, render,
};
