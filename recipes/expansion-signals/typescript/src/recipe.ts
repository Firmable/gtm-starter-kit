/**
 * Expansion signals: flags land-and-expand signals for each account and assigns an ordered tier.
 * Mirrors recipes/expansion-signals/python/src/expansion_signals/recipe.py; see that file and the
 * recipe README for the tier policy.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "expansion-signals";
export const TITLE = "Expansion signals";
export const DESCRIPTION = "Flags land-and-expand signals for each account (recorded in several countries, several sites, " +
  "local teams and offices in named markets) with Firmable Decisions, and assigns the first " +
  "configured tier whose signals answered yes.";

const STATUS_TONE: Record<string, string> = {
  tier_assigned: "good", no_tier: "neutral", needs_review: "review", incomplete: "error",
  company_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const RESULT_TONE: Record<string, string> = { yes: "good", no: "bad", unknown: "unknown", error: "error", not_asked: "neutral" };
const OUTCOME_TONE: Record<string, string> = {
  matched: "good", not_matched: "neutral", needs_review: "review", incomplete: "error", not_considered: "neutral",
};
export const LOCAL_TEAM_COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
const LOCAL_TEAM_ANSWERS = ["AU", "NZ"];
const FACT_KEYS: Record<string, string> = {
  is_multinational: "countries", has_multiple_sites: "locations_held",
  has_local_team: "staff_in_country", is_located_in: "location_held",
};
const ID = /^[a-z0-9_]{1,40}$/;
const CC = /^[A-Z]{2}$/;
const COMPANY_ID = /^f\d{12}$/;
const TOP_KEYS = new Set(["checks", "tiers", "with_reasoning", "fetch_company"]);
const CHECK_KEYS = new Set(["multinational", "multiple_sites", "local_teams", "offices_in"]);

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const whole = (v: unknown, minimum: number): v is number => typeof v === "number" && Number.isInteger(v) && v >= minimum;
const extraKeys = (o: Record<string, unknown>, allowed: string[]) => Object.keys(o).filter((k) => !allowed.includes(k)).sort();

function loadChecks(raw: unknown): [Json, Json[], string[]] {
  if (!isObj(raw)) {
    throw new ConfigError("checks must be an object with at least one of multinational, multiple_sites, " +
      "local_teams, offices_in.");
  }
  const unknown = Object.keys(raw).filter((k) => !CHECK_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown key(s) in checks: ${unknown.join(", ")}`);
  const signals: Json[] = [];
  const warnings: string[] = [];

  const multinational = raw.multinational ?? false;
  if (typeof multinational !== "boolean") throw new ConfigError("checks.multinational must be true or false.");
  if (multinational) {
    signals.push({ id: "multinational", label: "Recorded in two or more countries", tool: "is_multinational", inputs: {} });
  }

  const ms = raw.multiple_sites ?? false;
  let multipleSites: Json = null;
  if (typeof ms === "boolean") {
    if (ms) multipleSites = { min_sites: 2 };
  } else if (isObj(ms)) {
    const extra = extraKeys(ms, ["min_sites"]);
    if (extra.length) throw new ConfigError(`checks.multiple_sites accepts only min_sites, not: ${extra.join(", ")}`);
    const minSites = ms.min_sites ?? 2;
    if (!whole(minSites, 2)) throw new ConfigError("checks.multiple_sites.min_sites must be a whole number, 2 or more.");
    multipleSites = { min_sites: minSites };
  } else {
    throw new ConfigError("checks.multiple_sites must be true, false or an object with min_sites.");
  }
  if (multipleSites) {
    signals.push({ id: "multiple_sites", label: `At least ${multipleSites.min_sites} sites recorded`,
      tool: "has_multiple_sites", inputs: { min_sites: multipleSites.min_sites } });
  }

  const ltRaw = raw.local_teams ?? [];
  if (!Array.isArray(ltRaw)) throw new ConfigError("checks.local_teams must be a list of {country, min_staff}.");
  const localTeams: Json[] = [];
  ltRaw.forEach((t: unknown, idx: number) => {
    const i = idx + 1;
    if (!isObj(t)) throw new ConfigError(`checks.local_teams[${i}] must be an object.`);
    const extra = extraKeys(t, ["country", "min_staff"]);
    if (extra.length) throw new ConfigError(`checks.local_teams[${i}] has unknown key(s): ${extra.join(", ")}`);
    const country = t.country;
    if (typeof country !== "string" || !LOCAL_TEAM_COUNTRIES.includes(country)) {
      throw new ConfigError(`checks.local_teams[${i}].country must be one of ${LOCAL_TEAM_COUNTRIES.join(", ")}.`);
    }
    if (localTeams.some((x) => x.country === country)) throw new ConfigError(`checks.local_teams[${i}].country ${pyRepr(country)} is listed twice.`);
    const minStaff = t.min_staff ?? 5;
    if (!whole(minStaff, 1)) throw new ConfigError(`checks.local_teams[${i}].min_staff must be a whole number, 1 or more.`);
    localTeams.push({ country, min_staff: minStaff });
    signals.push({ id: `local_team:${country}`, label: `At least ${minStaff} staff in ${country}`,
      tool: "has_local_team", inputs: { country, min_staff: minStaff } });
  });
  const unanswered = localTeams.map((t) => t.country).filter((c: string) => !LOCAL_TEAM_ANSWERS.includes(c));
  if (unanswered.length) {
    warnings.push("has_local_team answers only for AU and NZ. These configured countries will answer " +
      `unknown: ${unanswered.join(", ")}.`);
  }

  const oiRaw = raw.offices_in ?? [];
  if (!Array.isArray(oiRaw)) throw new ConfigError("checks.offices_in must be a list of two-letter country codes.");
  const officesIn: string[] = [];
  oiRaw.forEach((cc: unknown, idx: number) => {
    const i = idx + 1;
    if (typeof cc !== "string" || !CC.test(cc)) throw new ConfigError(`checks.offices_in[${i}] must be a two-letter upper-case country code such as AU.`);
    if (officesIn.includes(cc)) throw new ConfigError(`checks.offices_in[${i}] ${pyRepr(cc)} is listed twice.`);
    officesIn.push(cc);
    signals.push({ id: `office_in:${cc}`, label: `Office or staff recorded in ${cc}`, tool: "is_located_in",
      inputs: { countries: [cc], where: "any_office" } });
  });

  if (!signals.length) throw new ConfigError("checks must turn on at least one signal.");
  const checks = { multinational, multiple_sites: multipleSites, local_teams: localTeams, offices_in: officesIn };
  return [checks, signals, warnings];
}

function loadTiers(raw: unknown, signalIds: string[]): Json[] {
  if (!Array.isArray(raw) || !raw.length || raw.length > 20) throw new ConfigError("tiers must be a list of 1 to 20 tiers.");
  const tiers: Json[] = [];
  raw.forEach((t: unknown, idx: number) => {
    const i = idx + 1;
    if (!isObj(t)) throw new ConfigError(`tiers[${i}] must be an object.`);
    let extra = extraKeys(t, ["id", "label", "when"]);
    if (extra.length) throw new ConfigError(`tiers[${i}] has unknown key(s): ${extra.join(", ")}`);
    const tid = t.id;
    if (typeof tid !== "string" || !ID.test(tid)) throw new ConfigError(`tiers[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (tiers.some((x) => x.id === tid)) throw new ConfigError(`tiers[${i}].id ${pyRepr(tid)} is used twice.`);
    const label = t.label ?? tid;
    if (typeof label !== "string" || !label) throw new ConfigError(`tiers[${i}].label must be text.`);
    const when = t.when;
    if (!isObj(when)) throw new ConfigError(`tiers[${i}].when must be an object with all and/or any.`);
    extra = extraKeys(when, ["all", "any"]);
    if (extra.length) throw new ConfigError(`tiers[${i}].when has unknown key(s): ${extra.join(", ")}`);
    const parts: Record<string, string[]> = {};
    for (const key of ["all", "any"]) {
      const ids = when[key] ?? [];
      if (!Array.isArray(ids) || !ids.every((s) => typeof s === "string")) throw new ConfigError(`tiers[${i}].when.${key} must be a list of signal ids.`);
      for (const s of ids as string[]) {
        if (!signalIds.includes(s)) {
          throw new ConfigError(`tiers[${i}].when.${key} names signal ${pyRepr(s)}, which checks does not turn on. ` +
            `Configured signals: ${signalIds.join(", ")}.`);
        }
      }
      if (new Set(ids).size !== ids.length) throw new ConfigError(`tiers[${i}].when.${key} repeats a signal.`);
      parts[key] = [...(ids as string[])];
    }
    if (!parts.all.length && !parts.any.length) throw new ConfigError(`tiers[${i}].when needs at least one signal in all or any.`);
    tiers.push({ id: tid, label, when: parts });
  });
  return tiers;
}

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const [checks, signals, warnings] = loadChecks(raw.checks);
  const tiers = loadTiers(raw.tiers, signals.map((s) => s.id));
  for (const key of ["with_reasoning", "fetch_company"]) {
    if (typeof (raw[key] ?? false) !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  return { checks, signals, tiers, with_reasoning: raw.with_reasoning ?? false, fetch_company: raw.fetch_company ?? false, warnings };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ["domain", "company_id"] });
}

function lookup(row: Row): [string | null, string | null, string | null] {
  const cid = (row.company_id ?? "").trim();
  if (cid) {
    if (COMPANY_ID.test(cid)) return ["id", cid, null];
    return [null, null, `company_id ${pyRepr(cid)} is not a Firmable company ID (f followed by 12 digits).`];
  }
  const domain = normaliseDomain(row.domain);
  if (domain) return ["fqdn", domain, null];
  return [null, null, `${pyRepr(row.domain)} is not a valid domain.`];
}

function rowKey(row: Row): string {
  const [by, value] = lookup(row);
  return by ? `${by}:${value}` : "";
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const keys: string[] = [];
  for (const r of rows) {
    const k = rowKey(r);
    if (k && !keys.includes(k)) keys.push(k);
  }
  const n = keys.length;
  const signals = config.signals.length;
  const calls = [new PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, [])];
  if (config.fetch_company) {
    calls.push(new PlannedCall("GET /company", "Company profile for each unique valid identifier", n,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  }
  let purpose = `${signals} signal(s) for each unique valid identifier`;
  if (config.warnings.length) purpose += ". Config warning: " + config.warnings.join(" ");
  calls.push(new PlannedCall("POST /decisions", purpose, n * signals, pricing.estimateDecision(n * signals, config.with_reasoning)));
  return calls;
}

export function factText(v: unknown): string {
  if (v === null || v === undefined) return "not returned";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (Array.isArray(v)) return v.map(factText).join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function evaluateTier(tier: Json, res: Record<string, string>): [string, string] {
  const allIds: string[] = tier.when.all;
  const anyIds: string[] = tier.when.any;
  const allNo = allIds.filter((s) => res[s] === "no");
  const anyYes = anyIds.filter((s) => res[s] === "yes");
  const anyAllNo = anyIds.length > 0 && anyIds.every((s) => res[s] === "no");
  if (allNo.length || anyAllNo) {
    const parts: string[] = [];
    if (allNo.length) parts.push(`required signal(s) answered no: ${allNo.join(", ")}`);
    if (anyAllNo) parts.push(`none of the signal(s) in any answered yes: ${anyIds.join(", ")}`);
    return ["not_matched", "Not matched: " + parts.join("; ") + "."];
  }
  const blockers = allIds.filter((s) => res[s] !== "yes");
  if (anyIds.length && !anyYes.length) blockers.push(...anyIds.filter((s) => res[s] !== "no"));
  if (!blockers.length) return ["matched", `Matched: ${[...allIds, ...anyYes].join(", ")} answered yes.`];
  const errored = blockers.filter((s) => res[s] === "error" || res[s] === "not_asked");
  if (errored.length) {
    return ["incomplete", "Cannot be decided: signal(s) have no answer because of an error or because the run " +
      `stopped: ${errored.join(", ")}.`];
  }
  return ["needs_review", `Would match if these signal(s) answered yes, but they answered unknown: ${blockers.join(", ")}.`];
}

function assign(tiers: Json[], res: Record<string, string>): [Json[], string, Json, string] {
  const trace: Json[] = [];
  let decided: Json = null;
  let status = "no_tier";
  const statusFor: Record<string, string> = { matched: "tier_assigned", needs_review: "needs_review", incomplete: "incomplete" };
  for (const t of tiers) {
    const info = { tier_id: t.id, label: t.label, all: t.when.all, any: t.when.any };
    if (decided !== null) {
      trace.push({ ...info, outcome: "not_considered", reason: "An earlier tier decided the outcome." });
      continue;
    }
    const [outcome, reason] = evaluateTier(t, res);
    trace.push({ ...info, outcome, reason });
    if (outcome !== "not_matched") {
      decided = { tier_id: t.id, label: t.label, reason };
      status = statusFor[outcome];
    }
  }
  const unknown = Object.values(res).filter((v) => v === "unknown").length;
  let text: string;
  if (status === "tier_assigned" && decided) text = `First matching tier: ${decided.label}. ${decided.reason}`;
  else if (status === "needs_review" && decided) {
    text = `Tier ${decided.label} depends on unknown answers. ${decided.reason} Unknown means Firmable ` +
      "does not hold the fact; it is not a no.";
  } else if (status === "incomplete" && decided) text = `Tier ${decided.label} could not be decided. ${decided.reason}`;
  else {
    text = "No tier matched.";
    if (unknown) text += ` ${unknown} signal(s) answered unknown, and no tier's outcome depended on them.`;
  }
  return [trace, status, decided, text];
}

function conflicts(signals: Json[]): string[] {
  const res: Record<string, string> = Object.fromEntries(signals.map((s) => [s.signal_id, s.result]));
  const officesYes = signals.filter((s) => s.signal_id.startsWith("office_in:") && s.result === "yes")
    .map((s) => s.signal_id.split(":")[1]);
  const out: string[] = [];
  if (res.multinational === "no" && officesYes.length >= 2) {
    out.push("is_multinational answered no, but is_located_in (any office) answered yes for " +
      `${officesYes.length} countries: ${officesYes.join(", ")}. Shown as returned; not corrected.`);
  }
  if (res.multiple_sites === "no" && officesYes.length >= 2) {
    out.push("has_multiple_sites answered no, but is_located_in (any office) answered yes for " +
      `${officesYes.length} countries: ${officesYes.join(", ")}. Shown as returned; not corrected.`);
  }
  return out;
}

const EMPTY_ANSWER = {
  answer: null, confidence: null, details: null, reasoning: null, answered_at: null, credits: null,
  answer_error: null, entity_id: null, request_id: null,
};

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = [
    "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a " +
      "no or as a yes.",
    "A no answer means the presence is not recorded in the data Firmable holds. It is not proof that the " +
      "company has no presence there.",
    "Expansion signals describe where a company is recorded. They do not show buying intent.",
  ];
  if (cfg.warnings.length) coverage.push(...cfg.warnings);
  else if (cfg.checks.local_teams.length) coverage.push("has_local_team counts staff only in AU and NZ. Other countries answer unknown.");

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length }, tier_counts: {}, signal_counts: {} }, coverage };
  };

  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no signals were asked.");
  }
  const problems = [...new Set(decisions.checkTools(catalogue, cfg.signals, "company"))];
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("The configuration needs decision tools or inputs the API does not accept.");
  }

  const modelTools = [...new Set<string>(cfg.signals.filter((s: Json) => decisions.usesModel(catalogue, s.tool)).map((s: Json) => s.tool))].sort();
  if (modelTools.length) {
    coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
      "This code applies only the tier rules in the configuration.");
  }
  if (!cfg.fetch_company) coverage.push("Company profiles were not fetched; signals were asked by the company_id or domain given.");

  const keyed = ctx.rows.map((r) => ({ ...r, _key: rowKey(r) })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier company identifier; their API responses were reused, not requested again.`);

  const one = async (row: Row): Promise<Json> => {
    const [by, value, problem] = lookup(row);
    const base: Json = {
      row_id: row.row_id, input: row, lookup: by ? { by, value } : null, duplicate_of: dupes[row.row_id] ?? null,
      company: null, signals: [], tiers: [], tier_id: null, tier_label: null, deciding_tier_id: null, data_conflicts: [],
    };
    if (!by) return { ...base, status: "invalid_input", status_reason: problem };
    let subject: Json = by === "id" ? { company_id: value } : { domain: value };
    if (cfg.fetch_company) {
      let company: Json;
      try {
        company = await client.getCompany({ [by]: value as string });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this identifier, so no signals were asked." };
        }
        throw e;
      }
      base.company = facts.companyFacts(company);
      subject = { company_id: company.id };
      const inputDomain = normaliseDomain(row.domain);
      const profileDomain = normaliseDomain(typeof company.fqdn === "string" ? company.fqdn : null);
      if (inputDomain && profileDomain && inputDomain !== profileDomain) {
        base.data_conflicts.push(`The input domain ${inputDomain} differs from the profile's fqdn ${profileDomain}.`);
      }
    }
    const results: Json[] = [];
    for (const s of cfg.signals) {
      const entry = { signal_id: s.id, label: s.label, tool: s.tool, inputs: s.inputs };
      if (ctx.stopped !== null) {
        results.push({ ...entry, result: "not_asked", ...EMPTY_ANSWER, fact: null, error: null });
        continue;
      }
      let resp: Json;
      try {
        resp = await client.makeDecision(s.tool, { ...subject, ...s.inputs }, { withReasoning: cfg.with_reasoning });
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        if (e instanceof NotFoundError && e.code === "company_not_found") {
          return { ...base, status: "company_not_found", signals: results,
            status_reason: "Decisions found no Firmable company for this identifier, so the remaining signals were not asked." };
        }
        results.push({ ...entry, result: "error", ...EMPTY_ANSWER, fact: null, error: ctx.recordError(row.row_id, e) });
        continue;
      }
      const ans = decisions.answerFrom(resp);
      const result = ["yes", "no", "unknown"].includes(ans.answer) ? ans.answer : "error";
      const details = isObj(ans.details) ? ans.details : {};
      const key = FACT_KEYS[s.tool];
      const fact = key in details ? { key, value: details[key] } : null;
      results.push({ ...entry, result, ...ans, fact, error: result === "error" ? ans.answer_error : null });
    }
    const res: Record<string, string> = Object.fromEntries(results.map((s) => [s.signal_id, s.result]));
    const [trace, status, decided, reason] = assign(cfg.tiers, res);
    base.data_conflicts.push(...conflicts(results));
    return {
      ...base, status, status_reason: reason, signals: results, tiers: trace,
      tier_id: status === "tier_assigned" && decided ? decided.tier_id : null,
      tier_label: status === "tier_assigned" && decided ? decided.label : null,
      deciding_tier_id: decided ? decided.tier_id : null,
    };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  const tierCounts: Record<string, number> = {};
  const signalCounts: Record<string, Record<string, number>> = {};
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    if (r.tier_id) tierCounts[r.tier_id] = (tierCounts[r.tier_id] ?? 0) + 1;
    for (const s of r.signals ?? []) {
      const sc = (signalCounts[s.signal_id] ??= { yes: 0, no: 0, unknown: 0, error: 0, not_asked: 0 });
      sc[s.result] += 1;
    }
  }
  const nConflicts = rows.filter((r) => (r.data_conflicts ?? []).length).length;
  if (nConflicts) {
    coverage.push(`${nConflicts} row(s) have answers or identifiers that disagree. They are shown as returned ` +
      "and are not corrected.");
  }
  return { rows, summary: { status_counts: counts, tier_counts: tierCounts, signal_counts: signalCounts }, coverage };
}

const BASE_COLUMNS = ["row_id", "input_domain", "input_company_id", "duplicate_of", "status", "status_reason", "tier_id",
  "tier_label", "deciding_tier_id", "company_id", "company_name"];
const TAIL_COLUMNS = ["signal_facts", "data_conflicts"];

export function csvColumns(result: Json): string[] {
  const cfg = result.input?.config ?? {};
  return [...BASE_COLUMNS, ...(cfg.signals ?? []).map((s: Json) => s.id), ...TAIL_COLUMNS];
}

export function csvRows(result: Json): Record<string, unknown>[] {
  const cfg = result.input?.config ?? {};
  return result.rows.map((r: Json) => {
    const company = r.company ?? {};
    const signals: Json[] = r.signals ?? [];
    const byId: Record<string, Json> = Object.fromEntries(signals.map((s) => [s.signal_id, s]));
    const row: Record<string, unknown> = {
      row_id: r.row_id, input_domain: r.input.domain ?? null, input_company_id: r.input.company_id ?? null,
      duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      tier_id: r.tier_id ?? null, tier_label: r.tier_label ?? null, deciding_tier_id: r.deciding_tier_id ?? null,
      company_id: company.id ?? null, company_name: company.name ?? null,
      signal_facts: signals.filter((s) => s.fact).map((s) => `${s.signal_id} ${s.fact.key}=${factText(s.fact.value)}`),
      data_conflicts: r.data_conflicts ?? [],
    };
    for (const s of cfg.signals ?? []) row[s.id] = byId[s.id] ? byId[s.id].result : null;
    return row;
  });
}

export function render(report: Report, result: Json): void {
  const s = result.summary;
  const cfg = result.input?.config ?? {};
  const counts: Record<string, number> = s.status_counts ?? {};
  report.add("Result", "Expansion signal summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    table(["Tier", "Accounts"], Object.entries(s.tier_counts ?? {}).map(([k, v]) => [k, v])) +
    table(["Signal", "Yes", "No", "Unknown", "Error", "Not asked"],
      Object.entries(s.signal_counts ?? {}).map(([k, v]: [string, Json]) => [k, v.yes, v.no, v.unknown, v.error, v.not_asked])) +
    note("A signal counts only on a yes. <strong>No</strong> means not recorded in the data Firmable " +
      "holds, not proof of no presence. <strong>Unknown</strong> sends an account to review, never to " +
      "a lower tier. These signals do not show buying intent.", "unknown"));
  if ((cfg.tiers ?? []).length) {
    report.add("Rules", "Tiers in the order they are tried",
      table(["#", "Tier", "All of", "At least one of"], cfg.tiers.map((t: Json, i: number) =>
        [i + 1, `${t.label} (${t.id})`, t.when.all.join(", ") || "-", t.when.any.join(", ") || "-"])) +
      table(["Signal", "Meaning", "Decision tool", "Inputs"],
        (cfg.signals ?? []).map((x: Json) => [x.id, x.label, x.tool, JSON.stringify(x.inputs)])) +
      (cfg.warnings ?? []).map((w: string) => note(esc(w), "unknown")).join(""));
  }
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const company = r.company;
    const title = company?.name || r.input.domain || r.input.company_id || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = "";
    if (r.tier_label) left += `<p><strong>Tier:</strong> ${esc(r.tier_label)}</p>`;
    left += `<p>${esc(reason)}</p>`;
    if (company) {
      left += kv([["Firmable ID", company.id], ["Domain", company.fqdn], ["Head office country", company.hq_country],
        ["Size", company.company_size]]);
    } else if (r.lookup) {
      left += kv([["Asked by", r.lookup.by], ["Identifier", r.lookup.value]]);
    }
    if (r.duplicate_of) left += note(`Same company identifier as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    for (const c of r.data_conflicts ?? []) left += note(esc(c), "review");
    let right = "";
    const signals: Json[] = r.signals ?? [];
    if (signals.length) {
      right += "<h3>Signals, as returned</h3>" + table(["Signal", "Answer", "Fact returned", "Confidence"], signals.map((x) => [
        x.label, new Raw(badge(x.result.replace(/_/g, " "), RESULT_TONE[x.result] ?? "neutral")),
        x.fact ? `${x.fact.key}: ${factText(x.fact.value)}` : null, x.confidence ?? null]));
      for (const x of signals) {
        const rs = x.reasoning ?? {};
        if (rs.reason) {
          right += `<p><strong>${esc(x.label)}:</strong> ${esc(rs.reason)}</p>` +
            `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
        }
        if (x.error) right += note(`${esc(x.label)}: ${esc(isObj(x.error) ? x.error.message : x.error)}`, "error");
      }
      const unknown = signals.filter((x) => x.result === "unknown").map((x) => x.label);
      if (unknown.length) right += "<h3>Not held by Firmable (unknown)</h3>" + bulletList(unknown);
    }
    if ((r.tiers ?? []).length) {
      right += "<h3>Tier trace</h3>" + table(["Tier", "Outcome", "Explanation"], r.tiers.map((t: Json) => [
        t.label, new Raw(badge(t.outcome.replace(/_/g, " "), OUTCOME_TONE[t.outcome] ?? "neutral")), t.reason]));
    }
    if (signals.length) right += detailsJson("Signals as JSON, with details as returned", signals);
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No signals were asked for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its signals and tier", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns, csvRows, render,
};
