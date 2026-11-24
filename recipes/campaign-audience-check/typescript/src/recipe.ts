/**
 * Campaign audience check: says include, exclude or review for each account in each campaign.
 * Mirrors recipes/campaign-audience-check/python/src/campaign_audience_check/recipe.py; see that file
 * and the recipe README for the result and status policy.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "campaign-audience-check";
export const TITLE = "Campaign audience check";
export const DESCRIPTION = "Checks each account against one or more campaign audiences with Firmable's " +
  "matches_campaign_audience decision tool, and says include, exclude or review for each campaign " +
  "with Firmable's reason shown as returned.";
export const TOOL = "matches_campaign_audience";

const STATUS_TONE: Record<string, string> = {
  in_campaigns: "good", no_campaign: "bad", needs_review: "review", excluded_customer: "neutral",
  incomplete: "error", invalid_input: "error", error: "error", not_processed: "unknown",
};
const RESULT_TONE: Record<string, string> = {
  include: "good", exclude: "bad", review: "review", excluded_customer: "neutral", error: "error", not_asked: "unknown",
};
const RESULTS = ["include", "exclude", "review", "excluded_customer", "error", "not_asked"];
const ID = /^[a-z0-9_]{1,40}$/;
const COMPANY_ID = /^f\d{12}$/;
const COUNTRY = /^[A-Z]{2}$/;
const TOP_KEYS = new Set(["campaigns", "exclude_existing_customers", "with_reasoning", "fetch_company"]);
const CAMPAIGN_ITEM_KEYS = new Set(["id", "label", "campaign"]);
export const CAMPAIGN_KEYS = ["min_staff", "max_staff", "staff_in", "industries", "countries", "revenue_bands", "message"];
export const STAFF_IN = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
export const REVENUE_BANDS = ["<$1M", "$1M - $5M", "$5M - $10M", "$10M - $50M", "$50M - $100M", "$100M - $250M",
  "$250M - $500M", "$500M - $1B", ">$1B+"];
const MAX_CAMPAIGNS = 5;

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x);
const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);

function checkCampaign(where: string, c: unknown): Json {
  if (!isObj(c)) throw new ConfigError(`${where} must be an object.`);
  const unknown = Object.keys(c).filter((k) => !CAMPAIGN_KEYS.includes(k)).sort();
  if (unknown.length) throw new ConfigError(`${where} has key(s) the campaign schema does not list: ${unknown.join(", ")}.`);
  const msg = c.message;
  if (typeof msg !== "string" || !msg.trim() || msg.length < 3 || msg.length > 2000) {
    throw new ConfigError(`${where}.message is required and must be 3 to 2000 characters.`);
  }
  for (const key of ["min_staff", "max_staff"]) {
    const v = c[key];
    if (v !== undefined && v !== null && (!isInt(v) || v < 0)) throw new ConfigError(`${where}.${key} must be a whole number, 0 or more.`);
  }
  if (isInt(c.min_staff) && isInt(c.max_staff) && c.min_staff > c.max_staff) {
    throw new ConfigError(`${where}.min_staff is more than ${where}.max_staff.`);
  }
  const staffIn = c.staff_in;
  if (staffIn !== undefined && staffIn !== null && !STAFF_IN.includes(staffIn)) {
    throw new ConfigError(`${where}.staff_in must be one of ${STAFF_IN.join(", ")}.`);
  }
  for (const key of ["industries", "countries", "revenue_bands"]) {
    const v = c[key];
    if (v === undefined || v === null) continue;
    if (!Array.isArray(v) || !v.every((x) => typeof x === "string" && x.trim())) {
      throw new ConfigError(`${where}.${key} must be a list of non-empty text values.`);
    }
  }
  for (const code of c.countries ?? []) {
    if (!COUNTRY.test(code)) throw new ConfigError(`${where}.countries has ${pyRepr(code)}; use two-letter upper-case ISO codes such as AU.`);
  }
  for (const band of c.revenue_bands ?? []) {
    if (!REVENUE_BANDS.includes(band)) throw new ConfigError(`${where}.revenue_bands has ${pyRepr(band)}; use one of: ${REVENUE_BANDS.join(", ")}.`);
  }
  return { ...c };
}

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const items = raw.campaigns;
  if (!Array.isArray(items) || !items.length) throw new ConfigError("campaigns must be a non-empty list.");
  if (items.length > MAX_CAMPAIGNS) throw new ConfigError(`campaigns has ${items.length} items; the most allowed is ${MAX_CAMPAIGNS}.`);
  const campaigns: Json[] = [];
  const seen = new Set<string>();
  items.forEach((item: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(item)) throw new ConfigError(`campaigns[${i}] must be an object.`);
    const extra = Object.keys(item).filter((k) => !CAMPAIGN_ITEM_KEYS.has(k)).sort();
    if (extra.length) throw new ConfigError(`campaigns[${i}] has unknown key(s): ${extra.join(", ")}.`);
    const cid = item.id;
    if (typeof cid !== "string" || !ID.test(cid)) throw new ConfigError(`campaigns[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(cid)) throw new ConfigError(`campaigns[${i}].id ${pyRepr(cid)} is used twice.`);
    seen.add(cid);
    const label = item.label ?? cid;
    if (typeof label !== "string" || !label.trim()) throw new ConfigError(`campaigns[${i}].label must be text.`);
    const campaign = checkCampaign(`campaigns[${i}].campaign`, item.campaign);
    campaigns.push({ id: cid, label, campaign });
  });
  for (const key of ["exclude_existing_customers", "with_reasoning", "fetch_company"]) {
    if (key in raw && typeof raw[key] !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  return {
    campaigns,
    exclude_existing_customers: raw.exclude_existing_customers ?? true,
    with_reasoning: raw.with_reasoning ?? true,
    fetch_company: raw.fetch_company ?? false,
  };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ["domain", "company_id"] });
}

/** Returns [value, problem]. Empty means not stated. */
function existingOf(row: Row): [boolean | null, string | null] {
  const v = (row.existing_customer ?? "").trim().toLowerCase();
  if (v === "") return [null, null];
  if (v === "true" || v === "false") return [v === "true", null];
  return [null, `existing_customer is ${pyRepr(row.existing_customer)}; use true, false or leave it empty.`];
}

