/**
 * Champion tracker: finds people at target accounts who used to work at companies you name.
 * Mirrors recipes/champion-tracker/python/src/champion_tracker/recipe.py; see that file and the
 * recipe README for the rules.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Recipe, Report, RunContext, Row, badge, card,
  decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note, pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "champion-tracker";
export const TITLE = "Champion tracker";
export const DESCRIPTION = "Finds people at target accounts who previously worked at your customers or at other companies you " +
  "name, using Firmable people search and the previously_worked_at decision tool. A past employer is a " +
  "route to a warm introduction, not evidence of buying intent.";

const TOOL = "previously_worked_at";
const COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
const PLACEHOLDERS = ["account_name", "first_name", "past_employer", "title"];
const ACCOUNT_COLUMNS = ["company_id", "domain"];
const DETAIL_KEYS = ["employer", "years_there", "title", "past_employers_checked"];

const STATUS_TONE: Record<string, string> = {
  champions_found: "good", champion: "good", no_champions_found: "bad", not_alumni: "bad", needs_review: "review",
  unknown: "unknown", incomplete: "error", no_people_found: "unknown", company_not_found: "unknown",
  person_not_found: "unknown", invalid_input: "error", error: "error", not_asked: "neutral", not_processed: "unknown",
};
const TOP_KEYS = new Set(["past_companies", "include_current", "search", "with_reasoning", "fetch_contact_details",
  "opener_templates"]);
const SEARCH_KEYS = new Set(["selected_country", "position", "seniority", "department", "max_people"]);
const PERSON_ID = /^fp\d{12}$/;
const COMPANY_ID = /^f\d{12}$/;
const PLACEHOLDER = /\{([^{}]*)\}/g;

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const whole = (v: unknown, lo: number, hi: number) => typeof v === "number" && Number.isInteger(v) && v >= lo && v <= hi;
const range = (n: number) => Array.from({ length: n }, (_, i) => String(i + 1));
const placeholders = (t: string) => [...t.matchAll(PLACEHOLDER)].map((m) => m[1]);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const companies = raw.past_companies ?? null;
  if (companies === null) throw new ConfigError("past_companies is required: a list of 1 to 50 company names or Firmable company IDs.");
  if (!Array.isArray(companies) || companies.length < 1 || companies.length > 50) {
    throw new ConfigError("past_companies must be a list of 1 to 50 company names or Firmable company IDs.");
  }
  const seen: string[] = [];
  companies.forEach((c: Json, idx: number) => {
    const i = idx + 1;
    if (typeof c !== "string" || !c.trim() || c.length > 200) throw new ConfigError(`past_companies[${i}] must be text of 1 to 200 characters.`);
    if (seen.includes(c.trim().toLowerCase())) throw new ConfigError(`past_companies[${i}] ${pyRepr(c)} is listed twice.`);
    seen.push(c.trim().toLowerCase());
  });
  const defaults: [string, boolean][] = [["include_current", false], ["with_reasoning", false], ["fetch_contact_details", false]];
  for (const [key, def] of defaults) {
    if (typeof (raw[key] ?? def) !== "boolean") throw new ConfigError(`${key} must be true or false.`);
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
  const templates = raw.opener_templates ?? null;
  if (templates !== null) {
    if (!Array.isArray(templates) || templates.length < 1 || templates.length > 10) {
      throw new ConfigError("opener_templates must be a list of 1 to 10 text templates.");
    }
    templates.forEach((t: Json, idx: number) => {
      const i = idx + 1;
      if (typeof t !== "string" || !t.trim() || t.length > 500) throw new ConfigError(`opener_templates[${i}] must be text of 1 to 500 characters.`);
      const bad = [...new Set(placeholders(t).filter((m) => !PLACEHOLDERS.includes(m)))].sort();
      if (bad.length) {
        const names = bad.map((b) => `{${b}}`).join(", ");
        const allowed = PLACEHOLDERS.map((p) => `{${p}}`).join(", ");
        throw new ConfigError(`opener_templates[${i}] uses unknown placeholder(s): ${names}. Allowed: ${allowed}.`);
      }
    });
  }
  return {
    past_companies: [...companies], include_current: raw.include_current ?? false,
    with_reasoning: raw.with_reasoning ?? false, fetch_contact_details: raw.fetch_contact_details ?? false,
    opener_templates: templates !== null ? [...templates] : [],
    search: { selected_country: country, position, seniority, department, max_people: maxPeople },
  };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ACCOUNT_COLUMNS });
}

/** Returns the checked account identifier of one row, or a reason the row cannot be used. */
export function parseRow(row: Row): [Json | null, string | null] {
  const cid = row.company_id;
  if (cid) {
    if (!COMPANY_ID.test(cid)) return [null, `company_id ${pyRepr(cid)} is not a Firmable company ID (f followed by 12 digits).`];
    return [{ company_id: cid, domain: null, key: `company_id=${cid}` }, null];
  }
  const domain = normaliseDomain(row.domain);
  if (!domain) return [null, `domain ${pyRepr(row.domain ?? null)} is not a valid domain.`];
  return [{ company_id: null, domain, key: `domain=${domain}` }, null];
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const accounts: string[] = [];
  for (const r of rows) {
    const [p] = parseRow(r);
    if (p !== null && !accounts.includes(p.key)) accounts.push(p.key);
  }
  const maxPeople: number = config.search.max_people;
  const people = accounts.length * maxPeople;
  const calls = [
    new PlannedCall("GET /decisions", "Read the decision tool list and check previously_worked_at", 1, []),
    new PlannedCall("GET /company", "Resolve each unique account by domain or company ID", accounts.length,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * accounts.length)]]),
    new PlannedCall("POST /people/search", "Search people at each unique account", accounts.length, []),
    new PlannedCall("POST /decisions", `previously_worked_at for up to ${maxPeople} people per account`,
      people, pricing.estimateDecision(people, config.with_reasoning)),
  ];
  if (config.fetch_contact_details) {
    calls.push(new PlannedCall("GET /people", "Person profile for each champion, assuming everyone checked is a " +
      "champion", people, [["person", pricing.round4(pricing.PERSON_PROFILE * people)]]));
  }
  return calls;
}

