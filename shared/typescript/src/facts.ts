/** Picks commonly used fields from API responses without changing their values. Mirrors facts.py. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

export const COMPANY_FIELDS = ["id", "name", "fqdn", "website", "hq_country", "hq_location", "company_size",
  "employee_count", "industries", "company_type", "year_founded", "linkedin", "revenue"];

export const PERSON_FIELDS = ["id", "name", "position", "seniority", "department", "country", "location",
  "time_in_current_role", "month_joined", "year_joined", "linkedin"];

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);

export function companyFacts(c: Json): Json | null {
  if (!isObj(c)) return null;
  const out: Json = {};
  for (const k of COMPANY_FIELDS) out[k] = c[k] ?? null;
  return out;
}

export function personFacts(p: Json): Json | null {
  if (!isObj(p)) return null;
  const out: Json = {};
  for (const k of PERSON_FIELDS) out[k] = p[k] ?? null;
  const cur = isObj(p.current_company) ? p.current_company : {};
  out.current_company_id = (cur as Json).id ?? null;
  out.current_company_name = (cur as Json).name ?? null;
  return out;
}

export function contactDetails(p: Json): Json {
  if (!isObj(p)) return { work_emails: [], personal_emails: [], phones: [] };
  const emails: Json = isObj(p.emails) ? p.emails : {};
  const vals = (lst: Json): string[] => (Array.isArray(lst) ? lst : []).filter((e: Json) => isObj(e) && e.value).map((e: Json) => e.value);
  const phones = (Array.isArray(p.phones) ? p.phones : []).filter((ph: Json) => isObj(ph) && ph.value)
    .map((ph: Json) => ({ value: ph.value, is_dnd: ph.is_dnd ?? null }));
  return { work_emails: vals(emails.work), personal_emails: vals(emails.personal), phones };
}
