/**
 * New role outreach: finds contacts whose role change falls inside a configured outreach window.
 * Mirrors recipes/new-role-outreach/python/src/new_role_outreach/recipe.py; see that file and the
 * recipe README for the eligibility rules.
 */
import {
  ConfigError, FirmableClient, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe,
  Report, RunContext, Row, badge, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain,
  note, parseDate, pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "new-role-outreach";
export const TITLE = "New role outreach";
export const DESCRIPTION = "Finds contacts whose new role, new executive role or job change falls inside a configured outreach " +
  "window, using Firmable people search and Decisions, and retrieves contact details only for the " +
  "people selected. A role change is a timing signal, not evidence of buying intent.";

const TRIGGERS = ["new_to_role", "new_exec", "changed_jobs"];
const TOOLS: Record<string, string> = { new_to_role: "is_new_to_role", new_exec: "is_new_exec", changed_jobs: "has_changed_jobs_since" };
const LABELS: Record<string, string> = { new_to_role: "New to role", new_exec: "New executive", changed_jobs: "Changed jobs since window start" };
const EXEC_LEVELS = ["C-Suite", "VP", "Director", "Head of", "Founder", "Owner"];
const COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
const ID_COLUMNS = ["person_id", "work_email", "linkedin_url"];
const ACCOUNT_COLUMNS = ["company_id", "domain"];

const STATUS_TONE: Record<string, string> = {
  eligible: "good", eligible_contacts_found: "good", not_eligible: "bad", no_eligible_contacts: "bad",
  needs_review: "review", incomplete: "error", no_people_found: "unknown", company_not_found: "unknown",
  invalid_input: "error", error: "error", not_processed: "unknown",
};
const RESULT_TONE: Record<string, string> = { pass: "good", fail: "bad", unknown: "unknown", error: "error", not_asked: "neutral" };
const TOP_KEYS = new Set(["evaluation_date", "window_months", "triggers", "executive_only", "executive_seniorities",
  "count_internal_moves", "with_reasoning", "fetch_contact_details", "search"]);
const SEARCH_KEYS = new Set(["selected_country", "position", "seniority", "department", "max_people"]);
const PERSON_ID = /^fp\d{12}$/;
const COMPANY_ID = /^f\d{12}$/;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const LINKEDIN = /linkedin\.com\/in\/([^/?#\s]+)/i;
const DATE = /^(\d{4})-(\d{2})(-(\d{2}))?$/;
const GET_PEOPLE_PARAM: Record<string, string> = { person_id: "id", work_email: "work_email", linkedin_url: "ln_url" };

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const whole = (v: unknown, lo: number, hi: number) => typeof v === "number" && Number.isInteger(v) && v >= lo && v <= hi;
const range = (n: number) => Array.from({ length: n }, (_, i) => String(i + 1));

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  let evaluationDate: string | null = null;
  if (raw.evaluation_date !== undefined && raw.evaluation_date !== null) evaluationDate = parseDate(raw.evaluation_date, "evaluation_date");
  const window = raw.window_months ?? 6;
  if (!whole(window, 1, 60)) throw new ConfigError("window_months must be a whole number from 1 to 60.");
  const triggers = raw.triggers ?? [...TRIGGERS];
  if (!Array.isArray(triggers) || !triggers.length) throw new ConfigError(`triggers must be a non-empty list of: ${[...TRIGGERS].sort().join(", ")}.`);
  const seen: string[] = [];
  triggers.forEach((t: Json, idx: number) => {
    const i = idx + 1;
    if (!TRIGGERS.includes(t)) throw new ConfigError(`triggers[${i}] must be one of: ${[...TRIGGERS].sort().join(", ")}.`);
    if (seen.includes(t)) throw new ConfigError(`triggers[${i}] ${pyRepr(t)} is listed twice.`);
    seen.push(t);
  });
  const defaults: [string, boolean][] = [["executive_only", false], ["count_internal_moves", true], ["with_reasoning", true],
    ["fetch_contact_details", true]];
  for (const [key, def] of defaults) {
    if (typeof (raw[key] ?? def) !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  const executiveOnly = raw.executive_only ?? false;
  if (executiveOnly && !seen.includes("new_exec")) throw new ConfigError("executive_only needs new_exec in triggers.");
  if (seen.includes("new_exec") && window > 36) {
    throw new ConfigError("window_months must be 36 or less when new_exec is in triggers, because is_new_exec " +
      "accepts at most 36.");
  }
  const levels = raw.executive_seniorities ?? null;
  if (levels !== null) {
    if (!Array.isArray(levels) || !levels.length || levels.some((s: Json) => !EXEC_LEVELS.includes(s)) || new Set(levels).size !== levels.length) {
      throw new ConfigError(`executive_seniorities must be a non-empty list without repeats, taken from: ${EXEC_LEVELS.join(", ")}.`);
    }
    if (!seen.includes("new_exec")) throw new ConfigError("executive_seniorities needs new_exec in triggers.");
  }
  const search = raw.search ?? {};
  if (!isObj(search)) throw new ConfigError("search must be an object.");
  const unknownSearch = Object.keys(search).filter((k) => !SEARCH_KEYS.has(k)).sort();
  if (unknownSearch.length) throw new ConfigError(`Unknown search key(s): ${unknownSearch.join(", ")}`);
  const country = search.selected_country ?? "AU";
  if (!COUNTRIES.includes(country as string)) throw new ConfigError(`search.selected_country must be one of: ${COUNTRIES.join(", ")}.`);
  const position = search.position ?? null;
  if (position !== null && (typeof position !== "string" || !position.trim() || position.length > 200)) {
    throw new ConfigError("search.position must be text of 1 to 200 characters.");
  }
  const seniority = search.seniority ?? null;
  if (seniority !== null && !range(7).includes(seniority as string)) {
    throw new ConfigError("search.seniority must be one seniority id from 1 to 7, written as text.");
  }
  const department = search.department ?? null;
  if (department !== null && !range(17).includes(department as string)) {
    throw new ConfigError("search.department must be one department id from 1 to 17, written as text.");
  }
  const maxPeople = search.max_people ?? 25;
  if (!whole(maxPeople, 1, 100)) throw new ConfigError("search.max_people must be a whole number from 1 to 100.");
  return {
    evaluation_date: evaluationDate, window_months: window, triggers: TRIGGERS.filter((t) => seen.includes(t)),
    executive_only: executiveOnly, executive_seniorities: levels, count_internal_moves: raw.count_internal_moves ?? true,
    with_reasoning: raw.with_reasoning ?? true, fetch_contact_details: raw.fetch_contact_details ?? true,
    search: { selected_country: country, position, seniority, department, max_people: maxPeople },
  };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: [...ID_COLUMNS, ...ACCOUNT_COLUMNS] });
}

export function rowKind(row: Row): string {
  return ID_COLUMNS.some((c) => row[c]) ? "contact" : "account";
}

/** Returns the checked identifier of one row, or a reason the row cannot be used. */
export function parseRow(row: Row): [Json | null, string | null] {
  if (rowKind(row) === "contact") {
    for (const col of ID_COLUMNS) {
      const v = row[col];
      if (!v) continue;
      if (col === "person_id" && !PERSON_ID.test(v)) return [null, `person_id ${pyRepr(v)} is not a Firmable person ID (fp followed by 12 digits).`];
      if (col === "work_email" && !EMAIL.test(v)) return [null, `work_email ${pyRepr(v)} is not an email address.`];
      if (col === "linkedin_url" && !LINKEDIN.test(v)) return [null, `linkedin_url ${pyRepr(v)} is not a LinkedIn profile URL (linkedin.com/in/...).`];
    }
    const kind = ID_COLUMNS.find((c) => row[c]) as string;
    return [{ kind: "contact", id_type: kind, id_value: row[kind], key: `${kind}=${row[kind]}` }, null];
  }
  const cid = row.company_id;
  if (cid) {
    if (!COMPANY_ID.test(cid)) return [null, `company_id ${pyRepr(cid)} is not a Firmable company ID (f followed by 12 digits).`];
    return [{ kind: "account", company_id: cid, domain: null, key: `company_id=${cid}` }, null];
  }
  const domain = normaliseDomain(row.domain);
  if (!domain) return [null, `domain ${pyRepr(row.domain ?? null)} is not a valid domain.`];
  return [{ kind: "account", company_id: null, domain, key: `domain=${domain}` }, null];
}

/** The same day `months` calendar months earlier, moved to the month's last day when it is shorter. */
export function monthsBefore(iso: string, months: number): string {
  const [yy, mm, dd] = iso.split("-").map(Number);
  let y = yy, m = mm - months;
  while (m < 1) { m += 12; y -= 1; }
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const day = Math.min(dd, last);
  return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const accounts: string[] = [];
  const contacts: string[] = [];
  const domains: string[] = [];
  for (const r of rows) {
    const [p] = parseRow(r);
    if (p === null) continue;
    const target = p.kind === "account" ? accounts : contacts;
    if (!target.includes(p.key)) target.push(p.key);
    if (p.kind === "account" && p.domain && !domains.includes(p.domain)) domains.push(p.domain);
  }
  const maxPeople: number = config.search.max_people;
  const people = accounts.length * maxPeople + contacts.length;
  const answers = people * config.triggers.length;
  const calls = [
    new PlannedCall("GET /decisions", "Read the decision tool list and check the trigger tools", 1, []),
    new PlannedCall("GET /company", "Resolve each unique account domain to a company ID", domains.length,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * domains.length)]]),
    new PlannedCall("POST /people/search", "Search people at each unique account", accounts.length, []),
    new PlannedCall("POST /decisions", `${config.triggers.length} trigger(s) for up to ${maxPeople} people ` +
      "per account and for each unique contact", answers, pricing.estimateDecision(answers, config.with_reasoning)),
  ];
  if (config.fetch_contact_details) {
    calls.push(new PlannedCall("GET /people", "Person profile for each eligible person, assuming everyone is " +
      "eligible", people, [["person", pricing.round4(pricing.PERSON_PROFILE * people)]]));
  }
  return calls;
}

