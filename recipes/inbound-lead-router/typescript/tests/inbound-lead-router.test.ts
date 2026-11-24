import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AddressInfo } from "node:net";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, makeClient, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";
import { HOST, MAX_BODY, handleRequest, makeServer, serveMain } from "../src/server.js";

const F = fixturesDir("inbound-lead-router");
const tmp = () => mkdtempSync(join(tmpdir(), "ilr-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const config = (name = "config.json"): Json => loadConfig(JSON.parse(readFileSync(join(F, name), "utf-8")));

test("the first rule that is not ruled out decides", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.l1.status, "routed");
  assert.equal(rows.l1.queue, "Enterprise AE");
  assert.equal(rows.l1.matched_rule, "enterprise_exec");
  assert.deepEqual(rows.l1.rules.map((r: Json) => r.outcome), ["not_matched", "matched", "matched", "not_matched", "matched"]);
  assert.equal(rows.l2.matched_rule, "enterprise");
  assert.equal(rows.l2.rules[1].failing_condition, "icp_fit");
  assert.equal(rows.l2.rules[1].reason, "icp_fit not met: wanted yes, observed no.");
  assert.equal(rows.l3.queue, "NZ team");
  assert.equal(rows.l3.person.status, "no_work_email");
  assert.equal(rows.l5.matched_rule, "commercial_fit");
  assert.equal(rows.l6.status, "routed_default");
  assert.equal(rows.l12.matched_rule, "software");
  assert.deepEqual(transport.unused(), []);
});

test("a missing fact goes to review, not to a later rule", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.equal(rows.l7.status, "needs_review");
  assert.equal(rows.l7.queue, "Sales ops review");
  assert.equal(rows.l7.rules[0].outcome, "skipped_missing_fact");
  assert.deepEqual(rows.l7.rules[0].missing_facts, ["hq_country"]);
  assert.equal(rows.l7.person.status, "not_found");
  assert.equal(rows.l7.icp.answer, "unknown");
  assert.equal(rows.l11.status, "needs_review");
  assert.deepEqual(rows.l11.rules[1].missing_facts, ["seniority"]);
  assert.equal(rows.l11.person.status, "found");
});

test("seniority rule does not apply without a work email", async () => {
  const [result] = await run("happy");
  const l14 = byId(result).l14;
  assert.equal(l14.person.status, "no_work_email");
  assert.equal(l14.rules[1].outcome, "not_matched");
  assert.equal(l14.rules[1].failing_condition, "seniority_in");
  assert.ok(l14.rules[1].reason.includes("no person to check"));
  assert.equal(l14.status, "routed");
  assert.equal(l14.matched_rule, "enterprise");
});

test("personal email and domain rules", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.l4.status, "personal_email");
  assert.equal(rows.l4.queue, "Self-serve");
  assert.equal(rows.l13.status, "personal_email");
  assert.equal(rows.l5.identifier.domain, "delta.example");
  assert.equal(rows.l5.person.status, "personal_email");
  assert.equal(rows.l5.flags.length, 2);
  assert.ok(rows.l6.flags[0].includes("differs from the work email domain eps.example"));
  assert.ok(rows.l12.flags[0].includes("theta-group.example differs"));
  assert.ok(rows.l2.flags[0].includes("f900000000099"));
  assert.ok(!transport.sent.some((r) => /personal\.example|outlook\.com/.test(JSON.stringify(r.query))));
});

test("invalid, missing and duplicate rows", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.l9.status, "invalid_input");
  assert.ok(rows.l9.status_reason.includes("is not an email address"));
  assert.equal(rows.l10.status, "company_not_found");
  assert.equal(rows.l8.duplicate_of, "l1");
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 10);
  assert.equal(transport.sent.filter((r) => r.path === "/decisions").length, 9);
  assert.equal(transport.sent.filter((r) => r.path === "/people").length, 6);
  assert.equal(result.ledger.filter((e: Json) => e.outcome === "cache_hit").length, 3);
  assert.deepEqual(result.summary.review_rows, ["l7", "l9", "l10", "l11"]);
});