/** Fills each template; a template with any empty placeholder is skipped and listed with what was missing. */
export function fillTemplates(templates: string[], values: Record<string, string | null>): [Json[], Json[]] {
  const filled: Json[] = [];
  const skipped: Json[] = [];
  templates.forEach((t, idx) => {
    const i = idx + 1;
    const missing: string[] = [];
    for (const name of placeholders(t)) if ((values[name] ?? null) === null && !missing.includes(name)) missing.push(name);
    if (missing.length) { skipped.push({ template: i, missing }); return; }
    filled.push({ template: i, text: t.replace(PLACEHOLDER, (_m, name: string) => String(values[name])) });
  });
  return [filled, skipped];
}

const dnd = (v: unknown) => (v === true ? "yes" : v === false ? "no" : "unknown");
const skippedText = (s: Json) => `template ${s.template} missing ` + s.missing.join(", ");

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const srch = cfg.search;
  const coverage: string[] = [
    "An unknown answer means Firmable does not hold the facts needed to answer. It is never counted as a no " +
      "or as a yes.",
    "A past employer is a route to a warm introduction. It does not show buying intent.",
    `Past companies checked (${cfg.past_companies.length}): ${cfg.past_companies.join(", ")}. ` +
      "previously_worked_at matches the past employer names Firmable holds, so a past employer recorded under " +
      "another name may not match.",
    cfg.include_current
      ? "include_current is true, so a person's current employer and earlier roles there also count."
      : "include_current is false, so a person's current employer does not count as a past employer.",
    `People search has no past-employer filter, so only the first ${srch.max_people} people per account are ` +
      "checked. A champion who was not checked is not observed in retrieved data. That does not mean none exist.",
    "Past employer details (employer, years_there, title, past_employers_checked) are shown as returned. They " +
      "are not corrected.",
    "This recipe sends no outreach.",
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length } }, coverage };
  };

  const inputs = { companies: cfg.past_companies, include_current: cfg.include_current };
  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no people were checked.");
  }
  const problems = decisions.checkTools(catalogue, [{ tool: TOOL, inputs }], "person");
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("previously_worked_at is missing or no longer accepts the inputs this recipe sends.");
  }
  if (decisions.usesModel(catalogue, TOOL)) {
    coverage.push(`${TOOL} uses a decision model inside Firmable's service. This code applies only the ` +
      "rules in the README.");
  }
  const filters = [`country ${srch.selected_country}`];
  for (const k of ["position", "seniority", "department"]) if (srch[k]) filters.push(`${k} ${srch[k]}`);
  coverage.push(`People search filters: ${filters.join(", ")}.`);
  if (!cfg.fetch_contact_details) {
    coverage.push("fetch_contact_details is false, so no contact details were retrieved and {first_name} " +
      "has no value in opener templates.");
  }
  if (cfg.opener_templates.length) {
    coverage.push("Suggested openers are predefined templates filled from API facts or input. They are not " +
      "API facts. A template with an empty placeholder is skipped and listed.");
  }

  const parsed = ctx.rows.map((r) => parseRow(r)[0]);
  const keyed = ctx.rows.map((r, i) => ({ ...r, _key: parsed[i] ? parsed[i].key : "" })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) {
    coverage.push(`${nDupes} row(s) repeat an earlier account; identical requests were answered from ` +
      "memory, not sent again.");
  }
  let emptySearches = 0;

  const ask = async (pid: string, rowId: string): Promise<Json> => {
    let out: Json = { tool: TOOL, answer: null, confidence: null, details: null, reasoning: null,
      alumni: Object.fromEntries(DETAIL_KEYS.map((k) => [k, null])), error: null };
    if (ctx.stopped !== null) return { ...out, result: "not_asked", status_reason: "Not asked, because the run stopped." };
    let resp: Json;
    try {
      resp = await client.makeDecision(TOOL, { person_id: pid, ...inputs }, { withReasoning: cfg.with_reasoning });
    } catch (e) {
      if (e instanceof NotFoundError && e.code === "person_not_found") {
        return { ...out, result: "person_not_found",
          status_reason: "Decisions found no Firmable person for this person ID, so the question has no answer." };
      }
      if (!(e instanceof FirmableError)) throw e;
      return { ...out, result: "error", error: ctx.recordError(rowId, e),
        status_reason: "The question has no answer because of an error." };
    }
    const ans = decisions.answerFrom(resp);
    const details = isObj(ans.details) ? ans.details : {};
    out = { ...out, ...ans, alumni: Object.fromEntries(DETAIL_KEYS.map((k) => [k, details[k] ?? null])) };
    const employer = text(details.employer);
    if (ans.answer === "yes") {
      const reason = `${TOOL} answered yes. ` + (employer ? `Past employer returned: ${employer}.` : "No past employer was returned.");
      return { ...out, result: "champion", status_reason: reason };
    }
    if (ans.answer === "no") return { ...out, result: "not_alumni", status_reason: `${TOOL} answered no for the past companies checked.` };
    if (ans.answer === "unknown") {
      return { ...out, result: "unknown",
        status_reason: `${TOOL} answered unknown. Firmable does not hold the facts needed to answer; it is not a no.` };
    }
    return { ...out, result: "error", error: ans.answer_error,
      status_reason: "The answer is not yes, no or unknown, so it is treated as an error." };
  };

  const notes = (q: Json, companyName: string | null, searchCompany: string | null): string[] => {
    const out: string[] = [];
    const employer = text(q.alumni.employer);
    if (q.result === "champion" && employer === null) out.push(`${TOOL} answered yes but returned no past employer in details.employer.`);
    if (q.result === "champion" && employer && !cfg.include_current && companyName &&
        employer.toLowerCase() === companyName.trim().toLowerCase()) {
      out.push(`${TOOL} returned the account itself (${employer}) as the past employer although ` +
        "include_current is false. The answer is shown as returned.");
    }
    if (q.result === "not_alumni" && employer) {
      out.push(`${TOOL} answered no but returned a past employer (${employer}) in details.employer. Both are ` +
        "shown as returned.");
    }
    if (companyName && searchCompany && searchCompany.toLowerCase() !== companyName.trim().toLowerCase()) {
      out.push(`People search returned company name ${searchCompany}, which differs from the account's ` +
        `company name ${companyName.trim()}.`);
    }
    return out;
  };

  const contactInfo = async (pid: string, rowId: string): Promise<[Json | null, string | null]> => {
    if (!cfg.fetch_contact_details) return [null, "Not retrieved, because fetch_contact_details is false."];
    if (ctx.stopped !== null) return [null, "Not retrieved, because the run stopped."];
    let person: Json;
    try {
      person = await client.getPerson({ id: pid });
    } catch (e) {
      if (e instanceof NotFoundError) return [null, "Firmable has no person profile for this person ID, so no contact details were retrieved."];
      if (!(e instanceof FirmableError)) throw e;
      const err = ctx.recordError(rowId, e);
      return [null, `Contact details could not be retrieved: ${err.message}`];
    }
    return [{ profile: facts.personFacts(person), first_name: text(person.first_name), contact: facts.contactDetails(person) }, null];
  };

  const one = async (row: Row): Promise<Json> => {
    const [p, problem] = parseRow(row);
    const base: Json = { row_id: row.row_id, input: row, account_name: row.account_name || null,
      duplicate_of: dupes[row.row_id] ?? null, company: null, company_id: null, search: null, people: [], champion_count: 0 };
    if (p === null) return { ...base, status: "invalid_input", status_reason: problem };
    let company: Json;
    try {
      company = p.domain ? await client.getCompany({ fqdn: p.domain }) : await client.getCompany({ id: p.company_id });
    } catch (e) {
      if (e instanceof NotFoundError) {
        const what = p.domain ? "domain" : "company ID";
        return { ...base, status: "company_not_found",
          status_reason: `Firmable has no company profile for this ${what}, so no people were searched.` };
      }
      throw e;
    }
    base.company = facts.companyFacts(company);
    const companyId = typeof company.id === "string" ? company.id : p.company_id;
    base.company_id = companyId;
    const companyName = text(company.name);
    const accountName = text(row.account_name) ?? companyName;
    const body: Json = { companyId, selectedCountry: srch.selected_country, from: "0", size: String(srch.max_people) };
    for (const k of ["position", "seniority", "department"]) if (srch[k]) body[k] = srch[k];
    const resp = await client.searchPeople(body);
    const empty = resp.success === false;
    if (empty) emptySearches += 1;
    const records: Json[] = (Array.isArray(resp.records) ? resp.records : []).filter((r: Json) => isObj(r));
    const total = typeof resp.total === "number" && Number.isInteger(resp.total) ? resp.total : null;
    const notChecked = total !== null && total > records.length ? total - records.length : 0;
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
      const q = await ask(pid, row.row_id);
      let info: Json | null = null, infoNote: string | null = null;
      if (q.result === "champion") [info, infoNote] = await contactInfo(pid, row.row_id);
      const profile = info ? info.profile : null;
      const position = text(rec.position) ?? text((profile ?? {}).position);
      const { result, ...rest } = q;
      const person: Json = { person_id: pid, name: rec.name || (profile ?? {}).name || null, position,
        search_company_name: rec.company_name ?? null, status: result, ...rest,
        notes: notes(q, companyName, text(rec.company_name)), profile, contact: info ? info.contact : null,
        contact_note: infoNote, suggested_openers: [], skipped_openers: [] };
      if (result === "champion" && cfg.opener_templates.length) {
        const values = { first_name: info ? info.first_name : null, past_employer: text(q.alumni.employer),
          account_name: accountName, title: position };
        [person.suggested_openers, person.skipped_openers] = fillTemplates(cfg.opener_templates, values);
      }
      people.push(person);
    }
    base.search = { request: body, total, retrieved: records.length, not_checked: notChecked, skipped, no_match_response: empty };
    base.people = people;
    const statuses: string[] = people.map((x) => x.status);
    const n = people.length;
    const count = (s: string) => statuses.filter((x) => x === s).length;
    const champions = count("champion");
    base.champion_count = champions;
    const unresolved = count("error") + count("not_asked");
    const unclear = count("unknown") + count("person_not_found");
    let status: string, reason: string;
    if (!n) {
      [status, reason] = ["no_people_found", records.length ? "People search returned no person this recipe could check."
        : "People search returned no people for this company with the configured filters."];
    } else if (champions) {
      [status, reason] = ["champions_found", `${champions} of ${n} person(s) checked previously worked at a listed company.`];
    } else if (unresolved) {
      [status, reason] = ["incomplete", `No champion was observed among the ${n} person(s) checked, and ` +
        `${unresolved} of ${n} have no answer because of an error or because the run stopped.`];
    } else if (unclear) {
      [status, reason] = ["needs_review", `No champion was observed among the ${n} person(s) checked, and ` +
        `${unclear} of ${n} answered unknown or were not found by Decisions.`];
    } else {
      [status, reason] = ["no_champions_found", `No champion was observed among the ${n} person(s) checked. Every ` +
        "person checked answered no."];
    }
    if (notChecked) reason += ` ${notChecked} more matching person(s) were not checked, because search.max_people is ${srch.max_people}.`;
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
  const championIds: string[] = [];
  let checked = 0, notChecked = 0;
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    if (r.duplicate_of) continue;
    notChecked += (r.search ?? {}).not_checked || 0;
    for (const person of r.people ?? []) {
      checked += 1;
      personCounts[person.status] = (personCounts[person.status] ?? 0) + 1;
      if (person.status === "champion" && !championIds.includes(person.person_id)) championIds.push(person.person_id);
    }
  }
  return { rows, summary: { status_counts: counts, person_status_counts: personCounts, people_checked: checked,
    people_not_checked: notChecked, unique_champions: championIds.length }, coverage };
}

