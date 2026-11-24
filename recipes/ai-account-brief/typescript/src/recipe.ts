/**
 * AI account brief: turns AI Adoption and Tech Intel into a factual, readable account brief.
 * Mirrors recipes/ai-account-brief/python/src/ai_account_brief/recipe.py; see that file and the
 * recipe README for the evidence rules and the template rules.
 */
import {
  ConfigError, FATAL_KINDS, FirmableError, Json, NotFoundError, PlannedCall, Recipe, Report, RunContext, Row,
  badge, bulletList, card, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note, pricing, pyRepr,
  readRows, table,
} from "firmable-gtm-client";

export const NAME = "ai-account-brief";
export const TITLE = "AI account brief";
export const DESCRIPTION = "Builds a factual account brief from each company's Firmable profile, AI Adoption and Tech " +
  "Intel, with evidence, missing data and template-based discovery questions kept separate " +
  "from API facts.";

const AREAS = ["architecture", "footprint", "hiring", "initiatives", "workforce"];
const KNOWN_LEVELS = ["NONE", "BASIC", "MEANINGFUL", "MODERATE", "STRONG", "ADVANCED"];
const BASE_AREA_KEYS = new Set(["level", "headline", "observability", "counts_toward_overall"]);
const ORG_ID_FIELDS = ["company_id", "firmable_id", "organisation_id"];
const ORG_NAME_FIELDS = ["company_name", "employer", "organisation", "company"];
const PLACEHOLDER_SOURCES: Record<string, string> = {
  company_name: "company profile",
  industry: "company profile",
  hq_country: "company profile",
  overall_level: "AI Adoption",
  momentum_label: "AI Adoption",
  architecture_level: "AI Adoption",
  footprint_level: "AI Adoption",
  hiring_level: "AI Adoption",
  initiatives_level: "AI Adoption",
  workforce_level: "AI Adoption",
  ai_tool: "AI Adoption evidence",
  ai_role: "AI Adoption evidence",
  technology: "Tech Intel",
};
const STATUS_TONE: Record<string, string> = {
  brief_ready: "good", no_ai_adoption_data: "unknown", incomplete: "error", company_not_found: "unknown",
  invalid_input: "error", error: "error", not_processed: "unknown",
};
const ID = /^[a-z0-9_]{1,40}$/;
const NAME_RE = /^[a-z][a-z0-9_]*$/;
const TOKEN = /\{([^{}]*)\}/g;
const TOP_KEYS = new Set(["ai_evidence", "tech_intel", "tech_intel_with_evidence", "tech_intel_max_pages", "seller_context",
  "discovery_questions"]);
const MAX_PAGES_LIMIT = 100;

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x);
const has = (o: Record<string, unknown>, k: string) => Object.prototype.hasOwnProperty.call(o, k);

