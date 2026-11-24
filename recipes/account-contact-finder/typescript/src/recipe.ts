/**
 * Account contact finder: finds relevant people at supplied accounts.
 * Mirrors recipes/account-contact-finder/python/src/account_contact_finder/recipe.py; see that file and
 * the recipe README for the selection policy.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report,
  RunContext, Row, badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain,
  note, pricing, readRows, table,
} from "firmable-gtm-client";

export const NAME = "account-contact-finder";
export const TITLE = "Account contact finder";
export const DESCRIPTION = "Finds people at each account with Firmable people search, checks each candidate with Decisions, " +
  "and retrieves contact details only for the people the selection policy selects.";

const COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
// Search ids and labels from the published people search reference.
export const SENIORITY: Record<string, string> = {
  "1": "Board Member / Company Director", "2": "Owner / Founder", "3": "C-Suite / Partner",
  "4": "VP/Director / Head of", "5": "Manager", "6": "Other", "7": "No Data Available",
};
export const DEPARTMENT: Record<string, string> = {
  "1": "General Management", "2": "Sales", "3": "Trades", "4": "Operations", "5": "Engineering & Technical",
  "6": "Human Resources", "7": "Customer Service", "8": "Medicine & Healthcare", "9": "Research & Analysis",
  "10": "Legal", "11": "Marketing", "12": "Education & Training", "13": "Consulting", "14": "Finance",
  "15": "Product", "16": "Other", "17": "No Data Available",
};
const SELECTIONS = ["selected", "needs_review", "not_selected", "rejected", "incomplete", "not_checked"];
const STATUS_TONE: Record<string, string> = {
  contacts_selected: "good", no_contacts_selected: "bad", no_candidates: "unknown", incomplete: "error",
  company_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const SELECTION_TONE: Record<string, string> = {
  selected: "good", needs_review: "review", not_selected: "bad", rejected: "bad", incomplete: "error", not_checked: "neutral",
};
const RESULT_TONE: Record<string, string> = { pass: "good", fail: "bad", unknown: "unknown", error: "error", not_asked: "neutral" };
const ID = /^[a-z0-9_]{1,40}$/;
const COMPANY_ID = /^f\d+$/;
const TOP_KEYS = new Set(["search", "checks", "policy", "with_reasoning", "max_candidates_checked", "max_details_per_account"]);
const SEARCH_KEYS = new Set(["country", "queries", "page_size", "max_pages"]);
const QUERY_KEYS = new Set(["id", "label", "position", "seniority", "department"]);
const CHECK_KEYS = new Set(["id", "label", "tool", "inputs", "want", "mandatory"]);

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const whole = (v: unknown, low: number, high: number | null): boolean =>
  typeof v === "number" && Number.isInteger(v) && v >= low && (high === null || v <= high);
const unknownKeys = (o: Record<string, unknown>, allowed: Set<string>) => Object.keys(o).filter((k) => !allowed.has(k)).sort();

/** Maps a documented id or its label to the id string. */
function code(value: unknown, tbl: Record<string, string>): string | null {
  if (typeof value === "number" && Number.isInteger(value)) value = String(value);
  if (typeof value !== "string") return null;
  const v = value.trim();
  if (v in tbl) return v;
  for (const [k, label] of Object.entries(tbl)) if (label.toLowerCase() === v.toLowerCase()) return k;
  return null;
}