export const CSV_COLUMNS = ["row_id", "account_name", "duplicate_of", "row_status", "row_status_reason", "company_id",
  "company_name", "people_matching", "people_not_checked", "person_id", "person_name", "position",
  "person_status", "person_status_reason", "answer", "confidence", "past_employer", "years_there",
  "past_title", "past_employers_checked", "notes", "work_emails", "phones", "contact_note",
  "suggested_openers", "skipped_openers"];

export function csvRows(result: Json): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const r of result.rows) {
    const company = r.company ?? {};
    const search = r.search ?? {};
    const head: Record<string, unknown> = { row_id: r.row_id, account_name: r.account_name ?? null,
      duplicate_of: r.duplicate_of ?? null, row_status: r.status,
      row_status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: r.company_id ?? null, company_name: company.name ?? null,
      people_matching: search.total ?? null, people_not_checked: search.not_checked ?? null };
    const people: Json[] = r.people ?? [];
    if (!people.length) { out.push(head); continue; }
    for (const person of people) {
      const contact = person.contact ?? {};
      const alumni = person.alumni ?? {};
      out.push({ ...head, person_id: person.person_id, person_name: person.name ?? null, position: person.position ?? null,
        person_status: person.status, person_status_reason: person.status_reason, answer: person.answer ?? null,
        confidence: person.confidence ?? null, past_employer: alumni.employer ?? null,
        years_there: alumni.years_there ?? null, past_title: alumni.title ?? null,
        past_employers_checked: alumni.past_employers_checked ?? null, notes: person.notes,
        work_emails: contact.work_emails ?? [],
        phones: (contact.phones ?? []).map((ph: Json) => `${ph.value} (do not call flag: ${dnd(ph.is_dnd)})`),
        contact_note: person.contact_note ?? null,
        suggested_openers: person.suggested_openers.map((o: Json) => o.text),
        skipped_openers: person.skipped_openers.map(skippedText) });
    }
  }
  return out;
}

