/**
 * Account qualification: checks a company list against a seller's qualification criteria.
 * Mirrors recipes/account-qualification/python/src/account_qualification/recipe.py; see that file
 * and the recipe README for the aggregation policy.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "account-qualification";
export const TITLE = "Account qualification";
export const DESCRIPTION = "Checks each company against the qualification checks in the configuration, using Firmable " +
  "company profiles and Decisions, and applies an explicit pass, fail and unknown policy.";

const STATUS_TONE: Record<string, string> = {
  qualified: "good", not_qualified: "bad", disqualified: "bad", needs_review: "review", incomplete: "error",
  company_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const RESULT_TONE: Record<string, string> = { pass: "good", fail: "bad", unknown: "unknown", error: "error", not_asked: "neutral" };
const ID = /^[a-z0-9_]{1,40}$/;
const TOP_KEYS = new Set(["fetch_company", "with_reasoning", "checks", "policy"]);

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const checksRaw = raw.checks;
  if (!Array.isArray(checksRaw) || !checksRaw.length) throw new ConfigError("checks must be a non-empty list.");
  const checks: Json[] = [];
  const seen = new Set<string>();
  checksRaw.forEach((c: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(c)) throw new ConfigError(`checks[${i}] must be an object.`);
    const cid = c.id;
    if (typeof cid !== "string" || !ID.test(cid)) throw new ConfigError(`checks[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(cid)) throw new ConfigError(`checks[${i}].id ${pyRepr(cid)} is used twice.`);
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
  if (typeof mop !== "number" || !Number.isInteger(mop) || mop < 0) throw new ConfigError("policy.min_optional_passes must be a whole number, 0 or more.");
  const optional = checks.filter((c) => !c.mandatory).length;
  if (mop > optional) throw new ConfigError(`policy.min_optional_passes is ${mop} but only ${optional} optional check(s) exist.`);
  for (const key of ["fetch_company", "with_reasoning"]) {
    if (typeof (raw[key] ?? true) !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  return { fetch_company: raw.fetch_company ?? true, with_reasoning: raw.with_reasoning ?? true, checks, policy: { min_optional_passes: mop } };
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
  const checks = config.checks.length;
  const calls = [new PlannedCall("GET /decisions", "Read the decision tool list and check the configured tools", 1, [])];
  if (config.fetch_company) {
    calls.push(new PlannedCall("GET /company", "Company profile for each unique valid domain", n,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  }
  calls.push(new PlannedCall("POST /decisions", `${checks} check(s) for each unique valid domain`, n * checks,
    pricing.estimateDecision(n * checks, config.with_reasoning)));
  return calls;
}

function aggregate(checks: Json[], mop: number): [string, string] {
  const labels = (items: Json[]) => items.map((c) => c.label).join(", ");
  const mandatory = checks.filter((c) => c.mandatory);
  const optional = checks.filter((c) => !c.mandatory);
  const failed = mandatory.filter((c) => c.result === "fail");
  const errored = mandatory.filter((c) => c.result === "error" || c.result === "not_asked");
  const unknown = mandatory.filter((c) => c.result === "unknown");
  if (failed.length) return ["disqualified", `Mandatory check(s) failed: ${labels(failed)}.`];
  if (errored.length) return ["incomplete", `Mandatory check(s) have no answer because of an error or because the run stopped: ${labels(errored)}.`];
  if (unknown.length) {
    return ["needs_review", `Mandatory check(s) answered unknown: ${labels(unknown)}. ` +
      "Unknown means Firmable does not hold the fact; it is not a no."];
  }
  const passes = optional.filter((c) => c.result === "pass").length;
  const optUnknown = optional.filter((c) => c.result === "unknown").length;
  const tail = optUnknown ? ` ${optUnknown} optional check(s) answered unknown.` : "";
  if (passes >= mop) {
    return ["qualified", `All mandatory checks passed; ${passes} of ${optional.length} optional check(s) passed (minimum ${mop}).${tail}`];
  }
  return ["not_qualified", `All mandatory checks passed, but only ${passes} of ${optional.length} optional check(s) passed (minimum ${mop}).${tail}`];
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = ["An unknown answer means Firmable does not hold the fact needed to answer. It is never " +
    "counted as a no or as a pass."];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length } }, coverage };
  };

  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no checks were asked.");
  }
  const problems = decisions.checkTools(catalogue, cfg.checks, "company");
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("The configuration names decision tools or inputs the API does not accept.");
  }

  const modelTools = [...new Set<string>(cfg.checks.filter((c: Json) => decisions.usesModel(catalogue, c.tool)).map((c: Json) => c.tool))].sort();
  if (modelTools.length) {
    coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
      "This code applies only the rules in the configuration.");
  }
  if (!cfg.fetch_company) coverage.push("Company profiles were not fetched; checks were asked by domain.");

  const keyed = ctx.rows.map((r) => ({ ...r, _domain: normaliseDomain(r.domain) ?? "" })) as Row[];
  const dupes = findDuplicates(keyed, "_domain");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier domain; their API responses were reused, not requested again.`);
  const mop: number = cfg.policy.min_optional_passes;

  const one = async (row: Row): Promise<Json> => {
    const domain = normaliseDomain(row.domain);
    const base: Json = { row_id: row.row_id, input: row, domain, duplicate_of: dupes[row.row_id] ?? null, company: null,
      checks: [], missing_information: [], errors: [] };
    if (!domain) return { ...base, status: "invalid_input", status_reason: `${pyRepr(row.domain)} is not a valid domain.` };
    let company: Json = null;
    if (cfg.fetch_company) {
      try {
        company = await client.getCompany({ fqdn: domain });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this domain, so no checks were asked." };
        }
        throw e;
      }
    }
    base.company = facts.companyFacts(company);
    const subject = company ? { company_id: company.id } : { domain };
    const results: Json[] = [];
    for (const c of cfg.checks) {
      const entry = { check_id: c.id, label: c.label, tool: c.tool, mandatory: c.mandatory, want: c.want };
      if (ctx.stopped !== null) { results.push({ ...entry, result: "not_asked", answer: null, error: null }); continue; }
      let resp: Json;
      try {
        resp = await client.makeDecision(c.tool, { ...subject, ...c.inputs }, { withReasoning: cfg.with_reasoning });
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        const err = ctx.recordError(row.row_id, e);
        results.push({ ...entry, result: "error", answer: null, error: err });
        continue;
      }
      const ans = decisions.answerFrom(resp);
      const result = decisions.compare(ans.answer, c.want);
      results.push({ ...entry, result, ...ans, error: result === "error" ? ans.answer_error : null });
    }
    const [status, reason] = aggregate(results, mop);
    return { ...base, status, status_reason: reason, checks: results,
      missing_information: results.filter((c) => c.result === "unknown").map((c) => c.label),
      errors: results.filter((c) => c.result === "error" || c.result === "not_asked").map((c) => c.label) };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const checkCounts: Record<string, Record<string, number>> = {};
  for (const r of rows) {
    for (const c of r.checks ?? []) {
      const cc = (checkCounts[c.check_id] ??= { pass: 0, fail: 0, unknown: 0, error: 0, not_asked: 0 });
      cc[c.result] += 1;
    }
  }
  return { rows, summary: { status_counts: counts, check_counts: checkCounts }, coverage };
}

export const CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
  "checks_passed", "checks_failed", "checks_unknown", "checks_error", "missing_information", "check_results"];

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const checks: Json[] = r.checks ?? [];
    const company = r.company ?? {};
    return {
      row_id: r.row_id, domain: r.domain || r.input.domain, duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: company.id ?? null, company_name: company.name ?? null,
      checks_passed: checks.filter((c) => c.result === "pass").length,
      checks_failed: checks.filter((c) => c.result === "fail").length,
      checks_unknown: checks.filter((c) => c.result === "unknown").length,
      checks_error: checks.filter((c) => c.result === "error" || c.result === "not_asked").length,
      missing_information: r.missing_information ?? [],
      check_results: checks.map((c) => `${c.check_id}=${c.result}(${c.answer || "-"})`),
    };
  });
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Qualification summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Unknown means Firmable does not hold the fact. A row with an unknown mandatory check is " +
      "<strong>needs review</strong>, never qualified.", "unknown"));
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
    if (r.duplicate_of) left += note(`Same domain as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let right = "";
    const checks: Json[] = r.checks ?? [];
    if (checks.length) {
      right += table(["Check", "Mandatory", "Wanted", "Answer", "Result", "Confidence"], checks.map((c) => [
        c.label, c.mandatory ? "yes" : "no", c.want, c.answer || "none",
        new Raw(badge(c.result.replace(/_/g, " "), RESULT_TONE[c.result] ?? "neutral")), c.confidence ?? null]));
      for (const c of checks) {
        const rs = c.reasoning ?? {};
        if (rs.reason) {
          right += `<p><strong>${esc(c.label)}:</strong> ${esc(rs.reason)}</p>` +
            `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.` +
            (rs.would_change_if ? ` Would change if: ${esc(rs.would_change_if)}.` : "") + "</p>";
        }
        if (c.error) right += note(`${esc(c.label)}: ${esc(isObj(c.error) ? c.error.message : c.error)}`, "error");
      }
    }
    if ((r.missing_information ?? []).length) right += "<h3>Missing information</h3>" + bulletList(r.missing_information);
    if (checks.length) right += detailsJson("Check results as JSON", checks);
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No checks were asked for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its checks", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
