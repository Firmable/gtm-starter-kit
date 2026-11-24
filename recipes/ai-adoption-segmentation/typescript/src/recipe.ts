/**
 * AI adoption segmentation: assigns each company to one configured campaign segment.
 * Mirrors recipes/ai-adoption-segmentation/python/src/ai_adoption_segmentation/recipe.py; see that
 * file and the recipe README for the assignment policy.
 */
import {
  ConfigError, FATAL_KINDS, FirmableError, Json, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, findDuplicates, kv, normaliseDomain, note, pricing, pyRepr,
  readRows, table,
} from "firmable-gtm-client";

export const NAME = "ai-adoption-segmentation";
export const TITLE = "AI adoption segmentation";
export const DESCRIPTION = "Assigns each company to one AI-related campaign segment using Firmable AI Adoption levels, " +
  "dimensions and momentum, optional Tech Intel and Decisions, and explicit ordered rules.";

const DIMENSIONS = ["architecture", "footprint", "hiring", "initiatives", "workforce"];
const DEFAULT_LEVELS = ["NONE", "BASIC", "MEANINGFUL", "MODERATE", "STRONG", "ADVANCED"];
const CONFIDENCE_RANK: Record<string, number> = { low: 0, medium: 1, high: 2 };
const TECH_KEYS = ["category_slug", "filter_slug", "product_slug", "vendor_slug"];
const CONDITION_KEYS: Record<string, Set<string>> = {
  overall_level: new Set(["type", "at_least", "at_most", "in"]),
  dimension_level: new Set(["type", "dimension", "at_least", "at_most", "in"]),
  momentum: new Set(["type", "in"]),
  momentum_signal: new Set(["type", "kind", "min_count"]),
  technology: new Set(["type", "category_slug", "filter_slug", "product_slug", "vendor_slug", "min_confidence", "min_count"]),
  decision: new Set(["type", "check"]),
};
const STATUS_TONE: Record<string, string> = {
  assigned: "good", needs_review: "review", incomplete: "error", no_segment_matched: "unknown", invalid_input: "error",
  error: "error", not_processed: "unknown",
};
const OUTCOME_TONE: Record<string, string> = {
  met: "good", not_met: "bad", unknown: "unknown", error: "error", not_evaluated: "neutral", matched: "good", not_matched: "bad",
};
const ID = /^[a-z0-9_]{1,40}$/;
const TOP_KEYS = new Set(["levels", "with_evidence", "tech_intel_max_pages", "with_reasoning", "decision_checks", "segments",
  "review_segment", "unmatched_segment"]);
const OPS = ["at_least", "at_most", "in"];

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x); // eslint-disable-line @typescript-eslint/no-explicit-any
const strList = (v: unknown): v is string[] => Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === "string" && x !== "");
const whole = (v: unknown, min: number): v is number => typeof v === "number" && Number.isInteger(v) && v >= min;

// ----- configuration --------------------------------------------------------------------------

function special(raw: Json, key: string, defaultId: string, defaultLabel: string): Json {
  const s = raw[key] || {};
  if (!isObj(s) || Object.keys(s).some((k) => k !== "id" && k !== "label")) throw new ConfigError(`${key} must be an object with id and label.`);
  const sid = s.id ?? defaultId;
  const label = s.label ?? defaultLabel;
  if (typeof sid !== "string" || !ID.test(sid)) throw new ConfigError(`${key}.id must be 1 to 40 lower-case letters, digits or underscores.`);
  if (typeof label !== "string" || !label) throw new ConfigError(`${key}.label must be text.`);
  return { id: sid, label };
}

