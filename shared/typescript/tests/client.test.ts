import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AuthenticationError, BadRequestError, BudgetExceededError, CreditBudget, FirmableClient, ForbiddenError,
  InsufficientCreditsError, InvalidResponseError, NotFoundError, RateLimitedError, RedirectBlockedError,
  ServerError, TransportTimeoutError,
} from "../src/index.js";
import { FixtureError, FixtureTransport, TEST_KEY, makeClient } from "../src/testing.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

function ex(method: string, path: string, response: Json, query?: Json, body?: Json, times = 1): Json {
  const request: Json = { method, path };
  if (query !== undefined) request.query = query;
  if (body !== undefined) request.body = body;
  return { request, response, times };
}

const COMPANY = { id: "f000000000001", name: "Acme Example", fqdn: "acme.example" };

test("company ok and ledger has no key", async () => {
  const [client] = makeClient({ exchanges: [ex("GET", "/company", { json: COMPANY }, { fqdn: "acme.example" })] });
  assert.equal((await client.getCompany({ fqdn: "acme.example" })).id, "f000000000001");
  const e = client.ledger.entries[0];
  assert.equal(e.outcome, "ok");
  assert.equal(e.published_estimate, 1);
  assert.ok(!JSON.stringify(client.ledger.entries).includes(TEST_KEY));
  assert.ok(!("headers" in e));
});

test("identical request is cached, not resent", async () => {
  const [client, t] = makeClient({ exchanges: [ex("GET", "/company", { json: COMPANY }, { fqdn: "acme.example" })] });
  await client.getCompany({ fqdn: "acme.example" });
  await client.getCompany({ fqdn: "acme.example" });
  assert.equal(t.sent.length, 1);
  assert.equal(client.ledger.entries[1].outcome, "cache_hit");
  assert.equal(client.budget.published, 1);
});

test("budget stops before the request is sent", async () => {
  const [client, t] = makeClient({ exchanges: [] }, 0.5);
  await assert.rejects(client.getCompany({ fqdn: "acme.example" }), BudgetExceededError);
  assert.equal(t.sent.length, 0);
  assert.equal(client.ledger.entries[0].outcome, "not_sent");
});

test("fractional reservation adds one allowance per category", () => {
  const b = new CreditBudget(10);
  assert.equal(b.reserve([["decision", 0.04]], "x"), 1.04);
  assert.equal(b.reserve([["decision", 0.04]], "x"), 0.04);
  assert.equal(b.reserved, 1.08);
});

const cases: [number, Json][] = [[400, BadRequestError], [401, AuthenticationError], [402, InsufficientCreditsError],
  [403, ForbiddenError], [404, NotFoundError], [429, RateLimitedError], [500, ServerError], [502, ServerError]];
for (const [status, cls] of cases) {
  test(`status ${status} maps to ${cls.name} and is not retried`, async () => {
    const resp = { status, json: { error: { code: "x_code", message: "went wrong" } }, headers: { "Retry-After": "3" } };
    const [client, t] = makeClient({ exchanges: [ex("GET", "/company", resp)] });
    const err: Json = await client.getCompany({ fqdn: "acme.example" }).catch((e) => e);
    assert.ok(err instanceof cls);
    assert.equal(t.sent.length, 1);
    assert.equal(err.status, status);
    assert.equal(err.message, "went wrong");
    if (status === 429) assert.equal(err.retryAfter, "3");
    if (status >= 500) { assert.ok(err.chargeUncertain); assert.equal(client.budget.uncertainRequests, 1); }
    else { assert.ok(!err.chargeUncertain); assert.equal(client.budget.reserved, 0); }
  });
}

test("free endpoint 5xx is not charge-uncertain", async () => {
  const [client] = makeClient({ exchanges: [ex("GET", "/decisions", { status: 503, text: "busy" })] });
  const err: Json = await client.listDecisionTools().catch((e) => e);
  assert.ok(err instanceof ServerError);
  assert.ok(!err.chargeUncertain);
  assert.ok(err.message.includes("busy"));
});

