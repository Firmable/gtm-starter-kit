/**
 * Firmable REST client for the developer API. Mirrors shared/python/src/firmable_client/client.py.
 *
 * - Live requests always go to https://api.firmable.com. The base URL cannot be changed.
 * - The API key is sent as a bearer token. Every request has a timeout.
 * - Nothing is retried automatically, because a retried request may be charged twice.
 * - Each request is checked against the credit budget before it is sent.
 * - Identical requests in one run are answered from memory and not sent again.
 * - Every call, including refused and failed ones, is written to the ledger without the key.
 */
import * as pricing from "./pricing.js";
import * as validate from "./validate.js";
import { CreditBudget, fmt } from "./budget.js";
import {
  FirmableError, InvalidResponseError, RedirectBlockedError, TransportTimeoutError, errorForStatus,
} from "./errors.js";
import { Ledger } from "./ledger.js";
import { FetchTransport, HttpResponse, Transport } from "./transport.js";

export const DEFAULT_TIMEOUT = 30.0;
export const DEFAULT_MIN_INTERVAL = 1.0 / 12;

export type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

export interface ClientOptions {
  budget: CreditBudget;
  ledger?: Ledger;
  transport?: Transport;
  timeout?: number;
  minInterval?: number;
  sleep?: (seconds: number) => Promise<void>;
  clock?: () => number; // seconds
}

export interface TechIntelResult {
  id: string | null;
  total: number | null;
  items: Json[];
  pages_fetched: number;
  complete: boolean;
  stop_reason: "last_page" | "page_limit" | "repeated_cursor" | "error" | "budget";
  error: ReturnType<FirmableError["toDict"]> | null;
}

const COMPANY_IDS = ["abn", "fqdn", "id", "ln_slug", "ln_url", "website"];
const PERSON_IDS = ["id", "ln_slug", "ln_url", "personal_email", "work_email"];
const SEARCH_FIELDS = new Set(["companyId", "selectedCountry", "position", "seniority", "department", "from", "size"]);

export class FirmableClient {
  readonly budget: CreditBudget;
  readonly ledger: Ledger;
  private readonly transport: Transport;
  private readonly timeout: number;
  private readonly minInterval: number;
  private readonly sleep: (s: number) => Promise<void>;
  private readonly clock: () => number;
  private readonly lastSent = new Map<string, number>();
  readonly cache = new Map<string, Json>();

  constructor(private readonly key: string, opts: ClientOptions) {
    if (!key) throw new Error("An API key is required for live requests.");
    const timeout = opts.timeout ?? DEFAULT_TIMEOUT;
    if (timeout <= 0) throw new Error("timeout must be positive");
    this.budget = opts.budget;
    this.ledger = opts.ledger ?? new Ledger(key);
    this.transport = opts.transport ?? new FetchTransport();
    this.timeout = timeout;
    this.minInterval = opts.minInterval ?? DEFAULT_MIN_INTERVAL;
    this.sleep = opts.sleep ?? ((s) => new Promise((r) => setTimeout(r, s * 1000)));
    this.clock = opts.clock ?? (() => performance.now() / 1000);
  }

  /** GET /company with one identifier: fqdn, id, abn, website, ln_slug or ln_url. */
  getCompany(identifier: Record<string, string>): Promise<Json> {
    oneIdentifier(identifier, COMPANY_IDS, "company");
    return this.call("GET", "/company", { query: identifier, parts: pricing.estimateCompany(), check: validate.company });
  }

  /** GET /people with one identifier: id, work_email, personal_email, ln_slug or ln_url. */
  getPerson(identifier: Record<string, string>): Promise<Json> {
    oneIdentifier(identifier, PERSON_IDS, "person");
    return this.call("GET", "/people", { query: identifier, parts: pricing.estimatePerson(), check: validate.person });
  }

