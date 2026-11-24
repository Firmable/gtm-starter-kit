/**
 * Inbound lead router: routes an inbound lead to an owner or queue with ordered, explicit rules.
 * Mirrors recipes/inbound-lead-router/python/src/inbound_lead_router/recipe.py; see that file and the
 * recipe README for the condition, rule and status definitions. Nothing is written to a CRM.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "inbound-lead-router";
export const TITLE = "Inbound lead router";
export const DESCRIPTION = "Routes each inbound lead from a work email or domain to an owner or queue by applying ordered " +
  "rules to the Firmable company profile, an optional ICP fit answer and an optional person " +
  "profile. Leads the rules cannot settle go to a review queue. Nothing is written to a CRM.";

export const ICP_TOOL = "is_icp_fit";
const STATUS_TONE: Record<string, string> = {
  routed: "good", routed_default: "good", needs_review: "review", personal_email: "neutral",
  company_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const OUTCOME_TONE: Record<string, string> = { matched: "good", not_matched: "neutral", skipped_missing_fact: "unknown", error: "error" };
export const REVIEW_STATUSES = new Set(["needs_review", "company_not_found", "invalid_input", "error"]);

const ID = /^[a-z][a-z0-9_]{0,39}$/;
const EMAIL = /^[^@\s]+@[^@\s]+$/;
const TOP_KEYS = new Set(["personal_email_domains", "icp", "fetch_person", "rules", "review_queue", "default_queue",
  "personal_email_queue", "with_reasoning"]);
const QUEUE_KEYS = ["review_queue", "default_queue", "personal_email_queue"];
const RULE_KEYS = new Set(["id", "queue", "when"]);
const COND_ORDER = ["hq_country_in", "employee_count_min", "employee_count_max", "industries_any", "icp_fit", "seniority_in"];
const COND_FIELD: Record<string, string> = {
  hq_country_in: "hq_country", employee_count_min: "employee_count", employee_count_max: "employee_count",
  industries_any: "industries", icp_fit: "icp_fit", seniority_in: "seniority",
};
const LIST_CONDS = new Set(["hq_country_in", "industries_any", "seniority_in"]);
const ICP_ORDER = ["min_staff", "max_staff", "staff_in", "industries", "countries", "revenue_bands", "criteria"];
const STAFF_IN = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
const REVENUE_BANDS = ["<$1M", "$1M - $5M", "$5M - $10M", "$10M - $50M", "$50M - $100M", "$100M - $250M",
  "$250M - $500M", "$500M - $1B", ">$1B+"];
const ID_RULE = "must start with a lower-case letter and use only lower-case letters, digits or underscores, up to 40 characters.";

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x);
const isInt = (x: unknown): x is number => typeof x === "number" && Number.isInteger(x);
const textList = (v: unknown): v is string[] =>
  Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === "string" && x.trim() !== "");
const chars = (s: string): number => [...s].length;

function unknownKeys(obj: Record<string, unknown>, allowed: Set<string>, where: string): void {
  const extra = Object.keys(obj).filter((k) => !allowed.has(k)).sort();
  if (extra.length) throw new ConfigError(`${where} has unknown key(s): ${extra.join(", ")}`);
}

function loadIcp(icp: unknown): Json {
  if (!isObj(icp)) throw new ConfigError("icp must be an object.");
  if ("description" in icp) throw new ConfigError("icp.description is not accepted by is_icp_fit. Give icp.criteria instead.");
  unknownKeys(icp, new Set(ICP_ORDER), "icp");
  const out: Json = {};
  for (const key of ICP_ORDER) {
    if (!(key in icp) || icp[key] === null || icp[key] === undefined) continue;
    const value = icp[key];
    if (key === "min_staff" || key === "max_staff") {
      if (!isInt(value) || value < 0) throw new ConfigError(`icp.${key} must be a whole number, 0 or more.`);
    } else if (key === "staff_in") {
      if (!STAFF_IN.includes(value)) throw new ConfigError(`icp.staff_in must be one of: ${STAFF_IN.join(", ")}.`);
    } else if (key === "criteria") {
      if (!Array.isArray(value) || !value.length || value.length > 10 ||
        !value.every((c) => typeof c === "string" && chars(c.trim()) >= 2)) {
        throw new ConfigError("icp.criteria must be a list of 1 to 10 text items, each at least 2 characters.");
      }
      if (value.reduce((s: number, c: string) => s + chars(c), 0) > 2000) {
        throw new ConfigError("icp.criteria must be 2,000 characters or fewer in all.");
      }
    } else {
      if (!textList(value)) throw new ConfigError(`icp.${key} must be a non-empty list of text values.`);
      if (key === "revenue_bands") {
        for (const band of value) {
          if (!REVENUE_BANDS.includes(band)) throw new ConfigError(`icp.revenue_bands has ${pyRepr(band)}, which is not a Firmable revenue band.`);
        }
      }
    }
    out[key] = value;
  }
  if ("min_staff" in out && "max_staff" in out && out.min_staff > out.max_staff) {
    throw new ConfigError("icp.min_staff is more than icp.max_staff.");
  }
  if (!Object.keys(out).length) throw new ConfigError("icp must hold at least one filter or criterion.");
  return out;
}

function loadWhen(when: unknown, where: string, hasIcp: boolean, fetchPerson: boolean): Json {
  if (!isObj(when) || !Object.keys(when).length) throw new ConfigError(`${where} must be an object with at least one condition.`);
  unknownKeys(when, new Set(COND_ORDER), where);
  const out: Json = {};
  for (const key of COND_ORDER) {
    if (!(key in when)) continue;
    const value = when[key];
    if (LIST_CONDS.has(key)) {
      if (!textList(value)) throw new ConfigError(`${where}.${key} must be a non-empty list of text values.`);
      if (key === "seniority_in" && !fetchPerson) throw new ConfigError(`${where}.seniority_in needs fetch_person set to true.`);
    } else if (key === "icp_fit") {
      if (value !== "yes" && value !== "no" && value !== "unknown") throw new ConfigError(`${where}.icp_fit must be yes, no or unknown.`);
      if (!hasIcp) throw new ConfigError(`${where}.icp_fit needs icp in the configuration.`);
    } else if (!isInt(value) || value < 0) {
      throw new ConfigError(`${where}.${key} must be a whole number, 0 or more.`);
    }
    out[key] = value;
  }
  if ("employee_count_min" in out && "employee_count_max" in out && out.employee_count_min > out.employee_count_max) {
    throw new ConfigError(`${where}.employee_count_min is more than employee_count_max.`);
  }
  return out;
}

export function loadConfig(raw: Json): Json {
  unknownKeys(raw, TOP_KEYS, "The configuration");
  const queues: Json = {};
  for (const key of QUEUE_KEYS) {
    const value = raw[key];
    if (typeof value !== "string" || !value.trim()) throw new ConfigError(`${key} must be a non-empty queue name.`);
    queues[key] = value;
  }
  const personalRaw = raw.personal_email_domains ?? [];
  if (!Array.isArray(personalRaw)) throw new ConfigError("personal_email_domains must be a list of domains.");
  const personal: string[] = [];
  personalRaw.forEach((d: unknown, idx: number) => {
    const nd = typeof d === "string" ? normaliseDomain(d) : null;
    if (!nd) throw new ConfigError(`personal_email_domains[${idx + 1}] is not a valid domain.`);
    if (!personal.includes(nd)) personal.push(nd);
  });
  const icp = raw.icp !== null && raw.icp !== undefined ? loadIcp(raw.icp) : null;
  const fetchPerson = raw.fetch_person ?? false;
  if (typeof fetchPerson !== "boolean") throw new ConfigError("fetch_person must be true or false.");
  const withReasoning = raw.with_reasoning ?? true;
  if (typeof withReasoning !== "boolean") throw new ConfigError("with_reasoning must be true or false.");

  const rulesRaw = raw.rules;
  if (!Array.isArray(rulesRaw) || !rulesRaw.length) throw new ConfigError("rules must be a non-empty list.");
  const rules: Json[] = [];
  const ruleIds = new Set<string>();
  rulesRaw.forEach((r: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(r)) throw new ConfigError(`rules[${i}] must be an object.`);
    unknownKeys(r, RULE_KEYS, `rules[${i}]`);
    const rid = r.id;
    if (typeof rid !== "string" || !ID.test(rid)) throw new ConfigError(`rules[${i}].id ${ID_RULE}`);
    if (ruleIds.has(rid)) throw new ConfigError(`rules[${i}].id ${pyRepr(rid)} is used twice.`);
    ruleIds.add(rid);
    const queue = r.queue;
    if (typeof queue !== "string" || !queue.trim()) throw new ConfigError(`rules[${i}].queue must be a non-empty queue name.`);
    const when = loadWhen(r.when, `rules[${i}].when`, icp !== null, fetchPerson);
    rules.push({ id: rid, queue, when });
  });
  return { personal_email_domains: personal, icp, fetch_person: fetchPerson, rules, ...queues, with_reasoning: withReasoning };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ["work_email", "domain"] });
}

/** Returns [identifier, problem]. problem explains an invalid work email or domain. */
export function identify(row: Row, personal: string[]): [Json | null, string | null] {
  const rawEmail = (row.work_email ?? "").trim();
  const rawDomain = (row.domain ?? "").trim();
  let email: string | null = null;
  let emailDomain: string | null = null;
  if (rawEmail) {
    if (!EMAIL.test(rawEmail)) return [null, `work_email ${pyRepr(rawEmail)} is not an email address.`];
    emailDomain = normaliseDomain(rawEmail.slice(rawEmail.lastIndexOf("@") + 1));
    if (!emailDomain) return [null, `work_email ${pyRepr(rawEmail)} does not end in a valid domain.`];
    email = rawEmail.toLowerCase();
  }
  let given: string | null = null;
  if (rawDomain) {
    given = normaliseDomain(rawDomain);
    if (!given) return [null, `domain ${pyRepr(rawDomain)} is not a valid domain.`];
  }
  const domain = (given || emailDomain) as string;
  return [{
    work_email: email, email_domain: emailDomain, given_domain: given, domain,
    domain_source: given ? "domain" : "work_email",
    work_email_personal: emailDomain ? personal.includes(emailDomain) : false,
    domain_personal: personal.includes(domain),
  }, null];
}

