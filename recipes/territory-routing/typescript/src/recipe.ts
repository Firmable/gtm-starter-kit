/**
 * Territory routing: assigns accounts to sales teams with ordered, deterministic routing rules.
 * Mirrors recipes/territory-routing/python/src/territory_routing/recipe.py; see that file and the
 * recipe README for the condition, overlap and review rules. Nothing is written to a CRM.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "territory-routing";
export const TITLE = "Territory routing";
export const DESCRIPTION = "Assigns each company to a sales team by applying ordered routing rules to Firmable company " +
  "profiles and optional Decisions answers, and sends ambiguous or incomplete records to a " +
  "review queue. Nothing is written to a CRM.";

const STATUS_TONE: Record<string, string> = {
  assigned: "good", assigned_default: "good", unassigned: "neutral", review: "review", incomplete: "error",
  company_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const OUTCOME_TONE: Record<string, string> = { match: "good", no_match: "neutral", unknown: "unknown", error: "error", not_evaluated: "neutral" };
const REVIEW_QUEUE = new Set(["review", "incomplete", "company_not_found", "invalid_input", "error"]);

const ID = /^[a-z][a-z0-9_]{0,39}$/;
const COMPANY_ID = /^f\d{12}$/;
const TOP_KEYS = new Set(["with_reasoning", "teams", "decision_checks", "rules", "overlap", "default_team"]);
const TEAM_KEYS = new Set(["id", "name", "owner"]);
const CHECK_KEYS = new Set(["id", "label", "tool", "inputs"]);
const RULE_KEYS = new Set(["id", "label", "team", "when"]);
const LIST_CONDS: Record<string, string> = { hq_country_in: "hq_country", states_any: "states", industries_any: "industries", company_size_in: "company_size" };
const NUM_CONDS: Record<string, string> = { employee_count_min: "employee_count", employee_count_max: "employee_count" };
const PROFILE_ORDER = ["hq_country_in", "states_any", "industries_any", "company_size_in", "employee_count_min", "employee_count_max"];
const SIZE_CONDS = new Set(["company_size_in", "employee_count_min", "employee_count_max"]);
const COND_KEYS = new Set([...PROFILE_ORDER, "decisions"]);
const BAND_RANGE = /^\s*([\d,]+)\s*-\s*([\d,]+)\s*$/;
const BAND_PLUS = /^\s*([\d,]+)\s*\+\s*$/;
const ID_RULE = "must start with a lower-case letter and use only lower-case letters, digits or underscores, up to 40 characters.";

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x);
const isInt = (x: unknown): x is number => typeof x === "number" && Number.isInteger(x);

function unknownKeys(obj: Record<string, unknown>, allowed: Set<string>, where: string): void {
  const extra = Object.keys(obj).filter((k) => !allowed.has(k)).sort();
  if (extra.length) throw new ConfigError(`${where} has unknown key(s): ${extra.join(", ")}`);
}

function loadWhen(when: unknown, where: string, checkIds: Set<string>): Json {
  if (!isObj(when) || !Object.keys(when).length) throw new ConfigError(`${where} must be an object with at least one condition.`);
  unknownKeys(when, COND_KEYS, where);
  const out: Json = {};
  for (const key of PROFILE_ORDER) {
    if (!(key in when)) continue;
    const value = when[key];
    if (key in LIST_CONDS) {
      if (!Array.isArray(value) || !value.length || !value.every((v) => typeof v === "string" && v.trim())) {
        throw new ConfigError(`${where}.${key} must be a non-empty list of text values.`);
      }
    } else if (!isInt(value) || value < 0) {
      throw new ConfigError(`${where}.${key} must be a whole number, 0 or more.`);
    }
    out[key] = value;
  }
  if ("employee_count_min" in out && "employee_count_max" in out && out.employee_count_min > out.employee_count_max) {
    throw new ConfigError(`${where}.employee_count_min is more than employee_count_max.`);
  }
  if ("decisions" in when) {
    const dec = when.decisions;
    if (!isObj(dec) || !Object.keys(dec).length) throw new ConfigError(`${where}.decisions must be an object that maps a decision check id to yes or no.`);
    for (const [cid, want] of Object.entries(dec)) {
      if (!checkIds.has(cid)) throw new ConfigError(`${where}.decisions names ${pyRepr(cid)}, which is not a decision_checks id.`);
      if (want !== "yes" && want !== "no") throw new ConfigError(`${where}.decisions.${cid} must be yes or no.`);
    }
    out.decisions = { ...dec };
  }
  return out;
}

export function loadConfig(raw: Json): Json {
  unknownKeys(raw, TOP_KEYS, "The configuration");
  const teamsRaw = raw.teams;
  if (!Array.isArray(teamsRaw) || !teamsRaw.length) throw new ConfigError("teams must be a non-empty list.");
  const teams: Json[] = [];
  const teamIds = new Set<string>();
  teamsRaw.forEach((t: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(t)) throw new ConfigError(`teams[${i}] must be an object.`);
    unknownKeys(t, TEAM_KEYS, `teams[${i}]`);
    const tid = t.id;
    if (typeof tid !== "string" || !ID.test(tid)) throw new ConfigError(`teams[${i}].id ${ID_RULE}`);
    if (teamIds.has(tid)) throw new ConfigError(`teams[${i}].id ${pyRepr(tid)} is used twice.`);
    teamIds.add(tid);
    const name = t.name ?? tid;
    if (typeof name !== "string" || !name.trim()) throw new ConfigError(`teams[${i}].name must be text.`);
    const owner = t.owner ?? null;
    if (owner !== null && typeof owner !== "string") throw new ConfigError(`teams[${i}].owner must be text or null.`);
    teams.push({ id: tid, name, owner });
  });

  const checksRaw = raw.decision_checks ?? [];
  if (!Array.isArray(checksRaw)) throw new ConfigError("decision_checks must be a list.");
  const checks: Json[] = [];
  const checkIds = new Set<string>();
  checksRaw.forEach((c: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(c)) throw new ConfigError(`decision_checks[${i}] must be an object.`);
    unknownKeys(c, CHECK_KEYS, `decision_checks[${i}]`);
    const cid = c.id;
    if (typeof cid !== "string" || !ID.test(cid)) throw new ConfigError(`decision_checks[${i}].id ${ID_RULE}`);
    if (checkIds.has(cid)) throw new ConfigError(`decision_checks[${i}].id ${pyRepr(cid)} is used twice.`);
    checkIds.add(cid);
    const tool = c.tool;
    if (typeof tool !== "string" || !tool) throw new ConfigError(`decision_checks[${i}].tool must name a decision tool.`);
    const inputs = c.inputs ?? {};
    if (!isObj(inputs)) throw new ConfigError(`decision_checks[${i}].inputs must be an object.`);
    const label = c.label ?? cid;
    if (typeof label !== "string") throw new ConfigError(`decision_checks[${i}].label must be text.`);
    checks.push({ id: cid, label, tool, inputs });
  });

  const rulesRaw = raw.rules;
  if (!Array.isArray(rulesRaw) || !rulesRaw.length) throw new ConfigError("rules must be a non-empty list.");
  const rules: Json[] = [];
  const ruleIds = new Set<string>();
  const usedChecks = new Set<string>();
  rulesRaw.forEach((r: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(r)) throw new ConfigError(`rules[${i}] must be an object.`);
    unknownKeys(r, RULE_KEYS, `rules[${i}]`);
    const rid = r.id;
    if (typeof rid !== "string" || !ID.test(rid)) throw new ConfigError(`rules[${i}].id ${ID_RULE}`);
    if (ruleIds.has(rid)) throw new ConfigError(`rules[${i}].id ${pyRepr(rid)} is used twice.`);
    ruleIds.add(rid);
    const team = r.team;
    if (typeof team !== "string" || !teamIds.has(team)) throw new ConfigError(`rules[${i}].team must be one of the team ids.`);
    const label = r.label ?? rid;
    if (typeof label !== "string") throw new ConfigError(`rules[${i}].label must be text.`);
    const when = loadWhen(r.when, `rules[${i}].when`, checkIds);
    for (const cid of Object.keys(when.decisions ?? {})) usedChecks.add(cid);
    rules.push({ id: rid, label, team, when });
  });
  for (const c of checks) {
    if (!usedChecks.has(c.id)) throw new ConfigError(`decision_checks id ${pyRepr(c.id)} is not used by any rule.`);
  }

  const overlap = raw.overlap ?? "first_match";
  if (overlap !== "first_match" && overlap !== "review") throw new ConfigError("overlap must be first_match or review.");
  const defaultTeam = raw.default_team ?? null;
  if (defaultTeam !== null && !(typeof defaultTeam === "string" && teamIds.has(defaultTeam))) {
    throw new ConfigError("default_team must be null or one of the team ids.");
  }
  const withReasoning = raw.with_reasoning ?? true;
  if (typeof withReasoning !== "boolean") throw new ConfigError("with_reasoning must be true or false.");
  return { with_reasoning: withReasoning, teams, decision_checks: checks, rules, overlap, default_team: defaultTeam };
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
  return [null, null, `${pyRepr(row.domain ?? null)} is not a valid domain.`];
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
  const calls: PlannedCall[] = [];
  const checks = config.decision_checks.length;
  if (checks) calls.push(new PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, []));
  calls.push(new PlannedCall("GET /company", "Company profile for each unique valid identifier", n,
    [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  if (checks) {
    calls.push(new PlannedCall("POST /decisions", `Up to ${checks} decision check(s) for each unique valid identifier`,
      n * checks, pricing.estimateDecision(n * checks, config.with_reasoning)));
  }
  return calls;
}

const norm = (s: string) => s.trim().toLowerCase();

function band(value: unknown): [number, number | null] | null {
  if (typeof value !== "string") return null;
  let m = BAND_RANGE.exec(value);
  if (m) return [parseInt(m[1].replace(/,/g, ""), 10), parseInt(m[2].replace(/,/g, ""), 10)];
  m = BAND_PLUS.exec(value);
  if (m) return [parseInt(m[1].replace(/,/g, ""), 10), null];
  return null;
}

function sizeConflict(f: Json): string | null {
  const count = f.employee_count;
  const b = band(f.company_size);
  if (!isInt(count) || b === null) return null;
  const [low, high] = b;
  if (count < low || (high !== null && count > high)) {
    return `employee_count ${count} is outside the company_size band ${f.company_size}. ` +
      "employee_count counts staff in the requested country region, so the two can differ " +
      "legitimately. This recipe does not correct either value.";
  }
  return null;
}

function profileCondition(key: string, expected: Json, f: Json): Json {
  let field: string;
  let observed: Json;
  let result: string;
  if (key in LIST_CONDS) {
    field = LIST_CONDS[key];
    observed = f[field] ?? null;
    const values = (Array.isArray(observed) ? observed : [observed]).filter((v: unknown) => typeof v === "string" && v.trim());
    if (!values.length) {
      result = "unknown";
    } else {
      const wanted = new Set((expected as string[]).map(norm));
      result = values.some((v: string) => wanted.has(norm(v))) ? "met" : "not_met";
    }
  } else {
    field = NUM_CONDS[key];
    observed = f[field] ?? null;
    if (typeof observed !== "number") result = "unknown";
    else if (key === "employee_count_min") result = observed >= expected ? "met" : "not_met";
    else result = observed <= expected ? "met" : "not_met";
  }
  return { condition: key, field, expected, observed, result };
}

function outcomeOf(conditions: Json[]): string {
  const results = conditions.map((c) => c.result);
  if (results.includes("not_met")) return "no_match";
  if (results.includes("error") || results.includes("not_asked")) return "error";
  if (results.includes("unknown")) return "unknown";
  return "match";
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const teams: Record<string, Json> = Object.fromEntries(cfg.teams.map((t: Json) => [t.id, t]));
  const checks: Record<string, Json> = Object.fromEntries(cfg.decision_checks.map((c: Json) => [c.id, c]));
  const coverage: string[] = [
    "An unknown condition means the company profile or Decisions did not hold the fact. It is never " +
    "treated as not met. A record whose route depends on an unknown goes to the review queue.",
    "Assignments are written to this run's output files only. Nothing is written to a CRM.",
    `Overlap handling: ${cfg.overlap}.`,
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length }, team_counts: {}, rule_match_counts: {}, review_queue: [] }, coverage };
  };

  if (cfg.decision_checks.length) {
    let catalogue: Json;
    try {
      catalogue = await client.listDecisionTools();
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      ctx.recordError(null, e);
      return stopAll("The decision tool list could not be read, so no rows were routed.");
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

  const keyed = ctx.rows.map((r) => ({ ...r, _key: rowKey(r) })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier company identifier; their API responses were reused, not requested again.`);

  const teamName = (tid: string): string => teams[tid].name;

  const one = async (row: Row): Promise<Json> => {
    const [by, value, problem] = lookup(row);
    const base: Json = {
      row_id: row.row_id, input: row, lookup: by ? { by, value } : null, duplicate_of: dupes[row.row_id] ?? null,
      company: null, decisions: [], rules: [], matched_rules: [], team_id: null, team_name: null, owner: null,
      candidate_teams: [], review_reasons: [], data_conflicts: [],
    };
    if (!by) return { ...base, status: "invalid_input", status_reason: problem };
    let company: Json;
    try {
      company = await client.getCompany({ [by]: value as string });
    } catch (e) {
      if (e instanceof NotFoundError) {
        return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this identifier, so no rules were evaluated." };
      }
      throw e;
    }
    const f: Json = facts.companyFacts(company) ?? {};
    f.states = company.states ?? null;
    base.company = f;

    let identifierConflict: string | null = null;
    const inputDomain = normaliseDomain(row.domain);
    const profileDomain = normaliseDomain(typeof company.fqdn === "string" ? company.fqdn : null);
    if (inputDomain && profileDomain && inputDomain !== profileDomain) {
      identifierConflict = `The input domain ${inputDomain} differs from the profile's fqdn ${profileDomain}.`;
    }
    const sizeProblem = sizeConflict(f);
    base.data_conflicts = [identifierConflict, sizeProblem].filter((c) => c);

    const answers = new Map<string, Json>();
    const ask = async (cid: string): Promise<Json> => {
      if (answers.has(cid)) return answers.get(cid);
      const c = checks[cid];
      const entry = { check_id: cid, label: c.label, tool: c.tool };
      let rec: Json;
      if (ctx.stopped !== null) {
        rec = { ...entry, asked: false, answer: null, error: null };
      } else {
        try {
          const resp = await client.makeDecision(c.tool, { company_id: company.id, ...c.inputs }, { withReasoning: cfg.with_reasoning });
          const ans = decisions.answerFrom(resp);
          const bad = !["yes", "no", "unknown"].includes(ans.answer);
          rec = { ...entry, asked: true, ...ans, error: bad ? ans.answer_error : null };
        } catch (e) {
          if (!(e instanceof FirmableError)) throw e;
          rec = { ...entry, asked: true, answer: null, error: ctx.recordError(row.row_id, e) };
        }
      }
      answers.set(cid, rec);
      return rec;
    };

    const evaluated: Json[] = [];
    let settled = false;
    for (const rule of cfg.rules) {
      const info = { rule_id: rule.id, label: rule.label, team_id: rule.team };
      if (settled) { evaluated.push({ ...info, outcome: "not_evaluated", conditions: [] }); continue; }
      const when = rule.when;
      const conds: Json[] = PROFILE_ORDER.filter((k) => k in when).map((k) => profileCondition(k, when[k], f));
      const profileFailed = conds.some((c) => c.result === "not_met");
      for (const [cid, want] of Object.entries(when.decisions ?? {}) as [string, string][]) {
        const cond: Json = { condition: "decision", field: cid, expected: want, observed: null };
        if (profileFailed) { conds.push({ ...cond, result: "skipped" }); continue; }
        const rec = await ask(cid);
        if (!rec.asked) { conds.push({ ...cond, result: "not_asked" }); continue; }
        cond.observed = rec.answer;
        const cmp = rec.error === null ? decisions.compare(rec.answer, want) : "error";
        conds.push({ ...cond, result: cmp === "pass" ? "met" : cmp === "fail" ? "not_met" : cmp });
      }
      const outcome = outcomeOf(conds);
      evaluated.push({ ...info, outcome, conditions: conds });
      if (cfg.overlap === "first_match" && outcome !== "no_match") settled = true;
    }

    const matched = evaluated.filter((r) => r.outcome === "match");
    const matchTeams: string[] = [];
    for (const r of matched) if (!matchTeams.includes(r.team_id)) matchTeams.push(r.team_id);
    const unsettled = evaluated.filter((r) => (r.outcome === "unknown" || r.outcome === "error") &&
      !(matchTeams.length === 1 && r.team_id === matchTeams[0]));
    const candidates = [...matchTeams];
    for (const r of unsettled) if (!candidates.includes(r.team_id)) candidates.push(r.team_id);

    const labels = (items: Json[]) => items.map((r) => r.label).join(", ");
    const reasons: [string, string][] = [];
    if (matchTeams.length > 1) {
      reasons.push(["review", `Matching rules route to different teams: ${matched.map((r) => `${r.label} (${teamName(r.team_id)})`).join(", ")}.`]);
    }
    const errored = unsettled.filter((r) => r.outcome === "error");
    if (errored.length) {
      reasons.push(["incomplete", `Rule(s) that could change the route have an error or were not asked because the run stopped: ${labels(errored)}.`]);
    }
    const unknown = unsettled.filter((r) => r.outcome === "unknown");
    if (unknown.length) {
      reasons.push(["review", `Rule(s) that could change the route depend on facts Firmable did not return: ${labels(unknown)}. Unknown is not treated as not met.`]);
    }
    if (identifierConflict) reasons.push(["review", identifierConflict]);
    const sizeRules = matched.filter((r) => r.conditions.some((c: Json) => SIZE_CONDS.has(c.condition)));
    if (sizeProblem && sizeRules.length) {
      reasons.push(["review", `Matching rule(s) use company size, but the profile's size facts disagree: ${labels(sizeRules)}.`]);
    }
    const out: Json = { ...base, decisions: [...answers.values()], rules: evaluated, matched_rules: matched.map((r) => r.rule_id),
      candidate_teams: candidates, review_reasons: reasons.map(([, text]) => text) };
    if (reasons.length) return { ...out, status: reasons[0][0], status_reason: reasons[0][1] };
    if (matchTeams.length) {
      const tid = matchTeams[0];
      let reason = `Rule ${matched[0].label} matched; routed to ${teamName(tid)}.`;
      if (matched.length > 1) reason += ` ${matched.length - 1} other matching rule(s) route to the same team.`;
      return { ...out, status: "assigned", status_reason: reason, team_id: tid, team_name: teamName(tid), owner: teams[tid].owner };
    }
    if (cfg.default_team) {
      const tid = cfg.default_team;
      return { ...out, status: "assigned_default", candidate_teams: [],
        status_reason: `No rule matched; routed to the default team, ${teamName(tid)}.`,
        team_id: tid, team_name: teamName(tid), owner: teams[tid].owner };
    }
    return { ...out, status: "unassigned", status_reason: "No rule matched and no default team is configured." };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  const teamCounts: Record<string, number> = {};
  const ruleCounts: Record<string, number> = Object.fromEntries(cfg.rules.map((r: Json) => [r.id, 0]));
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    if (r.team_id) teamCounts[r.team_id] = (teamCounts[r.team_id] ?? 0) + 1;
    for (const rid of r.matched_rules ?? []) ruleCounts[rid] += 1;
  }
  const conflicts = rows.filter((r) => (r.data_conflicts ?? []).length).length;
  if (conflicts) {
    coverage.push(`${conflicts} row(s) have company data that disagrees with the input or with itself. ` +
      "They are listed under data conflicts. This recipe reports them and does not correct upstream data.");
  }
  const queue = rows.filter((r) => REVIEW_QUEUE.has(r.status)).map((r) => r.row_id);
  return { rows, summary: { status_counts: counts, team_counts: teamCounts, rule_match_counts: ruleCounts, review_queue: queue }, coverage };
}

export const CSV_COLUMNS = ["row_id", "input_domain", "input_company_id", "duplicate_of", "status", "status_reason",
  "review_queue", "team_id", "team_name", "owner", "matched_rules", "candidate_teams", "review_reasons", "company_id",
  "company_name", "hq_country", "states", "employee_count", "company_size", "industries", "data_conflicts", "rule_results"];

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const company = r.company ?? {};
    return {
      row_id: r.row_id, input_domain: r.input.domain ?? null, input_company_id: r.input.company_id ?? null,
      duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      review_queue: REVIEW_QUEUE.has(r.status), team_id: r.team_id ?? null, team_name: r.team_name ?? null,
      owner: r.owner ?? null, matched_rules: r.matched_rules ?? [], candidate_teams: r.candidate_teams ?? [],
      review_reasons: r.review_reasons ?? [], company_id: company.id ?? null, company_name: company.name ?? null,
      hq_country: company.hq_country ?? null, states: Array.isArray(company.states) ? company.states : [],
      employee_count: company.employee_count ?? null, company_size: company.company_size ?? null,
      industries: Array.isArray(company.industries) ? company.industries : [],
      data_conflicts: r.data_conflicts ?? [],
      rule_results: (r.rules ?? []).map((x: Json) => `${x.rule_id}=${x.outcome}`),
    };
  });
}

function condText(c: Json): string {
  let observed = c.observed;
  if (Array.isArray(observed)) observed = observed.map(String).join(", ") || "none";
  let expected = c.expected;
  if (Array.isArray(expected)) expected = expected.join(", ");
  const seen = observed === null || observed === undefined ? "not returned" : observed;
  return `${c.condition} ${c.field}: wanted ${expected}, observed ${seen}, ${c.result.replace(/_/g, " ")}`;
}

export function render(report: Report, result: Json): void {
  const summary = result.summary;
  const counts: Record<string, number> = summary.status_counts ?? {};
  let body = "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>";
  const teams: Record<string, Json> = Object.fromEntries((result.input.config.teams ?? []).map((t: Json) => [t.id, t]));
  if (Object.keys(summary.team_counts ?? {}).length) {
    body += table(["Team", "Owner", "Accounts"], Object.entries(summary.team_counts).map(([k, v]) => [teams[k]?.name ?? k, teams[k]?.owner ?? null, v]));
  }
  if (Object.keys(summary.rule_match_counts ?? {}).length) {
    body += table(["Rule", "Rows matched"], Object.entries(summary.rule_match_counts).map(([k, v]) => [k, v]));
  }
  body += note("Unknown means the company profile or Decisions did not hold the fact. A record whose route " +
    "depends on an unknown goes to the <strong>review queue</strong>, never to a team by default.", "unknown");
  report.add("Result", "Routing summary", body);

  const queue = result.rows.filter((r: Json) => REVIEW_QUEUE.has(r.status));
  if (queue.length) {
    report.add("Review queue", "Records a person needs to route", table(["Row", "Company", "Status", "Reason", "Candidate teams"],
      queue.map((r: Json) => [r.row_id, r.company?.name || r.input.domain || r.input.company_id, r.status.replace(/_/g, " "),
        r.status_reason || r.reason || r.error?.message, (r.candidate_teams ?? []).join(", ") || null])));
  }

  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.input.domain || r.input.company_id || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    if (r.team_name) left += kv([["Team", r.team_name], ["Owner", r.owner]]);
    const company = r.company;
    if (company) {
      left += kv([["Firmable ID", company.id], ["Domain", company.fqdn], ["Head office country", company.hq_country],
        ["States", (company.states ?? []).join(", ") || null], ["Employee count", company.employee_count],
        ["Size", company.company_size], ["Industries", (company.industries ?? []).join(", ") || null]]);
    }
    if ((r.data_conflicts ?? []).length) left += "<h3>Data conflicts</h3>" + bulletList(r.data_conflicts);
    if ((r.review_reasons ?? []).length > 1) left += "<h3>All review reasons</h3>" + bulletList(r.review_reasons);
    if (r.duplicate_of) left += note(`Same identifier as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    const rules: Json[] = r.rules ?? [];
    if (rules.length) {
      right += table(["Rule", "Team", "Outcome", "Conditions"], rules.map((x) => [
        x.label, teams[x.team_id]?.name ?? x.team_id,
        new Raw(badge(x.outcome.replace(/_/g, " "), OUTCOME_TONE[x.outcome] ?? "neutral")),
        new Raw(bulletList(x.conditions.map(condText)) || '<span class="muted">Not evaluated</span>')]));
    }
    for (const d of r.decisions ?? []) {
      const rs = d.reasoning ?? {};
      right += `<p><strong>${esc(d.label)}</strong> (${esc(d.tool)}): ${esc(d.answer || (!d.asked ? "not asked" : "no answer"))}` +
        (d.confidence !== null && d.confidence !== undefined ? `, confidence ${esc(d.confidence)}` : "") + "</p>";
      if (rs.reason) {
        right += `<p>${esc(rs.reason)}</p><p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.` +
          (rs.would_change_if ? ` Would change if: ${esc(rs.would_change_if)}.` : "") + "</p>";
      }
      if (d.error) right += note(`${esc(d.label)}: ${esc(isObj(d.error) ? d.error.message : d.error)}`, "error");
    }
    if (rules.length) right += detailsJson("Rule results as JSON", rules);
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No rules were evaluated for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its routing", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