  /** POST /people/search. companyId is required by the published schema. Free. */
  searchPeople(body: Record<string, unknown>): Promise<Json> {
    if (!body.companyId) throw new Error("people search needs companyId");
    const unknown = Object.keys(body).filter((k) => !SEARCH_FIELDS.has(k)).sort();
    if (unknown.length) throw new Error(`people search does not accept: ${unknown.join(", ")}`);
    return this.call("POST", "/people/search", { body, parts: [], check: validate.peopleSearch }).then((resp: Json) =>
      // Documented no-match response. The ledger keeps it as returned; callers get an empty list.
      resp.success === false && !("records" in resp) ? { ...resp, records: [], total: resp.totalPeople ?? 0 } : resp);
  }

  /** GET /decisions. Free. */
  listDecisionTools(): Promise<Json> {
    return this.call("GET", "/decisions", { parts: [], check: validate.decisionsList });
  }

  /** POST /decisions with {tool, question}. withReasoning is a query parameter. */
  makeDecision(tool: string, question: Record<string, unknown>, opts: { withReasoning?: boolean } = {}): Promise<Json> {
    const withReasoning = opts.withReasoning ?? false;
    const answers = Array.isArray(question.records) ? question.records.length : 1;
    return this.call("POST", "/decisions", {
      query: withReasoning ? { withReasoning: "true" } : {},
      body: { tool, question },
      parts: pricing.estimateDecision(answers, withReasoning),
      check: validate.decision,
      reported: decisionCredits,
    });
  }

  /** POST /ai-adoption with company_id or domain, or records. withEvidence is a query parameter. */
  getAiAdoption(body: Record<string, unknown>, opts: { withEvidence?: boolean } = {}): Promise<Json> {
    const withEvidence = opts.withEvidence ?? false;
    const companies = Array.isArray(body.records) ? body.records.length : 1;
    return this.call("POST", "/ai-adoption", {
      query: withEvidence ? { withEvidence: "true" } : {},
      body,
      parts: pricing.estimateAiAdoption(companies, withEvidence),
      check: validate.aiAdoption,
    });
  }

  /** GET /tech-intel for one page. */
  getTechIntelPage(identifier: Record<string, string>, opts: { withEvidence?: boolean; cursor?: string | null } = {}): Promise<Json> {
    oneIdentifier(identifier, COMPANY_IDS, "company");
    const query: Record<string, string> = { ...identifier };
    if (opts.withEvidence) query.withEvidence = "true";
    if (opts.cursor) query.cursor = opts.cursor;
    return this.call("GET", "/tech-intel", {
      query,
      parts: pricing.estimateTechIntelPage(!opts.cursor, !!opts.withEvidence),
      check: validate.techIntel,
    });
  }

  /**
   * Follows next_cursor up to maxPages and reports whether the list is complete. Stops at the last
   * page, the page limit, a repeated cursor, or an error. Pages already fetched are kept.
   */
  async getTechIntel(identifier: Record<string, string>, opts: { withEvidence?: boolean; maxPages?: number } = {}): Promise<TechIntelResult> {
    const maxPages = opts.maxPages ?? 1;
    if (maxPages < 1) throw new Error("max_pages must be at least 1");
    const items: Json[] = [];
    const seen = new Set<string>();
    let cursor: string | null = null;
    let pages = 0;
    let total: number | null = null;
    let companyId: string | null = null;
    let error: FirmableError | null = null;
    let stop: TechIntelResult["stop_reason"] = "last_page";
    for (;;) {
      if (pages >= maxPages) { stop = "page_limit"; break; }
      let page: Json;
      try {
        page = await this.getTechIntelPage(identifier, { withEvidence: opts.withEvidence, cursor });
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        error = e;
        stop = e.kind === "budget_exceeded" ? "budget" : "error";
        break;
      }
      pages += 1;
      items.push(...(page.tech_intelligence ?? []));
      if ("total" in page) total = page.total;
      if ("id" in page) companyId = page.id;
      const next: string | undefined = page.next_cursor;
      if (!next) { stop = "last_page"; break; }
      if (seen.has(next) || next === cursor) { stop = "repeated_cursor"; break; }
      seen.add(next);
      cursor = next;
    }
    let complete = stop === "last_page";
    if (complete && pages === 0) complete = false;
    return { id: companyId, total, items, pages_fetched: pages, complete, stop_reason: stop, error: error ? error.toDict() : null };
  }

