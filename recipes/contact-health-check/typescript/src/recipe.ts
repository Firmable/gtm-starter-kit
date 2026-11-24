/**
 * Contact health check: reviews whether each contact in a list is still current and reachable.
 * Mirrors recipes/contact-health-check/python/src/contact_health_check/recipe.py; see that file
 * and the recipe README for the health policy.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "contact-health-check";
export const TITLE = "Contact health check";
export const DESCRIPTION = "Checks whether each contact still works at the expected employer and holds the expected role, and " +
  "what Firmable holds about their email deliverability, mobile and the Australian Do Not Call " +
  "register. These are Firmable-held observations, not live mailbox, telephone or legal checks.";

const CHECK_IDS = ["employment", "role", "email", "mobile", "dnc"];
const TOOLS: Record<string, string> = {
  employment: "works_at_company", role: "is_contact_current", email: "is_email_valid",
  mobile: "is_mobile_valid", dnc: "is_mobile_on_dnc",
};
const LABELS: Record<string, string> = {
  employment: "Works at the expected employer", role: "Still in the expected role",
  email: "Work email likely to deliver", mobile: "Mobile held by Firmable",
  dnc: "Mobile on the Australian Do Not Call register",
};
const WANT: Record<string, string> = { employment: "yes", role: "yes", email: "yes", mobile: "yes", dnc: "no" };
const CURRENT = ["employment", "role"];
const REACH = ["email", "mobile", "dnc"];
const ID_COLUMNS = ["person_id", "work_email", "linkedin_url"];

const STATUS_TONE: Record<string, string> = {
  healthy: "good", current: "good", reach_unconfirmed: "review", stale: "bad", needs_review: "review",
  incomplete: "error", person_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const RESULT_TONE: Record<string, string> = {
  pass: "good", fail: "bad", unknown: "unknown", error: "error", not_asked: "neutral", not_checked: "unknown",
  not_supplied: "neutral",
};
const TOP_KEYS = new Set(["fetch_person", "with_reasoning", "checks"]);
const PERSON_ID = /^fp\d{12}$/;
const COMPANY_ID = /^f\d{12}$/;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const LINKEDIN = /linkedin\.com\/in\/([^/?#\s]+)/i;
const MOBILE = /^[\d\s()+.\-]+$/;
const GET_PEOPLE_PARAM: Record<string, string> = { person_id: "id", work_email: "work_email", linkedin_url: "ln_url" };

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const checks = raw.checks ?? [...CHECK_IDS];
  if (!Array.isArray(checks) || !checks.length) throw new ConfigError("checks must be a non-empty list.");
  const seen: string[] = [];
  checks.forEach((c: Json, idx: number) => {
    const i = idx + 1;
    if (!CHECK_IDS.includes(c)) throw new ConfigError(`checks[${i}] must be one of: ${[...CHECK_IDS].sort().join(", ")}.`);
    if (seen.includes(c)) throw new ConfigError(`checks[${i}] ${pyRepr(c)} is listed twice.`);
    seen.push(c);
  });
  for (const key of ["fetch_person", "with_reasoning"]) {
    if (typeof (raw[key] ?? key === "with_reasoning") !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  return { fetch_person: raw.fetch_person ?? false, with_reasoning: raw.with_reasoning ?? true,
    checks: CHECK_IDS.filter((c) => seen.includes(c)) };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ID_COLUMNS });
}

/** Returns the checked values of one row, or a reason the row cannot be checked. */
export function parseRow(row: Row): [Json | null, string | null] {
  for (const col of ID_COLUMNS) {
    const v = row[col];
    if (!v) continue;
    if (col === "person_id" && !PERSON_ID.test(v)) return [null, `person_id ${pyRepr(v)} is not a Firmable person ID (fp followed by 12 digits).`];
    if (col === "work_email" && !EMAIL.test(v)) return [null, `work_email ${pyRepr(v)} is not an email address.`];
    if (col === "linkedin_url" && !LINKEDIN.test(v)) return [null, `linkedin_url ${pyRepr(v)} is not a LinkedIn profile URL (linkedin.com/in/...).`];
  }
  const kind = ID_COLUMNS.find((c) => row[c]) as string;
  const companyId = row.expected_company_id || null;
  if (companyId && !COMPANY_ID.test(companyId)) {
    return [null, `expected_company_id ${pyRepr(companyId)} is not a Firmable company ID (f followed by 12 digits).`];
  }
  let domain: string | null = null;
  if (!companyId && row.expected_domain) {
    domain = normaliseDomain(row.expected_domain);
    if (!domain) return [null, `expected_domain ${pyRepr(row.expected_domain)} is not a valid domain.`];
  }
  const title = row.expected_title || null;
  if (title !== null && !(title.length >= 2 && title.length <= 200)) return [null, "expected_title must be 2 to 200 characters."];
  const email = row.email_to_check || null;
  if (email !== null && !EMAIL.test(email)) return [null, `email_to_check ${pyRepr(email)} is not an email address.`];
  const mobile = row.mobile_to_check || null;
  if (mobile !== null && !(mobile.length >= 6 && mobile.length <= 40 && MOBILE.test(mobile))) {
    return [null, "mobile_to_check must be 6 to 40 digits, spaces, brackets, dots, dashes or a plus sign."];
  }
  return [{ id_type: kind, id_value: row[kind], company_id: companyId, domain, title, email, mobile }, null];
}