function rowKey(row: Row, personal: string[]): string {
  const [ident] = identify(row, personal);
  return ident ? `${ident.work_email ?? ""}|${ident.domain}` : "";
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const domains: string[] = [];
  const emails: string[] = [];
  for (const r of rows) {
    const [ident] = identify(r, config.personal_email_domains);
    if (!ident || ident.domain_personal) continue;
    if (!domains.includes(ident.domain)) domains.push(ident.domain);
    if (ident.work_email && !ident.work_email_personal && !emails.includes(ident.work_email)) emails.push(ident.work_email);
  }
  const n = domains.length;
  const calls = [new PlannedCall("GET /company", "Company profile for each unique business domain", n,
    [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]])];
  if (config.icp !== null) {
    calls.push(new PlannedCall("POST /decisions", "is_icp_fit for each unique business domain", n,
      pricing.estimateDecision(n, config.with_reasoning)));
  }
  if (config.fetch_person) {
    const m = emails.length;
    calls.push(new PlannedCall("GET /people", "Person profile for each unique work email", m,
      [["person", pricing.round4(pricing.PERSON_PROFILE * m)]]));
  }
  return calls;
}

const norm = (s: string) => s.trim().toLowerCase();

function text(v: unknown): string {
  if (v === null || v === undefined) return "not returned";
  if (Array.isArray(v)) return v.map(text).join(", ") || "none";
  return String(v);
}

