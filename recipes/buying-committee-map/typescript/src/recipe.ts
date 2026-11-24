/**
 * Buying committee map: shows who fills each buying-committee role at each account, and which roles have no one observed.
 * Mirrors recipes/buying-committee-map/python/src/buying_committee_map/recipe.py; see that file and the recipe
 * README for the role rules.
 */
import {
  ConfigError, FirmableError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row, badge,
  bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note, pricing, readRows,
  table,
} from "firmable-gtm-client";

export const NAME = "buying-committee-map";
export const TITLE = "Buying committee map";
export const DESCRIPTION = "Finds people at each account with Firmable people search, asks Decisions for each person's department " +
  "and seniority, and maps them to the buying-committee roles you define. Roles with no one observed " +
  "are listed as gaps.";

const COUNTRIES = ["AU", "NZ", "SG", "MY", "ID", "PH", "HK", "JP", "VN", "KR", "TH", "US", "CA"];
// Seniority labels accepted by matches_seniority, from the decision tool catalogue of 24 Nov 2026.
export const SENIORITY_LABELS = ["Owner / Founder", "C-Suite / Partner", "Board Member / Company Director",
  "VP/Director / Head of", "Manager", "Other"];
// People search ids and labels from the published people search reference.
const SEARCH_SENIORITY: Record<string, string> = {
  "1": "Board Member / Company Director", "2": "Owner / Founder", "3": "C-Suite / Partner",
  "4": "VP/Director / Head of", "5": "Manager", "6": "Other", "7": "No Data Available",
};
const SEARCH_DEPARTMENT: Record<string, string> = {
  "1": "General Management", "2": "Sales", "3": "Trades", "4": "Operations", "5": "Engineering & Technical",
  "6": "Human Resources", "7": "Customer Service", "8": "Medicine & Healthcare", "9": "Research & Analysis",
  "10": "Legal", "11": "Marketing", "12": "Education & Training", "13": "Consulting", "14": "Finance",
  "15": "Product", "16": "Other", "17": "No Data Available",
};
const MAX_LIST = 20; // maxItems for departments and seniorities in the decision tool input schemas
const ROLE_STATUSES = ["filled", "incomplete", "possible", "not_observed"];
const PERSON_STATUSES = ["assigned", "unclassified", "incomplete", "no_role", "person_not_found"];
const STATUS_TONE: Record<string, string> = {
  committee_complete: "good", gaps_found: "bad", needs_review: "review", incomplete: "error", no_people_found: "unknown",
  company_not_found: "unknown", invalid_input: "error", error: "error", not_processed: "unknown",
};
const ROLE_TONE: Record<string, string> = { filled: "good", incomplete: "error", possible: "review", not_observed: "bad" };
const PERSON_TONE: Record<string, string> = {
  assigned: "good", unclassified: "review", incomplete: "error", no_role: "neutral", person_not_found: "unknown",
};
const ID = /^[a-z0-9_]{1,40}$/;
const COMPANY_ID = /^f\d+$/;
const TOP_KEYS = new Set(["roles", "search", "with_reasoning", "decision_maker"]);
const ROLE_KEYS = new Set(["id", "label", "departments", "seniorities", "required"]);
const SEARCH_KEYS = new Set(["selected_country", "position", "seniority", "department", "max_people"]);
const DM_KEYS = new Set(["seller", "buyer"]);

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const whole = (v: unknown, low: number, high: number | null): boolean =>
  typeof v === "number" && Number.isInteger(v) && v >= low && (high === null || v <= high);
const unknownKeys = (o: Record<string, unknown>, allowed: Set<string>) => Object.keys(o).filter((k) => !allowed.has(k)).sort();
const blank = (v: unknown) => v === undefined || v === null || v === "";

/** Maps a documented people search id or its label to the id string. */
function code(value: unknown, tbl: Record<string, string>): string | null {
  if (typeof value === "number" && Number.isInteger(value)) value = String(value);
  if (typeof value !== "string") return null;
  const v = value.trim();
  if (v in tbl) return v;
  for (const [k, label] of Object.entries(tbl)) if (label.toLowerCase() === v.toLowerCase()) return k;
  return null;
}