  // ----- core ------------------------------------------------------------------------------

  private async call(method: string, path: string, o: {
    query?: Record<string, string>; body?: unknown; parts: pricing.Part[];
    check: (x: unknown) => void; reported?: (x: Json) => number | null;
  }): Promise<Json> {
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(o.query ?? {})) query[k] = String(v);
    const body = o.body === undefined ? null : o.body;
    const endpoint = `${method} ${path}`;
    const cacheKey = stableStringify([method, path, Object.entries(query).sort(), body]);
    const published = pricing.total(o.parts);
    const base = { endpoint, method, path, query, body };

    if (this.cache.has(cacheKey)) {
      this.ledger.record({ ...base, outcome: "cache_hit", status: null, published_estimate: 0, reserved: 0,
        reported_credits: null, charge_uncertain: false, note: "Same request already made in this run; reused the response." });
      return this.cache.get(cacheKey);
    }

    let hold: number;
    try {
      hold = this.budget.reserve(o.parts, endpoint);
    } catch (e) {
      if (e instanceof FirmableError) {
        this.ledger.record({ ...base, outcome: "not_sent", status: null, published_estimate: published, reserved: 0,
          reported_credits: null, charge_uncertain: false, error: e.toDict() });
      }
      throw e;
    }

    await this.throttle(endpoint);
    const started = this.clock();
    let response: HttpResponse;
    try {
      response = await this.transport.send({
        method, path, query, jsonBody: body,
        headers: { Authorization: `Bearer ${this.key}`, Accept: "application/json" },
        timeout: this.timeout,
      });
    } catch (e) {
      if (e instanceof TransportTimeoutError) {
        e.endpoint = endpoint;
        e.chargeUncertain = published > 0;
        if (e.chargeUncertain) this.budget.markUncertain();
        else this.budget.release(hold, published);
        this.ledger.record({ ...base, outcome: "timeout", status: null, published_estimate: published,
          reserved: e.chargeUncertain ? hold : 0, reported_credits: null, charge_uncertain: e.chargeUncertain,
          error: e.toDict(), duration_ms: this.ms(started) });
        throw e;
      }
      if (e instanceof FirmableError) {
        e.endpoint = endpoint;
        this.budget.release(hold, published);
        this.ledger.record({ ...base, outcome: "error", status: e.status, published_estimate: published, reserved: 0,
          reported_credits: null, charge_uncertain: false, error: e.toDict(), duration_ms: this.ms(started) });
      }
      throw e;
    }
    return this.handle(response, base, endpoint, published, hold, started, o.check, o.reported, cacheKey);
  }

  private handle(response: HttpResponse, base: Record<string, unknown>, endpoint: string, published: number,
    hold: number, started: number, check: (x: unknown) => void, reported: ((x: Json) => number | null) | undefined,
    cacheKey: string): Json {
    let parsed: Json = null;
    let isJson = true;
    try {
      parsed = response.text ? JSON.parse(response.text) : null;
    } catch {
      isJson = false;
    }

    if (response.status >= 300 && response.status <= 399) {
      const err = new RedirectBlockedError(
        `The API answered with a redirect (${response.status}). Redirects are not followed, ` +
          "so the API key is not sent to another location.",
        { status: response.status, endpoint, chargeUncertain: published > 0 },
      );
      if (err.chargeUncertain) this.budget.markUncertain();
      else this.budget.release(hold, published);
      this.ledger.record({ ...base, outcome: "error", status: response.status, published_estimate: published,
        reserved: err.chargeUncertain ? hold : 0, reported_credits: null, charge_uncertain: err.chargeUncertain,
        error: err.toDict(), duration_ms: this.ms(started) });
      throw err;
    }

    if (response.status < 200 || response.status > 299) {
      const [message, code] = errorMessage(parsed, response);
      const err = errorForStatus(response.status, message, { code, endpoint, billable: published > 0,
        retryAfter: response.headers["retry-after"] ?? null });
      if (err.chargeUncertain) this.budget.markUncertain();
      else this.budget.release(hold, published);
      this.ledger.record({ ...base, outcome: "error", status: response.status, published_estimate: published,
        reserved: err.chargeUncertain ? hold : 0, reported_credits: null, charge_uncertain: err.chargeUncertain,
        error: err.toDict(), response: isJson ? parsed : response.text.slice(0, 2000), duration_ms: this.ms(started) });
      throw err;
    }

    try {
      if (!isJson) throw new InvalidResponseError("The API returned a success status with a body that is not JSON.");
      check(parsed);
    } catch (e) {
      if (e instanceof InvalidResponseError) {
        e.endpoint = endpoint;
        e.status = response.status;
        e.chargeUncertain = published > 0;
        if (e.chargeUncertain) this.budget.markUncertain();
        this.ledger.record({ ...base, outcome: "invalid_response", status: response.status, published_estimate: published,
          reserved: hold, reported_credits: null, charge_uncertain: e.chargeUncertain, error: e.toDict(),
          response: isJson ? parsed : response.text.slice(0, 2000), duration_ms: this.ms(started) });
      }
      throw e;
    }

    const credits = reported ? reported(parsed) : null;
    this.budget.addReported(credits);
    this.ledger.record({ ...base, outcome: "ok", status: response.status, published_estimate: published, reserved: hold,
      reported_credits: credits, charge_uncertain: false, response: parsed, duration_ms: this.ms(started) });
    this.cache.set(cacheKey, parsed);
    return parsed;
  }

  private async throttle(endpoint: string): Promise<void> {
    if (this.minInterval <= 0) return;
    const last = this.lastSent.get(endpoint);
    const now = this.clock();
    if (last !== undefined && now - last < this.minInterval) await this.sleep(this.minInterval - (now - last));
    this.lastSent.set(endpoint, this.clock());
  }

  private ms(started: number): number {
    return Math.trunc((this.clock() - started) * 1000);
  }

  /** Seconds formatted as Python would print them, used in timeout messages. */
  static fmtSeconds(s: number): string {
    return fmt(s);
  }
}

