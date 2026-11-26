/**
 * Pre-call account pack: assembles factual information about an account before a sales call.
 * Mirrors recipes/pre-call-account-pack/python/src/pre_call_account_pack/recipe.py; see that file
 * and the recipe README for the workflow, the contact selection rule and the row statuses.
 */
import {
  ConfigError, FATAL_KINDS, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe,
  Report, RunContext, Row, badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, link,
  normaliseDomain, note, pricing, pyRepr, readRows, stableStringify, table,
} from "firmable-gtm-client";

export const NAME = "pre-call-account-pack";
export const TITLE = "Pre-call account pack";
export const DESCRIPTION = "Assembles a factual account pack before a sales call: the company profile, selected contacts, " +
  "Tech Intel, Decisions checks and, when requested, AI Adoption. Every fact shows the response it " +
  "came from. Suggested questions are predefined templates.";

const STATUS_TONE: Record<string, string> = {
  complete: "good", partial: "review", company_not_found: "unknown", invalid_input: "error", error: "error",
  not_processed: "unknown",
};
const SECTION_TONE: Record<string, string> = {
  ok: "good", no_data: "unknown", no_match: "unknown", not_found: "unknown", not_available: "review", error: "error",
  not_asked: "neutral", not_requested: "neutral",
};
const RESULT_TONE: Record<string, string> = { pass: "good", fail: "bad", unknown: "unknown", error: "error", not_asked: "neutral" };
const PROBLEM_WORDS: Record<string, string> = {
  error: "error", not_asked: "not asked because the run stopped", not_available: "access denied",
};
const SECTION_LABELS: [string, string][] = [["company_checks", "Company checks"], ["contacts", "Contacts"],
  ["tech_intel", "Tech Intel"], ["ai_adoption", "AI Adoption"]];
const COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
const SENIORITY_IDS = Array.from({ length: 7 }, (_, i) => String(i + 1));
const DEPARTMENT_IDS = Array.from({ length: 17 }, (_, i) => String(i + 1));
const TECH_FIELDS = ["product", "product_slug", "vendor", "vendor_slug", "category", "category_slug", "confidence",
  "recency", "last_observed", "nature"];
const AI_DIMENSION_FIELDS = ["level", "headline", "observability", "counts_toward_overall"];
const ID = /^[a-z0-9_]{1,40}$/;
const TOP_KEYS = new Set(["with_reasoning", "company_checks", "contact_checks", "contacts", "tech_intel", "ai_adoption",
  "suggested_questions"]);
const CHECK_KEYS = new Set(["id", "label", "tool", "inputs", "want"]);
const CONTACT_KEYS = new Set(["selected_country", "searches", "max_contacts", "fetch_profiles"]);
const SEARCH_KEYS = new Set(["id", "label", "position", "seniority", "department", "size"]);
const TECH_KEYS = new Set(["enabled", "with_evidence", "max_pages"]);
const AI_KEYS = new Set(["enabled", "with_evidence"]);
const QUESTION_KEYS = new Set(["enabled", "max_questions"]);

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const has = (o: Json, k: string) => Object.prototype.hasOwnProperty.call(o, k);
const unknownKeys = (o: Json, allowed: Set<string>) => Object.keys(o).filter((k) => !allowed.has(k)).sort();

// ----- configuration -------------------------------------------------------------------------

function obj(raw: Json, key: string, allowed: Set<string>): Json {
  const value = raw[key] ?? {};
  if (!isObj(value)) throw new ConfigError(`${key} must be an object.`);
  const unknown = unknownKeys(value, allowed);
  if (unknown.length) throw new ConfigError(`Unknown ${key} key(s): ${unknown.join(", ")}`);
  return value;
}

function bool(value: unknown, dflt: boolean, what: string): boolean {
  if (value === undefined || value === null) return dflt;
  if (typeof value !== "boolean") throw new ConfigError(`${what} must be true or false.`);
  return value;
}

function int(value: unknown, dflt: number, low: number, high: number, what: string): number {
  if (value === undefined || value === null) return dflt;
  if (typeof value !== "number" || !Number.isInteger(value) || value < low || value > high) {
    throw new ConfigError(`${what} must be a whole number from ${low} to ${high}.`);
  }
  return value;
}

