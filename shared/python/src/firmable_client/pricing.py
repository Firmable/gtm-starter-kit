"""Published Firmable developer prices, in one place.

These figures come from the public docs on the date below. They are estimates used to plan runs and
to stop a run before it goes over the limit you set. They are not a server-enforced spending cap,
and the API does not expose the actual wallet deduction for a request.

Two numbers are kept for every planned call:

- published estimate: the upper bound from the published price, assuming every profile is new to
  your organisation;
- reservation: the published estimate plus a one-off allowance of 1 credit for each fractional
  price category used in the run (decisions, AI adoption without evidence, Tech Intel evidence).
  Fractional charges carry over between calls and are taken as whole credits, so a single 0.04
  answer can cause a whole-credit deduction if earlier part-credits are waiting.
"""
from __future__ import annotations

PRICING_AS_OF = "2026-11-24"
PRICING_SOURCES = [
    "https://docs.firmable.com/developer/credits.md",
    "https://docs.firmable.com/api-reference/tech-intelligence.md",
    "https://docs.firmable.com/api-reference/decisions.md",
    "https://docs.firmable.com/api-reference/ai-adoption.md",
]

COMPANY_PROFILE = 1.0          # per new company profile, charged once per organisation
PERSON_PROFILE = 1.0           # per new person profile, charged once per organisation
PEOPLE_SEARCH = 0.0            # free
DECISIONS_LIST = 0.0           # free
DECISION_ANSWER = 0.04         # 1 credit per 25 yes or no answers; unknown answers are free
DECISION_ANSWER_REASONING = 0.08  # 2 credits per 25 with withReasoning=true
AI_ADOPTION = 0.2              # 1 credit per 5 companies found; not found is free
AI_ADOPTION_EVIDENCE = 3.0     # 3 credits per company found with withEvidence=true
TECH_INTEL_COMPANY = 5.0       # 1 credit per technology, up to 5 per company, charged once per company
TECH_INTEL_EVIDENCE_REQUEST = 0.2  # 1 credit per 5 requests with withEvidence=true, every page

ROUNDING_ALLOWANCE = 1.0
FRACTIONAL_CATEGORIES = {"decision", "ai_adoption", "tech_intel_evidence"}


def round4(x: float) -> float:
    """Rounds half away from zero to 4 decimals, identically in Python and TypeScript."""
    import math
    sign = -1.0 if x < 0 else 1.0
    return sign * math.floor(abs(x) * 10000 + 0.5) / 10000


def estimate_company() -> list[tuple[str, float]]:
    return [("company", COMPANY_PROFILE)]


def estimate_person() -> list[tuple[str, float]]:
    return [("person", PERSON_PROFILE)]


def estimate_decision(answers: int, with_reasoning: bool) -> list[tuple[str, float]]:
    unit = DECISION_ANSWER_REASONING if with_reasoning else DECISION_ANSWER
    return [("decision", round4(unit * answers))]


def estimate_ai_adoption(companies: int, with_evidence: bool) -> list[tuple[str, float]]:
    if with_evidence:
        return [("ai_adoption_evidence", round4(AI_ADOPTION_EVIDENCE * companies))]
    return [("ai_adoption", round4(AI_ADOPTION * companies))]


def estimate_tech_intel_page(first_page: bool, with_evidence: bool) -> list[tuple[str, float]]:
    parts: list[tuple[str, float]] = []
    if first_page:
        parts.append(("tech_intel", TECH_INTEL_COMPANY))
    if with_evidence:
        parts.append(("tech_intel_evidence", TECH_INTEL_EVIDENCE_REQUEST))
    return parts


def total(parts: list[tuple[str, float]]) -> float:
    return round4(sum(p[1] for p in parts))


def reservation_for(parts_list: list[list[tuple[str, float]]]) -> float:
    """Published total plus one rounding allowance per fractional category that appears."""
    published = round4(sum(total(p) for p in parts_list))
    categories = {c for parts in parts_list for c, v in parts if v > 0}
    allowance = ROUNDING_ALLOWANCE * len(categories & FRACTIONAL_CATEGORIES)
    return round4(published + allowance)