function union(roles: Json[], key: string): string[] {
  const out: string[] = [];
  for (const r of roles) for (const v of r[key]) if (!out.includes(v)) out.push(v);
  return out;
}

export function loadConfig(raw: Json): Json {
  const unknown = unknownKeys(raw, TOP_KEYS);
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const rolesRaw = raw.roles;
  if (!Array.isArray(rolesRaw) || rolesRaw.length < 1 || rolesRaw.length > 10) throw new ConfigError("roles must be a list of 1 to 10 roles.");
  const roles: Json[] = [];
  const seen = new Set<string>();
  rolesRaw.forEach((r: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(r)) throw new ConfigError(`roles[${i}] must be an object.`);
    const bad = unknownKeys(r, ROLE_KEYS);
    if (bad.length) throw new ConfigError(`roles[${i}] has unknown key(s): ${bad.join(", ")}`);
    const rid = r.id;
    if (typeof rid !== "string" || !ID.test(rid)) throw new ConfigError(`roles[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(rid)) throw new ConfigError(`roles[${i}].id '${rid}' is used twice.`);
    seen.add(rid);
    const label = r.label ?? rid;
    if (typeof label !== "string" || !label.trim()) throw new ConfigError(`roles[${i}].label must be non-empty text.`);
    const deps = r.departments;
    if (!Array.isArray(deps) || !deps.length) throw new ConfigError(`roles[${i}].departments must be a non-empty list.`);
    const departments: string[] = [];
    for (const d of deps) {
      if (typeof d !== "string" || !d.trim() || d.trim().length > 100) {
        throw new ConfigError(`roles[${i}].departments must hold non-empty text of up to 100 characters.`);
      }
      if (!departments.includes(d.trim())) departments.push(d.trim());
    }
    const sens = r.seniorities;
    if (!Array.isArray(sens) || !sens.length) throw new ConfigError(`roles[${i}].seniorities must be a non-empty list.`);
    const seniorities: string[] = [];
    for (const s of sens) {
      if (typeof s !== "string" || !SENIORITY_LABELS.includes(s.trim())) {
        throw new ConfigError(`roles[${i}].seniorities must use Firmable's seniority labels: ${SENIORITY_LABELS.join(", ")}.`);
      }
      if (!seniorities.includes(s.trim())) seniorities.push(s.trim());
    }
    const required = r.required ?? true;
    if (typeof required !== "boolean") throw new ConfigError(`roles[${i}].required must be true or false.`);
    roles.push({ id: rid, label: label.trim(), departments, seniorities, required });
  });
  if (!roles.some((r) => r.required)) throw new ConfigError("At least one role must be required.");
  if (union(roles, "departments").length > MAX_LIST) {
    throw new ConfigError(`The roles name more than ${MAX_LIST} departments in total; matches_department accepts up to ${MAX_LIST}.`);
  }

  const s = raw.search ?? {};
  if (!isObj(s)) throw new ConfigError("search must be an object.");
  const bad = unknownKeys(s, SEARCH_KEYS);
  if (bad.length) throw new ConfigError(`search has unknown key(s): ${bad.join(", ")}`);
  const country = s.selected_country ?? "AU";
  if (typeof country !== "string" || !COUNTRIES.includes(country)) {
    throw new ConfigError(`search.selected_country must be one of: ${COUNTRIES.join(", ")}.`);
  }
  const position = s.position ?? "";
  if (typeof position !== "string") throw new ConfigError("search.position must be text.");
  let seniority: string | null = null;
  if (!blank(s.seniority)) {
    seniority = code(s.seniority, SEARCH_SENIORITY);
    if (seniority === null) throw new ConfigError("search.seniority must be a seniority id 1 to 7 or its label.");
  }
  let department: string | null = null;
  if (!blank(s.department)) {
    department = code(s.department, SEARCH_DEPARTMENT);
    if (department === null) throw new ConfigError("search.department must be a department id 1 to 17 or its label.");
  }
  const maxPeople = s.max_people ?? 25;
  if (!whole(maxPeople, 1, 100)) throw new ConfigError("search.max_people must be a whole number from 1 to 100.");

  const withReasoning = raw.with_reasoning ?? false;
  if (typeof withReasoning !== "boolean") throw new ConfigError("with_reasoning must be true or false.");
  const dm = raw.decision_maker;
  let decisionMaker: Json = null;
  if (dm !== undefined && dm !== null) {
    if (!isObj(dm)) throw new ConfigError("decision_maker must be an object.");
    const b = unknownKeys(dm, DM_KEYS);
    if (b.length) throw new ConfigError(`decision_maker has unknown key(s): ${b.join(", ")}`);
    const seller = dm.seller;
    if (typeof seller !== "string" || seller.trim().length < 3 || seller.trim().length > 2000) {
      throw new ConfigError("decision_maker.seller must be text of 3 to 2000 characters.");
    }
    const buyer = dm.buyer;
    if (buyer !== undefined && buyer !== null && (typeof buyer !== "string" || buyer.trim().length > 2000)) {
      throw new ConfigError("decision_maker.buyer must be text of up to 2000 characters.");
    }
    decisionMaker = { seller: seller.trim(), buyer: typeof buyer === "string" && buyer.trim() ? buyer.trim() : null };
  }
  return {
    roles,
    search: { selected_country: country, position: position.trim() || null, seniority, department, max_people: maxPeople },
    with_reasoning: withReasoning, decision_maker: decisionMaker,
  };
}