function placeholders(template: string): string[] {
  const out: string[] = [];
  for (const m of template.matchAll(TOKEN)) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  for (const [key, def] of [["ai_evidence", true], ["tech_intel", true], ["tech_intel_with_evidence", false]] as [string, boolean][]) {
    if (typeof (raw[key] ?? def) !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  const pages = raw.tech_intel_max_pages ?? 1;
  if (typeof pages !== "number" || !Number.isInteger(pages) || pages < 1 || pages > MAX_PAGES_LIMIT) {
    throw new ConfigError(`tech_intel_max_pages must be a whole number from 1 to ${MAX_PAGES_LIMIT}.`);
  }
  const sellerRaw = raw.seller_context ?? {};
  if (!isObj(sellerRaw)) throw new ConfigError("seller_context must be an object of text values.");
  const seller: Record<string, string> = {};
  for (const [k, v] of Object.entries(sellerRaw)) {
    if (!NAME_RE.test(k) || k.length > 40) {
      throw new ConfigError(`seller_context key ${pyRepr(k)} must be lower-case letters, digits or underscores, ` +
        "starting with a letter.");
    }
    if (has(PLACEHOLDER_SOURCES, k)) throw new ConfigError(`seller_context key ${pyRepr(k)} is reserved for an API fact.`);
    if (typeof v !== "string" || !v.trim()) throw new ConfigError(`seller_context.${k} must be non-empty text.`);
    seller[k] = v.trim();
  }
  const questionsRaw = raw.discovery_questions ?? [];
  if (!Array.isArray(questionsRaw)) throw new ConfigError("discovery_questions must be a list.");
  const questions: Json[] = [];
  const seen = new Set<string>();
  questionsRaw.forEach((q: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(q)) throw new ConfigError(`discovery_questions[${i}] must be an object.`);
    const extra = Object.keys(q).filter((k) => k !== "id" && k !== "template").sort();
    if (extra.length) throw new ConfigError(`discovery_questions[${i}] has unknown key(s): ${extra.join(", ")}`);
    const qid = q.id;
    if (typeof qid !== "string" || !ID.test(qid)) throw new ConfigError(`discovery_questions[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(qid)) throw new ConfigError(`discovery_questions[${i}].id ${pyRepr(qid)} is used twice.`);
    seen.add(qid);
    const template = q.template;
    if (typeof template !== "string" || !template.trim()) throw new ConfigError(`discovery_questions[${i}].template must be non-empty text.`);
    const names = placeholders(template);
    for (const name of names) {
      if (!NAME_RE.test(name)) throw new ConfigError(`discovery_questions[${i}].template has an invalid placeholder {${name}}.`);
      if (!has(PLACEHOLDER_SOURCES, name) && !has(seller, name)) {
        throw new ConfigError(`discovery_questions[${i}].template uses {${name}}, which is not an API fact ` +
          "placeholder or a seller_context key.");
      }
    }
    const rest = template.replace(TOKEN, "");
    if (rest.includes("{") || rest.includes("}")) throw new ConfigError(`discovery_questions[${i}].template has an unmatched brace.`);
    questions.push({ id: qid, template: template.trim(), placeholders: names });
  });
  return { ai_evidence: raw.ai_evidence ?? true, tech_intel: raw.tech_intel ?? true,
    tech_intel_with_evidence: raw.tech_intel_with_evidence ?? false, tech_intel_max_pages: pages,
    seller_context: seller, discovery_questions: questions };
}

export function readInput(path: string): Row[] {
  return readRows(path, { required: ["domain"] });
}

function validUniqueDomains(rows: Row[]): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const d = normaliseDomain(r.domain);
    if (d && !out.includes(d)) out.push(d);
  }
  return out;
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const n = validUniqueDomains(rows).length;
  const calls = [
    new PlannedCall("GET /company", "Company profile for each unique valid domain", n,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]),
    new PlannedCall("POST /ai-adoption", "AI Adoption" + (config.ai_evidence ? " with evidence" : "") +
      " for each unique valid domain", n, pricing.estimateAiAdoption(n, config.ai_evidence)),
  ];
  if (config.tech_intel) {
    const pages: number = config.tech_intel_max_pages;
    const parts: [string, number][] = [["tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n)]];
    if (config.tech_intel_with_evidence) parts.push(["tech_intel_evidence", pricing.round4(pricing.TECH_INTEL_EVIDENCE_REQUEST * n * pages)]);
    calls.push(new PlannedCall("GET /tech-intel", `Up to ${pages} page(s) of Tech Intel for each unique valid domain`, n * pages, parts));
  }
  return calls;
}

function recordError(ctx: RunContext, rowId: string | null, err: Json): Json {
  const entry = { row_id: rowId, ...err };
  ctx.errors.push(entry);
  if (FATAL_KINDS.has(err.kind) && ctx.stopped === null) ctx.stopped = entry;
  return entry;
}

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);
const stripDots = (v: unknown): string => String(v ?? "").replace(/\.+$/, "");

function otherOrg(item: Json, companyId: unknown, names: string[]): Json[] {
  const found: Json[] = [];
  for (const field of ORG_ID_FIELDS) {
    const v = text(item[field]);
    if (v && v !== companyId) found.push({ field, value: v });
  }
  for (const field of ORG_NAME_FIELDS) {
    const v = text(item[field]);
    if (v && !names.includes(v.toLowerCase())) found.push({ field, value: v });
  }
  return found;
}

function area(name: string, dimRaw: unknown, available: unknown, evAll: unknown, aiEvidence: boolean, companyId: unknown,
  names: string[]): Json {
  const dim = isObj(dimRaw) ? dimRaw : null;
  const level = dim ? dim.level ?? null : null;
  const detail: Json = {};
  if (dim) for (const [k, v] of Object.entries(dim)) if (!BASE_AREA_KEYS.has(k)) detail[k] = v;
  const out: Json = {
    area: name, level,
    level_recognised: typeof level === "string" ? KNOWN_LEVELS.includes(level) : null,
    headline: dim ? dim.headline ?? null : null,
    observability: dim ? dim.observability ?? null : null,
    counts_toward_overall: dim ? dim.counts_toward_overall ?? null : null,
    evidence_available: available ?? null,
    detail, flags: [], notes: [],
  };
  if (dim === null) out.flags.push("The API returned no data for this area.");
  else if (typeof level === "string" && !KNOWN_LEVELS.includes(level)) {
    out.notes.push(`The level ${level} is not one of the documented levels. It is shown as returned.`);
  }
  if (!aiEvidence) { out.evidence = { status: "not_requested" }; return out; }
  const ev = isObj(evAll) ? evAll[name] : null;
  if (!isObj(ev)) {
    out.evidence = { status: "not_returned" };
    if (typeof available === "number" && Number.isInteger(available) && available > 0) {
      out.flags.push(`Firmable holds ${available} evidence item(s) for this area, but none were returned.`);
    } else {
      out.notes.push("No evidence was returned for this area.");
    }
    return out;
  }
  const items: Json[] = (ev.items ?? []).filter((i: unknown) => isObj(i));
  const qualifying = items.filter((i) => i.qualifies === true);
  const notQualifying = items.filter((i) => i.qualifies === false);
  const unstated = items.filter((i) => typeof i.qualifies !== "boolean");
  const count = ev.item_count ?? null;
  const truncated = ev.truncated === true;
  if (truncated) {
    const countText = typeof count === "number" && Number.isInteger(count) ? String(count) : "an unknown number of";
    out.flags.push(`Evidence is truncated: ${items.length} of ${countText} item(s) were returned.`);
  }
  if (unstated.length) out.notes.push(`${unstated.length} evidence item(s) do not say whether they qualify.`);
  const other: Json[] = [];
  if (name === "workforce") {
    for (const i of items) {
      const hits = otherOrg(i, companyId, names);
      if (hits.length) other.push({ ref: i.ref ?? null, fields: hits });
    }
    if (other.length) {
      out.flags.push(`${other.length} workforce evidence item(s) name a different organisation in fields ` +
        "the API returned. They are shown as returned, not removed.");
    }
  }
  out.evidence = { status: "returned", as_of: ev.as_of ?? null, framework_version: ev.framework_version ?? null,
    item_count: count, items_returned: items.length, truncated, qualifying, not_qualifying: notQualifying,
    qualification_unstated: unstated, other_organisation_items: other };
  return out;
}

function aiSection(resp: Json, cfg: Json, profile: Json): Json {
  const c = resp.company;
  if (!isObj(c)) {
    const err = isObj(resp.error) ? resp.error : {};
    return { status: "not_found", error_code: err.code ?? null, error_message: err.message ?? null,
      billable: (resp.usage ?? {}).billable ?? null };
  }
  const names = [text(c.company_name), text(profile.name)].filter((n): n is string => !!n).map((n) => n.toLowerCase());
  const dims = isObj(c.dimensions) ? c.dimensions : {};
  const available = isObj(c.evidence_available) ? c.evidence_available : {};
  const momentum = isObj(c.momentum) ? c.momentum : {};
  const level = c.overall_level ?? null;
  const notes: string[] = [];
  if (typeof level === "string" && !KNOWN_LEVELS.includes(level)) {
    notes.push(`The overall level ${level} is not one of the documented levels. It is shown as returned.`);
  }
  if (cfg.ai_evidence && !has(resp, "evidence")) notes.push("No evidence was returned for any area.");
  return {
    status: "found", firmable_id: c.firmable_id ?? null, company_name: c.company_name ?? null,
    country: c.country ?? null, industry_group: c.industry_group ?? null, size_band: c.size_band ?? null,
    overall_level: level, explanation: c.explanation ?? null,
    momentum: { label: momentum.label ?? null, signals: (momentum.signals ?? []).filter((s: unknown) => isObj(s)) },
    as_of: c.as_of ?? null, framework_version: c.framework_version ?? null,
    billable: (resp.usage ?? {}).billable ?? null, notes,
    areas: AREAS.map((a) => area(a, dims[a], available[a], resp.evidence, cfg.ai_evidence, c.firmable_id ?? null, names)),
  };
}

function conflicts(profile: Json, ai: Json): string[] {
  if (ai.status !== "found") return [];
  const out: string[] = [];
  if (text(ai.firmable_id) && ai.firmable_id !== profile.id) {
    out.push(`AI Adoption returned company ID ${ai.firmable_id}, but the company profile has ID ${profile.id}.`);
  }
  let a = text(ai.company_name);
  let b = text(profile.name);
  if (a && b && a.toLowerCase() !== b.toLowerCase()) out.push(`AI Adoption names the company "${a}"; the company profile names it "${b}".`);
  a = text(ai.country);
  b = text(profile.hq_country);
  if (a && b && a.toUpperCase() !== b.toUpperCase()) out.push(`AI Adoption gives the country ${a}; the company profile gives the head office country ${b}.`);
  return out;
}

function first(items: Json[], field: string): string | null {
  for (const i of items) {
    const v = text(i[field]);
    if (v) return v;
  }
  return null;
}

function factsForTemplates(profile: Json, ai: Json, tech: Json): Record<string, string | null> {
  const industries = Array.isArray(profile.industries) ? profile.industries : [];
  const values: Record<string, string | null> = {
    company_name: text(profile.name),
    industry: industries.length ? text(industries[0]) : null,
    hq_country: text(profile.hq_country),
    overall_level: null, momentum_label: null, ai_tool: null, ai_role: null, technology: null,
  };
  for (const a of AREAS) values[`${a}_level`] = null;
  if (ai.status === "found") {
    values.overall_level = text(ai.overall_level);
    values.momentum_label = text((ai.momentum ?? {}).label);
    for (const ar of ai.areas) {
      values[`${ar.area}_level`] = text(ar.level);
      const ev = ar.evidence ?? {};
      if (ar.area === "footprint") values.ai_tool = first(ev.qualifying ?? [], "technology");
      if (ar.area === "hiring") values.ai_role = first(ev.qualifying ?? [], "title");
    }
  }
  if (tech.status === "retrieved") values.technology = first(tech.technologies.filter((t: Json) => t.confidence === "high"), "product");
  return values;
}

function questions(cfg: Json, factValues: Record<string, string | null>): [Json[], Json[]] {
  const filled: Json[] = [];
  const skipped: Json[] = [];
  for (const q of cfg.discovery_questions) {
    const values: Record<string, string> = {};
    const sources: Json[] = [];
    const missing: string[] = [];
    for (const name of q.placeholders as string[]) {
      if (has(cfg.seller_context, name)) {
        values[name] = cfg.seller_context[name];
        sources.push({ placeholder: name, value: values[name], source: "seller context" });
      } else if (factValues[name]) {
        values[name] = factValues[name] as string;
        sources.push({ placeholder: name, value: values[name], source: PLACEHOLDER_SOURCES[name] });
      } else {
        missing.push(name);
      }
    }
    if (missing.length) { skipped.push({ id: q.id, template: q.template, missing }); continue; }
    const filledText = (q.template as string).replace(TOKEN, (_m, n: string) => values[n]);
    filled.push({ id: q.id, kind: "suggested_question", text: filledText, template: q.template, filled_from: sources });
  }
  return [filled, skipped];
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = [
    "AI Adoption levels, explanations, headlines and momentum are shown as returned by the API. This code " +
      "computes no AI score and no trend.",
    "An AI adoption level or a detected technology does not prove buying intent.",
    "Discovery questions are filled from fixed templates in the configuration, using only API facts and " +
      "seller context. They are suggested questions, not facts.",
    "Workforce evidence is checked for a different organisation only in fields the API returns that name " +
      "an organisation (company_id, firmable_id, organisation_id, company_name, employer, organisation, " +
      "company). The documented workforce item fields do not include an employer, so an item that belongs to " +
      "another organisation may not be detectable.",
  ];
  if (!cfg.ai_evidence) {
    coverage.push("AI Adoption evidence was not requested (ai_evidence is false), so qualifying and " +
      "non-qualifying evidence is not shown.");
  }
  if (cfg.tech_intel) {
    coverage.push("A technology not listed is not observed in retrieved data. It does not mean the company " +
      "does not use it.");
  } else {
    coverage.push("Tech Intel was not requested (tech_intel is false).");
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _domain: normaliseDomain(r.domain) ?? "" })) as Row[];
  const dupes = findDuplicates(keyed, "_domain");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier domain; their API responses were reused, not requested again.`);
  const state = { techForbidden: false };

  const techSection = async (rowId: string, companyId: string): Promise<Json> => {
    if (!cfg.tech_intel) return { status: "not_requested", reason: "Tech Intel is turned off in the configuration." };
    if (ctx.stopped !== null) return { status: "not_requested", reason: "The run stopped before Tech Intel was requested." };
    if (state.techForbidden) return { status: "not_on_plan", reason: "Tech Intel returned 403 earlier in this run, so it was not requested." };
    const ti = await client.getTechIntel({ id: companyId }, { withEvidence: cfg.tech_intel_with_evidence, maxPages: cfg.tech_intel_max_pages });
    const entry: Json = ti.error ? recordError(ctx, rowId, ti.error) : null;
    const items: Json[] = ti.items.filter((i) => isObj(i));
    const out: Json = { total: ti.total, retrieved: items.length, pages_fetched: ti.pages_fetched, complete: ti.complete,
      stop_reason: ti.stop_reason, error: entry, technologies: items };
    if (ti.pages_fetched === 0) {
      const kind = entry?.kind;
      if (kind === "forbidden") {
        state.techForbidden = true;
        return { ...out, status: "not_on_plan", reason: "Tech Intel returned 403: it is not on your plan, or this key cannot use it." };
      }
      if (kind === "not_found") return { ...out, status: "not_found", reason: "Tech Intel has no company for this Firmable ID (404)." };
      return { ...out, status: "error", reason: `Tech Intel could not be read: ${stripDots(entry?.message)}.` };
    }
    const total = ti.total;
    const totalText = typeof total === "number" && Number.isInteger(total) ? String(total) : "an unknown number of";
    let reason: string;
    if (ti.complete && total === 0) reason = "Firmable holds no technology data for this company.";
    else if (ti.complete) reason = `Every technology Firmable holds for this company was retrieved: ${totalText} in total.`;
    else {
      reason = `${items.length} of ${totalText} technologies were retrieved in ${ti.pages_fetched} page(s). ` +
        "Technologies not listed are not observed in retrieved data.";
    }
    return { ...out, status: "retrieved", reason };
  };

  const one = async (row: Row): Promise<Json> => {
    const domain = normaliseDomain(row.domain);
    const base: Json = { row_id: row.row_id, input: row, domain, duplicate_of: dupes[row.row_id] ?? null, company: null,
      ai_adoption: null, tech_intel: null, data_conflicts: [], suggested_questions: [], skipped_questions: [] };
    if (!domain) return { ...base, status: "invalid_input", status_reason: `${pyRepr(row.domain)} is not a valid domain.` };
    let profile: Json;
    try {
      profile = await client.getCompany({ fqdn: domain });
    } catch (e) {
      if (e instanceof NotFoundError) {
        return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this domain, so no brief was built." };
      }
      throw e;
    }
    base.company = facts.companyFacts(profile);
    let ai: Json;
    try {
      const resp = await client.getAiAdoption({ company_id: profile.id }, { withEvidence: cfg.ai_evidence });
      ai = aiSection(resp, cfg, profile);
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      ai = { status: "error", error: ctx.recordError(row.row_id, e) };
    }
    const tech = await techSection(row.row_id, profile.id);
    base.ai_adoption = ai;
    base.tech_intel = tech;
    base.data_conflicts = conflicts(profile, ai);
    [base.suggested_questions, base.skipped_questions] = questions(cfg, factsForTemplates(profile, ai, tech));

    const issues: string[] = [];
    if (ai.status === "error") issues.push(`AI Adoption could not be read: ${stripDots(ai.error.message)}.`);
    if (cfg.tech_intel && tech.status !== "retrieved") issues.push(tech.reason);
    if (issues.length) return { ...base, status: "incomplete", status_reason: issues.join(" ") };
    const techText = tech.status === "retrieved" ? ` Tech Intel: ${tech.reason}` : "";
    if (ai.status === "not_found") {
      return { ...base, status: "no_ai_adoption_data", status_reason: "Firmable holds no AI adoption data for this company." + techText };
    }
    return { ...base, status: "brief_ready", status_reason: `AI Adoption overall level ${text(ai.overall_level) ?? "not returned"}, ` +
      `as of ${text(ai.as_of) ?? "an unstated date"}.${techText}` };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const levels: Record<string, number> = {};
  const skipped: Record<string, number> = {};
  let flagged = 0;
  for (const r of rows) {
    const ai = r.ai_adoption ?? {};
    if (ai.status === "found") {
      const key = text(ai.overall_level) ?? "not returned";
      levels[key] = (levels[key] ?? 0) + 1;
      if (ai.areas.some((a: Json) => a.flags.length)) flagged += 1;
    }
    for (const s of r.skipped_questions ?? []) skipped[s.id] = (skipped[s.id] ?? 0) + 1;
  }
  if (flagged) {
    coverage.push(`${flagged} brief(s) have missing, truncated or questionable evidence flagged in at least ` +
      "one area.");
  }
  if (Object.keys(skipped).length) {
    coverage.push("Some discovery-question templates were skipped because a placeholder had no value: " +
      Object.entries(skipped).map(([k, v]) => `${k} (${v} row(s))`).join(", ") + ".");
  }
  if (rows.some((r) => (r.data_conflicts ?? []).length)) {
    coverage.push("Some company facts differ between the company profile and AI Adoption. Both values are " +
      "shown; this code does not correct upstream data.");
  }
  if (state.techForbidden) {
    coverage.push("Tech Intel returned 403: it is not on your plan, or this key cannot use it. It was not " +
      "requested again in this run.");
  }
  return { rows, summary: { status_counts: counts, overall_level_counts: levels, skipped_question_counts: skipped }, coverage };
}

export const CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
  "overall_level", "momentum_label", "architecture_level", "footprint_level", "hiring_level",
  "initiatives_level", "workforce_level", "ai_as_of", "technologies_total", "technologies_retrieved",
  "tech_list_complete", "flags", "suggested_questions", "skipped_questions"];

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const company = r.company ?? {};
    const ai = r.ai_adoption ?? {};
    const tech = r.tech_intel ?? {};
    const areas: Record<string, Json> = Object.fromEntries((ai.areas ?? []).map((a: Json) => [a.area, a]));
    const flags: string[] = [...(r.data_conflicts ?? [])];
    for (const a of ai.areas ?? []) flags.push(...a.flags.map((f: string) => `${a.area}: ${f}`));
    const row: Record<string, unknown> = {
      row_id: r.row_id, domain: r.domain || r.input.domain, duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: company.id ?? null, company_name: company.name ?? null,
      overall_level: ai.overall_level ?? null, momentum_label: (ai.momentum ?? {}).label ?? null,
      ai_as_of: ai.as_of ?? null, technologies_total: tech.total ?? null,
      technologies_retrieved: tech.retrieved ?? null, tech_list_complete: tech.complete ?? null,
      flags,
      suggested_questions: (r.suggested_questions ?? []).map((q: Json) => q.text),
      skipped_questions: (r.skipped_questions ?? []).map((q: Json) => q.id),
    };
    for (const a of AREAS) row[`${a}_level`] = (areas[a] ?? {}).level ?? null;
    return row;
  });
}

