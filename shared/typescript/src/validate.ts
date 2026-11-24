/**
 * Minimal response validation. Mirrors shared/python/src/firmable_client/validate.py.
 * Catches responses that would silently produce a wrong report; extra fields are allowed.
 */
import { InvalidResponseError } from "./errors.js";

export const DECISION_VALUES = new Set(["yes", "no", "unknown"]);

type Obj = Record<string, unknown>;

function obj(x: unknown, what: string): Obj {
  if (!x || typeof x !== "object" || Array.isArray(x)) throw new InvalidResponseError(`${what} response is not a JSON object.`);
  return x as Obj;
}

export function company(x: unknown): void {
  const d = obj(x, "Company");
  if (typeof d.id !== "string" || !d.id) throw new InvalidResponseError("Company response has no id.");
}

export function person(x: unknown): void {
  const d = obj(x, "Person");
  if (typeof d.id !== "string" || !d.id) throw new InvalidResponseError("Person response has no id.");
}

export function peopleSearch(x: unknown): void {
  const d = obj(x, "People search");
  if (d.success === false && !("records" in d)) return; // documented no-match response
  if (!Array.isArray(d.records)) throw new InvalidResponseError("People search response has no records list.");
}

export function decisionsList(x: unknown): void {
  const d = obj(x, "Decision tool list");
  const tools = d.tools;
  if (!Array.isArray(tools) || !tools.every((t) => t && typeof t === "object" && typeof (t as Obj).name === "string")) {
    throw new InvalidResponseError("Decision tool list has no tools with names.");
  }
}

export function decisionRow(r: unknown): void {
  const d = obj(r, "Decision");
  const v = d.decision;
  if (typeof v === "string" && DECISION_VALUES.has(v)) return;
  if ((v === null || v === undefined) && d.error) return;
  throw new InvalidResponseError(`Decision answer is ${pyRepr(v)}, not yes, no or unknown.`);
}

export function decision(x: unknown): void {
  const d = obj(x, "Decision");
  if ("results" in d) {
    if (!Array.isArray(d.results)) throw new InvalidResponseError("Decision batch response has no results list.");
    for (const r of d.results) decisionRow(r);
    return;
  }
  decisionRow(d);
}

export function aiAdoption(x: unknown): void {
  const d = obj(x, "AI adoption");
  if ("results" in d) {
    if (!Array.isArray(d.results)) throw new InvalidResponseError("AI adoption batch response has no results list.");
    for (const r of d.results) aiRow(r);
    return;
  }
  aiRow(d);
}

function aiRow(r: unknown): void {
  const d = obj(r, "AI adoption");
  if (!("company" in d)) throw new InvalidResponseError("AI adoption response has no company field.");
  const c = d.company;
  if (c !== null && (typeof c !== "object" || Array.isArray(c))) throw new InvalidResponseError("AI adoption company field is not an object or null.");
}

export function techIntel(x: unknown): void {
  const d = obj(x, "Tech Intel");
  if (!Array.isArray(d.tech_intelligence)) throw new InvalidResponseError("Tech Intel response has no tech_intelligence list.");
  if ("total" in d && d.total !== null && d.total !== undefined && !Number.isInteger(d.total)) {
    throw new InvalidResponseError("Tech Intel total is not a number.");
  }
}

function pyRepr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "string") return `'${v}'`;
  return String(v);
}
