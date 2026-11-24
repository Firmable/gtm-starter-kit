/**
 * Account change monitor: compares a new snapshot of each account with a saved snapshot.
 * Mirrors recipes/account-change-monitor/python/src/account_change_monitor/recipe.py; see that file
 * and the recipe README for the comparison policy.
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import {
  ConfigError, FATAL_KINDS, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, detailsJson, esc, findDuplicates, kv, normaliseDomain, note, parseDate, pricing, pyRepr,
  readRows, stableStringify, table,
} from "firmable-gtm-client";

export const NAME = "account-change-monitor";
export const TITLE = "Account change monitor";
export const DESCRIPTION = "Takes a new snapshot of company, contact, Tech Intel and AI Adoption data for each account, " +
  "compares it with a saved snapshot, and reports what differs in the retrieved data.";

export const SNAPSHOT_FORMAT = "gtm-account-change-monitor/1";
const SOURCES = ["company", "person", "ai_adoption", "tech_intel"];
const SOURCE_LABEL: Record<string, string> = {
  company: "Company profile", person: "Person profile", ai_adoption: "AI Adoption", tech_intel: "Tech Intel",
};
const ENDPOINT: Record<string, string> = {
  company: "GET /company", person: "GET /people", ai_adoption: "POST /ai-adoption", tech_intel: "GET /tech-intel",
};

const COMPANY_TOP = new Set(["id", "name", "website", "fqdn", "description", "linkedin", "employee_count", "year_founded",
  "hq_location", "hq_country", "company_size", "company_type", "locations", "industries", "revenue", "phones", "emails",
  "social_media", "industry_codes", "keywords", "org_registers", "states", "abn_operation_status"]);
const PERSON_TOP = new Set(["id", "name", "first_name", "last_name", "position", "seniority", "department", "headline",
  "country", "location", "time_in_current_role", "month_joined", "year_joined", "experience", "current_company", "emails",
  "phones", "skills", "linkedin", "slug", "social_media", "secondary_position"]);
const AI_TOP = new Set(["firmable_id", "company_name", "country", "industry_group", "size_band", "overall_level",
  "explanation", "momentum", "dimensions", "evidence_available", "framework_version"]);
const DEFAULT_COMPANY = ["name", "company_size", "employee_count", "hq_country", "hq_location", "industries",
  "company_type", "year_founded", "revenue", "abn_operation_status"];
const DEFAULT_PERSON = ["position", "seniority", "department", "current_company.id", "current_company.name", "country"];
const DEFAULT_AI = ["overall_level", "momentum.label", "dimensions.architecture.level", "dimensions.footprint.level",
  "dimensions.hiring.level", "dimensions.initiatives.level", "dimensions.workforce.level"];

const CHANGE_TYPES = ["newly_observed", "no_longer_observed_in_retrieved_data", "value_changed",
  "not_compared_fetch_failed", "not_compared_not_found"];
const DIFFERENCE_TYPES = new Set(["newly_observed", "no_longer_observed_in_retrieved_data", "value_changed"]);
const CHANGE_MEANING: Record<string, string> = {
  newly_observed: "Empty in the previous snapshot and has a value in this run's retrieved data.",
  no_longer_observed_in_retrieved_data: "Had a value in the previous snapshot and is empty in this run's " +
    "retrieved data. It does not mean the thing no longer exists.",
  value_changed: "Both snapshots hold a value and the values differ.",
  not_compared_fetch_failed: "The request failed or was not sent in this run, so nothing was compared.",
  not_compared_not_found: "Firmable returned no data for this subject in this run, so nothing was compared.",
};
const STATUS_TONE: Record<string, string> = {
  changes_detected: "review", no_changes_detected: "good", partially_compared: "unknown", baseline_created: "info",
  fetch_failed: "error", not_monitored: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const CHANGE_TONE: Record<string, string> = {
  newly_observed: "info", no_longer_observed_in_retrieved_data: "review", value_changed: "review",
  not_compared_fetch_failed: "error", not_compared_not_found: "unknown",
};

const TOP_KEYS = new Set(["previous_snapshot", "evaluation_date", "company_fields", "person_fields", "ai_adoption_fields",
  "tech_intel"]);
const FIELD = /^[a-z0-9_]+(\.[a-z0-9_]+)*$/;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const LINKEDIN = /^https?:\/\/([a-z]+\.)?linkedin\.com\/in\/[^/?#]+/i;

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x); // eslint-disable-line @typescript-eslint/no-explicit-any
const clone = <T>(x: T): T => structuredClone(x);

// ----- configuration --------------------------------------------------------------------------

function fields(raw: Json, key: string, allowed: Set<string>, dflt: string[]): string[] {
  const value = raw[key] ?? dflt;
  if (!Array.isArray(value) || !value.every((f) => typeof f === "string")) throw new ConfigError(`${key} must be a list of field names.`);
  const out: string[] = [];
  for (const f of value as string[]) {
    if (!FIELD.test(f) || !allowed.has(f.split(".")[0])) {
      throw new ConfigError(`${key} has an unknown field ${pyRepr(f)}. Allowed top-level fields: ${[...allowed].sort().join(", ")}.`);
    }
    if (out.includes(f)) throw new ConfigError(`${key} lists ${pyRepr(f)} twice.`);
    out.push(f);
  }
  return out;
}

function loadPrevious(value: unknown): Json | null {
  if (value === null || value === undefined) return null;
  let data: unknown;
  if (typeof value === "string") {
    if (!existsSync(value) || !statSync(value).isFile()) throw new ConfigError(`previous_snapshot file not found: ${value}`);
    try {
      data = JSON.parse(readFileSync(value, "utf-8"));
    } catch {
      throw new ConfigError(`previous_snapshot file ${value} is not valid JSON.`);
    }
  } else if (isObj(value)) {
    data = value;
  } else {
    throw new ConfigError("previous_snapshot must be a file path, a snapshot object or null.");
  }
  if (!isObj(data) || data.snapshot_format !== SNAPSHOT_FORMAT) {
    throw new ConfigError(`previous_snapshot is not a ${SNAPSHOT_FORMAT} snapshot. Use the snapshot.json written by an earlier run.`);
  }
  parseDate(data.taken_on, "previous_snapshot taken_on");
  if (!isObj(data.accounts)) throw new ConfigError("previous_snapshot has no accounts object.");
  for (const [key, acc] of Object.entries(data.accounts)) {
    if (!isObj(acc) || !isObj(acc.sources)) throw new ConfigError(`previous_snapshot account ${pyRepr(key)} has no sources object.`);
  }
  return data;
}

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const company = fields(raw, "company_fields", COMPANY_TOP, DEFAULT_COMPANY);
  const person = fields(raw, "person_fields", PERSON_TOP, DEFAULT_PERSON);
  const ai = fields(raw, "ai_adoption_fields", AI_TOP, DEFAULT_AI);
  const ti = raw.tech_intel ?? {};
  if (!isObj(ti)) throw new ConfigError("tech_intel must be an object.");
  const extra = Object.keys(ti).filter((k) => k !== "enabled" && k !== "max_pages").sort();
  if (extra.length) throw new ConfigError(`Unknown tech_intel key(s): ${extra.join(", ")}`);
  const enabled = ti.enabled ?? true;
  if (typeof enabled !== "boolean") throw new ConfigError("tech_intel.enabled must be true or false.");
  const maxPages = ti.max_pages ?? 1;
  if (typeof maxPages !== "number" || !Number.isInteger(maxPages) || maxPages < 1 || maxPages > 20) {
    throw new ConfigError("tech_intel.max_pages must be a whole number from 1 to 20.");
  }
  if (!company.length && !person.length && !ai.length && !enabled) {
    throw new ConfigError("Nothing to monitor: every field list is empty and tech_intel is disabled.");
  }
  let evaluationDate = raw.evaluation_date ?? null;
  if (evaluationDate !== null) evaluationDate = parseDate(evaluationDate, "evaluation_date");
  const prevRaw = raw.previous_snapshot ?? null;
  const previous = loadPrevious(prevRaw);
  return {
    previous_snapshot: typeof prevRaw === "string" ? prevRaw : (previous ? "inline" : null),
    evaluation_date: evaluationDate, company_fields: company, person_fields: person, ai_adoption_fields: ai,
    tech_intel: { enabled, max_pages: maxPages }, previous_snapshot_data: previous,
  };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ["domain", "company_id", "person_id", "work_email", "linkedin_url"] });
}

// ----- subjects -------------------------------------------------------------------------------

type Ident = Record<string, string> | null;

function subject(row: Row): [Ident, Ident, string[]] {
  const problems: string[] = [];
  let company: Ident = null;
  let person: Ident = null;
  if (row.company_id) company = { id: row.company_id };
  else if (row.domain) {
    const d = normaliseDomain(row.domain);
    if (d) company = { fqdn: d };
    else problems.push(`${pyRepr(row.domain)} is not a valid domain.`);
  }
  if (row.person_id) person = { id: row.person_id };
  else if (row.work_email) {
    const e = row.work_email.toLowerCase();
    if (EMAIL.test(e)) person = { work_email: e };
    else problems.push(`${pyRepr(row.work_email)} is not a valid work email.`);
  } else if (row.linkedin_url) {
    const u = row.linkedin_url;
    if (LINKEDIN.test(u)) person = { ln_url: u };
    else problems.push(`${pyRepr(u)} is not a LinkedIn profile URL.`);
  }
  return [company, person, problems];
}

function ident(identifier: Ident): string {
  if (!identifier) return "";
  const k = Object.keys(identifier)[0];
  return `${k}=${identifier[k]}`;
}

function keyOf(company: Ident, person: Ident): string {
  const parts: string[] = [];
  if (company) parts.push(`company:${ident(company)}`);
  if (person) parts.push(`person:${ident(person)}`);
  return parts.join("|");
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const companies: string[] = [];
  const persons: string[] = [];
  for (const r of rows) {
    const [company, person, problems] = subject(r);
    if (problems.length) continue;
    if (company && !companies.includes(ident(company))) companies.push(ident(company));
    if (person && !persons.includes(ident(person))) persons.push(ident(person));
  }
  const nc = companies.length;
  const npn = persons.length;
  const calls: PlannedCall[] = [];
  if (config.company_fields.length) {
    calls.push(new PlannedCall("GET /company", "Company profile for each unique company identifier", nc,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * nc)]]));
  }
  if (config.person_fields.length) {
    calls.push(new PlannedCall("GET /people", "Person profile for each unique contact identifier", npn,
      [["person", pricing.round4(pricing.PERSON_PROFILE * npn)]]));
  }
  if (config.ai_adoption_fields.length) {
    calls.push(new PlannedCall("POST /ai-adoption", "AI Adoption for each unique company identifier", nc,
      pricing.estimateAiAdoption(nc, false)));
  }
  const ti = config.tech_intel;
  if (ti.enabled) {
    calls.push(new PlannedCall("GET /tech-intel", `Up to ${ti.max_pages} page(s) of technologies for each unique company identifier`,
      nc * ti.max_pages, [["tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * nc)]]));
  }
  return calls;
}

// ----- values and comparison ------------------------------------------------------------------

function pick(obj: Json, path: string): Json {
  let cur = obj;
  for (const part of path.split(".")) {
    if (!isObj(cur)) return null;
    cur = cur[part];
  }
  return cur ?? null;
}

const canon = (x: unknown): string => stableStringify(x);

function empty(v: unknown): boolean {
  return v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length) || (isObj(v) && !Object.keys(v).length);
}

function change(source: string, field: string, kind: string, previous: Json, current: Json, noteText: string | null): Json {
  return { source, field, change_type: kind, previous: previous ?? null, current: current ?? null, note: noteText };
}

function techValues(result: Json): Json {
  const techs: Record<string, Json> = {};
  for (const item of result.items ?? []) {
    if (!isObj(item)) continue;
    const key = `${item.filter_slug || "-"}/${item.product_slug || item.product || "-"}`;
    if (key in techs) continue;
    techs[key] = { product: item.product ?? null, vendor: item.vendor ?? null, category: item.category ?? null,
      confidence: item.confidence ?? null, nature: item.nature ?? null };
  }
  return { total: result.total ?? null, complete: !!result.complete, technologies: techs };
}

function kindOf(a: unknown, b: unknown): string {
  return empty(a) ? "newly_observed" : (empty(b) ? "no_longer_observed_in_retrieved_data" : "value_changed");
}

function compareFields(source: string, prev: Json, cur: Json, contextNote: ((f: string) => string | null) | null): [Json[], string[]] {
  const changes: Json[] = [];
  const baseline: string[] = [];
  for (const [f, b] of Object.entries(cur)) {
    if (!(f in prev)) { baseline.push(f); continue; }
    const a = prev[f];
    if (canon(a) === canon(b)) continue;
    const kind = kindOf(a, b);
    changes.push(change(source, f, kind, a, b, kind === "value_changed" && contextNote ? contextNote(f) : null));
  }
  return [changes, baseline];
}

function compareTech(prev: Json, cur: Json): [Json[], number] {
  const changes: Json[] = [];
  if ("total" in prev && canon(prev.total ?? null) !== canon(cur.total ?? null)) {
    const a = prev.total ?? null;
    const b = cur.total ?? null;
    const kind = a === null ? "newly_observed" : (b === null ? "no_longer_observed_in_retrieved_data" : "value_changed");
    changes.push(change("tech_intel", "total", kind, a, b, null));
  }
  const pt: Json = prev.technologies ?? {};
  const ct: Json = cur.technologies ?? {};
  const prevComplete = !!prev.complete;
  const bothComplete = prevComplete && !!cur.complete;
  const newNote = prevComplete ? null : "The previous technology list was incomplete, so this technology may have been present but not retrieved.";
  for (const k of Object.keys(ct).filter((x) => !(x in pt)).sort()) {
    changes.push(change("tech_intel", `technology:${k}`, "newly_observed", null, ct[k], newNote));
  }
  let unchecked = 0;
  for (const k of Object.keys(pt).filter((x) => !(x in ct)).sort()) {
    if (bothComplete) changes.push(change("tech_intel", `technology:${k}`, "no_longer_observed_in_retrieved_data", pt[k], null, null));
    else unchecked += 1;
  }
  for (const k of Object.keys(pt).filter((x) => x in ct).sort()) {
    for (const attr of ["confidence", "nature"]) {
      const a = (pt[k] ?? {})[attr] ?? null;
      const b = (ct[k] ?? {})[attr] ?? null;
      if (canon(a) !== canon(b)) changes.push(change("tech_intel", `technology:${k}.${attr}`, kindOf(a, b), a, b, null));
    }
  }
  return [changes, unchecked];
}

// ----- run ------------------------------------------------------------------------------------

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const today: string = cfg.evaluation_date ?? ctx.today;
  const previous: Json = cfg.previous_snapshot_data;
  const prevAccounts: Record<string, Json> = previous?.accounts ?? {};
  const prevDate: string | null = previous?.taken_on ?? null;
  const coverage: string[] = [
    "A difference means the data Firmable returned in this run differs from the saved snapshot. It is not a " +
      "confirmed real-world change: Firmable may have refreshed, corrected or newly found the data.",
    "A failed or missing response is never reported as a deletion. The previous values are kept in the new snapshot.",
    "Volatile fields are never compared: request IDs, usage, AI Adoption as_of, and Tech Intel recency and last_observed.",
  ];
  if (previous === null) coverage.push("No previous snapshot was given, so this run records a baseline only and makes no claims about change.");
  else coverage.push(`Compared with the snapshot taken on ${prevDate}.`);
  coverage.push("This code does not schedule itself. See the README for how to run it on a schedule.");

  const keys: Record<string, string> = {};
  for (const r of ctx.rows) {
    const [company, person, problems] = subject(r);
    keys[r.row_id] = problems.length ? "" : keyOf(company, person);
  }
  const keyed = ctx.rows.map((r) => ({ ...r, _key: keys[r.row_id] })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier account; their API responses were reused, not requested again.`);

  const newAccounts: Record<string, Json> = {};
  const incompleteTech = new Set<string>();
  let forbiddenTech = 0;

  const record = (rowId: string, err: Json): Json => {
    const entry = { row_id: rowId, ...err };
    ctx.errors.push(entry);
    if (FATAL_KINDS.has(entry.kind) && ctx.stopped === null) ctx.stopped = entry;
    return entry;
  };

  const fetch = async (rowId: string, source: string, company: Ident, person: Ident, companyId: string | null): Promise<Json> => {
    const out: Json = { result: "not_requested", values: null, context: null, error: null, id: null, name: null };
    if (source === "company" && (!cfg.company_fields.length || !company)) return out;
    if (source === "person" && (!cfg.person_fields.length || !person)) return out;
    if (source === "ai_adoption" && (!cfg.ai_adoption_fields.length || !company)) return out;
    if (source === "tech_intel" && (!cfg.tech_intel.enabled || !company)) return out;
    if (ctx.stopped !== null) return { ...out, result: "skipped" };
    try {
      if (source === "company") {
        const c = await client.getCompany(company as Record<string, string>);
        const fs = ["id", ...cfg.company_fields.filter((f: string) => f !== "id")];
        return { ...out, result: "ok", values: Object.fromEntries(fs.map((f) => [f, pick(c, f)])), id: c.id ?? null, name: c.name ?? null };
      }
      if (source === "person") {
        const p = await client.getPerson(person as Record<string, string>);
        const fs = ["id", ...cfg.person_fields.filter((f: string) => f !== "id")];
        return { ...out, result: "ok", values: Object.fromEntries(fs.map((f) => [f, pick(p, f)])), id: p.id ?? null, name: p.name ?? null };
      }
      const co = company as Record<string, string>;
      if (source === "ai_adoption") {
        const body = companyId ? { company_id: companyId } : ("id" in co ? { company_id: co.id } : { domain: co.fqdn });
        const resp = await client.getAiAdoption(body);
        const comp = resp.company ?? null;
        const err = isObj(resp.error) ? resp.error : null;
        if (comp === null) {
          if (err && err.code !== null && err.code !== undefined && err.code !== "not_found") {
            const e = record(rowId, { kind: "answer_error", status: 200, code: err.code, endpoint: ENDPOINT[source],
              message: String(err.message || err.code), charge_uncertain: false, retry_after: null });
            return { ...out, result: "error", error: e };
          }
          return { ...out, result: "not_found" };
        }
        const fs = ["firmable_id", ...cfg.ai_adoption_fields.filter((f: string) => f !== "firmable_id")];
        return { ...out, result: "ok", values: Object.fromEntries(fs.map((f) => [f, pick(comp, f)])),
          context: { framework_version: comp.framework_version ?? null, as_of: comp.as_of ?? null },
          id: comp.firmable_id ?? null, name: comp.company_name ?? null };
      }
      const identifier = companyId ? { id: companyId } : co;
      const res = await client.getTechIntel(identifier, { maxPages: cfg.tech_intel.max_pages });
      let errEntry: Json = null;
      if (res.error) {
        errEntry = record(rowId, res.error);
        if (res.error.kind === "forbidden") forbiddenTech += 1;
      }
      if ((res.pages_fetched ?? 0) === 0) return { ...out, result: "error", error: errEntry };
      if (!res.complete) incompleteTech.add(ident(identifier));
      return { ...out, result: "ok", values: techValues(res), error: errEntry, id: res.id ?? null };
    } catch (e) {
      if (e instanceof NotFoundError) return { ...out, result: "not_found" };
      if (e instanceof FirmableError) return { ...out, result: "error", error: ctx.recordError(rowId, e) };
      throw e;
    }
  };

  const aiNote = (prevCtx: Json, curCtx: Json): ((f: string) => string | null) | null => {
    const pv = prevCtx?.framework_version ?? null;
    const cv = curCtx?.framework_version ?? null;
    if (pv === null || cv === null || pv === cv) return null;
    const text = `The AI Adoption framework version changed from ${pv} to ${cv}. The level may differ because of ` +
      "the new scoring rules, not because of the company.";
    return (f) => (f === "overall_level" || f.endsWith(".level") ? text : null);
  };

  const idNote = (source: string): ((f: string) => string | null) => {
    const text = source === "person"
      ? "The contact identifier now returns a different Firmable person ID. The other person fields may compare two different people."
      : `The company identifier now returns a different Firmable company ID. The other ${SOURCE_LABEL[source]} fields may compare two different companies.`;
    return (f) => (f === "id" || f === "firmable_id" ? text : null);
  };

  const one = async (row: Row): Promise<Json> => {
    const [company, person, problems] = subject(row);
    const key = keys[row.row_id];
    const base: Json = { row_id: row.row_id, input: row, key: key || null, duplicate_of: dupes[row.row_id] ?? null,
      company: null, person: null, sources: {}, changes: [], baseline_fields: [], errors: [] };
    if (problems.length) return { ...base, status: "invalid_input", status_reason: problems.join(" ") };
    const prevAcc = prevAccounts[key] ?? null;
    const prevSources: Json = prevAcc?.sources ?? {};
    const fetched: Record<string, Json> = {};
    let companyId: string | null = null;
    for (const source of SOURCES) {
      fetched[source] = await fetch(row.row_id, source, company, person, companyId);
      if (source === "company" && fetched[source].result === "ok") companyId = fetched[source].id;
    }
    if (fetched.company.result === "ok") base.company = { id: fetched.company.id, name: fetched.company.name };
    else if (fetched.ai_adoption.result === "ok") base.company = { id: fetched.ai_adoption.id, name: fetched.ai_adoption.name };
    if (fetched.person.result === "ok") base.person = { id: fetched.person.id, name: fetched.person.name };

    const sourcesOut: Record<string, Json> = {};
    const changes: Json[] = [];
    const baselineFields: string[] = [];
    const newSources: Record<string, Json> = {};
    let uncheckedTech = 0;
    for (const source of SOURCES) {
      const cur = fetched[source];
      const prevEntry = prevSources[source];
      const prevVals = isObj(prevEntry) ? (prevEntry.values ?? null) : null;
      const result: string = cur.result;
      let entry: Json;
      if (result === "ok") {
        entry = { values: cur.values, observed_on: today, last_result: "ok", last_attempt_on: today };
        if (cur.context !== null) entry.context = cur.context;
      } else if (isObj(prevEntry) && result !== "not_requested") {
        entry = { ...clone(prevEntry), last_result: result, last_attempt_on: today };
      } else if (isObj(prevEntry)) {
        entry = clone(prevEntry);
      } else {
        entry = { values: null, observed_on: null, last_result: result, last_attempt_on: result !== "not_requested" ? today : null };
      }
      newSources[source] = entry;

      let comparison: string;
      if (result === "not_requested") comparison = "not_requested";
      else if (prevAcc === null) comparison = "baseline";
      else if (result === "error" || result === "skipped") {
        comparison = "not_compared";
        const msg = result === "skipped" ? "The run stopped before this request, so nothing was compared."
          : "The request failed in this run, so nothing was compared.";
        changes.push(change(source, "*", "not_compared_fetch_failed", null, null, msg + " The previous values are kept in the new snapshot."));
      } else if (result === "not_found") {
        if (prevVals === null) comparison = "compared";
        else {
          comparison = "not_compared";
          changes.push(change(source, "*", "not_compared_not_found", null, null,
            "Firmable returned no data for this subject in this run. This is not reported as a deletion; the previous values are kept in the new snapshot."));
        }
      } else if (prevVals === null && (!isObj(prevEntry) || prevEntry.last_result !== "not_found")) {
        comparison = "baseline";
      } else {
        comparison = "compared";
        if (source === "tech_intel") {
          const prevT = prevVals !== null ? prevVals : { total: null, complete: true, technologies: {} };
          const [found, unchecked] = compareTech(prevT, cur.values);
          uncheckedTech = unchecked;
          changes.push(...found);
        } else {
          const prevF = prevVals !== null ? prevVals : Object.fromEntries(Object.keys(cur.values).map((f) => [f, null]));
          let noteFn: (f: string) => string | null;
          if (source === "ai_adoption") {
            const nAi = aiNote(isObj(prevEntry) ? prevEntry.context : null, cur.context);
            const nId = idNote(source);
            noteFn = (f) => nId(f) || (nAi ? nAi(f) : null);
          } else {
            noteFn = idNote(source);
          }
          const [found, baseF] = compareFields(source, prevF, cur.values, noteFn);
          changes.push(...found);
          baselineFields.push(...baseF.map((f) => `${source}.${f}`));
        }
      }
      sourcesOut[source] = { result, comparison, error: cur.error ?? null };
    }

    const requested = SOURCES.filter((s) => fetched[s].result !== "not_requested");
    if (!requested.length) {
      return { ...base, status: "not_monitored", sources: sourcesOut,
        status_reason: "No enabled source applies to this row's identifiers, so nothing was fetched." };
    }
    newAccounts[key] = { key, identifiers: { company, person }, sources: newSources };
    const failed = requested.filter((s) => fetched[s].result === "error" || fetched[s].result === "skipped");
    const diffs = changes.filter((c) => DIFFERENCE_TYPES.has(c.change_type));
    const notCompared = SOURCES.filter((s) => sourcesOut[s].comparison === "not_compared");
    let extra = "";
    if (uncheckedTech) {
      extra = ` ${uncheckedTech} technology(ies) from the previous snapshot were not in the retrieved pages; ` +
        "the list is incomplete, so they are not reported as no longer observed.";
    }
    let status: string;
    let reason: string;
    if (failed.length === requested.length) {
      status = "fetch_failed";
      reason = prevAcc === null
        ? "Every requested source failed in this run, so no baseline values were recorded for this account."
        : "Every requested source failed in this run, so nothing was compared. The previous values are kept in the new snapshot.";
    } else if (prevAcc === null) {
      status = "baseline_created";
      reason = previous === null
        ? "No previous snapshot was given. This run records a baseline and makes no claims about change."
        : "This account is not in the previous snapshot. This run records a baseline for it and makes no claims about change.";
      if (failed.length) reason += ` ${failed.length} source(s) failed and have no baseline yet.`;
    } else if (diffs.length) {
      status = "changes_detected";
      reason = `${diffs.length} difference(s) from the snapshot taken on ${prevDate}. A difference is a change ` +
        "in the retrieved data, not a confirmed real-world change.";
      if (notCompared.length) reason += ` ${notCompared.length} source(s) could not be compared.`;
    } else if (notCompared.length) {
      status = "partially_compared";
      reason = `No differences in the sources compared. ${notCompared.length} source(s) could not be compared.`;
    } else {
      status = "no_changes_detected";
      reason = `No differences from the snapshot taken on ${prevDate} in the monitored fields.`;
    }
    const newBaseline = requested.filter((s) => sourcesOut[s].comparison === "baseline");
    if (newBaseline.length && prevAcc !== null) reason += ` ${newBaseline.length} source(s) had no previous values and now have a baseline.`;
    if (baselineFields.length && prevAcc !== null) {
      reason += ` ${baselineFields.length} field(s) were not in the previous snapshot and now have a baseline.`;
    }
    reason += extra;
    const errors = failed.map((s) => `${SOURCE_LABEL[s]}: ${sourcesOut[s].error?.message || "not sent because the run stopped"}`);
    return { ...base, status, status_reason: reason, sources: sourcesOut, changes, baseline_fields: baselineFields, errors };
  };

  const rows = await ctx.runRows(ctx.rows, one);

  const snapshotAccounts: Record<string, Json> = clone(prevAccounts);
  Object.assign(snapshotAccounts, newAccounts);
  const inputKeys = new Set(Object.values(keys));
  const carried = Object.keys(prevAccounts).filter((k) => !(k in newAccounts)).sort();
  const notInInput = carried.filter((k) => !inputKeys.has(k));
  if (notInInput.length) {
    coverage.push(`${notInInput.length} account(s) in the previous snapshot are not in this input; they were copied to the new snapshot unchanged.`);
  }
  if (incompleteTech.size) {
    coverage.push(`${incompleteTech.size} Tech Intel list(s) are incomplete because of the page limit or an error. ` +
      "Technologies missing from an incomplete list are not reported as no longer observed.");
  }
  if (forbiddenTech) coverage.push(`Tech Intel returned 403 for ${forbiddenTech} request(s). Tech Intel must be on your plan.`);
  const snapshot = {
    snapshot_format: SNAPSHOT_FORMAT, taken_on: today,
    monitored: { company_fields: cfg.company_fields, person_fields: cfg.person_fields,
      ai_adoption_fields: cfg.ai_adoption_fields, tech_intel: cfg.tech_intel },
    accounts: snapshotAccounts,
  };

  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const changeCounts: Record<string, number> = Object.fromEntries(CHANGE_TYPES.map((t) => [t, 0]));
  for (const r of rows) for (const c of r.changes ?? []) changeCounts[c.change_type] += 1;
  const summary = { status_counts: counts, change_counts: changeCounts, change_type_meanings: CHANGE_MEANING,
    previous_snapshot_taken_on: prevDate, snapshot_taken_on: today, snapshot };
  return { rows, summary, coverage };
}

// ----- output ---------------------------------------------------------------------------------

export const CSV_COLUMNS = ["row_id", "key", "duplicate_of", "status", "status_reason", "company_id", "company_name",
  "person_id", "person_name", "differences", "not_compared", "baseline_fields", "changes"];

function short(v: unknown): string {
  if (v === null || v === undefined) return "-";
  const s = typeof v === "string" ? v : canon(v);
  const cp = Array.from(s);
  return cp.length <= 80 ? s : cp.slice(0, 77).join("") + "...";
}

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const changes: Json[] = r.changes ?? [];
    const company = r.company ?? {};
    const person = r.person ?? {};
    return {
      row_id: r.row_id, key: r.key ?? null, duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: company.id ?? null, company_name: company.name ?? null,
      person_id: person.id ?? null, person_name: person.name ?? null,
      differences: changes.filter((c) => DIFFERENCE_TYPES.has(c.change_type)).length,
      not_compared: changes.filter((c) => !DIFFERENCE_TYPES.has(c.change_type)).length,
      baseline_fields: r.baseline_fields ?? [],
      changes: changes.map((c) => `${c.source}.${c.field}=${c.change_type}(${short(c.previous)} -> ${short(c.current)})`),
    };
  });
}

export function render(report: Report, result: Json): void {
  const s = result.summary;
  const counts: Record<string, number> = s.status_counts ?? {};
  const meanings: Record<string, string> = s.change_type_meanings ?? CHANGE_MEANING;
  const body = "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    kv([["Previous snapshot taken on", s.previous_snapshot_taken_on || "none (baseline run)"], ["New snapshot taken on", s.snapshot_taken_on]]) +
    note("A difference is a difference in the data Firmable returned. It is <strong>not a confirmed real-world change</strong>. " +
      "A failed or missing response is never shown as a deletion.", "unknown") +
    table(["Change type", "Count", "Meaning"], CHANGE_TYPES.map((t) => [t, (s.change_counts ?? {})[t] ?? 0, meanings[t]]));
  report.add("Result", "Change summary", body);
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.person?.name || r.key || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    left += kv([["Account key", r.key], ["Company ID", r.company?.id], ["Person ID", r.person?.id]]);
    const srcs: Record<string, Json> = r.sources ?? {};
    if (Object.keys(srcs).length) {
      left += table(["Source", "This run", "Comparison"], Object.entries(srcs).map(([k, v]) =>
        [SOURCE_LABEL[k], v.result.replace(/_/g, " "), v.comparison.replace(/_/g, " ")]));
    }
    if (r.duplicate_of) left += note(`Same account as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    const changes: Json[] = r.changes ?? [];
    const diffs = changes.filter((c) => DIFFERENCE_TYPES.has(c.change_type));
    const gaps = changes.filter((c) => !DIFFERENCE_TYPES.has(c.change_type));
    if (diffs.length) {
      right += "<h3>Differences in retrieved data</h3>" + table(["Source", "Field", "Change", "Previous", "Current"], diffs.map((c) => [
        SOURCE_LABEL[c.source], c.field, new Raw(badge(c.change_type.replace(/_/g, " "), CHANGE_TONE[c.change_type] ?? "neutral")),
        short(c.previous), short(c.current)]));
      for (const c of diffs) if (c.note) right += note(`${esc(c.field)}: ${esc(c.note)}`, "info");
    }
    if (gaps.length) right += "<h3>Not compared</h3>" + bulletList(gaps.map((c) => `${SOURCE_LABEL[c.source]}: ${c.note}`));
    if ((r.baseline_fields ?? []).length && status !== "baseline_created") right += "<h3>New baseline fields</h3>" + bulletList(r.baseline_fields);
    if (changes.length) right += detailsJson("Changes as JSON", changes);
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No differences to show for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its differences", cards.join(""));
  report.add("Next run", "Snapshot for the next run",
    "<p>The new snapshot is in <code>results.json</code> under <code>summary.snapshot</code>. The command line also " +
    "writes it to <code>snapshot.json</code>. Set <code>previous_snapshot</code> in the configuration to that file for the next run.</p>" +
    note("This code does not schedule itself. To run it on a schedule, use cron, a CI schedule or another scheduler you control; see the README.", "info"));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