test("timeout is charge-uncertain and not retried", async () => {
  const [client, t] = makeClient({ exchanges: [ex("GET", "/company", { timeout: true })] });
  const err: Json = await client.getCompany({ fqdn: "acme.example" }).catch((e) => e);
  assert.ok(err instanceof TransportTimeoutError);
  assert.ok(err.chargeUncertain);
  assert.equal(t.sent.length, 1);
  assert.equal(client.budget.reserved, 1);
  assert.equal(err.message, "No response within 30.0 seconds.");
});

test("non-JSON success is an invalid response", async () => {
  const [client] = makeClient({ exchanges: [ex("GET", "/company", { status: 200, text: "<html>oops</html>" })] });
  await assert.rejects(client.getCompany({ fqdn: "acme.example" }), InvalidResponseError);
});

test("malformed decision is an invalid response", async () => {
  const [client] = makeClient({ exchanges: [ex("POST", "/decisions", { json: { decision: "maybe" } })] });
  await assert.rejects(client.makeDecision("is_b2b", { domain: "acme.example" }), InvalidResponseError);
});

test("redirect is blocked", async () => {
  const [client] = makeClient({ exchanges: [ex("GET", "/company", { status: 302, headers: { Location: "https://evil.example/" } })] });
  await assert.rejects(client.getCompany({ fqdn: "acme.example" }), RedirectBlockedError);
});

test("key in an error body is redacted", async () => {
  const [client] = makeClient({ exchanges: [ex("GET", "/company", { status: 401, json: { error: `bad key ${TEST_KEY}` } })] });
  await assert.rejects(client.getCompany({ fqdn: "acme.example" }), AuthenticationError);
  const s = JSON.stringify(client.ledger.entries);
  assert.ok(!s.includes(TEST_KEY));
  assert.ok(s.includes("[REDACTED]"));
});

test("decision reasoning query and reported credits", async () => {
  const [client] = makeClient({ exchanges: [ex("POST", "/decisions", { json: { decision: "yes", credits: 0.08, error: null } },
    { withReasoning: "true" }, { tool: "is_b2b", question: { domain: "acme.example" } })] });
  assert.equal((await client.makeDecision("is_b2b", { domain: "acme.example" }, { withReasoning: true })).decision, "yes");
  assert.equal(client.budget.reported, 0.08);
});

const page = (items: Json[], cursor?: string) => ({ json: { id: "f1", total: 3, tech_intelligence: items, ...(cursor ? { next_cursor: cursor } : {}) } });

test("tech intel follows cursor to the last page", async () => {
  const [client] = makeClient({ exchanges: [
    ex("GET", "/tech-intel", page([{ product_slug: "a" }], "c1"), { fqdn: "acme.example" }),
    ex("GET", "/tech-intel", page([{ product_slug: "b" }]), { fqdn: "acme.example", cursor: "c1" }),
  ] });
  const r = await client.getTechIntel({ fqdn: "acme.example" }, { maxPages: 5 });
  assert.ok(r.complete);
  assert.equal(r.pages_fetched, 2);
  assert.equal(client.budget.published, 5);
});

test("tech intel page limit is incomplete", async () => {
  const [client] = makeClient({ exchanges: [ex("GET", "/tech-intel", page([{ product_slug: "a" }], "c1"), { fqdn: "acme.example", withEvidence: "true" })] });
  const r = await client.getTechIntel({ fqdn: "acme.example" }, { withEvidence: true, maxPages: 1 });
  assert.equal(r.stop_reason, "page_limit");
  assert.ok(!r.complete);
  assert.equal(client.budget.published, 5.2);
});