function questionKeys(p: Json, checks: string[]): string[] {
  const subject = `${p.id_type}=${p.id_value}`;
  const employer = p.company_id || (p.domain ? `domain=${p.domain}` : "");
  const keys: string[] = [];
  for (const c of checks) {
    if (c === "employment" && employer) keys.push(`employment|${subject}|${employer}`);
    else if (c === "role" && p.title) keys.push(`role|${subject}|${employer}|${p.title}`);
    else if (c === "email") keys.push(`email|${subject}|${p.email ?? ""}`);
    else if (c === "mobile" || c === "dnc") keys.push(`${c}|${subject}|${p.mobile ?? ""}`);
  }
  return keys;
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const persons: string[] = [];
  const domains: string[] = [];
  const questions: string[] = [];
  for (const r of rows) {
    const [p] = parseRow(r);
    if (p === null) continue;
    const key = `${p.id_type}=${p.id_value}`;
    if (!persons.includes(key)) persons.push(key);
    if (p.domain && (config.checks.includes("employment") || config.checks.includes("role")) && !domains.includes(p.domain)) {
      domains.push(p.domain);
    }
    for (const q of questionKeys(p, config.checks)) if (!questions.includes(q)) questions.push(q);
  }
  const n = questions.length;
  const calls = [
    new PlannedCall("GET /decisions", "Read the decision tool list and check the tools this recipe uses", 1, []),
    new PlannedCall("GET /company", "Resolve each unique expected employer domain to a company ID", domains.length,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * domains.length)]]),
  ];
  if (config.fetch_person) {
    calls.push(new PlannedCall("GET /people", "Person profile for each unique valid identifier", persons.length,
      [["person", pricing.round4(pricing.PERSON_PROFILE * persons.length)]]));
  }
  calls.push(new PlannedCall("POST /decisions", "Each configured check that has the inputs it needs, once per " +
    "unique question", n, pricing.estimateDecision(n, config.with_reasoning)));
  return calls;
}

const labels = (items: Json[]) => items.map((c) => c.label).join(", ");

function phoneChannel(by: Record<string, Json>): string {
  const mobile = by.mobile;
  if (!mobile) return "not_checked";
  if (mobile.result === "fail") return "not_held";
  if (mobile.result === "unknown") return "unknown";
  if (mobile.result !== "pass") return "error";
  const dnc = by.dnc;
  if (!dnc) return "held_dnc_not_checked";
  return ({ pass: "held_not_on_dnc", fail: "held_on_dnc", unknown: "held_dnc_unknown" } as Record<string, string>)[dnc.result] ?? "held_dnc_error";
}

