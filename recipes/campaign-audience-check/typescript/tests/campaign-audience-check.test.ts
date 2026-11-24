import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("campaign-audience-check");
const tmp = () => mkdtempSync(join(tmpdir(), "cac-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const resultsOf = (row: Json): string[] => row.campaigns.map((c: Json) => c.result);
const readJson = (name: string) => JSON.parse(readFileSync(join(F, name), "utf-8"));

test("happy path statuses follow the policy", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.r1.status, "in_campaigns");
  assert.deepEqual(resultsOf(rows.r1), ["include", "exclude", "review"]);
  assert.deepEqual(rows.r1.needs_review, ["Enterprise payroll"]);
  assert.equal(rows.r2.status, "no_campaign");
  assert.deepEqual(rows.r2.asked_with, { company_id: "f900000000002" });
  assert.equal(rows.r3.status, "needs_review");
  assert.equal(rows.r4.duplicate_of, "r1");
  assert.equal(rows.r5.status, "invalid_input");
  assert.ok(rows.r7.status_reason.includes("'maybe'"));
  assert.ok(rows.r8.status_reason.includes("'f123'"));
  assert.equal(rows.r9.status, "in_campaigns");
  assert.equal(rows.r10.status, "no_campaign");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.length, 16);
});

test("existing customer is excluded before any call", async () => {
  const [result, transport] = await run("happy");
  const r6 = byId(result).r6;
  assert.equal(r6.status, "excluded_customer");
  assert.equal(r6.existing_customer, true);
  assert.deepEqual(resultsOf(r6), ["excluded_customer", "excluded_customer", "excluded_customer"]);
  assert.ok(!transport.sent.some((r) => (r.jsonBody as Json)?.question?.domain === "delta.example"));
  assert.equal(result.summary.campaign_counts.au_fleet.excluded_customer, 1);
});

test("reason and filters are kept as returned", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  const nz = rows.r9.campaigns[1];
  assert.equal(nz.reasoning.reason, "Sells through <b>several</b> stores in New Zealand.");
  assert.equal(nz.credits, 0.08);
  const au = rows.r10.campaigns[0];
  assert.equal(au.result, "exclude");
  assert.deepEqual(au.filters, { countries: "passed", min_staff: "passed" });
});

test("model tool is named in coverage", async () => {
  const [result] = await run("happy");
  assert.ok(result.coverage.some((c: string) => c.includes("decision model") && c.includes("matches_campaign_audience")));
});

test("fetch company for display and missing profile", async () => {
  const [result, transport] = await run("fetch", { config: "fetch.config.json" });
  const rows = byId(result);
  assert.deepEqual(rows.f1.asked_with, { company_id: "f900000000001" });
  assert.equal(rows.f2.company_lookup, "not_found");
  assert.deepEqual(rows.f2.asked_with, { domain: "ghost.example" });
  assert.equal(rows.f2.status, "needs_review");
  assert.equal(rows.f3.company.company_type, "Government agency");
  assert.equal(rows.f4.status, "excluded_customer");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 3);
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.campaigns[0].error.status, 502);
  assert.equal(rows.e1.campaigns[1].error.kind, "timeout");
  assert.equal(rows.e1.status, "in_campaigns");
  assert.equal(rows.e2.campaigns[0].error.kind, "invalid_response");
  assert.equal(rows.e2.campaigns[1].error.code, "decision_failed");
  assert.equal(rows.e2.status, "incomplete");
  assert.deepEqual(resultsOf(rows.e3), ["error", "not_asked", "not_asked"]);
  assert.equal(rows.e4.status, "not_processed");
  assert.equal(transport.sent.length, 8);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 1.2 });
  const rows = byId(result);
  assert.deepEqual(resultsOf(rows["row-1"]), ["include", "exclude", "error"]);
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 3);
});

test("missing tool and schema drift stop before paid calls", async () => {
  for (const [name, word] of [["bad-tool", "matches_campaign_audience"], ["schema-drift", "revenue_bands"]]) {
    const [result, transport] = await run(name);
    assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
    assert.ok(result.errors[0].message.includes(word));
    assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
  }
});