export function readInput(path: string): Row[] {
  return readRows(path, { anyOf: ["domain", "company_id"] });
}

function accountKey(row: Row): string {
  const cid = (row.company_id ?? "").trim();
  if (cid) return COMPANY_ID.test(cid) ? `id:${cid}` : "";
  const d = normaliseDomain(row.domain);
  return d ? `domain:${d}` : "";
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const keys: string[] = [];
  for (const r of rows) {
    const k = accountKey(r);
    if (k && !keys.includes(k)) keys.push(k);
  }
  const n = keys.length;
  const domains = keys.filter((k) => k.startsWith("domain:")).length;
  const mp: number = config.search.max_people;
  const perPerson = config.decision_maker ? 3 : 2;
  const answers = n * mp * perPerson;
  return [
    new PlannedCall("GET /decisions", "Read the decision tool list and check the tools this recipe uses", 1, []),
    new PlannedCall("GET /company", "Company profile for each unique valid domain without a company_id", domains,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * domains)]]),
    new PlannedCall("POST /people/search", "One search for each unique account", n, []),
    new PlannedCall("POST /decisions", `${perPerson} question(s) for up to ${mp} person(s) per account`, answers,
      pricing.estimateDecision(answers, config.with_reasoning)),
  ];
}

/** One answer's side for one role: yes, no, unclear or error. */
function side(check: Json, values: string[]): string {
  if (["error", "not_asked", "person_not_found"].includes(check.result)) return "error";
  if (check.result === "unknown") return "unclear";
  if (check.result === "fail") return "no";
  if (check.value === null) return "unclear";
  return values.includes(check.value) ? "yes" : "no";
}