function aggregate(checks: Json[]): [string, string, string[]] {
  const by: Record<string, Json> = Object.fromEntries(checks.map((c) => [c.check_id, c]));
  const current = checks.filter((c) => CURRENT.includes(c.check_id));
  const failed = current.filter((c) => c.result === "fail");
  if (failed.length) return ["stale", `Firmable data does not match the contact record: ${labels(failed)} answered no.`, []];
  const errored = current.filter((c) => c.result === "error" || c.result === "not_asked");
  if (errored.length) {
    return ["incomplete", "Employment or role check(s) have no answer because of an error or because the run " +
      `stopped: ${labels(errored)}.`, []];
  }
  const unknown = current.filter((c) => c.result === "unknown" || c.result === "not_checked");
  if (unknown.length) {
    let reason = `Employment or role check(s) could not be confirmed: ${labels(unknown)}.`;
    if (unknown.some((c) => c.result === "unknown")) reason += " Unknown means Firmable does not hold the fact; it is not a no.";
    for (const c of unknown) if (c.result === "not_checked") reason += ` ${c.note}`;
    return ["needs_review", reason, []];
  }
  const passed = current.filter((c) => c.result === "pass");
  if (!passed.length) {
    return ["needs_review", "No employment or role check was asked, because the row has no expected employer " +
      "or title, or those checks are turned off.", []];
  }
  const agrees = `Firmable data agrees with the contact record: ${labels(passed)}.`;
  const reach = checks.filter((c) => REACH.includes(c.check_id));
  if (!reach.length) return ["current", `${agrees} Reachability checks are turned off.`, []];
  const channels: string[] = [];
  if (by.email?.result === "pass") channels.push("email");
  if (["held_not_on_dnc", "held_dnc_not_checked"].includes(phoneChannel(by))) channels.push("mobile");
  if (channels.length) return ["healthy", `${agrees} Channel(s) confirmed by Firmable data: ${channels.join(", ")}.`, channels];
  const reachErr = reach.filter((c) => c.result === "error" || c.result === "not_asked");
  if (reachErr.length) {
    return ["incomplete", `${agrees} No channel is confirmed, and reachability check(s) have no answer: ${labels(reachErr)}.`, []];
  }
  return ["reach_unconfirmed", `${agrees} No email or mobile channel is confirmed by Firmable data.`, []];
}

function flagsFor(by: Record<string, Json>): string[] {
  const flags: string[] = [];
  const emp = by.employment, role = by.role;
  if (emp && role && [emp.answer, role.answer].includes("yes") && [emp.answer, role.answer].includes("no")) {
    flags.push(`works_at_company answered ${emp.answer} and is_contact_current answered ${role.answer}. ` +
      "Both answers are shown as returned; neither is corrected.");
  }
  const email = by.email;
  if (email && email.answer === "no") {
    const grade = (email.details ?? {}).deliverability_grade;
    flags.push("The email check answered no. The tool describes a no as a low deliverability grade, not a " +
      "bounce." + (grade ? ` Grade returned: ${grade}.` : ""));
  }
  const dnc = by.dnc;
  if (dnc && dnc.answer === "yes") {
    const asAt = (dnc.details ?? {}).as_at;
    flags.push("Firmable records a mobile for this person on the Australian Do Not Call register" +
      (asAt ? ` as at ${asAt}` : "") + ". This is not a live or legal check.");
  }
  return flags;
}