function checksFrom(raw: unknown, where: string, seen: Set<string>): Json[] {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) throw new ConfigError(`${where} must be a list.`);
  const out: Json[] = [];
  raw.forEach((c: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(c)) throw new ConfigError(`${where}[${i}] must be an object.`);
    const unknown = unknownKeys(c, CHECK_KEYS);
    if (unknown.length) throw new ConfigError(`${where}[${i}] has unknown key(s): ${unknown.join(", ")}`);
    const cid = c.id;
    if (typeof cid !== "string" || !ID.test(cid)) throw new ConfigError(`${where}[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(cid)) throw new ConfigError(`${where}[${i}].id ${pyRepr(cid)} is used twice.`);
    seen.add(cid);
    const tool = c.tool;
    if (typeof tool !== "string" || !tool) throw new ConfigError(`${where}[${i}].tool must name a decision tool.`);
    const inputs = has(c, "inputs") ? c.inputs : {};
    if (!isObj(inputs)) throw new ConfigError(`${where}[${i}].inputs must be an object.`);
    const want = has(c, "want") ? c.want : "yes";
    if (want !== "yes" && want !== "no") throw new ConfigError(`${where}[${i}].want must be yes or no.`);
    const label = has(c, "label") ? c.label : cid;
    if (typeof label !== "string" || !label) throw new ConfigError(`${where}[${i}].label must be text.`);
    out.push({ id: cid, label, tool, inputs, want });
  });
  return out;
}

export function loadConfig(raw: Json): Json {
  const unknown = unknownKeys(raw, TOP_KEYS);
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const seen = new Set<string>();
  const companyChecks = checksFrom(raw.company_checks, "company_checks", seen);
  const contactChecks = checksFrom(raw.contact_checks, "contact_checks", seen);
  const withReasoning = bool(raw.with_reasoning, true, "with_reasoning");

  const c = obj(raw, "contacts", CONTACT_KEYS);
  const country = has(c, "selected_country") ? c.selected_country : "AU";
  if (!COUNTRIES.includes(country)) throw new ConfigError(`contacts.selected_country must be one of ${COUNTRIES.join(", ")}.`);
  const searchesRaw = c.searches ?? [];
  if (!Array.isArray(searchesRaw)) throw new ConfigError("contacts.searches must be a list.");
  const searches: Json[] = [];
  const searchIds = new Set<string>();
  searchesRaw.forEach((s: Json, idx: number) => {
    const where = `contacts.searches[${idx + 1}]`;
    if (!isObj(s)) throw new ConfigError(`${where} must be an object.`);
    const bad = unknownKeys(s, SEARCH_KEYS);
    if (bad.length) throw new ConfigError(`${where} has unknown key(s): ${bad.join(", ")}`);
    const sid = s.id;
    if (typeof sid !== "string" || !ID.test(sid)) throw new ConfigError(`${where}.id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (searchIds.has(sid)) throw new ConfigError(`${where}.id ${pyRepr(sid)} is used twice.`);
    searchIds.add(sid);
    const label = has(s, "label") ? s.label : sid;
    if (typeof label !== "string" || !label) throw new ConfigError(`${where}.label must be text.`);
    const position = s.position ?? null;
    if (position !== null && (typeof position !== "string" || !position.trim())) throw new ConfigError(`${where}.position must be non-empty text.`);
    const seniority = s.seniority ?? null;
    if (seniority !== null && !SENIORITY_IDS.includes(seniority as string)) {
      throw new ConfigError(`${where}.seniority must be a seniority id from 1 to 7, written as text.`);
    }
    const department = s.department ?? null;
    if (department !== null && !DEPARTMENT_IDS.includes(department as string)) {
      throw new ConfigError(`${where}.department must be a department id from 1 to 17, written as text.`);
    }
    if (position === null && seniority === null && department === null) {
      throw new ConfigError(`${where} needs at least one of position, seniority or department.`);
    }
    const size = int(s.size, 5, 1, 100, `${where}.size`);
    searches.push({ id: sid, label, position: position ? (position as string).trim() : null, seniority, department, size });
  });
  const maxContacts = int(c.max_contacts, 3, 0, 50, "contacts.max_contacts");
  const fetchProfiles = bool(c.fetch_profiles, true, "contacts.fetch_profiles");
  if (contactChecks.length && !searches.length) throw new ConfigError("contact_checks need at least one entry in contacts.searches.");

  const t = obj(raw, "tech_intel", TECH_KEYS);
  const a = obj(raw, "ai_adoption", AI_KEYS);
  const q = obj(raw, "suggested_questions", QUESTION_KEYS);
  return {
    with_reasoning: withReasoning,
    company_checks: companyChecks,
    contact_checks: contactChecks,
    contacts: { selected_country: country, searches, max_contacts: maxContacts, fetch_profiles: fetchProfiles },
    tech_intel: {
      enabled: bool(t.enabled, true, "tech_intel.enabled"),
      with_evidence: bool(t.with_evidence, false, "tech_intel.with_evidence"),
      max_pages: int(t.max_pages, 1, 1, 20, "tech_intel.max_pages"),
    },
    ai_adoption: {
      enabled: bool(a.enabled, false, "ai_adoption.enabled"),
      with_evidence: bool(a.with_evidence, false, "ai_adoption.with_evidence"),
    },
    suggested_questions: {
      enabled: bool(q.enabled, true, "suggested_questions.enabled"),
      max_questions: int(q.max_questions, 6, 0, 20, "suggested_questions.max_questions"),
    },
  };
}

export function readInput(path: string): Row[] {
  return readRows(path, { required: ["domain"] });
}

// ----- plan ----------------------------------------------------------------------------------

function validUniqueDomains(rows: Row[]): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const d = normaliseDomain(r.domain);
    if (d && !out.includes(d)) out.push(d);
  }
  return out;
}