export function loadConfig(raw: Json): Json {
  const unknown = unknownKeys(raw, TOP_KEYS);
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const s = raw.search;
  if (!isObj(s)) throw new ConfigError("search must be an object.");
  let bad = unknownKeys(s, SEARCH_KEYS);
  if (bad.length) throw new ConfigError(`search has unknown key(s): ${bad.join(", ")}`);
  const country = s.country ?? "AU";
  if (typeof country !== "string" || !COUNTRIES.includes(country)) throw new ConfigError(`search.country must be one of: ${COUNTRIES.join(", ")}.`);
  const qs = s.queries;
  if (!Array.isArray(qs) || !qs.length) throw new ConfigError("search.queries must be a non-empty list.");
  const queries: Json[] = [];
  let seen = new Set<string>();
  qs.forEach((qr: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(qr)) throw new ConfigError(`search.queries[${i}] must be an object.`);
    const b = unknownKeys(qr, QUERY_KEYS);
    if (b.length) throw new ConfigError(`search.queries[${i}] has unknown key(s): ${b.join(", ")}`);
    const qid = qr.id;
    if (typeof qid !== "string" || !ID.test(qid)) throw new ConfigError(`search.queries[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(qid)) throw new ConfigError(`search.queries[${i}].id '${qid}' is used twice.`);
    seen.add(qid);
    const label = qr.label ?? qid;
    if (typeof label !== "string") throw new ConfigError(`search.queries[${i}].label must be text.`);
    const position = qr.position ?? "";
    if (typeof position !== "string") throw new ConfigError(`search.queries[${i}].position must be text.`);
    let seniority: string | null = null;
    if (qr.seniority !== undefined && qr.seniority !== null && qr.seniority !== "") {
      seniority = code(qr.seniority, SENIORITY);
      if (seniority === null) throw new ConfigError(`search.queries[${i}].seniority must be a seniority id 1 to 7 or its label.`);
    }
    let department: string | null = null;
    if (qr.department !== undefined && qr.department !== null && qr.department !== "") {
      department = code(qr.department, DEPARTMENT);
      if (department === null) throw new ConfigError(`search.queries[${i}].department must be a department id 1 to 17 or its label.`);
    }
    if (!position.trim() && seniority === null && department === null) {
      throw new ConfigError(`search.queries[${i}] needs at least one of position, seniority or department.`);
    }
    queries.push({ id: qid, label, position: position.trim() || null, seniority, department });
  });
  const pageSize = s.page_size ?? 25;
  if (!whole(pageSize, 1, 100)) throw new ConfigError("search.page_size must be a whole number from 1 to 100.");
  const maxPages = s.max_pages ?? 1;
  if (!whole(maxPages, 1, 10)) throw new ConfigError("search.max_pages must be a whole number from 1 to 10.");

  const checksRaw = raw.checks ?? [];
  if (!Array.isArray(checksRaw)) throw new ConfigError("checks must be a list.");
  const checks: Json[] = [];
  seen = new Set<string>();
  checksRaw.forEach((c: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(c)) throw new ConfigError(`checks[${i}] must be an object.`);
    bad = unknownKeys(c, CHECK_KEYS);
    if (bad.length) throw new ConfigError(`checks[${i}] has unknown key(s): ${bad.join(", ")}`);
    const cid = c.id;
    if (typeof cid !== "string" || !ID.test(cid)) throw new ConfigError(`checks[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(cid)) throw new ConfigError(`checks[${i}].id '${cid}' is used twice.`);
    seen.add(cid);
    const tool = c.tool;
    if (typeof tool !== "string" || !tool) throw new ConfigError(`checks[${i}].tool must name a decision tool.`);
    const inputs = c.inputs ?? {};
    if (!isObj(inputs)) throw new ConfigError(`checks[${i}].inputs must be an object.`);
    const want = c.want ?? "yes";
    if (want !== "yes" && want !== "no") throw new ConfigError(`checks[${i}].want must be yes or no.`);
    const mandatory = c.mandatory ?? true;
    if (typeof mandatory !== "boolean") throw new ConfigError(`checks[${i}].mandatory must be true or false.`);
    const label = c.label ?? cid;
    if (typeof label !== "string") throw new ConfigError(`checks[${i}].label must be text.`);
    checks.push({ id: cid, label, tool, inputs, want, mandatory });
  });
  const policy = raw.policy ?? {};
  if (!isObj(policy)) throw new ConfigError("policy must be an object.");
  const mop = policy.min_optional_passes ?? 0;
  if (!whole(mop, 0, null)) throw new ConfigError("policy.min_optional_passes must be a whole number, 0 or more.");
  const optional = checks.filter((c) => !c.mandatory).length;
  if ((mop as number) > optional) throw new ConfigError(`policy.min_optional_passes is ${mop} but only ${optional} optional check(s) exist.`);
  const withReasoning = raw.with_reasoning ?? false;
  if (typeof withReasoning !== "boolean") throw new ConfigError("with_reasoning must be true or false.");
  const mcc = raw.max_candidates_checked ?? 10;
  if (!whole(mcc, 1, null)) throw new ConfigError("max_candidates_checked must be a whole number, 1 or more.");
  const mdp = raw.max_details_per_account ?? 3;
  if (!whole(mdp, 0, null)) throw new ConfigError("max_details_per_account must be a whole number, 0 or more.");
  return { search: { country, queries, page_size: pageSize, max_pages: maxPages }, checks,
    policy: { min_optional_passes: mop }, with_reasoning: withReasoning, max_candidates_checked: mcc,
    max_details_per_account: mdp };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ["domain", "company_id"] });
}