const labels = (items: Json[]) => items.map((c) => c.label).join(", ");

function windowPosition(started: unknown, since: string, evaluationDate: string): string | null {
  if (typeof started !== "string") return null;
  const m = DATE.exec(started);
  if (!m) return null;
  const [lo, hi] = m[4] ? [since, evaluationDate] : [since.slice(0, 7), evaluationDate.slice(0, 7)];
  if (started < lo) return "before";
  if (started > hi) return "after";
  return null;
}

/** People search. A documented no-match response (success false, no records) counts as zero people. */
async function search(client: FirmableClient, body: Json): Promise<[Json, boolean]> {
  const resp = await client.searchPeople(body);
  return [resp, resp.success === false];
}

const dnd = (v: unknown) => (v === true ? "yes" : v === false ? "no" : "unknown");

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const evaluationDate: string = cfg.evaluation_date ?? ctx.today;
  const since = monthsBefore(evaluationDate, cfg.window_months);
  const coverage: string[] = [
    "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no " +
      "or as a yes.",
    "A new role, new executive role or job change is a timing signal for outreach. It does not show buying " +
      "intent.",
    `Evaluation date ${evaluationDate}, window ${cfg.window_months} month(s), window start ${since}. ` +
      `has_changed_jobs_since is asked with since ${since}.`,
    "is_new_to_role and is_new_exec take no reference date, so they count back from Firmable's current date, " +
      "not from the evaluation date.",
    "Start dates are reported as returned in details.started. They are not corrected.",
    "This recipe sends no outreach.",
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length } }, coverage };
  };

  const inputs: Record<string, Json> = {
    new_to_role: { within_months: cfg.window_months, count_internal_moves: cfg.count_internal_moves },
    new_exec: { within_months: cfg.window_months },
    changed_jobs: { since },
  };
  if (cfg.executive_seniorities) inputs.new_exec.seniorities = cfg.executive_seniorities;
  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no triggers were asked.");
  }
  const problems = decisions.checkTools(catalogue, cfg.triggers.map((t: string) => ({ tool: TOOLS[t], inputs: inputs[t] })), "person");
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("The trigger tools are missing or no longer accept the inputs this recipe sends.");
  }
  const modelTools = [...new Set<string>(cfg.triggers.filter((t: string) => decisions.usesModel(catalogue, TOOLS[t])).map((t: string) => TOOLS[t]))].sort();
  if (modelTools.length) {
    coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
      "This code applies only the eligibility rules in the README.");
  }
  if (cfg.executive_only) {
    coverage.push("executive_only is set, so only New executive (is_new_exec) decides eligibility. Other " +
      "triggers are shown for information.");
  }
  const parsed = ctx.rows.map((r) => parseRow(r)[0]);
  const srch = cfg.search;
  if (parsed.some((p) => p && p.kind === "account")) {
    const filters = [`country ${srch.selected_country}`];
    for (const k of ["position", "seniority", "department"]) if (srch[k]) filters.push(`${k} ${srch[k]}`);
    coverage.push(`People search filters: ${filters.join(", ")}. People search has no start-date filter, so ` +
      `each retrieved person is checked with Decisions, up to ${srch.max_people} people per account.`);
  }
  if (!cfg.fetch_contact_details) coverage.push("fetch_contact_details is false, so no contact details were retrieved.");

  const keyed = ctx.rows.map((r, i) => ({ ...r, _key: parsed[i] ? parsed[i].key : "" })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) {
    coverage.push(`${nDupes} row(s) repeat an earlier account or contact; identical requests were ` +
      "answered from memory, not sent again.");
  }
  let emptySearches = 0;

  const conflicts = (results: Json[]): string[] => {
    const by: Record<string, Json> = Object.fromEntries(results.map((r) => [r.trigger, r]));
    const out: string[] = [];
    const ntr = by.new_to_role, cj = by.changed_jobs;
    if (cfg.count_internal_moves && ntr && ntr.answer === "no") {
      for (const t of ["new_exec", "changed_jobs"]) {
        const other = by[t];
        if (other && other.answer === "yes") out.push(`${other.tool} answered yes but is_new_to_role answered no. Both answers are shown as returned.`);
      }
    }
    if (ntr && cj && ntr.answer === "yes" && cj.answer === "no" && (ntr.details ?? {}).inside_same_company === false) {
      out.push("is_new_to_role answered yes for a move to a different company, but has_changed_jobs_since " +
        "answered no. Both answers are shown as returned.");
    }
    const yes = results.filter((r) => r.answer === "yes" && r.started);
    const starts: string[] = [];
    for (const r of yes) if (!starts.includes(String(r.started))) starts.push(String(r.started));
    if (starts.length > 1) {
      out.push("Triggers that answered yes returned different start dates: " + yes.map((r) => `${r.tool} ${r.started}`).join("; ") + ".");
    }
    for (const r of yes) {
      const pos = windowPosition(r.started, since, evaluationDate);
      if (pos === "before") out.push(`${r.tool} answered yes but returned start date ${r.started}, which is before the window start ${since}.`);
      else if (pos === "after") out.push(`${r.tool} answered yes but returned start date ${r.started}, which is after the evaluation date ${evaluationDate}.`);
    }
    return out;
  };

  const evaluate = async (subject: Json, rowId: string): Promise<Json> => {
    const results: Json[] = [];
    for (const t of cfg.triggers as string[]) {
      const entry = { trigger: t, label: LABELS[t], tool: TOOLS[t], answer: null, started: null, error: null };
      if (ctx.stopped !== null) { results.push({ ...entry, result: "not_asked" }); continue; }
      let resp: Json;
      try {
        resp = await client.makeDecision(TOOLS[t], { ...subject, ...inputs[t] }, { withReasoning: cfg.with_reasoning });
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        results.push({ ...entry, result: "error", error: ctx.recordError(rowId, e) });
        continue;
      }
      const ans = decisions.answerFrom(resp);
      const result = decisions.compare(ans.answer, "yes");
      const details = isObj(ans.details) ? ans.details : {};
      results.push({ ...entry, ...ans, result, started: details.started ?? null, error: result === "error" ? ans.answer_error : null });
    }
    const qualifying: string[] = cfg.executive_only ? ["new_exec"] : cfg.triggers;
    const q = results.filter((r) => qualifying.includes(r.trigger));
    const yes = q.filter((r) => r.result === "pass");
    const errored = q.filter((r) => r.result === "error" || r.result === "not_asked");
    const unknown = q.filter((r) => r.result === "unknown");
    let status: string, reason: string;
    if (yes.length) [status, reason] = ["eligible", `Qualifying trigger(s) answered yes: ${labels(yes)}.`];
    else if (errored.length) {
      [status, reason] = ["incomplete", "No qualifying trigger answered yes, and these have no answer because of " +
        `an error or because the run stopped: ${labels(errored)}.`];
    } else if (unknown.length) {
      [status, reason] = ["needs_review", `No qualifying trigger answered yes, and these answered unknown: ` +
        `${labels(unknown)}. Unknown means Firmable does not hold the fact; it is not a no.`];
    } else [status, reason] = ["not_eligible", "Every qualifying trigger answered no."];
    if (cfg.executive_only && status !== "eligible") {
      const others = results.filter((r) => !qualifying.includes(r.trigger) && r.result === "pass");
      if (others.length) reason += ` ${labels(others)} answered yes, but executive_only counts only New executive.`;
    }
    const reasons = results.filter((r) => r.result === "pass").map((r) => `${r.label} (${r.tool}) answered yes; ` +
      (r.started ? `start date returned: ${r.started}.` : "no start date returned."));
    return { status, status_reason: reason, triggers: results, trigger_reasons: reasons, conflicts: conflicts(results) };
  };

  const contactInfo = async (params: Record<string, string>, rowId: string): Promise<[Json | null, string | null]> => {
    if (!cfg.fetch_contact_details) return [null, "Not retrieved, because fetch_contact_details is false."];
    if (ctx.stopped !== null) return [null, "Not retrieved, because the run stopped."];
    let person: Json;
    try {
      person = await client.getPerson(params);
    } catch (e) {
      if (e instanceof NotFoundError) return [null, "Firmable has no person profile for this identifier, so no contact details were retrieved."];
      if (!(e instanceof FirmableError)) throw e;
      const err = ctx.recordError(rowId, e);
      return [null, `Contact details could not be retrieved: ${err.message}`];
    }
    return [{ profile: facts.personFacts(person), contact: facts.contactDetails(person) }, null];
  };

  const personRecord = (ev: Json, info: Json | null, infoNote: string | null, fields: Json): Json => {
    const profile = info ? info.profile : null;
    return { ...fields, ...ev, profile, contact: info ? info.contact : null, contact_note: infoNote,
      name: fields.name || (profile ?? {}).name || null,
      position: fields.position || (profile ?? {}).position || null,
      company_name: fields.company_name || (profile ?? {}).current_company_name || null };
  };

  const one = async (row: Row): Promise<Json> => {
    const [p, problem] = parseRow(row);
    const base: Json = { row_id: row.row_id, input: row, row_type: rowKind(row), duplicate_of: dupes[row.row_id] ?? null,
      company: null, company_id: null, search: null, people: [], eligible_count: 0 };
    if (p === null) return { ...base, status: "invalid_input", status_reason: problem };
    if (p.kind === "contact") {
      const ev = await evaluate({ [p.id_type]: p.id_value }, row.row_id);
      let info: Json | null = null, infoNote: string | null = null;
      if (ev.status === "eligible") [info, infoNote] = await contactInfo({ [GET_PEOPLE_PARAM[p.id_type]]: p.id_value }, row.row_id);
      const entity = ev.triggers.find((t: Json) => t.entity_id)?.entity_id ?? null;
      const personId = p.id_type === "person_id" ? p.id_value : entity;
      const person = personRecord(ev, info, infoNote, { person_id: personId,
        identifier: { type: p.id_type, value: p.id_value }, source: "input", name: null, position: null, company_name: null });
      return { ...base, status: ev.status, status_reason: ev.status_reason, people: [person],
        eligible_count: ev.status === "eligible" ? 1 : 0 };
    }
    let companyId: string | null = p.company_id;
    if (p.domain) {
      let company: Json;
      try {
        company = await client.getCompany({ fqdn: p.domain });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this domain, so no people were searched." };
        }
        throw e;
      }
      base.company = facts.companyFacts(company);
      companyId = company.id;
    }
    base.company_id = companyId;
    const body: Json = { companyId, selectedCountry: srch.selected_country, from: "0", size: String(srch.max_people) };
    for (const k of ["position", "seniority", "department"]) if (srch[k]) body[k] = srch[k];
    const [resp, empty] = await search(client, body);
    if (empty) emptySearches += 1;
    const records: Json[] = (Array.isArray(resp.records) ? resp.records : []).filter((r: Json) => isObj(r));
    const total = typeof resp.total === "number" && Number.isInteger(resp.total) ? resp.total : null;
    const notRetrieved = total !== null && total > records.length ? total - records.length : 0;
    const skipped: Json[] = [];
    const people: Json[] = [];
    const seen: string[] = [];
    for (const rec of records) {
      const pid = rec.person_id || rec.id || null;
      if (typeof pid !== "string" || !PERSON_ID.test(pid)) {
        skipped.push({ id: pid, name: rec.name ?? null,
          reason: "The person ID from people search is not in the fp format that Decisions accepts." });
        continue;
      }
      if (seen.includes(pid)) continue;
      seen.push(pid);
      const ev = await evaluate({ person_id: pid }, row.row_id);
      let info: Json | null = null, infoNote: string | null = null;
      if (ev.status === "eligible") [info, infoNote] = await contactInfo({ id: pid }, row.row_id);
      people.push(personRecord(ev, info, infoNote, { person_id: pid, identifier: null, source: "people_search",
        name: rec.name ?? null, position: rec.position ?? null, company_name: rec.company_name ?? null }));
    }
    base.search = { request: body, total, retrieved: records.length, not_retrieved: notRetrieved, skipped, no_match_response: empty };
    base.people = people;
    const statuses: string[] = people.map((x) => x.status);
    const n = people.length;
    const count = (s: string) => statuses.filter((x) => x === s).length;
    const eligible = count("eligible");
    base.eligible_count = eligible;
    let status: string, reason: string;
    if (!n) {
      [status, reason] = ["no_people_found", records.length ? "People search returned no person this recipe could check."
        : "People search returned no people for this company with the configured filters."];
    }
    else if (eligible) [status, reason] = ["eligible_contacts_found", `${eligible} of ${n} person(s) evaluated are eligible.`];
    else if (statuses.includes("incomplete")) {
      [status, reason] = ["incomplete", `No person evaluated is eligible, and ${count("incomplete")} of ` +
        `${n} have a qualifying trigger without an answer because of an error or because the run stopped.`];
    } else if (statuses.includes("needs_review")) {
      [status, reason] = ["needs_review", `No person evaluated is eligible, and ${count("needs_review")} ` +
        `of ${n} have a qualifying trigger that answered unknown.`];
    } else [status, reason] = ["no_eligible_contacts", `None of the ${n} person(s) evaluated is eligible.`];
    if (notRetrieved) reason += ` ${notRetrieved} more matching person(s) were not retrieved, because search.max_people is ${srch.max_people}.`;
    if (skipped.length) reason += ` ${skipped.length} search record(s) were skipped, because their person ID is not in the format Decisions accepts.`;
    return { ...base, status, status_reason: reason };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  if (emptySearches) {
    coverage.push(`People search answered success false with no records list for ${emptySearches} ` +
      "account(s). The API docs describe this as the response when nobody matches; it is counted as " +
      "zero people.");
  }
  const counts: Record<string, number> = {};
  const personCounts: Record<string, number> = {};
  const eligibleIds: string[] = [];
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    for (const person of r.people ?? []) {
      personCounts[person.status] = (personCounts[person.status] ?? 0) + 1;
      const key = person.person_id || `${r.row_id}`;
      if (person.status === "eligible" && !eligibleIds.includes(key)) eligibleIds.push(key);
    }
  }
  return { rows, summary: { status_counts: counts, person_status_counts: personCounts,
    unique_eligible_people: eligibleIds.length, evaluation_date: evaluationDate, window_start: since,
    window_months: cfg.window_months }, coverage };
}

