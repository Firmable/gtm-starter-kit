/**
 * Published Firmable developer prices, in one place. Mirrors shared/python/src/firmable_client/pricing.py.
 *
 * These figures come from the public docs on the date below. They are estimates used to plan runs
 * and to stop a run before it goes over the limit you set. They are not a server-enforced spending
 * cap, and the API does not expose the actual wallet deduction for a request.
 *
 * - published estimate: the upper bound from the published price, assuming every profile is new;
 * - reservation: the published estimate plus a one-off allowance of 1 credit for each fractional
 *   price category used in the run, because part-credits carry over and are taken as whole credits.
 */

export const PRICING_AS_OF = "2026-11-24";
export const PRICING_SOURCES = [
  "https://docs.firmable.com/developer/credits.md",
  "https://docs.firmable.com/api-reference/tech-intelligence.md",
  "https://docs.firmable.com/api-reference/decisions.md",
  "https://docs.firmable.com/api-reference/ai-adoption.md",
];

export const COMPANY_PROFILE = 1.0;
export const PERSON_PROFILE = 1.0;
export const PEOPLE_SEARCH = 0.0;
export const DECISIONS_LIST = 0.0;
export const DECISION_ANSWER = 0.04;
export const DECISION_ANSWER_REASONING = 0.08;
export const AI_ADOPTION = 0.2;
export const AI_ADOPTION_EVIDENCE = 3.0;
export const TECH_INTEL_COMPANY = 5.0;
export const TECH_INTEL_EVIDENCE_REQUEST = 0.2;

export const ROUNDING_ALLOWANCE = 1.0;
export const FRACTIONAL_CATEGORIES = new Set(["decision", "ai_adoption", "tech_intel_evidence"]);

export type Part = [string, number];

/** Rounds half away from zero to 4 decimals, identically in Python and TypeScript. */
export function round4(x: number): number {
  const sign = x < 0 ? -1 : 1;
  return (sign * Math.floor(Math.abs(x) * 10000 + 0.5)) / 10000;
}

export const estimateCompany = (): Part[] => [["company", COMPANY_PROFILE]];
export const estimatePerson = (): Part[] => [["person", PERSON_PROFILE]];

export function estimateDecision(answers: number, withReasoning: boolean): Part[] {
  const unit = withReasoning ? DECISION_ANSWER_REASONING : DECISION_ANSWER;
  return [["decision", round4(unit * answers)]];
}

export function estimateAiAdoption(companies: number, withEvidence: boolean): Part[] {
  if (withEvidence) return [["ai_adoption_evidence", round4(AI_ADOPTION_EVIDENCE * companies)]];
  return [["ai_adoption", round4(AI_ADOPTION * companies)]];
}

export function estimateTechIntelPage(firstPage: boolean, withEvidence: boolean): Part[] {
  const parts: Part[] = [];
  if (firstPage) parts.push(["tech_intel", TECH_INTEL_COMPANY]);
  if (withEvidence) parts.push(["tech_intel_evidence", TECH_INTEL_EVIDENCE_REQUEST]);
  return parts;
}

export function total(parts: Part[]): number {
  return round4(parts.reduce((s, p) => s + p[1], 0));
}

/** Published total plus one rounding allowance per fractional category that appears. */
export function reservationFor(partsList: Part[][]): number {
  const published = round4(partsList.reduce((s, p) => s + total(p), 0));
  const cats = new Set<string>();
  for (const parts of partsList) for (const [c, v] of parts) if (v > 0 && FRACTIONAL_CATEGORIES.has(c)) cats.add(c);
  return round4(published + ROUNDING_ALLOWANCE * cats.size);
}
