/**
 * Client-side credit budget, checked before each request. A request whose reservation would take
 * the run over the limit is not sent. This safeguard is in this code only; the API does not enforce it.
 */
import { BudgetExceededError } from "./errors.js";
import { FRACTIONAL_CATEGORIES, Part, ROUNDING_ALLOWANCE, round4 } from "./pricing.js";

export interface BudgetSummary {
  limit: number | null;
  published_estimate_upper_bound: number;
  reserved_by_this_code: number;
  response_reported_credits: number;
  requests_with_uncertain_charge: number;
  wallet_deduction: string;
  pricing_as_of?: string;
}

export class CreditBudget {
  reserved = 0;
  published = 0;
  reported = 0;
  uncertainRequests = 0;
  private allowances = new Set<string>();

  constructor(public readonly limit: number | null) {
    if (limit !== null && limit < 0) throw new Error("credit limit must be zero or more");
  }

  /** Reserves credits for one request and returns the amount held. Throws without changing state. */
  reserve(parts: Part[], endpoint: string): number {
    const amount = parts.reduce((s, p) => s + p[1], 0);
    const fresh = new Set<string>();
    for (const [c, v] of parts) if (v > 0 && FRACTIONAL_CATEGORIES.has(c) && !this.allowances.has(c)) fresh.add(c);
    const hold = round4(amount + ROUNDING_ALLOWANCE * fresh.size);
    if (this.limit !== null && this.reserved + hold > this.limit + 1e-9) {
      const remaining = round4(Math.max(this.limit - this.reserved, 0));
      throw new BudgetExceededError(
        `Stopped before calling ${endpoint}: it needs a reservation of ${fmt(hold)} credits ` +
          `and ${fmt(remaining)} of the ${fmt(this.limit)} credit limit remain.`,
        { endpoint },
      );
    }
    for (const c of fresh) this.allowances.add(c);
    this.reserved = round4(this.reserved + hold);
    this.published = round4(this.published + amount);
    return hold;
  }

  /** Returns a reservation for a request the server refused, which was not charged. */
  release(hold: number, published: number): void {
    this.reserved = round4(Math.max(this.reserved - hold, 0));
    this.published = round4(Math.max(this.published - published, 0));
  }

  addReported(credits: number | null): void {
    if (credits !== null) this.reported = round4(this.reported + credits);
  }

  markUncertain(): void {
    this.uncertainRequests += 1;
  }

  summary(): BudgetSummary {
    return {
      limit: this.limit,
      published_estimate_upper_bound: this.published,
      reserved_by_this_code: this.reserved,
      response_reported_credits: this.reported,
      requests_with_uncertain_charge: this.uncertainRequests,
      wallet_deduction: "Not exposed by the API. Check Usage in the developer portal.",
    };
  }
}

/** Formats a number the way Python's str() does for these values (2 -> "2.0", 0.5 -> "0.5"). */
export function fmt(n: number): string {
  return Number.isInteger(n) ? `${n}.0` : `${n}`;
}