export const CSV_COLUMNS = ["row_id", "row_type", "duplicate_of", "row_status", "row_status_reason", "company_id", "company_name",
  "person_id", "person_name", "position", "person_status", "person_status_reason", "trigger_results",
  "trigger_reasons", "start_dates", "conflicts", "work_emails", "phones", "contact_note"];

export function csvRows(result: Json): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const r of result.rows) {
    const company = r.company ?? {};
    const head: Record<string, unknown> = { row_id: r.row_id, row_type: r.row_type ?? null, duplicate_of: r.duplicate_of ?? null,
      row_status: r.status, row_status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: r.company_id ?? null, company_name: company.name ?? null };
    const people: Json[] = r.people ?? [];
    if (!people.length) { out.push(head); continue; }
    for (const person of people) {
      const contact = person.contact ?? {};
      const starts: string[] = [];
      for (const t of person.triggers) if (t.result === "pass" && t.started && !starts.includes(String(t.started))) starts.push(String(t.started));
      out.push({ ...head, company_name: company.name || person.company_name || null,
        person_id: person.person_id ?? null, person_name: person.name ?? null, position: person.position ?? null,
        person_status: person.status, person_status_reason: person.status_reason,
        trigger_results: person.triggers.map((t: Json) => `${t.trigger}=${t.result}(${t.answer || "-"})`),
        trigger_reasons: person.trigger_reasons, start_dates: starts, conflicts: person.conflicts,
        work_emails: contact.work_emails ?? [],
        phones: (contact.phones ?? []).map((ph: Json) => `${ph.value} (do not call flag: ${dnd(ph.is_dnd)})`),
        contact_note: person.contact_note ?? null });
    }
  }
  return out;
}