function condition(key: string, expected: Json, company: Json, icp: Json | null, person: Json | null): Json {
  const field = COND_FIELD[key];
  let observed: Json = null;
  let result: string;
  if (key === "hq_country_in" || key === "industries_any") {
    observed = company[field] ?? null;
    const values = (Array.isArray(observed) ? observed : [observed]).filter((v: unknown) => typeof v === "string" && v.trim());
    if (!values.length) {
      result = "missing";
    } else {
      const wanted = new Set((expected as string[]).map(norm));
      result = values.some((v: string) => wanted.has(norm(v))) ? "met" : "not_met";
    }
  } else if (key === "employee_count_min" || key === "employee_count_max") {
    observed = company[field] ?? null;
    if (typeof observed !== "number") result = "missing";
    else if (key === "employee_count_min") result = observed >= expected ? "met" : "not_met";
    else result = observed <= expected ? "met" : "not_met";
  } else if (key === "icp_fit") {
    const rec = icp ?? {};
    observed = rec.answer ?? null;
    if (!rec.asked) result = "not_asked";
    else if (rec.error !== null && rec.error !== undefined) result = "error";
    else if (observed === expected) result = "met";
    else if (observed === "unknown") result = "missing";
    else result = "not_met";
  } else {
    const rec = person ?? {};
    observed = (rec.facts ?? {}).seniority ?? null;
    if (rec.status === "error") result = "error";
    else if (rec.status === "not_asked") result = "not_asked";
    else if (rec.status === "no_work_email" || rec.status === "personal_email") result = "no_person";
    else if (typeof observed !== "string" || !observed.trim()) result = "missing";
    else {
      const wanted = new Set((expected as string[]).map(norm));
      result = wanted.has(norm(observed)) ? "met" : "not_met";
    }
  }
  return { condition: key, field, expected, observed, result };
}