test("tool list error stops the run", async () => {
  const [result] = await run("catalogue-error");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed", "not_processed"]);
  assert.equal(result.errors[0].status, 500);
});

test("existing customers are asked when exclusion is off", async () => {
  const [result, transport] = await run("no-reasoning", { config: "no-reasoning.config.json" });
  const n1 = byId(result).n1;
  assert.equal(n1.status, "in_campaigns");
  assert.equal(n1.campaigns[0].reasoning, null);
  assert.ok(transport.sent.filter((r) => r.method === "POST").every((r) => Object.keys(r.query).length === 0));
});

test("outputs are safe and have a column per campaign", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<script>alert") && !html.includes("<b>several</b>"));
  const [header, ...rows] = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  const first = Object.fromEntries(header.map((h: string, i: number) => [h, rows[0][i]]));
  assert.equal(first.campaign_au_fleet, "include");
  assert.equal(first.campaign_enterprise_payroll, "review");
  assert.equal(first.company_id, "f900000000001");
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique asked accounts", () => {
  const cfg = loadConfig(readJson("config.json"));
  const calls = Object.fromEntries(plan(RECIPE.readInput(join(F, "happy.input.csv")), cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"], undefined);
  assert.equal(calls["POST /decisions"].requests, 15);
  const fetch = loadConfig(readJson("fetch.config.json"));
  const fc = Object.fromEntries(plan(RECIPE.readInput(join(F, "fetch.input.csv")), fetch).map((c) => [c.endpoint, c]));
  assert.equal(fc["GET /company"].requests, 3);
  assert.equal(fc["POST /decisions"].requests, 6);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.campaigns = []; }, "campaigns must be a non-empty list"],
  [(c) => { c.campaigns = [...c.campaigns, ...c.campaigns]; }, "the most allowed is 5"],
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
  [(c) => { c.campaigns[1].id = "au_fleet"; }, "used twice"],
  [(c) => { c.campaigns[0].tone = "loud"; }, "unknown key(s): tone"],
  [(c) => { c.campaigns[0].campaign.colour = "orange"; }, "does not list: colour"],
  [(c) => { delete c.campaigns[0].campaign.message; }, "message is required"],
  [(c) => { c.campaigns[0].campaign.message = "hi"; }, "message is required"],
  [(c) => { c.campaigns[0].campaign.min_staff = -1; }, "min_staff must be a whole number"],
  [(c) => { c.campaigns[0].campaign.max_staff = 10; }, "min_staff is more than"],
  [(c) => { c.campaigns[0].campaign.staff_in = "GB"; }, "staff_in must be one of"],
  [(c) => { c.campaigns[0].campaign.countries = ["au"]; }, "two-letter upper-case"],
  [(c) => { c.campaigns[2].campaign.revenue_bands = ["$1B+"]; }, "revenue_bands has '$1B+'"],
  [(c) => { c.exclude_existing_customers = "yes"; }, "exclude_existing_customers must be true or false"],
];
for (const [mutate, message] of bad) {
  test(`invalid config: ${message}`, () => {
    const cfg = readJson("config.json");
    mutate(cfg);
    assert.throws(() => loadConfig(cfg), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("config defaults", () => {
  const cfg = loadConfig({ campaigns: [{ id: "a", campaign: { message: "Fleet telematics" } }] });
  assert.equal(cfg.exclude_existing_customers, true);
  assert.equal(cfg.with_reasoning, true);
  assert.equal(cfg.fetch_company, false);
  assert.equal(cfg.campaigns[0].label, "a");
});

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 1.2);
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await cliMain(RECIPE, [...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects input without an identifier", async () => {
  const d = tmp();
  writeFileSync(join(d, "in.csv"), "account_name\nAcme\n");
  assert.equal(await cliMain(RECIPE, ["--input", join(d, "in.csv"), "--config", join(F, "config.json"), "--out", d]), 2);
});