export function render(report: Report, result: Json): void {
  const summary = result.summary;
  const counts: Record<string, number> = summary.status_counts ?? {};
  report.add("Result", "Outreach window summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    kv([["Evaluation date", summary.evaluation_date], ["Window start", summary.window_start],
      ["Window, months", summary.window_months], ["Unique eligible people", summary.unique_eligible_people]]) +
    note("A new role is a timing signal for outreach. It does not show buying intent.", "info") +
    note("Unknown means Firmable does not hold the fact. A person whose qualifying triggers answered " +
      "unknown is <strong>needs review</strong>, never eligible.", "unknown"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const inp = r.input;
    const company = r.company ?? {};
    const title = company.name || inp.domain || inp.company_id || inp.person_id || inp.work_email || inp.linkedin_url || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    left += kv([["Row type", r.row_type], ["Firmable company ID", r.company_id], ["Domain", company.fqdn],
      ["Head office country", company.hq_country]]);
    const s = r.search;
    if (s) {
      left += kv([["People matching the search", s.total], ["Retrieved", s.retrieved], ["Not retrieved", s.not_retrieved]]);
      for (const k of s.skipped ?? []) left += note(`Skipped ${esc(k.name)} (${esc(k.id)}): ${esc(k.reason)}`, "review");
    }
    if (r.duplicate_of) left += note(`Same account or contact as row ${esc(r.duplicate_of)}; identical requests were reused.`, "info");
    let right = "";
    for (const person of r.people ?? []) {
      right += `<h3>${esc(person.name || person.person_id || "Contact")} ` +
        `${badge(person.status.replace(/_/g, " "), STATUS_TONE[person.status] ?? "neutral")}</h3>` +
        `<p>${esc(person.status_reason)}</p>`;
      right += kv([["Firmable person ID", person.person_id], ["Position", person.position], ["Company", person.company_name]]);
      right += table(["Trigger", "Tool", "Answer", "Result", "Start date returned", "Confidence"], person.triggers.map((t: Json) => [
        t.label, t.tool, t.answer || "none", new Raw(badge(t.result.replace(/_/g, " "), RESULT_TONE[t.result] ?? "neutral")),
        t.started, t.confidence ?? null]));
      for (const t of person.triggers) {
        const rs = t.reasoning ?? {};
        if (rs.reason) {
          right += `<p><strong>${esc(t.label)}:</strong> ${esc(rs.reason)}</p>` +
            `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
        }
        if (t.error) right += note(`${esc(t.label)}: ${esc(typeof t.error === "object" ? t.error.message : t.error)}`, "error");
      }
      for (const c of person.conflicts ?? []) right += note(esc(c), "review");
      const contact = person.contact;
      if (contact) {
        right += kv([["Work emails", (contact.work_emails ?? []).join(", ") || null],
          ["Phones", (contact.phones ?? []).map((ph: Json) => `${ph.value} (do not call flag: ${dnd(ph.is_dnd)})`).join(", ") || null]]);
      }
      if (person.contact_note) right += `<p class="muted">${esc(person.contact_note)}</p>`;
      right += detailsJson("Triggers as JSON", person.triggers);
    }
    cards.push(card(String(title), badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No people were evaluated for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Rows", "Each account or contact with the people evaluated", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