function itemsTable(items: Json[]): string {
  return table(["Item", "Type or category", "Date", "Confidence or seniority", "State", "Reference"], items.map((i) => [
    i.technology || i.title || i.summary || "", i.type || i.category, i.date || i.last_seen || i.first_seen,
    i.confidence_band || i.seniority, i.state || i.observation, isObj(i.ref) ? i.ref.id : null]));
}

function areaHtml(a: Json): string {
  let html = `<h3>${esc(a.area.charAt(0).toUpperCase() + a.area.slice(1))}: ${esc(a.level || "no level")}</h3>`;
  html += a.headline ? `<p>${esc(a.headline)}</p>` : '<p class="muted">No headline returned.</p>';
  for (const f of a.flags) html += note(esc(f), "review");
  for (const n of a.notes) html += note(esc(n), "unknown");
  const ev = a.evidence ?? {};
  if (ev.status === "returned") {
    html += `<p class="muted">Evidence as of ${esc(ev.as_of)}: ${esc(ev.items_returned)} of ${esc(ev.item_count)} item(s) returned` +
      (ev.truncated ? ", truncated" : "") + ".</p>";
    if (ev.qualifying.length) html += "<p><strong>Qualifying evidence</strong></p>" + itemsTable(ev.qualifying);
    if (ev.not_qualifying.length) html += "<p><strong>Evidence that does not qualify</strong></p>" + itemsTable(ev.not_qualifying);
    if (ev.qualification_unstated.length) html += "<p><strong>Evidence that does not say whether it qualifies</strong></p>" + itemsTable(ev.qualification_unstated);
    if (ev.other_organisation_items.length) html += detailsJson("Workforce items that name another organisation", ev.other_organisation_items);
  }
  return html;
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Brief summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Levels, explanations and headlines are the API's own words. This report computes no AI " +
      "score and no trend. An AI adoption level or a detected technology does not prove buying " +
      "intent.", "unknown"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.domain || r.input.domain || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    const company = r.company;
    if (company) {
      left += kv([["Firmable ID", company.id], ["Domain", company.fqdn], ["Head office country", company.hq_country],
        ["Size", company.company_size], ["Industries", (company.industries ?? []).join(", ") || null]]);
    }
    const ai = r.ai_adoption ?? {};
    if (ai.status === "found") {
      left += "<h3>AI Adoption</h3>" + kv([
        ["Overall level", ai.overall_level], ["Explanation", ai.explanation], ["Momentum", (ai.momentum ?? {}).label],
        ["Momentum signals", ((ai.momentum ?? {}).signals ?? []).map((s: Json) => `${s.kind}: ${s.count}, latest ${s.latest}`).join("; ") || null],
        ["Data as of", ai.as_of], ["Framework version", ai.framework_version]]);
      for (const n of ai.notes ?? []) left += note(esc(n), "unknown");
    } else if (ai.status === "not_found") {
      left += note("Firmable holds no AI adoption data for this company.", "unknown");
    } else if (ai.status === "error") {
      left += note(`AI Adoption could not be read: ${esc(ai.error.message)}`, "error");
    }
    for (const c of r.data_conflicts ?? []) left += note(esc(c), "review");
    if (r.duplicate_of) left += note(`Same domain as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    if (ai.status === "found") {
      right += table(["Area", "Level", "Observability", "Counts toward overall", "Evidence held", "Flags"],
        ai.areas.map((a: Json) => [a.area, a.level, a.observability, a.counts_toward_overall, a.evidence_available, a.flags.length]));
      right += ai.areas.map((a: Json) => areaHtml(a)).join("");
    }
    const tech = r.tech_intel ?? {};
    if (r.tech_intel) {
      right += `<h3>Tech Intel</h3><p class="muted">${esc(tech.reason)}</p>`;
      const techs: Json[] = tech.technologies ?? [];
      if (techs.length) {
        right += table(["Product", "Vendor", "Category", "Confidence", "Recency", "Last observed", "Nature"],
          techs.map((t) => [t.product, t.vendor, t.category, t.confidence, t.recency, t.last_observed, t.nature]));
      }
    }
    const qs: Json[] = r.suggested_questions ?? [];
    const skippedQs: Json[] = r.skipped_questions ?? [];
    if (qs.length || skippedQs.length) {
      let body = "<p><strong>Suggested questions.</strong> Filled from fixed templates. These are not facts.</p>";
      body += bulletList(qs.map((q) => q.text));
      if (skippedQs.length) {
        body += '<p class="muted">Skipped templates (a placeholder had no value):</p>' +
          bulletList(skippedQs.map((s) => `${s.id}: missing ${s.missing.join(", ")}`));
      }
      right += `<div class="template">${body}</div>`;
    }
    if (r.ai_adoption || r.tech_intel) right += detailsJson("Brief as JSON", { ai_adoption: ai, tech_intel: tech });
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No brief was built for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account brief", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