const missingLabel = (c: Json): string => c.condition === "icp_fit" ? "icp_fit (is_icp_fit answered unknown)" : c.field;

function evaluate(rule: Json, company: Json, icp: Json | null, person: Json | null): Json {
  const conds = COND_ORDER.filter((k) => k in rule.when).map((k) => condition(k, rule.when[k], company, icp, person));
  const results = conds.map((c) => c.result);
  let failing: string | null = null;
  let missing: string[] = [];
  let outcome: string;
  let reason: string;
  if (results.includes("not_met") || results.includes("no_person")) {
    const first = conds.find((c) => c.result === "not_met" || c.result === "no_person") as Json;
    failing = first.condition;
    outcome = "not_matched";
    reason = first.result === "no_person"
      ? `${first.condition} does not apply: the lead has no business work email, so there is no person to check.`
      : `${first.condition} not met: wanted ${text(first.expected)}, observed ${text(first.observed)}.`;
  } else if (results.includes("error") || results.includes("not_asked")) {
    const bad = conds.filter((c) => c.result === "error" || c.result === "not_asked").map((c) => c.field);
    outcome = "error";
    reason = `Not evaluated: the request for ${bad.join(", ")} failed or was not made.`;
  } else if (results.includes("missing")) {
    missing = conds.filter((c) => c.result === "missing").map(missingLabel);
    outcome = "skipped_missing_fact";
    reason = `Skipped because these facts were missing: ${missing.join(", ")}.`;
  } else {
    outcome = "matched";
    reason = "All conditions met.";
  }
  return { rule_id: rule.id, queue: rule.queue, outcome, failing_condition: failing, missing_facts: missing, reason, conditions: conds };
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const personal: string[] = cfg.personal_email_domains;
  const coverage: string[] = [
    "Rules are read in order. The first rule that is not ruled out decides the lead.",
    "A condition on a missing fact does not match. When the deciding rule was skipped for a missing fact, " +
    "the lead goes to the review queue, because that rule might have matched.",
    "Personal email domains are matched against the configured personal_email_domains list only.",
    "Routes are written to this run's output files only. Nothing is written to a CRM.",
  ];
  if (cfg.icp !== null) {
    coverage.push("is_icp_fit uses a decision model inside Firmable's service (uses_decision_model is true " +
      "in the tool list read on 24 Nov 2026). This code applies only the rules in the configuration.");
    coverage.push("An is_icp_fit answer of unknown never matches an icp_fit condition of yes or no. It is never treated as no.");
  }
  if (cfg.fetch_person) {
    coverage.push("Person profiles are looked up by work email only. A lead with no work email, or a work email on " +
      "a personal domain, has no person facts.");
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _key: rowKey(r, personal) })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} lead(s) repeat an earlier work email and domain; identical requests were answered from memory, not sent again.`);

  const one = async (row: Row): Promise<Json> => {
    const rowId = row.row_id;
    const [ident, problem] = identify(row, personal);
    const base: Json = {
      row_id: rowId, input: row, duplicate_of: dupes[rowId] ?? null, identifier: ident, company: null, icp: null,
      person: null, rules: [], matched_rule: null, queue: null, flags: [],
    };
    if (ident === null) return { ...base, status: "invalid_input", status_reason: problem, queue: cfg.review_queue };
    const domain: string = ident.domain;
    const flags: string[] = [];
    if (ident.given_domain && ident.email_domain && ident.given_domain !== ident.email_domain) {
      flags.push(`The given domain ${ident.given_domain} differs from the work email domain ${ident.email_domain}. The given domain was used.`);
    }
    base.flags = flags;
    if (ident.domain_personal) {
      return { ...base, status: "personal_email", queue: cfg.personal_email_queue,
        status_reason: `${domain} is a personal email domain in personal_email_domains, so no API call was made.` };
    }
    if (ident.work_email_personal) {
      flags.push(`The work email uses the personal email domain ${ident.email_domain}. The lead was routed on the given domain.`);
    }
    let company: Json;
    try {
      company = await client.getCompany({ fqdn: domain });
    } catch (e) {
      if (e instanceof NotFoundError) {
        return { ...base, status: "company_not_found", queue: cfg.review_queue,
          status_reason: "Firmable has no company profile for this domain, so no rules were evaluated." };
      }
      throw e;
    }
    const f: Json = facts.companyFacts(company) ?? {};
    base.company = f;
    const profileDomain = normaliseDomain(typeof company.fqdn === "string" ? company.fqdn : null);
    if (profileDomain && profileDomain !== domain) {
      flags.push(`The company profile's fqdn ${profileDomain} differs from the domain looked up, ${domain}.`);
    }

    let icp: Json | null = null;
    if (cfg.icp !== null) {
      if (ctx.stopped !== null) {
        icp = { tool: ICP_TOOL, asked: false, answer: null, error: null };
      } else {
        try {
          const resp = await client.makeDecision(ICP_TOOL, { company_id: company.id, icp: cfg.icp }, { withReasoning: cfg.with_reasoning });
          const ans = decisions.answerFrom(resp);
          const bad = !["yes", "no", "unknown"].includes(ans.answer);
          icp = { tool: ICP_TOOL, asked: true, ...ans, error: bad ? ans.answer_error : null };
        } catch (e) {
          if (!(e instanceof FirmableError)) throw e;
          icp = { tool: ICP_TOOL, asked: true, answer: null, error: ctx.recordError(rowId, e) };
        }
      }
    }
    base.icp = icp;

    let person: Json | null = null;
    if (cfg.fetch_person) {
      person = { status: null, work_email: ident.work_email, facts: null, error: null };
      if (!ident.work_email) person.status = "no_work_email";
      else if (ident.work_email_personal) person.status = "personal_email";
      else if (ctx.stopped !== null) person.status = "not_asked";
      else {
        try {
          const p = await client.getPerson({ work_email: ident.work_email });
          person.status = "found";
          person.facts = facts.personFacts(p);
          const current = person.facts.current_company_id;
          if (current && current !== company.id) {
            flags.push(`The person's current company in Firmable (${current}) is not the company found for the domain (${company.id}).`);
          }
        } catch (e) {
          if (e instanceof NotFoundError) person.status = "not_found";
          else if (e instanceof FirmableError) {
            person.status = "error";
            person.error = ctx.recordError(rowId, e);
          } else throw e;
        }
      }
    }
    base.person = person;

    const evaluated = cfg.rules.map((rule: Json) => evaluate(rule, f, icp, person));
    const out: Json = { ...base, rules: evaluated };
    const decider = evaluated.find((r: Json) => r.outcome !== "not_matched");
    if (!decider) {
      return { ...out, status: "routed_default", queue: cfg.default_queue, status_reason: "No rule matched; routed to the default queue." };
    }
    if (decider.outcome === "matched") {
      return { ...out, status: "routed", queue: decider.queue, matched_rule: decider.rule_id,
        status_reason: `Rule ${decider.rule_id} matched; routed to ${decider.queue}.` };
    }
    if (decider.outcome === "skipped_missing_fact") {
      return { ...out, status: "needs_review", queue: cfg.review_queue,
        status_reason: `Rule ${decider.rule_id} was skipped because these facts were missing: ` +
          `${decider.missing_facts.join(", ")}. It might have matched, so the lead ` +
          "goes to the review queue rather than to a later rule or the default queue." };
    }
    return { ...out, status: "error", queue: cfg.review_queue,
      status_reason: `Rule ${decider.rule_id} could not be evaluated because a request failed or ` +
        "was not made. The lead goes to the review queue." };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  for (const r of rows) {
    if (!("queue" in r)) r.queue = r.status === "error" ? cfg.review_queue : null;
  }
  const counts: Record<string, number> = {};
  const queueCounts: Record<string, number> = {};
  const ruleCounts: Record<string, number> = Object.fromEntries(cfg.rules.map((r: Json) => [r.id, 0]));
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    if (r.queue !== null) queueCounts[r.queue] = (queueCounts[r.queue] ?? 0) + 1;
    if (r.matched_rule) ruleCounts[r.matched_rule] += 1;
  }
  const flagged = rows.filter((r) => (r.flags ?? []).length).length;
  if (flagged) {
    coverage.push(`${flagged} lead(s) have flags where identifiers or profile facts disagree. The recipe ` +
      "reports them and does not correct upstream data.");
  }
  const review = rows.filter((r) => REVIEW_STATUSES.has(r.status)).map((r) => r.row_id);
  return { rows, summary: { status_counts: counts, queue_counts: queueCounts, rule_match_counts: ruleCounts, review_rows: review }, coverage };
}