function maxContactsOf(config: Json): number {
  const c = config.contacts;
  return Math.min(c.max_contacts, c.searches.reduce((s: number, x: Json) => s + x.size, 0));
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const n = validUniqueDomains(rows).length;
  const cc: Json[] = config.company_checks;
  const kc: Json[] = config.contact_checks;
  const searches: Json[] = config.contacts.searches;
  const contacts = maxContactsOf(config);
  const calls: PlannedCall[] = [];
  if (cc.length || kc.length) calls.push(new PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []));
  calls.push(new PlannedCall("GET /company", "Company profile for each unique valid domain", n,
    [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  if (searches.length) {
    calls.push(new PlannedCall("POST /people/search", `${searches.length} people search(es) for each account`, n * searches.length, []));
  }
  if (searches.length && config.contacts.fetch_profiles && contacts) {
    calls.push(new PlannedCall("GET /people", `Up to ${contacts} contact profile(s) for each account`, n * contacts,
      [["person", pricing.round4(pricing.PERSON_PROFILE * n * contacts)]]));
  }
  const answers = n * (cc.length + contacts * kc.length);
  if (answers) {
    calls.push(new PlannedCall("POST /decisions",
      `${cc.length} company check(s) and ${kc.length} check(s) for each of up to ${contacts} contact(s)`,
      answers, pricing.estimateDecision(answers, config.with_reasoning)));
  }
  const t = config.tech_intel;
  if (t.enabled) {
    const parts: pricing.Part[] = [["tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n)]];
    if (t.with_evidence) parts.push(["tech_intel_evidence", pricing.round4(pricing.TECH_INTEL_EVIDENCE_REQUEST * n * t.max_pages)]);
    calls.push(new PlannedCall("GET /tech-intel", `Up to ${t.max_pages} page(s) for each account`, n * t.max_pages, parts));
  }
  const a = config.ai_adoption;
  if (a.enabled) calls.push(new PlannedCall("POST /ai-adoption", "AI Adoption for each account", n, pricing.estimateAiAdoption(n, a.with_evidence)));
  return calls;
}

// ----- helpers -------------------------------------------------------------------------------

function resolve(entries: Json[], e: Json): Json {
  if (e.outcome === "cache_hit") {
    for (const o of entries) {
      if (o.outcome === "ok" && o.method === e.method && o.path === e.path &&
        stableStringify(o.query ?? null) === stableStringify(e.query ?? null) &&
        stableStringify(o.body ?? null) === stableStringify(e.body ?? null)) {
        return { endpoint: e.endpoint, ledger_seq: o.seq, reused: true };
      }
    }
  }
  return { endpoint: e.endpoint, ledger_seq: e.seq, reused: false };
}

function sourceSince(ctx: RunContext, start: number): Json {
  const entries = ctx.client.ledger.entries;
  if (entries.length <= start) return null;
  return resolve(entries, entries[entries.length - 1]);
}

function recordDict(ctx: RunContext, rowId: string, err: Json): Json {
  const entry = { row_id: rowId, ...err };
  ctx.errors.push(entry);
  if (FATAL_KINDS.has(err.kind) && ctx.stopped === null) ctx.stopped = entry;
  return entry;
}

function product(item: Json): string {
  return item.product || item.product_slug || "an unnamed technology";
}

function sectionStatus(statuses: string[]): string {
  if (statuses.includes("error")) return "error";
  if (statuses.includes("not_asked")) return "not_asked";
  return "ok";
}

function questions(cfg: Json, pack: Json): Json[] {
  const out: Json[] = [];
  for (const c of pack.company_checks.checks) {
    if (c.result === "unknown") {
      out.push({ template_id: "confirm_unknown_check", question: `Can you confirm this for us: ${c.label}?`,
        basis: `The company check "${c.label}" answered unknown. Firmable does not hold that fact.` });
    }
  }
  if (pack.contacts.status === "no_match") {
    out.push({ template_id: "who_else", question: "Who else should be part of this conversation?",
      basis: "No contacts were selected from the configured people searches in retrieved data." });
  }
  const categories: string[] = [];
  for (const item of pack.tech_intel.items ?? []) {
    const cat = item.category;
    if (item.confidence === "high" && typeof cat === "string" && cat && !categories.includes(cat)) {
      categories.push(cat);
      out.push({ template_id: "technology_category",
        question: `How does your team handle ${cat} today, and what is working well?`,
        basis: `Tech Intel observed ${product(item)} in the ${cat} category with high confidence, last observed ` +
          `${item.last_observed || "not given"}. An observation does not show active use, spend or buying intent.` });
      if (categories.length === 2) break;
    }
  }
  const ai = pack.ai_adoption;
  if (ai.status === "ok" && ai.overall_level) {
    out.push({ template_id: "ai_adoption",
      question: "Where is AI already part of your team's work, and where would you like it to be?",
      basis: `AI Adoption overall level ${ai.overall_level} as of ${ai.as_of || "not given"}. A level does not show buying intent.` });
  }
  out.push({ template_id: "why_now", question: "What prompted you to look at this now?",
    basis: "Standard opening question. It is not based on API data." });
  return out.slice(0, cfg.suggested_questions.max_questions);
}

// ----- run -----------------------------------------------------------------------------------

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = [
    "Each fact carries source.endpoint and source.ledger_seq, which point to its request and response in " +
      "the ledger and the raw responses section.",
    "Company and person profiles carry no date in the response. Dates shown come from the API fields that " +
      "hold them: Decisions answered_at and last_refreshed, Tech Intel last_observed and AI Adoption as_of.",
    "Suggested questions are predefined templates filled from API fields. They are not API facts.",
    "A detected technology or an AI Adoption level shows what Firmable observed. It does not show buying " +
      "intent, spend or active use.",
    "A technology that is not listed was not observed in retrieved data. That does not mean the company " +
      "does not use it.",
    "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no " +
      "or as a pass.",
  ];
  if (cfg.contacts.searches.length) {
    coverage.push("People search filters by company, position, seniority and department only. It has no " +
      "name or date filters. Contacts are the first people found, in configuration order.");
    coverage.push("Personal email addresses are not copied into the pack. Phone numbers are shown with " +
      "the do-not-call flag as returned.");
  }

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length } }, coverage };
  };

  const allChecks: Json[] = [...cfg.company_checks, ...cfg.contact_checks];
  let tools = new Map<string, Json>();
  if (allChecks.length) {
    let catalogue: Json;
    try {
      catalogue = await client.listDecisionTools();
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      ctx.recordError(null, e);
      return stopAll("The decision tool list could not be read, so no account was processed.");
    }
    const problems = [...decisions.checkTools(catalogue, cfg.company_checks, "company"),
      ...decisions.checkTools(catalogue, cfg.contact_checks, "person")];
    if (problems.length) {
      for (const p of problems) {
        ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
          message: p, charge_uncertain: false, retry_after: null });
      }
      coverage.push(...problems);
      return stopAll("The configuration names decision tools or inputs the API does not accept.");
    }
    tools = decisions.toolIndex(catalogue);
    const modelTools = [...new Set<string>(allChecks.filter((c) => decisions.usesModel(catalogue, c.tool)).map((c) => c.tool))].sort();
    if (modelTools.length) {
      coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
        "This code applies only the rules in the configuration.");
    }
    const withCompany = [...new Set<string>(cfg.contact_checks
      .filter((c: Json) => has(tools.get(c.tool)?.input_schema?.properties ?? {}, "company_id"))
      .map((c: Json) => c.tool))].sort();
    if (withCompany.length) {
      coverage.push(`Contact checks with these tools were asked with the account's company_id: ${withCompany.join(", ")}.`);
    }
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _domain: normaliseDomain(r.domain) ?? "" })) as Row[];
  const dupes = findDuplicates(keyed, "_domain");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) {
    coverage.push(`${nDupes} row(s) repeat an earlier domain; their pack was copied from the earlier ` +
      "row and no request was repeated.");
  }
  let emptySearch = false;
  const packs = new Map<string, Json>();

  const ask = async (rowId: string, c: Json, subject: Json): Promise<Json> => {
    const entry = { check_id: c.id, label: c.label, tool: c.tool, want: c.want };
    if (ctx.stopped !== null) return { ...entry, result: "not_asked", answer: null, error: null, source: null };
    const start = client.ledger.entries.length;
    let resp: Json;
    try {
      resp = await client.makeDecision(c.tool, { ...subject, ...c.inputs }, { withReasoning: cfg.with_reasoning });
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      const err = ctx.recordError(rowId, e);
      return { ...entry, result: "error", answer: null, error: err, source: sourceSince(ctx, start) };
    }
    const ans = decisions.answerFrom(resp);
    const result = decisions.compare(ans.answer, c.want);
    return { ...entry, result, ...ans, error: result === "error" ? ans.answer_error : null, source: sourceSince(ctx, start) };
  };

  const attempt = async (rowId: string, fn: () => Promise<Json>): Promise<[Json, string, Json, Json]> => {
    if (ctx.stopped !== null) return [null, "not_asked", null, null];
    const start = client.ledger.entries.length;
    try {
      const resp = await fn();
      return [resp, "ok", null, sourceSince(ctx, start)];
    } catch (e) {
      if (e instanceof NotFoundError) return [null, "not_found", null, sourceSince(ctx, start)];
      if (!(e instanceof FirmableError)) throw e;
      return [null, "error", ctx.recordError(rowId, e), sourceSince(ctx, start)];
    }
  };

  const contactsSection = async (rowId: string, companyId: string, notes: string[]): Promise<Json> => {
    const cc = cfg.contacts;
    if (!cc.searches.length) return { status: "not_requested", searches: [], selected: [], not_selected: 0 };
    const searches: Json[] = [];
    const found = new Map<string, Json>();
    for (const s of cc.searches) {
      const body: Json = { companyId, selectedCountry: cc.selected_country, from: "0", size: String(s.size) };
      for (const k of ["position", "seniority", "department"]) if (s[k] !== null) body[k] = s[k];
      const entry = { search_id: s.id, label: s.label, request: body, total: null, returned: 0, error: null, source: null };
      if (ctx.stopped !== null) { searches.push({ ...entry, status: "not_asked" }); continue; }
      const start = client.ledger.entries.length;
      let resp: Json;
      try {
        resp = await client.searchPeople(body);
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        searches.push({ ...entry, status: "error", error: ctx.recordError(rowId, e), source: sourceSince(ctx, start) });
        continue;
      }
      const src = sourceSince(ctx, start);
      if (resp.success === false) emptySearch = true;
      const records: Json[] = (resp.records ?? []).filter((r: Json) => isObj(r));
      searches.push({ ...entry, status: records.length ? "ok" : "no_match", total: resp.total ?? null,
        returned: records.length, source: src });
      for (const rec of records) {
        const pid = rec.person_id || rec.id;
        if (typeof pid !== "string" || !pid) continue;
        if (found.has(pid)) { found.get(pid).matched_searches.push(s.id); continue; }
        const flags: Json = {};
        for (const k of ["has_email", "has_phone", "has_mobile", "has_dnd_phone"]) flags[k] = rec[k] ?? null;
        found.set(pid, { person_id: pid, name: rec.name ?? null, position: rec.position ?? null,
          company_name: rec.company_name ?? null, matched_searches: [s.id], search_flags: flags, search_source: src });
      }
    }
    const people = [...found.values()];
    const selected = people.slice(0, cc.max_contacts);
    const statuses: string[] = searches.map((s) => s.status);
    for (const p of selected) {
      p.profile = null;
      p.profile_error = null;
      p.profile_source = null;
      if (!cc.fetch_profiles) {
        p.profile_status = "not_requested";
      } else {
        const [resp, status, err, src] = await attempt(rowId, () => client.getPerson({ id: p.person_id }));
        p.profile_status = status;
        p.profile_error = err;
        p.profile_source = src;
        statuses.push(status);
        if (resp !== null) {
          const contact = facts.contactDetails(resp);
          p.profile = { ...(facts.personFacts(resp) ?? {}), work_emails: contact.work_emails, phones: contact.phones };
          const cur = p.profile.current_company_id;
          if (cur && cur !== companyId) {
            notes.push(`Contact ${p.name || p.person_id}: the person profile's current company ID (${cur}) ` +
              `differs from this account (${companyId}).`);
          }
        }
      }
      p.checks = [];
      for (const c of cfg.contact_checks) {
        const subject: Json = { person_id: p.person_id };
        if (has(tools.get(c.tool)?.input_schema?.properties ?? {}, "company_id")) subject.company_id = companyId;
        p.checks.push(await ask(rowId, c, subject));
      }
      statuses.push(...p.checks.map((c: Json) => c.result));
    }
    let status = sectionStatus(statuses);
    if (status === "ok" && !people.length) status = "no_match";
    return { status, searches, selected, not_selected: people.length - selected.length };
  };

  const techSection = async (rowId: string, companyId: string, notes: string[]): Promise<Json> => {
    const t = cfg.tech_intel;
    const base = { items: [], total: null, pages_fetched: 0, complete: false, stop_reason: null, error: null };
    if (!t.enabled) return { ...base, status: "not_requested" };
    if (ctx.stopped !== null) return { ...base, status: "not_asked" };
    const start = client.ledger.entries.length;
    const res = await client.getTechIntel({ id: companyId }, { withEvidence: t.with_evidence, maxPages: t.max_pages });
    const err = res.error ? recordDict(ctx, rowId, res.error) : null;
    const entries = client.ledger.entries;
    const items: Json[] = [];
    for (const e of entries.slice(start)) {
      if (e.endpoint !== "GET /tech-intel" || (e.outcome !== "ok" && e.outcome !== "cache_hit")) continue;
      const src = resolve(entries, e);
      const page: Json = (entries[src.ledger_seq - 1] as Json).response || {};
      for (const it of page.tech_intelligence ?? []) {
        if (!isObj(it)) continue;
        const item: Json = {};
        for (const k of TECH_FIELDS) item[k] = it[k] ?? null;
        if (t.with_evidence) {
          const ev: Json = isObj(it.evidence_detail) ? it.evidence_detail : {};
          const records: Json[] = (ev.records ?? []).filter((r: Json) => isObj(r));
          const missing = records.filter((r) => !r.location).length;
          Object.assign(item, { evidence_count: ev.evidence_count ?? null, first_seen: ev.first_seen ?? null,
            last_seen: ev.last_seen ?? null, evidence_records_returned: records.length,
            evidence_records_without_location: missing });
          if (missing) notes.push(`Tech Intel evidence for ${product(item)}: ${missing} record(s) have no source location.`);
        }
        item.source = src;
        items.push(item);
      }
    }
    if (res.id && res.id !== companyId) notes.push(`The Tech Intel response is for company ID ${res.id}, not ${companyId}.`);
    const out = { items, total: res.total ?? null, pages_fetched: res.pages_fetched, complete: res.complete,
      stop_reason: res.stop_reason, error: err };
    if (err) {
      if (err.kind === "forbidden" && !items.length) return { ...out, status: "not_available" };
      return { ...out, status: "error" };
    }
    if (!items.length && !res.total) return { ...out, status: "no_data" };
    return { ...out, status: "ok" };
  };

  const aiSection = async (rowId: string, companyId: string, notes: string[]): Promise<Json> => {
    const a = cfg.ai_adoption;
    const base = { overall_level: null, explanation: null, momentum: null, dimensions: {}, evidence_available: null,
      as_of: null, framework_version: null, firmable_id: null, error: null, source: null };
    if (!a.enabled) return { ...base, status: "not_requested" };
    const [resp, status, err, src] = await attempt(rowId,
      () => client.getAiAdoption({ company_id: companyId }, { withEvidence: a.with_evidence }));
    if (resp === null) return { ...base, status, error: err, source: src };
    const comp = resp.company;
    if (!isObj(comp)) {
      const rerr = isObj(resp.error) ? resp.error : null;
      const code = rerr?.code ?? null;
      return { ...base, status: code === "not_found" ? "not_found" : "error", error: rerr, source: src };
    }
    const c: Json = comp;
    const dimsRaw: Json = isObj(c.dimensions) ? c.dimensions : {};
    const dims: Json = {};
    for (const [k, v] of Object.entries(dimsRaw)) {
      if (!isObj(v)) continue;
      const d: Json = {};
      for (const f of AI_DIMENSION_FIELDS) d[f] = (v as Json)[f] ?? null;
      dims[k] = d;
    }
    const out: Json = { ...base, status: "ok", overall_level: c.overall_level ?? null, explanation: c.explanation ?? null,
      momentum: c.momentum ?? null, dimensions: dims, evidence_available: c.evidence_available ?? null,
      as_of: c.as_of ?? null, framework_version: c.framework_version ?? null, firmable_id: c.firmable_id ?? null,
      source: src };
    if (c.firmable_id && c.firmable_id !== companyId) notes.push(`The AI Adoption answer is for company ID ${c.firmable_id}, not ${companyId}.`);
    if (a.with_evidence) {
      const summary: Json = {};
      const evidence: Json = isObj(resp.evidence) ? resp.evidence : {};
      for (const [area, evRaw] of Object.entries(evidence) as [string, Json][]) {
        if (!isObj(evRaw)) continue;
        const ev: Json = evRaw;
        const items: Json[] = (ev.items ?? []).filter((i: Json) => isObj(i));
        summary[area] = {
          item_count: ev.item_count ?? null, items_returned: items.length,
          qualifying: items.filter((i) => i.qualifies === true).length,
          not_qualifying: items.filter((i) => i.qualifies === false).length,
          qualifies_missing: items.filter((i) => typeof i.qualifies !== "boolean").length,
          truncated: ev.truncated ?? null, as_of: ev.as_of ?? null,
        };
        if (ev.truncated === true) notes.push(`AI Adoption ${area} evidence is truncated; ${items.length} item(s) returned.`);
      }
      out.evidence_summary = summary;
    }
    return out;
  };

  const one = async (row: Row): Promise<Json> => {
    const rowId = row.row_id;
    const domain = normaliseDomain(row.domain);
    const base: Json = { row_id: rowId, input: row, domain, duplicate_of: dupes[rowId] ?? null, company: null };
    if (!domain) return { ...base, status: "invalid_input", status_reason: `${pyRepr(row.domain)} is not a valid domain.` };
    if (dupes[rowId] && packs.has(domain)) {
      return { ...structuredClone(packs.get(domain)), row_id: rowId, input: row, duplicate_of: dupes[rowId] };
    }
    const start = client.ledger.entries.length;
    let company: Json;
    try {
      company = await client.getCompany({ fqdn: domain });
    } catch (e) {
      if (e instanceof NotFoundError) {
        const out = { ...base, status: "company_not_found", company_source: sourceSince(ctx, start),
          status_reason: "Firmable has no company profile for this domain, so no pack was built." };
        packs.set(domain, out);
        return out;
      }
      if (!(e instanceof FirmableError)) throw e;
      const err = ctx.recordError(rowId, e);
      const out = { ...base, status: "error", error: err, company_source: sourceSince(ctx, start),
        status_reason: `The company profile could not be retrieved: ${err.message}` };
      packs.set(domain, out);
      return out;
    }
    const cid: string = company.id;
    const notes: string[] = [];
    const pack: Json = { ...base, company: { ...(facts.companyFacts(company) ?? {}), source: sourceSince(ctx, start) } };
    const checks: Json[] = [];
    for (const c of cfg.company_checks) checks.push(await ask(rowId, c, { company_id: cid }));
    for (const c of checks) {
      const ent = c.entity_id;
      if (ent && ent !== cid) notes.push(`The answer to "${c.label}" is about entity ${ent}, not ${cid}.`);
    }
    pack.company_checks = { status: checks.length ? sectionStatus(checks.map((c) => c.result)) : "not_requested", checks };
    pack.contacts = await contactsSection(rowId, cid, notes);
    pack.tech_intel = await techSection(rowId, cid, notes);
    pack.ai_adoption = await aiSection(rowId, cid, notes);
    pack.data_quality_notes = notes;
    const problems = SECTION_LABELS.filter(([key]) => has(PROBLEM_WORDS, pack[key].status))
      .map(([key, label]) => `${label} (${PROBLEM_WORDS[pack[key].status]})`);
    if (problems.length) {
      pack.status = "partial";
      pack.status_reason = `Some requested sections are incomplete: ${problems.join(", ")}.`;
    } else {
      pack.status = "complete";
      pack.status_reason = "Every requested section was retrieved.";
    }
    const t = pack.tech_intel;
    if (t.status === "ok" && !t.complete) {
      const total = Number.isInteger(t.total) ? String(t.total) : "unknown";
      pack.status_reason += ` Tech Intel is incomplete: ${t.items.length} of ${total} technologies retrieved within the page limit.`;
    }
    pack.suggested_questions = cfg.suggested_questions.enabled ? questions(cfg, pack) : [];
    packs.set(domain, pack);
    return pack;
  };

  const rows = await ctx.runRows(ctx.rows, one);
  if (emptySearch) {
    coverage.push("A people search with no matches returns success false without a records list; it is " +
      "counted as zero matches.");
  }
  const counts: Record<string, number> = {};
  const sections: Record<string, Record<string, number>> = {};
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    for (const [key] of SECTION_LABELS) {
      const sec: Json = r[key];
      if (isObj(sec)) {
        const s = (sections[key] ??= {});
        const st: string = (sec as Json).status;
        s[st] = (s[st] ?? 0) + 1;
      }
    }
  }
  return { rows, summary: { status_counts: counts, section_counts: sections }, coverage };
}