function accountKey(row: Row): string {
  const cid = (row.company_id ?? "").trim();
  if (cid) return COMPANY_ID.test(cid) ? `id:${cid}` : "";
  const d = normaliseDomain(row.domain);
  return d ? `domain:${d}` : "";
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const keys: string[] = [];
  for (const r of rows) {
    const k = accountKey(r);
    if (k && !keys.includes(k)) keys.push(k);
  }
  const n = keys.length;
  const domains = keys.filter((k) => k.startsWith("domain:")).length;
  const s = config.search;
  const nq = s.queries.length;
  const perAccount = nq * s.page_size * s.max_pages;
  const checked = Math.min(config.max_candidates_checked, perAccount);
  const details = Math.min(config.max_details_per_account, checked);
  const nchecks = config.checks.length;
  const calls: PlannedCall[] = [];
  if (nchecks) calls.push(new PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []));
  calls.push(new PlannedCall("GET /company", "Company profile for each unique valid domain without a company_id",
    domains, [["company", pricing.round4(pricing.COMPANY_PROFILE * domains)]]));
  calls.push(new PlannedCall("POST /people/search", `${nq} search(es) of up to ${s.max_pages} page(s) for each unique account`,
    n * nq * s.max_pages, []));
  if (nchecks) {
    calls.push(new PlannedCall("POST /decisions", `${nchecks} check(s) for up to ${checked} candidate(s) per account`,
      n * checked * nchecks, pricing.estimateDecision(n * checked * nchecks, config.with_reasoning)));
  }
  calls.push(new PlannedCall("GET /people", `Person profile for up to ${details} selected candidate(s) per account`,
    n * details, [["person", pricing.round4(pricing.PERSON_PROFILE * n * details)]]));
  return calls;
}

function filtersText(qr: Json): string {
  const parts: string[] = [];
  if (qr.position) parts.push(`position '${qr.position}'`);
  if (qr.seniority) parts.push(`seniority ${SENIORITY[qr.seniority]}`);
  if (qr.department) parts.push(`department ${DEPARTMENT[qr.department]}`);
  return parts.join(", ");
}

function select(checks: Json[], mop: number): [string, string] {
  if (!checks.length) return ["selected", "Selected on the search criteria alone; no checks are configured."];
  const labels = (items: Json[]) => items.map((c) => c.label).join(", ");
  const mandatory = checks.filter((c) => c.mandatory);
  const optional = checks.filter((c) => !c.mandatory);
  const failed = mandatory.filter((c) => c.result === "fail");
  const errored = mandatory.filter((c) => c.result === "error" || c.result === "not_asked");
  const unknown = mandatory.filter((c) => c.result === "unknown");
  if (failed.length) return ["rejected", `Mandatory check(s) failed: ${labels(failed)}.`];
  if (errored.length) return ["incomplete", `Mandatory check(s) have no answer because of an error or because the run stopped: ${labels(errored)}.`];
  if (unknown.length) {
    return ["needs_review", `Mandatory check(s) answered unknown: ${labels(unknown)}. ` +
      "Unknown means Firmable does not hold the fact; it is not a no."];
  }
  const passes = optional.filter((c) => c.result === "pass").length;
  const optUnknown = optional.filter((c) => c.result === "unknown").length;
  const tail = optUnknown ? ` ${optUnknown} optional check(s) answered unknown.` : "";
  if (passes >= mop) return ["selected", `All mandatory checks passed; ${passes} of ${optional.length} optional check(s) passed (minimum ${mop}).${tail}`];
  return ["not_selected", `All mandatory checks passed, but only ${passes} of ${optional.length} optional check(s) passed (minimum ${mop}).${tail}`];
}