function detailValue(ans: Json, attr: string): string | null {
  const v = isObj(ans.details) ? ans.details[attr] : null;
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

function labels(roles: Json[], ids: string[]): string {
  const by: Record<string, string> = Object.fromEntries(roles.map((r) => [r.id, r.label]));
  return ids.map((i) => by[i]).join(", ");
}

/** Assigns roles to one person in place and writes the status, reason and notes. */
function classify(person: Json, roles: Json[], unions: Record<string, string[]>): void {
  const dep = person.department_check;
  const sen = person.seniority_check;
  Object.assign(person, { roles_filled: [], roles_possible: [], roles_undecided: [], notes: [] });
  const pairs: [Json, string, string][] = [[dep, "department", "matches_department"], [sen, "seniority", "matches_seniority"]];
  for (const [c, attr, tool] of pairs) {
    if (c.result === "pass" && c.value !== null && !unions[attr].includes(c.value)) {
      person.notes.push(`${tool} answered yes but returned ${attr} '${c.value}', which is not a ` +
        `configured ${attr}. No role is assigned on it. Both are shown as returned.`);
    }
    if (c.result === "fail" && c.value !== null && unions[attr].includes(c.value)) {
      person.notes.push(`${tool} answered no but returned ${attr} '${c.value}', which is a configured ` +
        `${attr}. The answer no is used. Both are shown as returned.`);
    }
  }
  if (dep.result === "person_not_found" || sen.result === "person_not_found") {
    person.status = "person_not_found";
    person.reason = "Decisions found no Firmable person for this ID, so the remaining questions were not " +
      "asked. No role is assigned.";
    return;
  }
  for (const role of roles) {
    const d = side(dep, role.departments);
    const s = side(sen, role.seniorities);
    if (d === "no" || s === "no") continue;
    if (d === "yes" && s === "yes") person.roles_filled.push(role.id);
    else if (d === "error" || s === "error") person.roles_undecided.push(role.id);
    else person.roles_possible.push(role.id);
  }
  if (person.roles_filled.length) {
    person.status = "assigned";
    person.reason = `Fills: ${labels(roles, person.roles_filled)}.`;
  } else if (person.roles_undecided.length) {
    person.status = "incomplete";
    person.reason = "A question has no answer because of an error or because the run stopped, so these " +
      `roles are undecided: ${labels(roles, person.roles_undecided)}.`;
  } else if (person.roles_possible.length) {
    const why: string[] = [];
    for (const [c, attr, tool] of pairs) {
      if (c.result === "unknown") why.push(`${tool} answered unknown, so Firmable does not hold the ${attr}.`);
      else if (c.result === "pass" && c.value === null) why.push(`${tool} answered yes but returned no ${attr} value, so the role cannot be decided.`);
    }
    person.status = "unclassified";
    person.reason = `Could fill: ${labels(roles, person.roles_possible)}. ${why.join(" ")}`;
  } else {
    person.status = "no_role";
    person.reason = "Matches no configured role on the department and seniority returned.";
  }
}

function accountStatus(roles: Json[], roleResults: Json[]): [string, string] {
  const req = roleResults.filter((_, i) => roles[i].required);
  const filled = roleResults.filter((rr) => rr.status === "filled").length;
  const names = (items: Json[]) => items.map((rr) => rr.label).join(", ");
  const gaps = req.filter((rr) => rr.status === "not_observed");
  const undecided = req.filter((rr) => rr.status === "incomplete");
  const possible = req.filter((rr) => rr.status === "possible");
  if (req.every((rr) => rr.status === "filled")) {
    return ["committee_complete", `Every required role is filled. ${filled} of ${roles.length} role(s) filled.`];
  }
  if (gaps.length) {
    return ["gaps_found", `Required role(s) not observed among the people retrieved: ${names(gaps)}. ` +
      `${filled} of ${roles.length} role(s) filled.`];
  }
  if (undecided.length) {
    return ["incomplete", "Required role(s) are undecided because a question has an error or was not asked: " +
      `${names(undecided)}. ${filled} of ${roles.length} role(s) filled.`];
  }
  return ["needs_review", `Required role(s) could be filled only by unclassified people: ${names(possible)}. ` +
    `Unknown is not a no. ${filled} of ${roles.length} role(s) filled.`];
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const s = cfg.search;
  const roles: Json[] = cfg.roles;
  const dm = cfg.decision_maker;
  const unions: Record<string, string[]> = { department: union(roles, "departments"), seniority: union(roles, "seniorities") };
  const coverage: string[] = [
    "An unknown answer means Firmable does not hold the fact needed to answer. It is never counted as a no " +
      "or as a pass.",
    "Roles are inferred by Firmable's rules from each person's main title. This recipe assigns a role only from " +
      "the department and seniority values the answers return, by exact text comparison after trimming.",
    `Only people retrieved by one people search per account are checked: up to ${s.max_people} people in ` +
      `country ${s.selected_country} (selectedCountry).`,
    "A role marked not_observed has no one among the people retrieved. It is not observed in retrieved data; " +
      "it does not mean no such person works there.",
    "People search accepts only companyId, selectedCountry, position, seniority, department, from and size. " +
      "It has no name, location or date filter.",
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length } }, coverage };
  };

  const checks: Json[] = [
    { tool: "matches_department", inputs: { departments: unions.department } },
    { tool: "matches_seniority", inputs: { seniorities: unions.seniority } },
  ];
  const dmInputs: Json = {};
  if (dm) {
    dmInputs.seller = dm.seller;
    if (dm.buyer) dmInputs.buyer = dm.buyer;
    checks.push({ tool: "is_decision_maker", inputs: dmInputs });
  }
  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no searches or questions were made.");
  }
  const problems = decisions.checkTools(catalogue, checks, "person");
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll("The decision tools this recipe needs are not available with the inputs it sends.");
  }
  const modelTools = [...new Set<string>(checks.filter((c) => decisions.usesModel(catalogue, c.tool)).map((c) => c.tool))].sort();
  if (modelTools.length) {
    coverage.push(`These decision tools use a decision model inside Firmable's service: ${modelTools.join(", ")}. ` +
      "This code applies only the role rules in the configuration.");
  }
  if (dm) {
    coverage.push("is_decision_maker is asked only for people who fill a role. Its answer is shown as " +
      "returned and does not change role assignment.");
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _key: accountKey(r) })) as Row[];
  const dupes = findDuplicates(keyed, "_key");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier account; their API responses were reused, not requested again.`);
  let emptySearchSeen = false;

  const ask = async (rowId: string, tool: string, question: Json): Promise<Json> => {
    if (ctx.stopped !== null) return { tool, result: "not_asked", answer: null, error: null };
    let resp: Json;
    try {
      resp = await client.makeDecision(tool, question, { withReasoning: cfg.with_reasoning });
    } catch (e) {
      if (!(e instanceof FirmableError)) throw e;
      if (e instanceof NotFoundError && e.code === "person_not_found") return { tool, result: "person_not_found", answer: null, error: null };
      return { tool, result: "error", answer: null, error: ctx.recordError(rowId, e) };
    }
    const ans = decisions.answerFrom(resp);
    const result = decisions.compare(ans.answer, "yes");
    return { tool, result, ...ans, error: result === "error" ? ans.answer_error : null };
  };

  const one = async (row: Row): Promise<Json> => {
    const domain = normaliseDomain(row.domain);
    const cidIn = (row.company_id ?? "").trim();
    const base: Json = { row_id: row.row_id, input: row, domain, duplicate_of: dupes[row.row_id] ?? null,
      company_id: null, company_id_source: null, company: null, search: null, people: [], roles: [] };
    if (cidIn && !COMPANY_ID.test(cidIn)) return { ...base, status: "invalid_input", status_reason: `'${cidIn}' is not a Firmable company ID.` };
    if (!cidIn && !domain) return { ...base, status: "invalid_input", status_reason: `'${row.domain ?? ""}' is not a valid domain.` };
    let companyId: string;
    let source: string;
    if (cidIn) {
      [companyId, source] = [cidIn, "input"];
    } else {
      let company: Json;
      try {
        company = await client.getCompany({ fqdn: domain! });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this domain, so no search was made." };
        }
        throw e;
      }
      base.company = facts.companyFacts(company);
      [companyId, source] = [company.id, "company_lookup"];
    }
    base.company_id = companyId;
    base.company_id_source = source;

    const body: Record<string, unknown> = { companyId, selectedCountry: s.selected_country };
    for (const k of ["position", "seniority", "department"]) if (s[k]) body[k] = s[k];
    body.from = "0";
    body.size = String(s.max_people);
    const resp = await client.searchPeople(body);
    if (resp.success === false) emptySearchSeen = true;
    const records: Json[] = (Array.isArray(resp.records) ? resp.records : []).filter((x: unknown) => isObj(x));
    const total: number | null = typeof resp.total === "number" && Number.isInteger(resp.total) ? resp.total : null;
    const people: Json[] = [];
    const seen = new Set<string>();
    let noId = 0;
    for (const rec of records) {
      const pid = rec.person_id || rec.id;
      if (typeof pid !== "string" || !pid) { noId += 1; continue; }
      if (seen.has(pid)) continue;
      seen.add(pid);
      people.push({ person_id: pid, name: rec.name ?? null, position: rec.position ?? null });
    }
    const notChecked = total !== null && total > records.length ? total - records.length : 0;
    const filters = Object.fromEntries(Object.entries(body).filter(([k]) => !["companyId", "from", "size"].includes(k)));
    base.search = { filters, total, retrieved: records.length, people: people.length, no_person_id: noId, not_checked: notChecked };

    for (const p of people) {
      const q = { person_id: p.person_id };
      const dep = await ask(row.row_id, "matches_department", { ...q, departments: unions.department });
      const sen = dep.result === "person_not_found"
        ? { tool: "matches_seniority", result: "not_asked", answer: null, error: null }
        : await ask(row.row_id, "matches_seniority", { ...q, seniorities: unions.seniority });
      dep.value = detailValue(dep, "department");
      sen.value = detailValue(sen, "seniority");
      p.department_check = dep;
      p.seniority_check = sen;
      p.department = dep.value;
      p.seniority = sen.value;
      classify(p, roles, unions);
      p.decision_maker = null;
      if (dm && p.status === "assigned") p.decision_maker = await ask(row.row_id, "is_decision_maker", { ...q, ...dmInputs });
    }

    const roleResults: Json[] = [];
    for (const role of roles) {
      const pick = (key: string) => people.filter((p) => p[key].includes(role.id));
      const filled = pick("roles_filled");
      const undecided = pick("roles_undecided");
      const possible = pick("roles_possible");
      const status = filled.length ? "filled" : undecided.length ? "incomplete" : possible.length ? "possible" : "not_observed";
      const who = (items: Json[]) => items.map((p) => ({ person_id: p.person_id, name: p.name }));
      roleResults.push({ role_id: role.id, label: role.label, required: role.required, status,
        filled_by: who(filled), possible: who(possible), undecided: who(undecided) });
    }
    let status: string;
    let reason: string;
    if (!people.length) {
      [status, reason] = ["no_people_found", "People search returned no people for this account."];
    } else {
      [status, reason] = accountStatus(roles, roleResults);
      reason += ` People checked: ${people.length}.`;
      const missing = people.filter((p) => p.status === "person_not_found").length;
      if (missing) reason += ` ${missing} person(s) were not found by Decisions.`;
    }
    if (total === null) reason += " The API did not report a search total.";
    else if (notChecked) reason += ` The search reported ${total} people; ${records.length} were retrieved and ${notChecked} were not checked.`;
    if (noId) reason += ` ${noId} search record(s) had no person ID and were left out.`;
    return { ...base, status, status_reason: reason, people, roles: roleResults };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  if (emptySearchSeen) {
    coverage.push("A people search with no matches returns success false without a records list; it is " +
      "counted as zero people.");
  }
  const counts: Record<string, number> = {};
  const roleCounts: Record<string, Record<string, number>> = {};
  for (const role of roles) roleCounts[role.id] = Object.fromEntries(ROLE_STATUSES.map((k) => [k, 0]));
  const personCounts: Record<string, number> = Object.fromEntries(PERSON_STATUSES.map((k) => [k, 0]));
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    for (const rr of r.roles ?? []) roleCounts[rr.role_id][rr.status] += 1;
    for (const p of r.people ?? []) personCounts[p.status] += 1;
  }
  return { rows, summary: { status_counts: counts, role_counts: roleCounts, person_counts: personCounts }, coverage };
}

export const BASE_COLUMNS = ["row_id", "domain", "company_id", "company_name", "account_status", "account_status_reason",
  "search_total", "people_checked", "required_roles_not_observed", "required_roles_possible",
  "required_roles_undecided", "role_statuses"];

export function csvColumns(result: Json): string[] {
  const roles: Json[] = result?.input?.config?.roles ?? [];
  return [...BASE_COLUMNS, ...roles.map((r) => `role_${r.id}`)];
}

export function csvRows(result: Json): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const r of result.rows) {
    const search = r.search ?? {};
    const rr: Json[] = r.roles ?? [];
    const line: Record<string, unknown> = {
      row_id: r.row_id, domain: r.domain || r.input.domain || null, company_id: r.company_id || r.input.company_id || null,
      company_name: r.company?.name ?? null, account_status: r.status,
      account_status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      search_total: search.total ?? null, people_checked: search.people ?? null,
      required_roles_not_observed: rr.filter((x) => x.required && x.status === "not_observed").map((x) => x.label),
      required_roles_possible: rr.filter((x) => x.required && x.status === "possible").map((x) => x.label),
      required_roles_undecided: rr.filter((x) => x.required && x.status === "incomplete").map((x) => x.label),
      role_statuses: rr.map((x) => `${x.role_id}=${x.status}`),
    };
    for (const x of rr) line[`role_${x.role_id}`] = x.filled_by.map((p: Json) => p.name || p.person_id);
    out.push(line);
  }
  return out;
}

const who = (items: Json[]): string => items.map((p) => p.name || p.person_id).join(", ");

function checkText(c: Json): string {
  if (!c) return "not asked";
  if (["pass", "fail", "unknown"].includes(c.result)) return c.answer;
  return String(c.result).replace(/_/g, " ");
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  const roles: Json[] = result.input?.config?.roles ?? [];
  report.add("Result", "Buying committee summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Not observed means no one among the people retrieved fills the role. It does not mean no such " +
      "person works at the account. Unknown answers make a person <strong>unclassified</strong>, never a no.", "unknown"));
  const gridRows: unknown[][] = [];
  for (const r of result.rows) {
    const title = r.company?.name || r.domain || r.company_id || r.row_id;
    const cells: unknown[] = [title, new Raw(badge(r.status.replace(/_/g, " "), STATUS_TONE[r.status] ?? "neutral"))];
    const by: Record<string, Json> = Object.fromEntries((r.roles ?? []).map((x: Json) => [x.role_id, x]));
    for (const role of roles) {
      const x = by[role.id];
      if (!x) { cells.push(""); continue; }
      const names = who(x.filled_by.length ? x.filled_by : x.undecided.length ? x.undecided : x.possible);
      cells.push(new Raw(badge(x.status.replace(/_/g, " "), ROLE_TONE[x.status] ?? "neutral") + (names ? `<br>${esc(names)}` : "")));
    }
    gridRows.push(cells);
  }
  const headers = ["Account", "Status", ...roles.map((role) => `${role.label}${role.required ? " (required)" : ""}`)];
  report.add("Grid", "Roles by account", table(headers, gridRows));

  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.domain || r.company_id || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    if (r.company_id) {
      left += kv([["Firmable company ID", r.company_id],
        ["ID from", r.company_id_source === "input" ? "input file" : "company lookup"],
        ["Domain", r.company?.fqdn || r.domain]]);
    }
    if (r.duplicate_of) left += note(`Same account as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    const search = r.search;
    if (search) {
      left += "<h3>Search coverage</h3>" + kv([["Total reported", search.total ?? "not reported"],
        ["Records retrieved", search.retrieved], ["People checked", search.people], ["Not checked", search.not_checked]]);
    }
    if ((r.roles ?? []).length) {
      left += "<h3>Roles</h3>" + table(["Role", "Required", "Status", "People"], r.roles.map((x: Json) => [
        x.label, x.required ? "yes" : "no", new Raw(badge(x.status.replace(/_/g, " "), ROLE_TONE[x.status] ?? "neutral")),
        who(x.filled_by.length ? x.filled_by : x.undecided.length ? x.undecided : x.possible) || "not observed in retrieved data"]));
    }
    let right = "";
    const people: Json[] = r.people ?? [];
    if (people.length) {
      right += "<h3>Facts returned by Decisions</h3>" + table(
        ["Name", "Position", "Department", "Seniority", "Classification", "Decision maker"], people.map((p) => [
          p.name, p.position, `${checkText(p.department_check)}: ${p.department || "no value returned"}`,
          `${checkText(p.seniority_check)}: ${p.seniority || "no value returned"}`,
          new Raw(badge(p.status.replace(/_/g, " "), PERSON_TONE[p.status] ?? "neutral")),
          p.decision_maker ? checkText(p.decision_maker) : "not asked"]));
      for (const p of people) {
        let extra = "";
        for (const c of [p.department_check, p.seniority_check, p.decision_maker]) {
          const rs = c?.reasoning ?? {};
          if (rs.reason) {
            extra += `<p class="muted"><strong>${esc(c.tool)}:</strong> ${esc(rs.reason)} ` +
              `Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.</p>`;
          }
        }
        if (p.status === "assigned" && !p.notes.length && !extra) continue;
        right += `<h3>${esc(p.name || p.person_id)}</h3><p>${esc(p.reason)}</p>` + extra;
        if (p.notes.length) right += note(bulletList(p.notes), "review");
      }
      right += detailsJson("People as JSON", people);
    }
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No people checked for this account.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its people", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns, csvRows, render,
};