// ----- outputs -------------------------------------------------------------------------------

export const CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
  "hq_country", "company_size", "company_checks", "contacts_selected", "contacts", "contact_checks",
  "tech_intel_status", "tech_total", "tech_retrieved", "tech_complete", "technologies",
  "ai_adoption_status", "ai_overall_level", "ai_as_of", "data_quality_notes", "suggested_questions"];

const checkText = (c: Json) => `${c.check_id}=${c.result}(${c.answer || "-"})`;

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const company = r.company ?? {};
    const contacts: Json[] = r.contacts?.selected ?? [];
    const tech = r.tech_intel;
    const ai = r.ai_adoption ?? {};
    return {
      row_id: r.row_id, domain: r.domain || r.input.domain, duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: company.id ?? null, company_name: company.name ?? null,
      hq_country: company.hq_country ?? null, company_size: company.company_size ?? null,
      company_checks: (r.company_checks?.checks ?? []).map(checkText),
      contacts_selected: r.contacts ? contacts.length : null,
      contacts: contacts.map((p) => `${p.name || p.person_id} (${p.position || "-"})`),
      contact_checks: contacts.flatMap((p) => (p.checks ?? []).map((c: Json) => `${p.person_id}:${checkText(c)}`)),
      tech_intel_status: tech?.status ?? null, tech_total: tech?.total ?? null,
      tech_retrieved: tech ? (tech.items ?? []).length : null,
      tech_complete: tech ? tech.complete ?? null : null,
      technologies: (tech?.items ?? []).slice(0, 10).map((i: Json) => i.product ?? null),
      ai_adoption_status: ai.status ?? null, ai_overall_level: ai.overall_level ?? null, ai_as_of: ai.as_of ?? null,
      data_quality_notes: r.data_quality_notes ?? [],
      suggested_questions: (r.suggested_questions ?? []).map((q: Json) => q.question),
    };
  });
}