export const CSV_COLUMNS = ["row_id", "input_work_email", "input_domain", "lead_name", "source", "duplicate_of", "status",
  "status_reason", "queue", "matched_rule", "domain_used", "domain_source", "company_id", "company_name", "hq_country",
  "employee_count", "industries", "icp_fit", "person_status", "person_seniority", "flags", "rule_results"];

const reasonOf = (r: Json): string | null => r.status_reason || r.reason || r.error?.message || null;

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const company = r.company ?? {};
    const ident = r.identifier ?? {};
    const person = r.person ?? {};
    return {
      row_id: r.row_id, input_work_email: r.input.work_email ?? null, input_domain: r.input.domain ?? null,
      lead_name: r.input.lead_name ?? null, source: r.input.source ?? null, duplicate_of: r.duplicate_of ?? null,
      status: r.status, status_reason: reasonOf(r), queue: r.queue ?? null, matched_rule: r.matched_rule ?? null,
      domain_used: ident.domain ?? null, domain_source: ident.domain_source ?? null, company_id: company.id ?? null,
      company_name: company.name ?? null, hq_country: company.hq_country ?? null,
      employee_count: company.employee_count ?? null,
      industries: Array.isArray(company.industries) ? company.industries : [],
      icp_fit: r.icp?.answer ?? null, person_status: person.status ?? null,
      person_seniority: person.facts?.seniority ?? null, flags: r.flags ?? [],
      rule_results: (r.rules ?? []).map((x: Json) => `${x.rule_id}=${x.outcome}`),
    };
  });
}