/** Notes where a search filter and a Decisions check about the same attribute disagree. */
function disagreements(candidate: Json, queries: Record<string, Json>): string[] {
  const notes: string[] = [];
  for (const c of candidate.checks) {
    if (c.answer !== "no" || (c.tool !== "matches_seniority" && c.tool !== "matches_department")) continue;
    const [attr, tbl, key] = c.tool === "matches_seniority"
      ? ["seniority", SENIORITY, "seniorities"] : ["department", DEPARTMENT, "departments"];
    const wanted = (Array.isArray(c.inputs[key]) ? c.inputs[key] : []).map((x: unknown) => String(x).toLowerCase());
    for (const qid of candidate.matched_queries) {
      const cd = queries[qid][attr];
      if (cd && wanted.includes(tbl[cd].toLowerCase())) {
        notes.push(`Found by search '${queries[qid].label}' filtered on ${attr} ${tbl[cd]}, but ` +
          `check '${c.label}' answered no. Both are shown as returned.`);
        break;
      }
    }
  }
  return notes;
}

function countsText(cands: Json[]): string {
  const parts: string[] = [];
  for (const s of SELECTIONS) {
    const n = cands.filter((c) => c.selection === s).length;
    if (n) parts.push(`${s} ${n}`);
  }
  return parts.length ? ` Selection results: ${parts.join(", ")}.` : "";
}