test("icp_fit unknown never matches yes or no", async () => {
  const [result, transport] = await run("icp-unknown", { config: "icp-unknown.config.json" });
  const rows = byId(result);
  assert.equal(rows.u1.matched_rule, "icp_unknown");
  assert.equal(rows.u2.matched_rule, "icp_yes");
  assert.equal(rows.u3.status, "routed_default");
  assert.ok(transport.sent.filter((r) => r.path === "/decisions").every((r) => Object.keys(r.query).length === 0));
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.status, "error");
  assert.equal(rows.e1.queue, "Sales ops review");
  assert.equal(rows.e2.icp.error.status, 502);
  assert.equal(rows.e2.matched_rule, "enterprise");
  assert.equal(rows.e3.icp.error.kind, "timeout");
  assert.equal(rows.e3.rules[3].outcome, "error");
  assert.equal(rows.e4.icp.error.kind, "invalid_response");
  assert.equal(rows.e5.person.error.status, 503);
  assert.equal(rows.e6.error.kind, "insufficient_credits");
  assert.equal(rows.e7.status, "not_processed");
  assert.equal(rows.e7.queue, null);
  assert.equal(result.credits.requests_with_uncertain_charge, 5);
  assert.equal(transport.sent.length, 12);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { config: "budget.config.json", maxCredits: 1.5 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "routed");
  assert.equal(rows["row-2"].status, "error");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(rows["row-3"].status, "not_processed");
  assert.equal(transport.sent.length, 1);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>Theta</b>") && html.includes("&lt;b&gt;Theta&lt;/b&gt;"));
  assert.ok(readFileSync(join(out, "results.csv"), "utf-8").includes("'=HYPERLINK"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique business domains and emails", () => {
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, config()).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 10);
  assert.equal(calls["POST /decisions"].requests, 10);
  assert.equal(calls["GET /people"].requests, 7);
  assert.deepEqual(plan(rows, config("basic.config.json")).map((c) => c.endpoint), ["GET /company"]);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.rules = []; }, "rules must be a non-empty list"],
  [(c) => { delete c.review_queue; }, "review_queue must be a non-empty queue name"],
  [(c) => { c.rules[1].id = "nz_leads"; }, "used twice"],
  [(c) => { c.rules[0].queue = ""; }, "queue must be a non-empty queue name"],
  [(c) => { c.rules[0].when = {}; }, "at least one condition"],
  [(c) => { c.rules[0].when.states_any = ["VIC"]; }, "unknown key"],
  [(c) => { c.rules[1].when.employee_count_max = 10; }, "more than employee_count_max"],
  [(c) => { c.rules[1].when.icp_fit = "maybe"; }, "icp_fit must be yes, no or unknown"],
  [(c) => { delete c.icp; }, "icp_fit needs icp in the configuration"],
  [(c) => { c.fetch_person = false; }, "seniority_in needs fetch_person set to true"],
  [(c) => { c.icp.description = "Mid-market SaaS"; }, "icp.description is not accepted"],
  [(c) => { c.icp.staff_in = "UK"; }, "icp.staff_in must be one of"],
  [(c) => { c.icp.revenue_bands = ["$2M"]; }, "not a Firmable revenue band"],
  [(c) => { c.icp.criteria = ["x"]; }, "icp.criteria must be a list"],
  [(c) => { c.icp.colour = "orange"; }, "unknown key"],
  [(c) => { c.personal_email_domains = ["not a domain"]; }, "personal_email_domains[1] is not a valid domain"],
  [(c) => { c.with_reasoning = "yes"; }, "with_reasoning must be true or false"],
  [(c) => { c.rules[0].id = "1rule"; }, "must start with a lower-case letter"],
  [(c) => { c.colour = "orange"; }, "unknown key"],
];
for (const [mutate, message] of bad) {
  test(`invalid config: ${message}`, () => {
    const cfg = JSON.parse(readFileSync(join(F, "config.json"), "utf-8"));
    mutate(cfg);
    assert.throws(() => loadConfig(cfg), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 17.8);
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await cliMain(RECIPE, [...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects bad input", async () => {
  const d = tmp();
  writeFileSync(join(d, "in.csv"), "name\nAcme\n");
  assert.equal(await cliMain(RECIPE, ["--input", join(d, "in.csv"), "--config", join(F, "config.json"), "--out", d]), 2);
});

// ----- single-lead server ------------------------------------------------------------------------

const LEAD = JSON.parse(readFileSync(join(F, "serve.input.json"), "utf-8"))[0];
const factory = (maxCredits = 5) => () => makeClient(join(F, "serve.scenario.json"), maxCredits)[0];

test("handleRequest routes one lead like the batch", async () => {
  const cfg = config("serve.config.json");
  const answer = await handleRequest("POST", "/route", JSON.stringify(LEAD), { config: cfg, clientFactory: factory() });
  assert.equal(answer.status, 200);
  const [batch] = await runWithFixtures(RECIPE, { inputPath: join(F, "serve.input.json"),
    configPath: join(F, "serve.config.json"), scenario: join(F, "serve.scenario.json") });
  assert.deepEqual(answer.body, batch.rows[0]);
  assert.equal(answer.body.queue, "Enterprise AE");
  assert.ok(!("ledger" in answer.body));
  assert.ok(!JSON.stringify(answer.body).includes("fbl_dev_"));
});

test("handleRequest rejects bad requests", async () => {
  const cfg = config("serve.config.json");
  const call = (method: string, path: string, body: string) => handleRequest(method, path, body, { config: cfg, clientFactory: factory() });
  assert.equal((await call("POST", "/other", "{}")).status, 404);
  assert.equal((await call("GET", "/route", "")).status, 405);
  assert.equal((await call("POST", "/route", "x".repeat(MAX_BODY + 1))).status, 413);
  assert.equal((await call("POST", "/route", "{not json")).status, 400);
  assert.equal((await call("POST", "/route", "[1]")).status, 400);
  assert.equal((await call("POST", "/route", JSON.stringify({ lead_name: "No identifier" }))).body.error.code, "bad_lead");
  assert.equal((await call("POST", "/route", JSON.stringify({ domain: { nested: 1 } }))).status, 400);
});

test("each request gets a fresh budget", async () => {
  const cfg = config("serve.config.json");
  let answer = await handleRequest("POST", "/route", JSON.stringify(LEAD), { config: cfg, clientFactory: factory(0.5) });
  assert.equal(answer.body.status, "error");
  assert.equal(answer.body.error.kind, "budget_exceeded");
  answer = await handleRequest("POST", "/route", JSON.stringify(LEAD), { config: cfg, clientFactory: factory() });
  assert.equal(answer.body.status, "routed");
});

test("server on port 0 routes one lead", async () => {
  const server = makeServer(config("serve.config.json"), factory());
  await new Promise<void>((resolve) => server.listen(0, HOST, resolve));
  try {
    const addr = server.address() as AddressInfo;
    assert.equal(addr.address, "127.0.0.1");
    const res = await fetch(`http://127.0.0.1:${addr.port}/route`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(LEAD),
    });
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("access-control-allow-origin"), null);
    assert.equal((await res.json() as Json).matched_rule, "enterprise_exec");
    const get = await fetch(`http://127.0.0.1:${addr.port}/route`);
    assert.equal(get.status, 405);
    await get.arrayBuffer();
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("serve needs a limit and a key", async () => {
  assert.equal(await serveMain(["--config", join(F, "config.json")]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await serveMain(["--config", join(F, "config.json"), "--max-credits-per-lead", "2"]), 2);
});
