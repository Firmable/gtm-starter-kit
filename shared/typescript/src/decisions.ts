/**
 * Helpers for the Decisions API shared by recipes. Mirrors firmable_client/decisions.py.
 * `unknown` means Firmable does not hold the fact. It is never treated as no.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

export const SUBJECT_KEYS = new Set(["company_id", "domain", "person_id", "linkedin_url", "work_email", "external_id", "records"]);

export function toolIndex(catalogue: Json): Map<string, Json> {
  const m = new Map<string, Json>();
  for (const t of catalogue?.tools ?? []) if (t && typeof t === "object" && "name" in t) m.set(t.name, t);
  return m;
}

const q = (s: unknown) => `'${s}'`;

/** Returns problems with configured checks; an empty list means every check can be asked. */
export function checkTools(catalogue: Json, checks: Json[], subject: string): string[] {
  const tools = toolIndex(catalogue);
  const problems: string[] = [];
  for (const c of checks) {
    const name = c.tool;
    const t = tools.get(name);
    if (!t) { problems.push(`Decision tool ${q(name)} is not in the current tool list from GET /decisions.`); continue; }
    if (!String(t.subject ?? "").includes(subject)) problems.push(`Decision tool ${q(name)} answers about ${t.subject}, not a ${subject}.`);
    const props = t.input_schema?.properties ?? {};
    const inputs = c.inputs ?? {};
    const extra = Object.keys(inputs).filter((k) => !(k in props) || SUBJECT_KEYS.has(k)).sort();
    if (extra.length) problems.push(`Decision tool ${q(name)} does not accept input(s): ${extra.join(", ")}.`);
    const required: string[] = t.input_schema?.required ?? [];
    const missing = required.filter((k) => !(k in inputs) && !SUBJECT_KEYS.has(k)).sort();
    if (missing.length) problems.push(`Decision tool ${q(name)} needs input(s): ${missing.join(", ")}.`);
  }
  return problems;
}

/** A stable record of one answer. Fields the API did not send are null. */
export function answerFrom(resp: Json): Json {
  const reasoning = resp?.reasoning;
  const entity = resp?.entity;
  return {
    answer: resp?.decision ?? null,
    confidence: resp?.confidence ?? null,
    details: resp?.details ?? null,
    reasoning: reasoning && typeof reasoning === "object" && !Array.isArray(reasoning) ? reasoning : null,
    answered_at: resp?.answered_at ?? null,
    credits: resp?.credits ?? null,
    answer_error: resp?.error ?? null,
    entity_id: entity && typeof entity === "object" ? entity.id ?? null : null,
    request_id: resp?.request_id ?? null,
  };
}

export function compare(answer: string | null | undefined, want: string): "pass" | "fail" | "unknown" | "error" {
  if (answer === "unknown") return "unknown";
  if (answer !== "yes" && answer !== "no") return "error";
  return answer === want ? "pass" : "fail";
}

export function usesModel(catalogue: Json, tool: string): boolean | null {
  const t = toolIndex(catalogue).get(tool);
  return t === undefined ? null : !!t.uses_decision_model;
}