function condition(c: unknown, where: string, levels: string[], checks: Set<string>): Json {
  if (!isObj(c)) throw new ConfigError(`${where} must be an object.`);
  const ctype = c.type;
  if (typeof ctype !== "string" || !(ctype in CONDITION_KEYS)) throw new ConfigError(`${where}.type must be one of: ${Object.keys(CONDITION_KEYS).join(", ")}.`);
  const extra = Object.keys(c).filter((k) => !CONDITION_KEYS[ctype].has(k)).sort();
  if (extra.length) throw new ConfigError(`${where} has unknown key(s) for type ${ctype}: ${extra.join(", ")}`);
  const out: Json = { type: ctype };
  if (ctype === "overall_level" || ctype === "dimension_level") {
    if (ctype === "dimension_level") {
      if (!DIMENSIONS.includes(c.dimension)) throw new ConfigError(`${where}.dimension must be one of: ${DIMENSIONS.join(", ")}.`);
      out.dimension = c.dimension;
    }
    const ops = OPS.filter((k) => k in c);
    if (ops.length !== 1) throw new ConfigError(`${where} needs exactly one of at_least, at_most or in.`);
    const op = ops[0];
    const value = c[op];
    if ((op === "in" && !strList(value)) || (op !== "in" && typeof value !== "string")) {
      throw new ConfigError(`${where}.${op} must be ${op === "in" ? "a list of levels" : "a level"}.`);
    }
    const wanted: string[] = op === "in" ? value : [value];
    const missing = wanted.filter((v) => !levels.includes(v));
    if (missing.length) throw new ConfigError(`${where}.${op} uses level(s) not in levels: ${missing.join(", ")}`);
    out[op] = value;
  } else if (ctype === "momentum") {
    if (!strList(c.in)) throw new ConfigError(`${where}.in must be a non-empty list of momentum labels.`);
    out.in = c.in;
  } else if (ctype === "momentum_signal") {
    if (typeof c.kind !== "string" || !c.kind) throw new ConfigError(`${where}.kind must name a momentum signal kind.`);
    const mc = c.min_count ?? 1;
    if (!whole(mc, 1)) throw new ConfigError(`${where}.min_count must be a whole number, 1 or more.`);
    out.kind = c.kind;
    out.min_count = mc;
  } else if (ctype === "technology") {
    const given = TECH_KEYS.filter((k) => k in c);
    if (!given.length) throw new ConfigError(`${where} needs at least one of: ${TECH_KEYS.join(", ")}.`);
    for (const k of given) {
      if (!strList(c[k])) throw new ConfigError(`${where}.${k} must be a non-empty list of slugs.`);
      out[k] = c[k];
    }
    const mconf = c.min_confidence ?? "low";
    if (typeof mconf !== "string" || !(mconf in CONFIDENCE_RANK)) throw new ConfigError(`${where}.min_confidence must be low, medium or high.`);
    const mc = c.min_count ?? 1;
    if (!whole(mc, 1)) throw new ConfigError(`${where}.min_count must be a whole number, 1 or more.`);
    out.min_confidence = mconf;
    out.min_count = mc;
  } else {
    if (typeof c.check !== "string" || !checks.has(c.check)) throw new ConfigError(`${where}.check must name an id from decision_checks.`);
    out.check = c.check;
  }
  return out;
}

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const levels = raw.levels ?? DEFAULT_LEVELS;
  if (!strList(levels) || new Set(levels).size !== levels.length) {
    throw new ConfigError("levels must be a non-empty list of distinct level names, lowest first.");
  }
  for (const key of ["with_evidence", "with_reasoning"]) {
    if (typeof (raw[key] ?? key === "with_reasoning") !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  const maxPages = raw.tech_intel_max_pages ?? 1;
  if (!whole(maxPages, 1) || maxPages > 20) throw new ConfigError("tech_intel_max_pages must be a whole number from 1 to 20.");

  const checksRaw = raw.decision_checks ?? [];
  if (!Array.isArray(checksRaw)) throw new ConfigError("decision_checks must be a list.");
  const checks: Json[] = [];
  checksRaw.forEach((c: unknown, idx: number) => {
    const i = idx + 1;
    if (!isObj(c)) throw new ConfigError(`decision_checks[${i}] must be an object.`);
    const extra = Object.keys(c).filter((k) => !["id", "label", "tool", "inputs", "want"].includes(k)).sort();
    if (extra.length) throw new ConfigError(`decision_checks[${i}] has unknown key(s): ${extra.join(", ")}`);
    const cid = c.id;
    if (typeof cid !== "string" || !ID.test(cid)) throw new ConfigError(`decision_checks[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (checks.some((x) => x.id === cid)) throw new ConfigError(`decision_checks[${i}].id ${pyRepr(cid)} is used twice.`);
    if (typeof c.tool !== "string" || !c.tool) throw new ConfigError(`decision_checks[${i}].tool must name a decision tool.`);
    const inputs = c.inputs ?? {};
    if (!isObj(inputs)) throw new ConfigError(`decision_checks[${i}].inputs must be an object.`);
    const want = c.want ?? "yes";
    if (want !== "yes" && want !== "no") throw new ConfigError(`decision_checks[${i}].want must be yes or no.`);
    const label = c.label ?? cid;
    if (typeof label !== "string" || !label) throw new ConfigError(`decision_checks[${i}].label must be text.`);
    checks.push({ id: cid, label, tool: c.tool, inputs, want });
  });
  const checkIds = new Set<string>(checks.map((c) => c.id));

  const segsRaw = raw.segments;
  if (!Array.isArray(segsRaw) || !segsRaw.length) throw new ConfigError("segments must be a non-empty list.");
  const review = special(raw, "review_segment", "review", "Needs review");
  const unmatched = special(raw, "unmatched_segment", "unmatched", "No segment matched");
  if (review.id === unmatched.id) throw new ConfigError("review_segment.id and unmatched_segment.id must differ.");
  const segments: Json[] = [];
  const seen = new Set<string>([review.id, unmatched.id]);
  segsRaw.forEach((s: unknown, idx: number) => {
    const where = `segments[${idx + 1}]`;
    if (!isObj(s)) throw new ConfigError(`${where} must be an object.`);
    const extra = Object.keys(s).filter((k) => !["id", "label", "description", "match", "conditions"].includes(k)).sort();
    if (extra.length) throw new ConfigError(`${where} has unknown key(s): ${extra.join(", ")}`);
    const sid = s.id;
    if (typeof sid !== "string" || !ID.test(sid)) throw new ConfigError(`${where}.id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(sid)) throw new ConfigError(`${where}.id ${pyRepr(sid)} is used twice or clashes with the review or unmatched segment.`);
    seen.add(sid);
    const label = s.label ?? sid;
    if (typeof label !== "string" || !label) throw new ConfigError(`${where}.label must be text.`);
    const desc = s.description ?? "";
    if (typeof desc !== "string") throw new ConfigError(`${where}.description must be text.`);
    const match = s.match ?? "all";
    if (match !== "all" && match !== "any") throw new ConfigError(`${where}.match must be all or any.`);
    const conds = s.conditions;
    if (!Array.isArray(conds) || !conds.length) throw new ConfigError(`${where}.conditions must be a non-empty list.`);
    segments.push({ id: sid, label, description: desc, match,
      conditions: conds.map((c: unknown, j: number) => condition(c, `${where}.conditions[${j + 1}]`, levels, checkIds)) });
  });
  const used = new Set(segments.flatMap((s) => s.conditions.filter((c: Json) => c.type === "decision").map((c: Json) => c.check)));
  for (const c of checks) {
    if (!used.has(c.id)) throw new ConfigError(`decision_checks ${pyRepr(c.id)} is not used by any segment. Remove it so no answer is paid for without a reason.`);
  }
  return { levels, with_evidence: raw.with_evidence ?? false, tech_intel_max_pages: maxPages,
    with_reasoning: raw.with_reasoning ?? true, decision_checks: checks, segments, review_segment: review,
    unmatched_segment: unmatched };
}

export function readInput(path: string): Row[] {
  return readRows(path, { required: ["domain"] });
}

const uses = (config: Json, ctype: string): boolean =>
  config.segments.some((s: Json) => s.conditions.some((c: Json) => c.type === ctype));

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
  const calls: PlannedCall[] = [];
  const checks = config.decision_checks.length;
  if (checks) calls.push(new PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []));
  calls.push(new PlannedCall("POST /ai-adoption", "AI Adoption for each unique valid domain" + (config.with_evidence ? ", with evidence" : ""),
    n, pricing.estimateAiAdoption(n, config.with_evidence)));
  if (uses(config, "technology")) {
    const mp = config.tech_intel_max_pages;
    calls.push(new PlannedCall("GET /tech-intel", `Up to ${mp} page(s) of technologies for each unique valid domain, only when a technology condition is tried`,
      n * mp, [["tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n)]]));
  }
  if (checks) {
    calls.push(new PlannedCall("POST /decisions", `${checks} check(s) for each unique valid domain, only when a segment that uses them is tried`,
      n * checks, pricing.estimateDecision(n * checks, config.with_reasoning)));
  }
  return calls;
}

// ----- evaluation -----------------------------------------------------------------------------

function rankText(subject: string, level: string, op: string, value: Json, levels: string[]): [string, string] {
  if (op === "in") {
    return value.includes(level) ? ["met", `${subject} ${level} is one of ${value.join(", ")}.`]
      : ["not_met", `${subject} ${level} is not one of ${value.join(", ")}.`];
  }
  const a = levels.indexOf(level);
  const b = levels.indexOf(value);
  if (op === "at_least") return a >= b ? ["met", `${subject} ${level} is at least ${value}.`] : ["not_met", `${subject} ${level} is below ${value}.`];
  return a <= b ? ["met", `${subject} ${level} is at most ${value}.`] : ["not_met", `${subject} ${level} is above ${value}.`];
}

function describe(c: Json): string {
  const t = c.type;
  if (t === "overall_level" || t === "dimension_level") {
    const subject = t === "overall_level" ? "overall level" : `${c.dimension} level`;
    const op = OPS.find((k) => k in c) as string;
    const value = op === "in" ? c[op].join(", ") : c[op];
    return `${subject} ${op.replace(/_/g, " ")} ${value}`;
  }
  if (t === "momentum") return `momentum in ${c.in.join(", ")}`;
  if (t === "momentum_signal") return `momentum signal ${c.kind} with count at least ${c.min_count}`;
  if (t === "technology") return `at least ${c.min_count} technology(ies) with ${techFilter(c)}`;
  return `decision check ${c.check}`;
}

function techFilter(c: Json): string {
  const parts = TECH_KEYS.filter((k) => k in c).map((k) => `${k} ${c[k].join(", ")}`);
  return `${parts.join("; ")}; confidence ${c.min_confidence} or higher`;
}

const sentence = (text: unknown): string => String(text).replace(/\.+$/, "") + ".";
const unique = (items: string[]): string[] => [...new Set(items)];

function techMatch(item: Json, c: Json): boolean {
  for (const k of TECH_KEYS) if (k in c && !c[k].includes(item[k])) return false;
  const rank = CONFIDENCE_RANK[String(item.confidence)];
  return rank !== undefined && rank >= CONFIDENCE_RANK[c.min_confidence];
}

function aiSummary(comp: Json): Json {
  const mom = isObj(comp.momentum) ? comp.momentum : null;
  const dims = isObj(comp.dimensions) ? comp.dimensions : {};
  const pickDim = (d: Json) => (isObj(d) ? {
    level: d.level ?? null, headline: d.headline ?? null, observability: d.observability ?? null,
    counts_toward_overall: d.counts_toward_overall ?? null,
  } : null);
  return {
    firmable_id: comp.firmable_id ?? null, company_name: comp.company_name ?? null, country: comp.country ?? null,
    industry_group: comp.industry_group ?? null, size_band: comp.size_band ?? null,
    overall_level: comp.overall_level ?? null, explanation: comp.explanation ?? null,
    momentum_label: mom?.label ?? null,
    momentum_signals: (mom?.signals ?? []).filter((s: unknown) => isObj(s)),
    dimensions: Object.fromEntries(DIMENSIONS.map((d) => [d, pickDim(dims[d])])),
    evidence_available: comp.evidence_available ?? null, as_of: comp.as_of ?? null,
    framework_version: comp.framework_version ?? null,
  };
}

function evidenceSummary(evidence: unknown): Json {
  if (!isObj(evidence)) return null;
  const out: Json = {};
  for (const area of DIMENSIONS) {
    const e = evidence[area];
    if (!isObj(e)) continue;
    const items = (e.items ?? []).filter((i: unknown) => isObj(i));
    out[area] = { item_count: e.item_count ?? null, items_returned: items.length,
      qualifying: items.filter((i: Json) => i.qualifies === true).length,
      not_qualifying: items.filter((i: Json) => i.qualifies === false).length,
      truncated: e.truncated === true, as_of: e.as_of ?? null };
  }
  return out;
}

const capitalise = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1);

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const levels: string[] = cfg.levels;
  const review = cfg.review_segment;
  const unmatched = cfg.unmatched_segment;
  const checks: Record<string, Json> = Object.fromEntries(cfg.decision_checks.map((c: Json) => [c.id, c]));
  const coverage: string[] = [
    "Segments are campaign groupings produced by the configured rules. An AI adoption level, a momentum label or " +
      "a detected technology does not prove buying intent.",
    "Segments are tried in order and the first match wins. When a segment cannot be evaluated because data is " +
      "missing or a request failed, the account goes to the review segment, not to a lower-priority segment.",
    `Levels are ranked by the configured list, lowest first: ${levels.join(", ")}. A level the rules read that is ` +
      "not in this list sends the account to review.",
    "AI Adoption explanations and headlines, and decision answers and reasons, are shown as returned.",
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length }, segment_counts: {} }, coverage };
  };

  if (cfg.decision_checks.length) {
    let catalogue: Json;
    try {
      catalogue = await client.listDecisionTools();
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      ctx.recordError(null, e);
      return stopAll("The decision tool list could not be read, so no account was segmented.");
    }
    const problems = decisions.checkTools(catalogue, cfg.decision_checks, "company");
    if (problems.length) {
      for (const p of problems) {
        ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
          message: p, charge_uncertain: false, retry_after: null });
      }
      coverage.push(...problems);
      return stopAll("The configuration names decision tools or inputs the API does not accept.");
    }
    const modelTools = [...new Set<string>(cfg.decision_checks.filter((c: Json) => decisions.usesModel(catalogue, c.tool)).map((c: Json) => c.tool))].sort();
    if (modelTools.length) {
      coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
        "This code applies only the rules in the configuration.");
    }
  }
  if (uses(cfg, "technology")) {
    coverage.push("Technology conditions use the Tech Intel list without evidence. A technology missing from an " +
      "incomplete list is unknown, not absent.");
  }
  if (cfg.with_evidence) {
    coverage.push("AI Adoption evidence items with qualifies false are counted separately; they do not count " +
      "towards a level. Evidence is shown as returned and is not used by the segment rules.");
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _domain: normaliseDomain(r.domain) ?? "" })) as Row[];
  const dupes = findDuplicates(keyed, "_domain");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier domain; their API responses were reused, not requested again.`);
  const referenced = new Set<string>();
  for (const s of cfg.segments) {
    for (const c of s.conditions) {
      if (c.type === "overall_level") referenced.add("overall_level");
      else if (c.type === "dimension_level") referenced.add(`dimensions.${c.dimension}.level`);
    }
  }

  const one = async (row: Row): Promise<Json> => {
    const domain = normaliseDomain(row.domain);
    const base: Json = { row_id: row.row_id, input: row, domain, duplicate_of: dupes[row.row_id] ?? null,
      segment_id: null, segment_label: null, ai_adoption: null, ai_adoption_found: null, unfamiliar_levels: [],
      evidence_summary: null, technologies: null, decisions: [], trace: [], unknowns: [], errors: [] };
    if (!domain) return { ...base, status: "invalid_input", status_reason: `${pyRepr(row.domain)} is not a valid domain.` };

    const state: Json = { ai: null, ai_error: null, tech: null, tech_error: null, decisions: {} as Record<string, Json> };
    try {
      const resp = await client.getAiAdoption({ domain }, { withEvidence: cfg.with_evidence });
      const comp = resp.company;
      const err = isObj(resp.error) ? resp.error : null;
      if (isObj(comp)) {
        state.ai = aiSummary(comp);
        base.ai_adoption_found = true;
        base.evidence_summary = cfg.with_evidence ? evidenceSummary(resp.evidence) : null;
      } else if (err && err.code !== null && err.code !== undefined && err.code !== "not_found") {
        state.ai_error = `${err.code}: ${err.message ?? "None"}`;
        ctx.errors.push({ row_id: row.row_id, kind: "answer_error" as never, status: 200, code: err.code,
          endpoint: "POST /ai-adoption", message: String(err.message || err.code), charge_uncertain: false, retry_after: null });
      } else {
        base.ai_adoption_found = false;
      }
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      const entry = ctx.recordError(row.row_id, e);
      state.ai_error = entry.message;
    }
    base.ai_adoption = state.ai;
    const ai = state.ai;
    const firmableId: string | null = ai?.firmable_id ?? null;

    if (ai) {
      const found: [string, string][] = [];
      for (const path of ["overall_level", ...DIMENSIONS.map((d) => `dimensions.${d}.level`)]) {
        const lvl = path === "overall_level" ? ai.overall_level : (ai.dimensions[path.split(".")[1]] ?? {}).level;
        if (typeof lvl === "string" && !levels.includes(lvl)) found.push([path, lvl]);
      }
      base.unfamiliar_levels = found.map(([p, l]) => `${p}=${l}`);
      const blocking = found.filter(([p]) => referenced.has(p)).map(([p, l]) => `${p} ${l}`);
      if (blocking.length) {
        const reason = `AI Adoption returned level(s) not in the configured level list: ${blocking.join(", ")}. ` +
          "The account goes to review instead of being ranked.";
        return { ...base, status: "needs_review", segment_id: review.id, segment_label: review.label, status_reason: reason, unknowns: [reason] };
      }
    }

    const aiMissing = (): [string, string] => (state.ai_error !== null
      ? ["error", sentence(`The AI Adoption request failed: ${state.ai_error}`)]
      : ["unknown", "Firmable has no AI Adoption data for this company (not found)."]);

    const tech = async (): Promise<void> => {
      if (state.tech !== null || state.tech_error !== null) return;
      if (ctx.stopped !== null) { state.tech_error = "not requested because the run stopped"; return; }
      const identifier: Record<string, string> = firmableId ? { id: firmableId } : { fqdn: domain };
      const res = await client.getTechIntel(identifier, { maxPages: cfg.tech_intel_max_pages });
      if (res.error) {
        const entry: Json = { row_id: row.row_id, ...res.error };
        ctx.errors.push(entry);
        if (FATAL_KINDS.has(entry.kind) && ctx.stopped === null) ctx.stopped = entry;
      }
      if ((res.pages_fetched ?? 0) === 0) { state.tech_error = res.error?.message || "no page was returned"; return; }
      state.tech = res;
    };

    const decide = async (checkId: string): Promise<Json> => {
      if (checkId in state.decisions) return state.decisions[checkId];
      const c = checks[checkId];
      const entry = { check_id: c.id, label: c.label, tool: c.tool, want: c.want };
      let rec: Json;
      if (ctx.stopped !== null) rec = { ...entry, result: "not_asked", answer: null, error: null };
      else {
        const subject = firmableId ? { company_id: firmableId } : { domain };
        try {
          const resp = await client.makeDecision(c.tool, { ...subject, ...c.inputs }, { withReasoning: cfg.with_reasoning });
          const ans = decisions.answerFrom(resp);
          const result = decisions.compare(ans.answer, c.want);
          rec = { ...entry, result, ...ans, error: result === "error" ? ans.answer_error : null };
        } catch (e) {
          if (!(e instanceof FirmableError)) throw e;
          rec = { ...entry, result: "error", answer: null, error: ctx.recordError(row.row_id, e) };
        }
      }
      state.decisions[checkId] = rec;
      return rec;
    };

    const evaluate = async (c: Json): Promise<[string, string]> => {
      const t = c.type;
      if (["overall_level", "dimension_level", "momentum", "momentum_signal"].includes(t) && ai === null) return aiMissing();
      if (t === "overall_level" || t === "dimension_level") {
        const op = OPS.find((k) => k in c) as string;
        let subject: string;
        let level: unknown;
        let extra = "";
        if (t === "overall_level") {
          subject = "Overall level";
          level = ai.overall_level;
        } else {
          const dim = ai.dimensions[c.dimension];
          if (dim === null || dim === undefined) return ["unknown", `The ${c.dimension} area was not returned.`];
          subject = `${capitalise(c.dimension)} level`;
          level = dim.level;
          if (dim.observability === "LOW") extra = ` Firmable's observability for this area is ${dim.observability}.`;
        }
        if (typeof level !== "string" || !level) return ["unknown", `${subject} was not returned.`];
        if (!levels.includes(level)) return ["unknown", `${subject} ${level} is not in the configured level list, so it cannot be ranked.`];
        const [outcome, text] = rankText(subject, level, op, c[op], levels);
        return [outcome, text + extra];
      }
      if (t === "momentum") {
        const label = ai.momentum_label;
        if (typeof label !== "string" || !label) return ["unknown", "Momentum was not returned."];
        return c.in.includes(label) ? ["met", `Momentum ${label} is one of ${c.in.join(", ")}.`]
          : ["not_met", `Momentum ${label} is not one of ${c.in.join(", ")}.`];
      }
      if (t === "momentum_signal") {
        if (ai.momentum_label === null && !ai.momentum_signals.length) return ["unknown", "Momentum was not returned."];
        const sig = ai.momentum_signals.find((s: Json) => s.kind === c.kind);
        if (!sig) return ["not_met", `Momentum signal ${c.kind} was not observed in retrieved data.`];
        const count = sig.count;
        if (typeof count !== "number" || !Number.isInteger(count)) return ["unknown", `Momentum signal ${c.kind} has no count.`];
        const latest = sig.latest ? ` (latest ${sig.latest})` : "";
        return count >= c.min_count ? ["met", `Momentum signal ${c.kind} has count ${count}, at least ${c.min_count}${latest}.`]
          : ["not_met", `Momentum signal ${c.kind} has count ${count}, below ${c.min_count}${latest}.`];
      }
      if (t === "technology") {
        await tech();
        if (state.tech === null) return ["error", sentence(`The Tech Intel request failed: ${state.tech_error}`)];
        const res = state.tech;
        const items = (res.items ?? []).filter((i: unknown) => isObj(i));
        const matched = items.filter((i: Json) => techMatch(i, c));
        const names = [...new Set<string>(matched.map((i: Json) => String(i.product || "-")))].sort();
        const desc = techFilter(c);
        const n = matched.length;
        if (n >= c.min_count) return ["met", `${n} technology(ies) with ${desc} observed: ${names.slice(0, 5).join(", ")}.`];
        const total = res.total;
        const totalText = typeof total === "number" && Number.isInteger(total) ? String(total) : "unknown";
        if (res.complete) return ["not_met", `${n} technology(ies) with ${desc} observed in retrieved data, fewer than ${c.min_count}. The list of ${items.length} technologies is complete.`];
        return ["unknown", `${n} technology(ies) with ${desc} observed in retrieved data, fewer than ${c.min_count}, but only ${items.length} of ${totalText} technologies were retrieved.`];
      }
      const rec = await decide(c.check);
      if (rec.result === "pass") return ["met", `Decision ${rec.label}: answered ${rec.answer}, wanted ${rec.want}.`];
      if (rec.result === "fail") return ["not_met", `Decision ${rec.label}: answered ${rec.answer}, wanted ${rec.want}.`];
      if (rec.result === "unknown") return ["unknown", `Decision ${rec.label}: answered unknown. Firmable does not hold the fact; it is not a no.`];
      if (rec.result === "not_asked") return ["error", `Decision ${rec.label}: not asked because the run stopped.`];
      const err = rec.error;
      const msg = isObj(err) ? err.message : err;
      return ["error", sentence(`Decision ${rec.label}: no answer because of an error: ${msg === null || msg === undefined ? "None" : msg}`)];
    };

    const trace: Json[] = [];
    let finalOutcome = "not_matched";
    let finalSeg: Json = null;
    for (const seg of cfg.segments) {
      const results: Json[] = [];
      let stop = false;
      for (const c of seg.conditions) {
        if (stop) { results.push({ condition: c, description: describe(c), outcome: "not_evaluated", explanation: null }); continue; }
        const [outcome, text] = await evaluate(c);
        results.push({ condition: c, description: describe(c), outcome, explanation: text });
        if ((seg.match === "all" && outcome === "not_met") || (seg.match === "any" && outcome === "met")) stop = true;
      }
      const outs = results.map((x) => x.outcome);
      let segOut: string;
      if (seg.match === "all") {
        segOut = outs.includes("not_met") ? "not_matched" : outs.includes("error") ? "error" : outs.includes("unknown") ? "unknown" : "matched";
      } else {
        segOut = outs.includes("met") ? "matched" : outs.includes("error") ? "error" : outs.includes("unknown") ? "unknown" : "not_matched";
      }
      trace.push({ segment_id: seg.id, label: seg.label, match: seg.match, outcome: segOut, conditions: results });
      if (segOut !== "not_matched") { finalOutcome = segOut; finalSeg = seg; break; }
    }

    const techRes = state.tech;
    base.technologies = techRes === null ? null : {
      total: techRes.total ?? null, retrieved: (techRes.items ?? []).length, complete: !!techRes.complete,
      pages_fetched: techRes.pages_fetched ?? null };
    base.decisions = Object.values(state.decisions);
    const unknowns = unique(trace.flatMap((t) => t.conditions.filter((x: Json) => x.outcome === "unknown" || x.outcome === "error").map((x: Json) => x.explanation)));
    const errors = unique(trace.flatMap((t) => t.conditions.filter((x: Json) => x.outcome === "error").map((x: Json) => x.explanation)));
    Object.assign(base, { trace, unknowns, errors });
    if (finalOutcome === "matched" && finalSeg) {
      const met = trace[trace.length - 1].conditions.filter((x: Json) => x.outcome === "met").map((x: Json) => x.explanation);
      return { ...base, status: "assigned", segment_id: finalSeg.id, segment_label: finalSeg.label,
        status_reason: `Matched segment ${finalSeg.label}. ` + met.join(" ") };
    }
    if ((finalOutcome === "unknown" || finalOutcome === "error") && finalSeg) {
      const gaps = unique(trace[trace.length - 1].conditions.filter((x: Json) => x.outcome === "unknown" || x.outcome === "error").map((x: Json) => x.explanation));
      const why = finalOutcome === "error" ? "an API error" : "missing data";
      return { ...base, status: finalOutcome === "error" ? "incomplete" : "needs_review", segment_id: review.id,
        segment_label: review.label,
        status_reason: `Segment ${finalSeg.label} could not be evaluated because of ${why}. ` + gaps.join(" ") + " Later segments were not tried." };
    }
    return { ...base, status: "no_segment_matched", segment_id: unmatched.id, segment_label: unmatched.label,
      status_reason: "No configured segment matched. Each segment had a condition that was not met." };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const notFound = rows.filter((r) => r.ai_adoption_found === false && !r.duplicate_of).length;
  if (notFound) {
    coverage.push(`${notFound} company(ies) have no AI Adoption data in Firmable (not found). This means no ` +
      "data was retrieved, not that the company has no AI adoption.");
  }
  const unfamiliar = [...new Set<string>(rows.flatMap((r) => r.unfamiliar_levels ?? []))].sort();
  if (unfamiliar.length) {
    coverage.push(`AI Adoption returned level(s) not in the configured list: ${unfamiliar.join(", ")}. Add ` +
      "them to levels in the right order once you know where they rank.");
  }
  const truncated = rows.filter((r) => !r.duplicate_of)
    .reduce((n, r) => n + Object.values(r.evidence_summary ?? {}).filter((e: Json) => e.truncated).length, 0);
  if (truncated) coverage.push(`${truncated} evidence area(s) are truncated: the response holds fewer items than item_count.`);
  const counts: Record<string, number> = {};
  const segCounts: Record<string, number> = {};
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    if (r.segment_id) segCounts[r.segment_id] = (segCounts[r.segment_id] ?? 0) + 1;
  }
  return { rows, summary: { status_counts: counts, segment_counts: segCounts }, coverage };
}