function phoneText(ph: Json): string {
  if (ph.is_dnd === true) return `${ph.value} (do not call)`;
  if (ph.is_dnd === false) return String(ph.value);
  return `${ph.value} (do-not-call status not returned)`;
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const s = cfg.search;
  const queries: Record<string, Json> = Object.fromEntries(s.queries.map((qr: Json) => [qr.id, qr]));
  const checksCfg: Json[] = cfg.checks;
  const mop: number = cfg.policy.min_optional_passes;
  const coverage: string[] = [
    "People search accepts only companyId, selectedCountry, position, seniority, department, from and size. " +
      "It has no name, location or date filter, so this recipe offers none.",
    `Searches are limited to country ${s.country} (selectedCountry). People Firmable records in other ` +
      "countries are not returned.",
    "How people search matches position text is not documented. Results are shown as returned.",
    "Each search shows the total the API reported beside the records retrieved. Fewer records are retrieved " +
      "than the total when page_size and max_pages stop paging.",
    "A person not in the search results is not observed in retrieved data. It does not mean no such person " +
      "works there.",
    `Only selected candidates get a person profile (GET /people), up to ${cfg.max_details_per_account} per ` +
      "account. Contact details are shown as returned; phones flagged is_dnd true are marked do not call.",
  ];
  if (checksCfg.length) {
    coverage.unshift("An unknown answer means Firmable does not hold the fact needed to answer. It is never " +
      "counted as a no or as a pass.");
  }

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length } }, coverage };
  };

  if (checksCfg.length) {
    let catalogue: Json;
    try {
      catalogue = await client.listDecisionTools();
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      ctx.recordError(null, e);
      return stopAll("The decision tool list could not be read, so no searches or checks were made.");
    }
    const problems = decisions.checkTools(catalogue, checksCfg, "person");
    if (problems.length) {
      for (const p of problems) {
        ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
          message: p, charge_uncertain: false, retry_after: null });
      }
      coverage.push(...problems);
      return stopAll("The configuration names decision tools or inputs the API does not accept.");
    }
    const modelTools = [...new Set<string>(checksCfg.filter((c) => decisions.usesModel(catalogue, c.tool)).map((c) => c.tool))].sort();
    if (modelTools.length) {
      coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
        "This code applies only the rules in the configuration.");
    }
    if (checksCfg.some((c) => c.tool === "works_at_company")) {
      coverage.push("works_at_company is asked with the account's Firmable company ID, added by this recipe.");
    }
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _key: accountKey(r) })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier account; their API responses were reused, not requested again.`);
  let emptySearchSeen = false;

  const search = async (companyId: string, qr: Json, rowId: string): Promise<[Json, Json[]]> => {
    const bodyBase: Record<string, unknown> = { companyId, selectedCountry: s.country };
    if (qr.position) bodyBase.position = qr.position;
    if (qr.seniority) bodyBase.seniority = qr.seniority;
    if (qr.department) bodyBase.department = qr.department;
    const info: Json = { query_id: qr.id, label: qr.label, filters: filtersText(qr), total: null, retrieved: 0,
      pages_fetched: 0, complete: false, status: "ok", error: null };
    const records: Json[] = [];
    for (let page = 0; page < s.max_pages; page++) {
      if (ctx.stopped !== null) { info.status = "not_requested"; break; }
      const body = { ...bodyBase, from: String(page * s.page_size), size: String(s.page_size) };
      let resp: Json;
      try {
        resp = await client.searchPeople(body);
      } catch (e) {
        if (e instanceof FirmableError) {
          info.status = "error";
          info.error = ctx.recordError(rowId, e);
          break;
        } else {
          throw e;
        }
      }
      if (resp.success === false) emptySearchSeen = true;
      const pageRecords = (Array.isArray(resp.records) ? resp.records : []).filter((x: unknown) => isObj(x));
      info.pages_fetched += 1;
      info.retrieved += pageRecords.length;
      if (typeof resp.total === "number" && Number.isInteger(resp.total)) info.total = resp.total;
      records.push(...pageRecords);
      if (pageRecords.length < s.page_size || (info.total !== null && info.retrieved >= info.total)) {
        info.complete = true;
        break;
      }
    }
    if (info.status === "ok" && info.retrieved === 0) info.status = "no_matches";
    return [info, records];
  };

  const one = async (row: Row): Promise<Json> => {
    const domain = normaliseDomain(row.domain);
    const cidIn = (row.company_id ?? "").trim();
    const base: Json = { row_id: row.row_id, input: row, domain, duplicate_of: dupes[row.row_id] ?? null,
      company_id: null, company_id_source: null, company: null, searches: [], candidates: [] };
    if (cidIn && !COMPANY_ID.test(cidIn)) return { ...base, status: "invalid_input", status_reason: `'${cidIn}' is not a Firmable company ID.` };
    if (!cidIn && !domain) return { ...base, status: "invalid_input", status_reason: `'${row.domain ?? ""}' is not a valid domain.` };
    let companyId: string;
    let source: string;
    if (cidIn) {
      [companyId, source] = [cidIn, "input"];
    } else {
      let company: Json;
      try {
        company = await client.getCompany({ fqdn: domain! });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this domain, so no search was made." };
        }
        throw e;
      }
      base.company = facts.companyFacts(company);
      [companyId, source] = [company.id, "company_lookup"];
    }
    base.company_id = companyId;
    base.company_id_source = source;

    const searches: Json[] = [];
    const found = new Map<string, Json>();
    let noId = 0;
    for (const qr of s.queries) {
      const [info, records] = await search(companyId, qr, row.row_id);
      searches.push(info);
      for (const rec of records) {
        const pid = rec.person_id || rec.id;
        if (typeof pid !== "string" || !pid) { noId += 1; continue; }
        const prev = found.get(pid);
        if (prev) {
          if (!prev.matched_queries.includes(qr.id)) prev.matched_queries.push(qr.id);
          continue;
        }
        found.set(pid, { person_id: pid, name: rec.name ?? null, position: rec.position ?? null,
          company_name: rec.company_name ?? null, has_email: rec.has_email ?? null, has_phone: rec.has_phone ?? null,
          has_mobile: rec.has_mobile ?? null, has_dnd_phone: rec.has_dnd_phone ?? null, matched_queries: [qr.id] });
      }
    }
    const candidates = [...found.values()];
    for (let n = 0; n < candidates.length; n++) {
      const cand = candidates[n];
      cand.checks = [];
      cand.notes = [];
      if (n >= cfg.max_candidates_checked) {
        cand.selection = "not_checked";
        cand.selection_reason = `Not checked: the account reached max_candidates_checked (${cfg.max_candidates_checked}).`;
        continue;
      }
      for (const c of checksCfg) {
        const entry = { check_id: c.id, label: c.label, tool: c.tool, inputs: c.inputs, mandatory: c.mandatory, want: c.want };
        if (ctx.stopped !== null) { cand.checks.push({ ...entry, result: "not_asked", answer: null, error: null }); continue; }
        const question: Json = { person_id: cand.person_id, ...c.inputs };
        if (c.tool === "works_at_company") question.company_id = companyId;
        let resp: Json;
        try {
          resp = await client.makeDecision(c.tool, question, { withReasoning: cfg.with_reasoning });
        } catch (e) {
          if (!(e instanceof FirmableError)) throw e;
          const err = ctx.recordError(row.row_id, e);
          cand.checks.push({ ...entry, result: "error", answer: null, error: err });
          continue;
        }
        const ans = decisions.answerFrom(resp);
        const result = decisions.compare(ans.answer, c.want);
        cand.checks.push({ ...entry, result, ...ans, error: result === "error" ? ans.answer_error : null });
      }
      [cand.selection, cand.selection_reason] = select(cand.checks, mop);
      cand.notes = disagreements(cand, queries);
    }

    let fetched = 0;
    for (const cand of candidates) {
      Object.assign(cand, { details_status: "not_selected", person: null, contact: null,
        current_company_matches_account: null, details_error: null });
      if (cand.selection !== "selected") continue;
      if (fetched >= cfg.max_details_per_account) { cand.details_status = "not_fetched_limit"; continue; }
      if (ctx.stopped !== null) { cand.details_status = "not_requested"; continue; }
      fetched += 1;
      let person: Json;
      try {
        person = await client.getPerson({ id: cand.person_id });
      } catch (e) {
        if (e instanceof NotFoundError) { cand.details_status = "not_found"; continue; }
        if (!(e instanceof FirmableError)) throw e;
        cand.details_status = "error";
        cand.details_error = ctx.recordError(row.row_id, e);
        continue;
      }
      cand.details_status = "fetched";
      cand.person = facts.personFacts(person);
      cand.contact = facts.contactDetails(person);
      const cur = cand.person.current_company_id;
      cand.current_company_matches_account = !cur ? "unknown" : cur === companyId ? "yes" : "no";
      if (cand.current_company_matches_account === "no") {
        cand.notes.push(`The person profile shows current company ${cand.person.current_company_id}, ` +
          `not the account ${companyId}. Both are shown as returned.`);
      }
    }

    const selected = candidates.filter((c) => c.selection === "selected").length;
    const got = candidates.filter((c) => c.details_status === "fetched").length;
    const failed = searches.some((x) => x.status === "error" || x.status === "not_requested");
    const retrieved = searches.reduce((a, x) => a + x.retrieved, 0);
    const totals = searches.map((x) => x.total);
    let cover = totals.every((t) => t !== null)
      ? ` Searches retrieved ${retrieved} of ${totals.reduce((a, t) => a + t, 0)} record(s) the API reported.`
      : ` Searches retrieved ${retrieved} record(s); the API did not report a total for every search.`;
    if (noId) cover += ` ${noId} search record(s) had no person ID and were left out.`;
    let status: string;
    let reason: string;
    const partial = candidates.some((c) => c.selection === "incomplete" || c.details_status === "error" || c.details_status === "not_requested");
    if (failed || partial) {
      status = "incomplete";
      reason = "A search, check or profile request has no result because of an error or because the run " +
        `stopped. ${selected} of ${candidates.length} candidate(s) selected; person profiles retrieved for ${got}.`;
    } else if (!candidates.length) {
      status = "no_candidates";
      reason = "People search returned no candidates for the configured searches.";
    } else if (selected) {
      status = "contacts_selected";
      reason = `${selected} of ${candidates.length} candidate(s) selected; person profiles retrieved for ${got}.`;
    } else {
      status = "no_contacts_selected";
      reason = `${candidates.length} candidate(s) found; none selected.`;
    }
    reason += countsText(candidates) + cover;
    return { ...base, status, status_reason: reason, searches, candidates, candidates_found: candidates.length,
      candidates_selected: selected, profiles_fetched: got };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  if (emptySearchSeen) {
    coverage.push("A people search with no matches returns success false without a records list; it is " +
      "counted as zero matches.");
  }
  const counts: Record<string, number> = {};
  const selCounts: Record<string, number> = {};
  for (const k of SELECTIONS) selCounts[k] = 0;
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    for (const c of r.candidates ?? []) selCounts[c.selection] += 1;
  }
  return { rows, summary: { status_counts: counts, selection_counts: selCounts }, coverage };
}

export const CSV_COLUMNS = ["row_id", "domain", "company_id", "company_name", "account_status", "account_status_reason",
  "search_total", "search_retrieved", "person_id", "name", "position", "matched_queries", "selection",
  "selection_reason", "check_results", "details_status", "work_emails", "personal_emails", "phones",
  "current_company_matches_account", "notes"];

export function csvRows(result: Json): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const r of result.rows) {
    const searches: Json[] = r.searches ?? [];
    const totals = searches.map((x) => x.total);
    const account = {
      row_id: r.row_id, domain: r.domain || r.input.domain || null, company_id: r.company_id || r.input.company_id || null,
      company_name: r.company?.name ?? null, account_status: r.status,
      account_status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      search_total: searches.length && totals.every((t) => t !== null) ? totals.reduce((a, t) => a + t, 0) : null,
      search_retrieved: searches.length ? searches.reduce((a, x) => a + x.retrieved, 0) : null,
    };
    const cands: Json[] = r.candidates ?? [];
    if (!cands.length) { out.push(account); continue; }
    for (const c of cands) {
      const contact = c.contact ?? {};
      out.push({ ...account, person_id: c.person_id, name: c.name ?? null, position: c.position ?? null,
        matched_queries: c.matched_queries, selection: c.selection, selection_reason: c.selection_reason,
        check_results: c.checks.map((x: Json) => `${x.check_id}=${x.result}(${x.answer || "-"})`),
        details_status: c.details_status, work_emails: contact.work_emails ?? [], personal_emails: contact.personal_emails ?? [],
        phones: (contact.phones ?? []).map(phoneText), current_company_matches_account: c.current_company_matches_account ?? null,
        notes: c.notes ?? [] });
    }
  }
  return out;
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  const sel: Record<string, number> = result.summary.selection_counts ?? {};
  report.add("Result", "Contact finder summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    "<p>" + Object.entries(sel).filter(([, v]) => v).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, SELECTION_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Unknown means Firmable does not hold the fact. A candidate with an unknown mandatory check " +
      "<strong>needs review</strong> and is never selected automatically.", "unknown"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.domain || r.company_id || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    if (r.company_id) {
      left += kv([["Firmable company ID", r.company_id],
        ["ID from", r.company_id_source === "input" ? "input file" : "company lookup"],
        ["Domain", r.company?.fqdn || r.domain]]);
    }
    if (r.duplicate_of) left += note(`Same account as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    const searches: Json[] = r.searches ?? [];
    if (searches.length) {
      left += "<h3>Search coverage</h3>" + table(["Search", "Filters", "Total reported", "Retrieved", "Pages", "Complete", "Status"],
        searches.map((x) => [x.label, x.filters, x.total ?? "not reported", x.retrieved, x.pages_fetched,
          x.complete ? "yes" : "no", String(x.status).replace(/_/g, " ")]));
    }
    let right = "";
    const cands: Json[] = r.candidates ?? [];
    if (cands.length) {
      right += table(["Name", "Position", "Selection", "Checks", "Profile"], cands.map((c) => [
        c.name, c.position, new Raw(badge(c.selection.replace(/_/g, " "), SELECTION_TONE[c.selection] ?? "neutral")),
        new Raw(c.checks.map((x: Json) => badge(`${x.label}: ${x.answer || x.result}`, RESULT_TONE[x.result] ?? "neutral")).join(" ")),
        String(c.details_status).replace(/_/g, " ")]));
      for (const c of cands) {
        if (c.selection !== "selected" && !(c.notes ?? []).length) continue;
        right += `<h3>${esc(c.name || c.person_id)}</h3><p>${esc(c.selection_reason)}</p>`;
        const contact = c.contact;
        if (contact) {
          right += kv([["Work email", contact.work_emails.join(", ") || "none returned"],
            ["Personal email", contact.personal_emails.join(", ") || "none returned"],
            ["Phone", contact.phones.map(phoneText).join(", ") || "none returned"],
            ["Current company matches account", c.current_company_matches_account]]);
        }
        for (const x of c.checks) {
          const rs = x.reasoning ?? {};
          if (rs.reason) {
            right += `<p class="muted"><strong>${esc(x.label)}:</strong> ${esc(rs.reason)} ` +
              `Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
          }
        }
        if ((c.notes ?? []).length) right += note(bulletList(c.notes), "review");
      }
      right += detailsJson("Candidates as JSON", cands);
    }
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No candidates for this account.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its candidates", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