function oneIdentifier(identifier: Record<string, string>, allowed: string[], what: string): void {
  const present = Object.entries(identifier).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k]) => k);
  if (present.length !== 1 || !allowed.includes(present[0])) {
    throw new Error(`${what} lookup needs exactly one of: ${allowed.join(", ")}`);
  }
}

function errorMessage(parsed: Json, response: HttpResponse): [string, string | null] {
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const err = parsed.error;
    let code: string | null = null;
    let message: string | null = null;
    if (err && typeof err === "object") {
      code = err.code ?? null;
      message = err.message ?? null;
    } else if (typeof err === "string") {
      code = err.includes(" ") ? null : err;
      message = err;
    }
    message = message || parsed.message || parsed.detail || null;
    if (message) return [String(message), code];
  }
  const snippet = response.text ? response.text.trim().slice(0, 200) : "";
  return [`HTTP ${response.status}` + (snippet ? `: ${snippet}` : ""), null];
}

function decisionCredits(parsed: Json): number | null {
  if (parsed && Array.isArray(parsed.results)) {
    // A batch reports its total at the top level; rows may not carry credits.
    if (typeof parsed.credits === "number") return parsed.credits;
    const nums = parsed.results.map((r: Json) => r?.credits).filter((v: unknown) => typeof v === "number");
    return nums.length ? pricing.round4(nums.reduce((a: number, b: number) => a + b, 0)) : null;
  }
  if (parsed && typeof parsed.credits === "number") return parsed.credits;
  return null;
}

export function stableStringify(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableStringify).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v as object).sort().map((k) => `${JSON.stringify(k)}:${stableStringify((v as Record<string, unknown>)[k])}`).join(",")}}`;
  }
  return JSON.stringify(v ?? null);
}