// ----- output ---------------------------------------------------------------------------------

export const CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "segment_id", "segment_label", "status_reason",
  "company_id", "company_name", "overall_level", "momentum", "architecture_level", "footprint_level", "hiring_level",
  "initiatives_level", "workforce_level", "unfamiliar_levels", "unknowns", "trace"];

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const ai = r.ai_adoption ?? {};
    const dims = ai.dimensions ?? {};
    const row: Record<string, unknown> = {
      row_id: r.row_id, domain: r.domain || r.input.domain, duplicate_of: r.duplicate_of ?? null, status: r.status,
      segment_id: r.segment_id ?? null, segment_label: r.segment_label ?? null,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: ai.firmable_id ?? null, company_name: ai.company_name ?? null, overall_level: ai.overall_level ?? null,
      momentum: ai.momentum_label ?? null, unfamiliar_levels: r.unfamiliar_levels ?? [], unknowns: r.unknowns ?? [],
      trace: (r.trace ?? []).map((t: Json) => `${t.segment_id}=${t.outcome}`),
    };
    for (const d of DIMENSIONS) row[`${d}_level`] = (dims[d] ?? {}).level ?? null;
    return row;
  });
}

export function render(report: Report, result: Json): void {
  const s = result.summary;
  const cfg = result.input?.config ?? {};
  report.add("Result", "Segment summary",
    "<p>" + Object.entries(s.status_counts ?? {}).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    table(["Segment", "Accounts"], Object.entries(s.segment_counts ?? {}).map(([k, v]) => [k, v])) +
    note("A segment is a grouping from your rules. An AI adoption level or a detected technology does " +
      "<strong>not prove buying intent</strong>. Unknown data sends an account to review, never to a lower segment.", "unknown"));
  if ((cfg.segments ?? []).length) {
    report.add("Rules", "Segments in the order they are tried", table(["#", "Segment", "Match", "Conditions"],
      cfg.segments.map((sg: Json, i: number) => [i + 1, `${sg.label} (${sg.id})`, sg.match, sg.conditions.map(describe).join("; ")])));
  }
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const ai = r.ai_adoption;
    const title = ai?.company_name || r.domain || r.input.domain || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = "";
    if (r.segment_label) left += `<p><strong>Segment:</strong> ${esc(r.segment_label)}</p>`;
    left += `<p>${esc(reason)}</p>`;
    if (ai) {
      left += "<h3>AI Adoption, as returned</h3>" + kv([["Firmable ID", ai.firmable_id], ["Overall level", ai.overall_level],
        ["Explanation", ai.explanation], ["Momentum", ai.momentum_label], ["Data as of", ai.as_of], ["Framework version", ai.framework_version]]);
      left += table(["Area", "Level", "Observability", "Counts toward overall", "Headline"],
        Object.entries(ai.dimensions ?? {}).map(([d, dim]: [string, Json]) => [d, dim?.level ?? null, dim?.observability ?? null,
          dim?.counts_toward_overall ?? null, dim?.headline ?? null]));
    } else if (r.ai_adoption_found === false) {
      left += note("Firmable has no AI Adoption data for this company. This is not evidence of no adoption.", "unknown");
    }
    if (r.duplicate_of) left += note(`Same domain as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    for (const t of r.trace ?? []) {
      right += `<h3>${esc(t.label)} ${badge(t.outcome.replace(/_/g, " "), OUTCOME_TONE[t.outcome] ?? "neutral")}</h3><p class="muted">Match ${esc(t.match)}</p>`;
      right += table(["Condition", "Outcome", "Explanation"], t.conditions.map((x: Json) => [x.description,
        new Raw(badge(x.outcome.replace(/_/g, " "), OUTCOME_TONE[x.outcome] ?? "neutral")), x.explanation]));
    }
    if ((r.unknowns ?? []).length) right += "<h3>Unknowns and errors</h3>" + bulletList(r.unknowns);
    for (const d of r.decisions ?? []) {
      const rs = d.reasoning ?? {};
      if (rs.reason) {
        right += `<p><strong>${esc(d.label)}:</strong> ${esc(rs.reason)}</p>` +
          `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
      }
    }
    const ev = r.evidence_summary;
    if (ev && Object.keys(ev).length) {
      right += "<h3>Evidence returned</h3>" + table(["Area", "Items held", "Items returned", "Qualifying", "Not qualifying", "Truncated"],
        Object.entries(ev).map(([a, e]: [string, Json]) => [a, e.item_count, e.items_returned, e.qualifying, e.not_qualifying, e.truncated ? "yes" : "no"]));
    }
    if ((r.trace ?? []).length) right += detailsJson("Rule trace as JSON", r.trace);
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No segment was tried for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its segment and rule trace", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