test("tech intel repeated cursor stops", async () => {
  const [client] = makeClient({ exchanges: [
    ex("GET", "/tech-intel", page([{ product_slug: "a" }], "c1"), { fqdn: "acme.example" }),
    ex("GET", "/tech-intel", page([{ product_slug: "b" }], "c1"), { fqdn: "acme.example", cursor: "c1" }),
  ] });
  const r = await client.getTechIntel({ fqdn: "acme.example" }, { maxPages: 5 });
  assert.equal(r.stop_reason, "repeated_cursor");
  assert.equal(r.items.length, 2);
});

test("tech intel keeps partial pages on error", async () => {
  const [client] = makeClient({ exchanges: [
    ex("GET", "/tech-intel", page([{ product_slug: "a" }], "c1"), { fqdn: "acme.example" }),
    ex("GET", "/tech-intel", { status: 500, text: "boom" }, { fqdn: "acme.example", cursor: "c1" }),
  ] });
  const r = await client.getTechIntel({ fqdn: "acme.example" }, { maxPages: 5 });
  assert.equal(r.stop_reason, "error");
  assert.equal(r.items.length, 1);
  assert.equal(r.error?.status, 500);
});

test("tech intel budget exhaustion mid-paging", async () => {
  const [client, t] = makeClient({ exchanges: [ex("GET", "/tech-intel", page([{ product_slug: "a" }], "c1"), { fqdn: "acme.example", withEvidence: "true" })] }, 6.3);
  const r = await client.getTechIntel({ fqdn: "acme.example" }, { withEvidence: true, maxPages: 5 });
  assert.equal(r.stop_reason, "budget");
  assert.equal(t.sent.length, 1);
});

test("people search rejects unsupported fields", () => {
  const [client] = makeClient({ exchanges: [] });
  assert.throws(() => client.searchPeople({ companyId: "f1", name: "Jane" }));
  assert.throws(() => client.searchPeople({ position: "CTO" }));
});

test("unmatched request fails loudly", async () => {
  const [client] = makeClient({ exchanges: [] });
  await assert.rejects(client.listDecisionTools(), FixtureError);
});

test("wrong bearer token is detected", async () => {
  const t = new FixtureTransport({ exchanges: [] });
  const c = new FirmableClient("fbl_dev_other", { budget: new CreditBudget(1), transport: t, minInterval: 0 });
  await assert.rejects(c.listDecisionTools(), FixtureError);
});

test("throttle waits between requests to the same endpoint", async () => {
  const waits: number[] = [];
  const t = new FixtureTransport({ exchanges: [ex("GET", "/decisions", { json: { tools: [] } }), ex("GET", "/decisions", { json: { tools: [{ name: "x" }] } })] });
  const c = new FirmableClient(TEST_KEY, { budget: new CreditBudget(1), transport: t, minInterval: 0.1,
    sleep: async (s) => { waits.push(Math.round(s * 1000) / 1000); }, clock: () => 0 });
  await c.listDecisionTools();
  c.cache.clear();
  await c.listDecisionTools();
  assert.deepEqual(waits, [0.1]);
});

test("people search with no match returns empty records", async () => {
  const [client] = makeClient({ exchanges: [ex("POST", "/people/search", { json: { success: false, totalPeople: 0 } })] });
  const r = await client.searchPeople({ companyId: "f900000000001" });
  assert.deepEqual(r.records, []);
  assert.equal(r.total, 0);
  assert.equal(client.ledger.entries[0].outcome, "ok");
});

test("decision batch reports top-level credits", async () => {
  const resp = { json: { tool: "is_b2b", credits: 0.28, usage: { billable_rows: 7 },
    results: [{ decision: "yes", credits: null }, { decision: "unknown", credits: null }] } };
  const [client] = makeClient({ exchanges: [ex("POST", "/decisions", resp)] });
  await client.makeDecision("is_b2b", { records: [{ domain: "a.example" }, { domain: "b.example" }] });
  assert.equal(client.budget.reported, 0.28);
});