function src(s: Json): string {
  if (!isObj(s)) return "no response";
  return `#${s.ledger_seq} ${s.endpoint}` + (s.reused ? " (reused)" : "");
}

const sectionBadge = (status: string) => badge(status.replace(/_/g, " "), SECTION_TONE[status] ?? "neutral");

function left(r: Json): string {
  let html = `<p>${esc(r.status_reason || r.reason || r.error?.message || "")}</p>`;
  const company = r.company;
  if (company) {
    html += "<h3>Company profile</h3>" + kv([
      ["Firmable ID", company.id], ["Name", company.name], ["Website", new Raw(link(company.website))],
      ["Head office", company.hq_location], ["Head office country", company.hq_country], ["Size", company.company_size],
      ["Employee count", company.employee_count], ["Company type", company.company_type],
      ["Year founded", company.year_founded], ["Industries", (company.industries ?? []).join(", ") || null],
      ["Revenue", company.revenue]]);
    html += `<p class="muted">Source: ${esc(src(company.source))}. The profile carries no date.</p>`;
  }
  if (r.duplicate_of) html += note(`Same domain as row ${esc(r.duplicate_of)}; the pack was copied.`, "info");
  if ((r.data_quality_notes ?? []).length) {
    html += "<h3>Data quality notes</h3>" + bulletList(r.data_quality_notes) +
      '<p class="muted">Shown as returned. This recipe does not correct upstream data.</p>';
  }
  const cc = r.company_checks;
  if (cc && cc.checks.length) {
    html += "<h3>Company checks " + sectionBadge(cc.status) + "</h3>" + table(
      ["Check", "Wanted", "Answer", "Result", "Confidence", "Answered at", "Source"],
      cc.checks.map((c: Json) => [c.label, c.want, c.answer || "none",
        new Raw(badge(c.result.replace(/_/g, " "), RESULT_TONE[c.result] ?? "neutral")),
        c.confidence ?? null, c.answered_at ?? null, src(c.source)]));
    for (const c of cc.checks) {
      const rs = c.reasoning ?? {};
      if (rs.reason) {
        html += `<p><strong>${esc(c.label)}:</strong> ${esc(rs.reason)}</p>` +
          `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
      }
      if (isObj(c.error)) html += note(`${esc(c.label)}: ${esc(c.error.message)}`, "error");
    }
  }
  return html;
}

function right(r: Json): string {
  let html = "";
  const contacts = r.contacts;
  if (contacts && contacts.status !== "not_requested") {
    html += "<h3>Contacts " + sectionBadge(contacts.status) + "</h3>";
    html += table(["Search", "Status", "Total", "Returned", "Source"],
      contacts.searches.map((s: Json) => [s.label, s.status.replace(/_/g, " "), s.total, s.returned, src(s.source)]));
    const rows = contacts.selected.map((p: Json) => {
      const prof = p.profile ?? {};
      const phones = (prof.phones ?? []).map((ph: Json) => `${ph.value}${ph.is_dnd ? " (do not call)" : ""}`).join(", ");
      const checks = (p.checks ?? []).map((c: Json) => `${c.label}: ${c.answer || c.result}`).join("; ");
      return [p.name, prof.position || p.position, prof.seniority ?? null, (prof.work_emails ?? []).join(", ") || null,
        phones || null, p.profile_status.replace(/_/g, " "), checks || null, `${src(p.search_source)}; ${src(p.profile_source)}`];
    });
    if (rows.length) html += table(["Name", "Position", "Seniority", "Work email", "Phone", "Profile", "Checks", "Sources"], rows);
    if (contacts.not_selected) html += `<p class="muted">${esc(contacts.not_selected)} more person(s) matched but were not selected.</p>`;
  }
  const tech = r.tech_intel;
  if (tech && tech.status !== "not_requested") {
    html += "<h3>Tech Intel " + sectionBadge(tech.status) + "</h3>";
    html += `<p class="muted">${esc(tech.items.length)} of ${esc(tech.total)} technologies retrieved in ` +
      `${esc(tech.pages_fetched)} page(s); complete: ${esc(tech.complete)}. Technologies not listed were not observed in retrieved data.</p>`;
    if (tech.items.length) {
      html += table(["Product", "Vendor", "Category", "Confidence", "Recency", "Last observed", "Source"],
        tech.items.map((i: Json) => [i.product, i.vendor, i.category, i.confidence, i.recency, i.last_observed, src(i.source)]));
    }
    if (isObj(tech.error)) html += note(esc(tech.error.message), "error");
  }
  const ai = r.ai_adoption;
  if (ai && ai.status !== "not_requested") {
    html += "<h3>AI Adoption " + sectionBadge(ai.status) + "</h3>";
    if (ai.status === "ok") {
      html += kv([["Overall level", ai.overall_level], ["Explanation", ai.explanation],
        ["Momentum", isObj(ai.momentum) ? ai.momentum.label : null], ["As of", ai.as_of],
        ["Framework version", ai.framework_version]]);
      html += table(["Area", "Level", "Observability", "Counts toward overall", "Headline"],
        Object.entries(ai.dimensions as Record<string, Json>).map(([k, v]) => [k, v.level, v.observability, v.counts_toward_overall, v.headline]));
      if (ai.evidence_summary && Object.keys(ai.evidence_summary).length) {
        html += table(["Evidence area", "Held", "Returned", "Qualifying", "Not qualifying", "Truncated", "As of"],
          Object.entries(ai.evidence_summary as Record<string, Json>).map(([k, v]) =>
            [k, v.item_count, v.items_returned, v.qualifying, v.not_qualifying, v.truncated, v.as_of]));
      }
      html += '<p class="muted">An AI Adoption level does not show buying intent.</p>';
    } else if (ai.status === "not_found") {
      html += "<p>Firmable has no AI Adoption data for this company. This is not an error.</p>";
    }
    if (isObj(ai.error) && ai.status !== "not_found") html += note(esc(ai.error.message), "error");
    html += `<p class="muted">Source: ${esc(src(ai.source))}.</p>`;
  }
  const qs: Json[] = r.suggested_questions ?? [];
  if (qs.length) {
    html += '<div class="template"><h3>Suggested questions</h3><p class="muted">Predefined templates filled ' +
      "from API fields. These are prompts for the seller, not API facts.</p>" +
      bulletList(qs.map((q) => new Raw(`${esc(q.question)}<br><span class="muted">${esc(q.basis)}</span>`))) + "</div>";
  }
  return html;
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Account pack summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Facts come from the API response named in each source line. Suggested questions are " +
      "predefined templates and are shown separately.", "info"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.domain || r.input.domain || r.row_id;
    const rightHtml = r.company ? right(r) : "";
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left(r),
      rightHtml || '<p class="muted">No pack was built for this row.</p>', `Row ${r.row_id}`));
    if (r.company) {
      const { input: _input, ...rest } = r;
      cards.push(detailsJson(`Pack for row ${r.row_id} as JSON`, rest));
    }
  }
  report.add("Accounts", "Each account pack", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