/** Returns [subject, problem]. company_id wins over domain when both are given. */
function subjectOf(row: Row): [Record<string, string> | null, string | null] {
  const cid = (row.company_id ?? "").trim();
  if (cid) {
    if (!COMPANY_ID.test(cid)) return [null, `company_id ${pyRepr(cid)} is not a Firmable company ID (f followed by 12 digits).`];
    return [{ company_id: cid }, null];
  }
  const domain = normaliseDomain(row.domain);
  if (!domain) return [null, `${pyRepr(row.domain)} is not a valid domain.`];
  return [{ domain }, null];
}

function keyOf(subject: Record<string, string> | null): string {
  if (!subject) return "";
  return "company_id" in subject ? "company_id:" + subject.company_id : "domain:" + subject.domain;
}

function paidSubjects(rows: Row[], config: Json): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const [existing, problem] = existingOf(r);
    const [subject, bad] = subjectOf(r);
    if (problem || bad) continue;
    if (existing && config.exclude_existing_customers) continue;
    const k = keyOf(subject);
    if (!out.includes(k)) out.push(k);
  }
  return out;
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const n = paidSubjects(rows, config).length;
  const k = config.campaigns.length;
  const calls = [new PlannedCall("GET /decisions", "Read the decision tool list and check the campaign schema", 1, [])];
  if (config.fetch_company) {
    calls.push(new PlannedCall("GET /company", "Company profile for each unique account that is asked", n,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  }
  calls.push(new PlannedCall("POST /decisions", `${k} campaign(s) for each unique account that is asked`, n * k,
    pricing.estimateDecision(n * k, config.with_reasoning)));
  return calls;
}

function schemaProblems(catalogue: Json, campaigns: Json[]): string[] {
  const tool = decisions.toolIndex(catalogue).get(TOOL) ?? {};
  const props = tool.input_schema?.$defs?.Campaign?.properties;
  if (!isObj(props)) return [];
  const problems: string[] = [];
  for (const c of campaigns) {
    const extra = Object.keys(c.campaign).filter((k) => !(k in props)).sort();
    if (extra.length) {
      problems.push(`Campaign ${pyRepr(c.id)} uses key(s) the live ${TOOL} schema does not list: ${extra.join(", ")}.`);
    }
  }
  return problems;
}

const labels = (items: Json[]): string => items.map((c) => c.label).join(", ");

function aggregate(results: Json[]): [string, string] {
  const n = results.length;
  const inc = results.filter((c) => c.result === "include");
  const err = results.filter((c) => c.result === "error" || c.result === "not_asked");
  const rev = results.filter((c) => c.result === "review");
  const errText = `${err.length} campaign(s) have no answer because of an error or because the run stopped: ${labels(err)}.`;
  const revText = `${rev.length} campaign(s) answered unknown and need review: ${labels(rev)}.`;
  if (inc.length) {
    const tail = (rev.length ? ` ${revText}` : "") + (err.length ? ` ${errText}` : "");
    return ["in_campaigns", `Included in ${inc.length} of ${n} campaign(s): ${labels(inc)}.${tail}`];
  }
  if (err.length) return ["incomplete", `No campaign answered include. ${errText}${rev.length ? ` ${revText}` : ""}`];
  if (rev.length) {
    return ["needs_review", `No campaign answered include. ${revText} Unknown means Firmable does not hold ` +
      "the facts needed to answer; it is not an exclude."];
  }
  return ["no_campaign", `All ${n} campaign(s) answered exclude.`];
}

const OUTCOME: Record<string, string> = { yes: "include", no: "exclude", unknown: "review" };
const outcome = (answer: unknown): string => (typeof answer === "string" && OUTCOME[answer]) || "error";

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const campaigns: Json[] = cfg.campaigns;
  const coverage: string[] = [
    "A review result means Firmable answered unknown because it does not hold the facts needed to " +
      "answer. It is never counted as an include or an exclude.",
    "Reasons and filter details are shown as Firmable returned them.",
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length }, campaign_counts: {} }, coverage };
  };

  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no campaigns were asked.");
  }
  let problems = decisions.checkTools(catalogue, [{ tool: TOOL, inputs: { campaign: {} } }], "company");
  if (!problems.length) problems = schemaProblems(catalogue, campaigns);
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("The decision tool list does not accept the configured campaigns.");
  }

  if (decisions.usesModel(catalogue, TOOL)) {
    coverage.push(`These decision tools use a decision model inside Firmable's service: ${TOOL}. It judges ` +
      "each campaign message against the company. This code applies only the rules in the configuration.");
  }
  if (cfg.fetch_company) {
    coverage.push("Company profiles were fetched for display only. Campaigns were asked by Firmable company ID " +
      "when a profile was found.");
  } else {
    coverage.push("Company profiles were not fetched; campaigns were asked by the company_id or domain given.");
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _key: keyOf(subjectOf(r)[0]) })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier account; their API responses were reused, not requested again.`);

  const one = async (row: Row): Promise<Json> => {
    const [existing, existingProblem] = existingOf(row);
    const [subject, subjectProblem] = subjectOf(row);
    const base: Json = {
      row_id: row.row_id, input: row, account_name: row.account_name || null, subject, existing_customer: existing,
      duplicate_of: dupes[row.row_id] ?? null, company_lookup: "not_requested", asked_with: null, company: null,
      campaigns: [], needs_review: [], errors: [],
    };
    const problem = subjectProblem || existingProblem;
    if (problem) return { ...base, status: "invalid_input", status_reason: problem };
    if (existing && cfg.exclude_existing_customers) {
      const results = campaigns.map((c) => ({ campaign_id: c.id, label: c.label, result: "excluded_customer" }));
      return { ...base, status: "excluded_customer", campaigns: results,
        status_reason: "Marked as an existing customer, so it was excluded from every campaign before any paid call." };
    }
    let asked: Record<string, string> = { ...(subject ?? {}) };
    if (cfg.fetch_company) {
      const ident: Record<string, string> = "company_id" in asked ? { id: asked.company_id } : { fqdn: asked.domain };
      try {
        const company = await client.getCompany(ident);
        base.company_lookup = "found";
        base.company = facts.companyFacts(company);
        if (company.id) asked = { company_id: company.id };
      } catch (e) {
        if (!(e instanceof NotFoundError)) throw e;
        base.company_lookup = "not_found";
      }
    }
    base.asked_with = asked;
    const results: Json[] = [];
    for (const c of campaigns) {
      const entry = { campaign_id: c.id, label: c.label };
      if (ctx.stopped !== null) {
        results.push({ ...entry, result: "not_asked", answer: null, filters: null, error: null });
        continue;
      }
      let resp: Json;
      try {
        resp = await client.makeDecision(TOOL, { ...asked, campaign: c.campaign }, { withReasoning: cfg.with_reasoning });
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        const err = ctx.recordError(row.row_id, e);
        results.push({ ...entry, result: "error", answer: null, filters: null, error: err });
        continue;
      }
      const ans = decisions.answerFrom(resp);
      const result = outcome(ans.answer);
      const details = isObj(ans.details) ? ans.details : {};
      results.push({ ...entry, result, ...ans, filters: details.filters ?? null,
        error: result === "error" ? ans.answer_error : null });
    }
    const [status, reason] = aggregate(results);
    return { ...base, status, status_reason: reason, campaigns: results,
      needs_review: results.filter((c) => c.result === "review").map((c) => c.label),
      errors: results.filter((c) => c.result === "error" || c.result === "not_asked").map((c) => c.label) };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const excluded = rows.filter((r) => r.status === "excluded_customer").length;
  if (excluded) {
    coverage.push(`${excluded} row(s) marked as existing customers were excluded from every campaign before any paid call.`);
  }
  const notFound = rows.filter((r) => r.company_lookup === "not_found").length;
  if (notFound) {
    coverage.push(`${notFound} row(s) have no Firmable company profile; their campaigns were still asked with the identifier given.`);
  }
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const campaignCounts: Record<string, Record<string, number>> = {};
  for (const r of rows) {
    for (const c of r.campaigns ?? []) {
      const cc = (campaignCounts[c.campaign_id] ??= Object.fromEntries(RESULTS.map((k) => [k, 0])));
      cc[c.result] += 1;
    }
  }
  return { rows, summary: { status_counts: counts, campaign_counts: campaignCounts }, coverage };
}

export const BASE_COLUMNS = ["row_id", "account_name", "company_id", "domain", "existing_customer", "duplicate_of", "status",
  "status_reason", "company_name", "included_in", "needs_review"];

export function csvColumns(result: Json): string[] {
  const cols = [...BASE_COLUMNS];
  for (const c of result.input.config.campaigns) cols.push(`campaign_${c.id}`, `campaign_${c.id}_reason`);
  return cols;
}

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const inp = r.input;
    const list: Json[] = r.campaigns ?? [];
    const byId: Record<string, Json> = Object.fromEntries(list.map((c) => [c.campaign_id, c]));
    const company = r.company ?? {};
    const row: Record<string, unknown> = {
      row_id: r.row_id, account_name: inp.account_name ?? null,
      company_id: company.id || list.find((c) => c.entity_id)?.entity_id || inp.company_id || null, domain: inp.domain ?? null,
      existing_customer: r.existing_customer ?? null, duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_name: company.name ?? null,
      included_in: list.filter((c) => c.result === "include").map((c) => c.campaign_id),
      needs_review: list.filter((c) => c.result === "review").map((c) => c.campaign_id),
    };
    for (const c of result.input.config.campaigns) {
      const got = byId[c.id] ?? {};
      row[`campaign_${c.id}`] = got.result ?? null;
      row[`campaign_${c.id}_reason`] = got.reasoning?.reason ?? null;
    }
    return row;
  });
}

const show = (v: unknown): string => (Array.isArray(v) ? v.map(String).join(", ") : String(v));

function titleOf(r: Json): string {
  const inp = r.input;
  return r.company?.name || inp.account_name || inp.domain || inp.company_id || r.row_id;
}

export function render(report: Report, result: Json): void {
  const campaigns: Json[] = result.input.config.campaigns;
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Campaign audience summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("<strong>Review</strong> means Firmable answered unknown. It is not an exclude. An account " +
      "with no include and at least one review is <strong>needs review</strong>.", "unknown"));
  report.add("Campaigns", "Configured campaigns", table(["Id", "Label", "Message", "Audience filters"],
    campaigns.map((c) => [c.id, c.label, c.campaign.message,
      Object.entries(c.campaign).filter(([k]) => k !== "message").map(([k, v]) => `${k}: ${show(v)}`).join("; ") || "none"])));
  const grid: unknown[][] = [];
  for (const r of result.rows) {
    const byId: Record<string, Json> = Object.fromEntries((r.campaigns ?? []).map((c: Json) => [c.campaign_id, c]));
    const cells: unknown[] = [titleOf(r), new Raw(badge(r.status.replace(/_/g, " "), STATUS_TONE[r.status] ?? "neutral"))];
    for (const c of campaigns) {
      const got = byId[c.id];
      if (!got) { cells.push("none"); continue; }
      const reason = got.reasoning?.reason;
      cells.push(new Raw(badge(got.result.replace(/_/g, " "), RESULT_TONE[got.result] ?? "neutral") +
        (reason ? `<br><span class="muted">${esc(reason)}</span>` : "")));
    }
    grid.push(cells);
  }
  report.add("Grid", "Accounts by campaign", table(["Account", "Status", ...campaigns.map((c) => c.label)], grid));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    const existingText = r.existing_customer === true ? "yes" : r.existing_customer === false ? "no" : null;
    left += kv([["Asked with", Object.entries(r.asked_with ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || null],
      ["Existing customer", existingText], ["Company profile", (r.company_lookup ?? "").replace(/_/g, " ") || null]]);
    const company = r.company;
    if (company) {
      left += kv([["Firmable ID", company.id], ["Name", company.name], ["Domain", company.fqdn],
        ["Head office country", company.hq_country], ["Size", company.company_size],
        ["Industries", (company.industries ?? []).join(", ") || null], ["Revenue", company.revenue]]);
    }
    if (r.company_lookup === "not_found") {
      left += note("Firmable has no company profile for this identifier. Campaigns were still asked.", "unknown");
    }
    if (r.duplicate_of) left += note(`Same account as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    const results: Json[] = r.campaigns ?? [];
    for (const c of results) {
      right += `<h3>${esc(c.label)} ${badge(c.result.replace(/_/g, " "), RESULT_TONE[c.result] ?? "neutral")}</h3>`;
      if (c.answer !== undefined && c.answer !== null) {
        right += kv([["Answer", c.answer], ["Confidence", c.confidence], ["Answered at", c.answered_at]]);
      }
      const rs = c.reasoning ?? {};
      if (rs.reason) {
        right += `<p><strong>Firmable's reason:</strong> ${esc(rs.reason)}</p>` +
          `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.` +
          (rs.would_change_if ? ` Would change if: ${esc(rs.would_change_if)}.` : "") + "</p>";
      }
      if (c.filters !== undefined && c.filters !== null) right += detailsJson("Audience filter details as returned", c.filters);
      if (c.error) right += note(esc(isObj(c.error) ? c.error.message : c.error), "error");
    }
    if ((r.needs_review ?? []).length) right += "<h3>Needs review</h3>" + bulletList(r.needs_review);
    if (results.length) right += detailsJson("Campaign results as JSON", results);
    cards.push(card(titleOf(r), badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No campaigns were asked for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its campaign answers", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns, csvRows, render,
};