const condText = (c: Json): string =>
  `${c.condition}: wanted ${text(c.expected)}, observed ${text(c.observed)}, ${c.result.replace(/_/g, " ")}`;

export function render(report: Report, result: Json): void {
  const summary = result.summary;
  const counts: Record<string, number> = summary.status_counts ?? {};
  let body = "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>";
  if (Object.keys(summary.queue_counts ?? {}).length) {
    body += table(["Queue", "Leads"], Object.entries(summary.queue_counts).map(([k, v]) => [k, v]));
  }
  if (Object.keys(summary.rule_match_counts ?? {}).length) {
    body += table(["Rule", "Leads routed"], Object.entries(summary.rule_match_counts).map(([k, v]) => [k, v]));
  }
  body += note("A condition on a missing fact does not match. When the rule that would decide a lead was " +
    "skipped for a missing fact, the lead goes to the <strong>review queue</strong>. An is_icp_fit " +
    "answer of unknown is never treated as no.", "unknown");
  report.add("Result", "Routing summary", body);

  const review = result.rows.filter((r: Json) => REVIEW_STATUSES.has(r.status));
  if (review.length) {
    report.add("Review queue", "Leads a person needs to route", table(["Row", "Lead", "Status", "Reason"],
      review.map((r: Json) => [r.row_id, r.input.lead_name || r.input.work_email || r.input.domain,
        r.status.replace(/_/g, " "), reasonOf(r)])));
  }

  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.input.lead_name || r.input.work_email || r.input.domain || r.row_id;
    let left = `<p>${esc(reasonOf(r) ?? "")}</p>`;
    const ident = r.identifier ?? {};
    left += kv([["Queue", r.queue], ["Matched rule", r.matched_rule], ["Source", r.input.source],
      ["Domain used", ident.domain], ["Domain taken from", ident.domain_source]]);
    const company = r.company;
    if (company) {
      left += "<h3>Company facts from Firmable</h3>" + kv([["Firmable ID", company.id], ["Name", company.name],
        ["Domain", company.fqdn], ["Head office country", company.hq_country], ["Employee count", company.employee_count],
        ["Size", company.company_size], ["Industries", (company.industries ?? []).join(", ") || null]]);
    }
    const person = r.person;
    if (person) {
      const pf = person.facts ?? {};
      left += "<h3>Person</h3>" + kv([["Lookup", String(person.status ?? "").replace(/_/g, " ")], ["Position", pf.position],
        ["Seniority", pf.seniority], ["Department", pf.department]]);
    }
    if ((r.flags ?? []).length) left += "<h3>Flags</h3>" + bulletList(r.flags);
    if (r.duplicate_of) left += note(`Same work email and domain as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    const icp = r.icp;
    if (icp) {
      const rs = icp.reasoning ?? {};
      right += `<p><strong>ICP fit</strong> (${esc(icp.tool)}): ${esc(icp.answer || (!icp.asked ? "not asked" : "no answer"))}` +
        (icp.confidence !== null && icp.confidence !== undefined ? `, confidence ${esc(icp.confidence)}` : "") + "</p>";
      if (rs.reason) {
        right += `<p>${esc(rs.reason)}</p><p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.` +
          (rs.would_change_if ? ` Would change if: ${esc(rs.would_change_if)}.` : "") + "</p>";
      }
      if (icp.error) right += note(`is_icp_fit: ${esc(isObj(icp.error) ? icp.error.message : icp.error)}`, "error");
    }
    if (person?.error) right += note(`GET /people: ${esc(person.error.message)}`, "error");
    const rules: Json[] = r.rules ?? [];
    if (rules.length) {
      right += table(["Rule", "Queue", "Outcome", "Conditions"], rules.map((x) => [
        x.rule_id, x.queue, new Raw(badge(x.outcome.replace(/_/g, " "), OUTCOME_TONE[x.outcome] ?? "neutral")),
        new Raw(`<p>${esc(x.reason)}</p>` + bulletList(x.conditions.map(condText)))]));
      right += detailsJson("Rule results as JSON", rules);
    }
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No rules were evaluated for this lead.</p>', `Row ${r.row_id}`));
  }
  report.add("Leads", "Each lead with its route", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