export function render(report: Report, result: Json): void {
  const summary = result.summary;
  const counts: Record<string, number> = summary.status_counts ?? {};
  report.add("Result", "Champion summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    kv([["People checked", summary.people_checked], ["Matching people not checked", summary.people_not_checked],
      ["Unique champions", summary.unique_champions]]) +
    note("A past employer is a route to a warm introduction. It does not show buying intent.", "info") +
    note("A missing champion means none was observed among the people checked. It does not mean none " +
      "exist. Unknown is never counted as a no.", "unknown"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const inp = r.input;
    const company = r.company ?? {};
    const title = r.account_name || company.name || inp.domain || inp.company_id || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    left += kv([["Firmable company ID", r.company_id], ["Company name", company.name], ["Domain", company.fqdn],
      ["Head office country", company.hq_country]]);
    const s = r.search;
    if (s) {
      left += kv([["People matching the search", s.total], ["Retrieved", s.retrieved], ["Matching people not checked", s.not_checked]]);
      for (const k of s.skipped ?? []) left += note(`Skipped ${esc(k.name)} (${esc(k.id)}): ${esc(k.reason)}`, "review");
    }
    if (r.duplicate_of) left += note(`Same account as row ${esc(r.duplicate_of)}; identical requests were reused.`, "info");
    let right = "";
    for (const person of r.people ?? []) {
      const alumni = person.alumni ?? {};
      right += `<h3>${esc(person.name || person.person_id)} ` +
        `${badge(person.status.replace(/_/g, " "), STATUS_TONE[person.status] ?? "neutral")}</h3>` +
        `<p>${esc(person.status_reason)}</p>`;
      right += kv([["Firmable person ID", person.person_id], ["Current position", person.position],
        ["Answer", person.answer], ["Confidence", person.confidence]]);
      if (person.answer === "yes" || person.answer === "no") {
        right += '<p class="muted">Past employer details as returned by previously_worked_at:</p>' +
          table(["Past employer", "Years there", "Title there", "Past employers checked"],
            [[alumni.employer, alumni.years_there, alumni.title, alumni.past_employers_checked]]);
      }
      const rs = person.reasoning ?? {};
      if (rs.reason) {
        right += `<p><strong>Reason:</strong> ${esc(rs.reason)}</p>` +
          `<p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
      }
      if (person.error) {
        const msg = typeof person.error === "object" ? person.error.message : person.error;
        right += note(esc(msg ?? "No valid answer was returned."), "error");
      }
      for (const c of person.notes ?? []) right += note(esc(c), "review");
      const contact = person.contact;
      if (contact) {
        right += kv([["Work emails", (contact.work_emails ?? []).join(", ") || null],
          ["Phones", (contact.phones ?? []).map((ph: Json) => `${ph.value} (do not call flag: ${dnd(ph.is_dnd)})`).join(", ") || null]]);
      }
      if (person.contact_note) right += `<p class="muted">${esc(person.contact_note)}</p>`;
      if (person.suggested_openers.length || person.skipped_openers.length) {
        right += '<div class="template"><h3>Suggested openers</h3><p class="muted">Predefined templates ' +
          "filled from API facts or input. They are not API facts.</p><ul>" +
          person.suggested_openers.map((o: Json) => `<li>${esc(o.text)}</li>`).join("") + "</ul>" +
          person.skipped_openers.map((k: Json) => `<p class="muted">Skipped ${esc(skippedText(k))}.</p>`).join("") +
          "</div>";
      }
      right += detailsJson("Answer as JSON", Object.fromEntries(["answer", "confidence", "details", "reasoning", "answered_at",
        "credits", "entity_id", "request_id", "error"].map((k) => [k, person[k] ?? null])));
    }
    cards.push(card(String(title), badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No people were checked for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Rows", "Each account with the people checked", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