function refresh(checks: Json[]): string[] {
  const out: string[] = [];
  for (const c of checks) {
    const parts: string[] = [];
    if (c.as_at) parts.push(`as at ${c.as_at}`);
    if (c.last_refreshed) parts.push(`last refreshed ${c.last_refreshed}`);
    if (c.fact_date) parts.push(`fact dated ${c.fact_date}`);
    if (parts.length) out.push(`${c.label}: ${parts.join(", ")}`);
  }
  return out;
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = [
    "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no " +
      "or as a pass.",
    "Email, mobile and Do Not Call answers are Firmable-held observations as at Firmable's last refresh. They " +
      "are not live mailbox or telephone checks, and they are not a legal Do Not Call compliance check.",
    "is_mobile_on_dnc covers Australian numbers only; other numbers answer unknown.",
    "This recipe sends no email or SMS and makes no calls.",
  ];

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
  const parsed = ctx.rows.map((r) => parseRow(r)[0]);
  const usesEmail = parsed.some((p) => p && p.email);
  const usesMobile = parsed.some((p) => p && p.mobile);
  const wanted = cfg.checks.map((c: string) => {
    const inputs: Json = {};
    if (c === "role") inputs.title = "Example title";
    if (c === "email" && usesEmail) inputs.email = "name@example.com";
    if ((c === "mobile" || c === "dnc") && usesMobile) inputs.mobile = "0400000000";
    return { tool: TOOLS[c], inputs };
  });
  const problems = decisions.checkTools(catalogue, wanted, "person");
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("The decision tools this recipe needs are missing or have changed their inputs.");
  }
  const modelTools = [...new Set<string>(cfg.checks.filter((c: string) => decisions.usesModel(catalogue, TOOLS[c])).map((c: string) => TOOLS[c]))].sort();
  if (modelTools.length) {
    coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
      "This code applies only the rules in the health policy.");
  }
  if (!cfg.fetch_person) coverage.push("Person profiles were not fetched; checks were asked with the identifier in each row.");
  coverage.push("A LinkedIn URL can match the wrong person when a record carries someone else's link. Rows " +
    "with a person_id are asked by person_id.");

  const keyed = ctx.rows.map((r, i) => ({ ...r, _key: parsed[i] ? `${parsed[i].id_type}=${parsed[i].id_value}` : "" })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) {
    coverage.push(`${nDupes} row(s) repeat an earlier person identifier; identical requests were ` +
      "answered from memory, not sent again.");
  }
  const companies = new Map<string, Json>();

  const resolve = async (domain: string, rowId: string): Promise<Json> => {
    if (companies.has(domain)) return companies.get(domain);
    let out: Json;
    try {
      const company = await client.getCompany({ fqdn: domain });
      out = { id: company.id, name: company.name ?? null, error: null };
    } catch (e) {
      if (e instanceof NotFoundError) out = { id: null, name: null, error: null };
      else if (e instanceof FirmableError) return { id: null, name: null, error: ctx.recordError(rowId, e) };
      else throw e;
    }
    companies.set(domain, out);
    return out;
  };

  const one = async (row: Row): Promise<Json> => {
    const [p, problem] = parseRow(row);
    const base: Json = { row_id: row.row_id, input: row, duplicate_of: dupes[row.row_id] ?? null,
      identifier: null, expected_company_id: null, expected_title: null, person: null, checks: [],
      phone_channel: null, channels: [], flags: [], last_refresh: [], missing_information: [] };
    if (p === null) return { ...base, status: "invalid_input", status_reason: problem };
    base.identifier = { type: p.id_type, value: p.id_value };
    base.expected_title = p.title;
    let subject: Json = { [p.id_type]: p.id_value };
    if (cfg.fetch_person) {
      let person: Json;
      try {
        person = await client.getPerson({ [GET_PEOPLE_PARAM[p.id_type]]: p.id_value });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "person_not_found",
            status_reason: "Firmable has no person profile for this identifier, so no checks were asked." };
        }
        throw e;
      }
      base.person = facts.personFacts(person);
      if (PERSON_ID.test(String(person.id))) subject = { person_id: person.id };
    }
    let companyId: string | null = p.company_id;
    let employerProblem: [string, Json] | null = null;
    const needsEmployer = cfg.checks.includes("employment") || cfg.checks.includes("role");
    if (p.domain && needsEmployer) {
      const company = await resolve(p.domain, row.row_id);
      if (company.error) employerProblem = ["error", company.error];
      else if (company.id === null) employerProblem = ["not_checked", "Firmable has no company profile for the expected employer domain."];
      companyId = company.id;
    }
    base.expected_company_id = companyId;
    const results: Json[] = [];
    for (const c of cfg.checks as string[]) {
      const entry: Json = { check_id: c, label: LABELS[c], tool: TOOLS[c], want: WANT[c], answer: null, note: null, error: null };
      const question: Json = { ...subject };
      if (c === "employment") {
        if (employerProblem !== null) {
          const [kind, detail] = employerProblem;
          results.push({ ...entry, result: kind, error: kind === "error" ? detail : null,
            note: kind === "not_checked" ? detail : "The expected employer could not be resolved because of an error." });
          continue;
        }
        if (!companyId) { results.push({ ...entry, result: "not_supplied", note: "No expected employer was supplied." }); continue; }
        question.company_id = companyId;
      } else if (c === "role") {
        if (!p.title) { results.push({ ...entry, result: "not_supplied", note: "No expected title was supplied." }); continue; }
        question.title = p.title;
        if (companyId) question.company_id = companyId;
      } else if (c === "email" && p.email) {
        question.email = p.email;
      } else if ((c === "mobile" || c === "dnc") && p.mobile) {
        question.mobile = p.mobile;
      }
      if (ctx.stopped !== null) { results.push({ ...entry, result: "not_asked" }); continue; }
      let resp: Json;
      try {
        resp = await client.makeDecision(TOOLS[c], question, { withReasoning: cfg.with_reasoning });
      } catch (e) {
        if (e instanceof NotFoundError && e.code === "person_not_found") {
          return { ...base, status: "person_not_found", checks: results,
            status_reason: "Decisions found no Firmable person for this identifier, so the remaining checks were not asked." };
        }
        if (!(e instanceof FirmableError)) throw e;
        results.push({ ...entry, result: "error", error: ctx.recordError(row.row_id, e) });
        continue;
      }
      const ans = decisions.answerFrom(resp);
      const result = decisions.compare(ans.answer, WANT[c]);
      const reasoning = ans.reasoning ?? {};
      const details = ans.details && typeof ans.details === "object" && !Array.isArray(ans.details) ? ans.details : null;
      results.push({ ...entry, ...ans, result, note: null, error: result === "error" ? ans.answer_error : null,
        as_at: details ? details.as_at ?? null : null, last_refreshed: reasoning.last_refreshed ?? null,
        fact_date: reasoning.fact_date ?? null });
    }
    const [status, reason, channels] = aggregate(results);
    const by: Record<string, Json> = Object.fromEntries(results.map((c) => [c.check_id, c]));
    return { ...base, status, status_reason: reason, checks: results, phone_channel: phoneChannel(by), channels,
      flags: flagsFor(by), last_refresh: refresh(results),
      missing_information: results.filter((c) => c.result === "unknown" || c.result === "not_checked").map((c) => c.label) };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const checkCounts: Record<string, Record<string, number>> = {};
  for (const r of rows) {
    for (const c of r.checks ?? []) {
      const cc = (checkCounts[c.check_id] ??= Object.fromEntries(Object.keys(RESULT_TONE).map((k) => [k, 0])));
      cc[c.result] += 1;
    }
  }
  return { rows, summary: { status_counts: counts, check_counts: checkCounts }, coverage };
}

export const CSV_COLUMNS = ["row_id", "identifier", "duplicate_of", "status", "status_reason", "person_id", "expected_company_id",
  "expected_title", "employment", "role", "email", "mobile", "dnc", "phone_channel", "flags", "last_refresh"];

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const by: Record<string, Json> = Object.fromEntries((r.checks ?? []).map((c: Json) => [c.check_id, c]));
    const ident = r.identifier;
    const person = r.person ?? {};
    const row: Record<string, unknown> = {
      row_id: r.row_id, identifier: ident ? `${ident.type}:${ident.value}` : null,
      duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      person_id: person.id ?? null, expected_company_id: r.expected_company_id ?? null,
      expected_title: r.expected_title ?? null, phone_channel: r.phone_channel ?? null,
      flags: r.flags ?? [], last_refresh: r.last_refresh ?? [],
    };
    for (const c of CHECK_IDS) {
      const ch = by[c];
      row[c] = ch ? `${ch.result}(${ch.answer || "-"})` : null;
    }
    return row;
  });
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Contact health summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Unknown means Firmable does not hold the fact. A contact whose employment or role answered " +
      "unknown is <strong>needs review</strong>, never healthy.", "unknown") +
    note("Email, mobile and Do Not Call answers are Firmable-held observations as at its last refresh. " +
      "They are not live mailbox, telephone or legal checks.", "info"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const ident = r.identifier ?? {};
    const person = r.person ?? {};
    const title = person.name || ident.value || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    if (r.identifier) {
      left += kv([["Identifier", `${ident.type}: ${ident.value}`], ["Expected employer ID", r.expected_company_id],
        ["Expected title", r.expected_title], ["Phone channel", (r.phone_channel ?? "").replace(/_/g, " ") || null],
        ["Firmable person ID", person.id], ["Current position", person.position],
        ["Current employer", person.current_company_name]]);
    }
    if (r.duplicate_of) left += note(`Same identifier as row ${esc(r.duplicate_of)}; identical requests were reused.`, "info");
    for (const f of r.flags ?? []) left += note(esc(f), "review");
    let right = "";
    const checks: Json[] = r.checks ?? [];
    if (checks.length) {
      right += table(["Check", "Tool", "Wanted", "Answer", "Result", "Confidence"], checks.map((c) => [
        c.label, c.tool, c.want, c.answer || "none",
        new Raw(badge(c.result.replace(/_/g, " "), RESULT_TONE[c.result] ?? "neutral")), c.confidence ?? null]));
      for (const c of checks) {
        const rs = c.reasoning ?? {};
        if (rs.reason) {
          right += `<p><strong>${esc(c.label)}:</strong> ${esc(rs.reason)}</p>` +
            `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.` +
            (rs.would_change_if ? ` Would change if: ${esc(rs.would_change_if)}.` : "") + "</p>";
        }
        if (c.note) right += `<p class="muted">${esc(c.label)}: ${esc(c.note)}</p>`;
        if (c.error) right += note(`${esc(c.label)}: ${esc(typeof c.error === "object" ? c.error.message : c.error)}`, "error");
      }
    }
    if ((r.last_refresh ?? []).length) right += "<h3>Last refresh, as returned</h3>" + bulletList(r.last_refresh);
    if ((r.missing_information ?? []).length) right += "<h3>Missing information</h3>" + bulletList(r.missing_information);
    if (checks.length) right += detailsJson("Check results as JSON", checks);
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No checks were asked for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Contacts", "Each contact with its checks", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
