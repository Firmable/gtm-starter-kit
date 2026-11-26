import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("tech-sales-opportunities");
const tmp = () => mkdtempSync(join(tmpdir(), "tso-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  const own = `${name}.config.json`;
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? (existsSync(join(F, own)) ? own : "config.json")),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const targets = (row: Json): Record<string, Json> => Object.fromEntries(row.targets.map((t: Json) => [t.target_id, t]));

test("happy path statuses", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h1.status, "matched");
  assert.equal(rows.h2.status, "not_observed_incomplete");
  assert.equal(rows.h3.status, "no_technology_data");
  assert.equal(rows.h4.duplicate_of, "h1");
  assert.equal(rows.h5.status, "invalid_input");
  assert.equal(rows.h6.status, "company_not_found");
  assert.equal(rows.h7.status, "matched");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/tech-intel").length, 7);
});

test("target results keep observed and not observed distinct", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  const h1 = targets(rows.h1);
  assert.equal(h1.salesforce.result, "observed");
  assert.equal(h1.snowflake.result, "not_observed");
  assert.equal(h1.snowflake.result_text, "Not observed in retrieved data.");
  assert.equal(targets(rows.h2).salesforce.result, "not_observed_in_pages_retrieved");
  assert.ok(rows.h2.status_reason.includes("page limit was reached: 20 of 25"));
  assert.ok(rows.h6.targets.every((t: Json) => t.result === "not_checked"));
});

test("matching is explicit with no fuzzy matching", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  const hub = targets(rows.h1).hubspot.matches;
  assert.deepEqual(hub.map((m: Json) => m.technology.product), ["HubSpot Marketing Hub"]);
  assert.deepEqual(hub[0].match_basis, ["alias of vendor name HubSpot"]);
  assert.deepEqual(targets(rows.h7).snowflake.matches.map((m: Json) => m.technology.product), ["SNOWFLAKE"]);
});

test("evidence locations are flagged, not dropped", async () => {
  const [result] = await run("happy");
  const ev = targets(byId(result).h1).salesforce.matches[0].evidence;
  assert.equal(ev.records_listed, 4);
  assert.deepEqual(ev.records.map((r: Json) => r.flags),
    [[], ["location_differs_from_company_domain"], ["no_location"], ["location_not_a_domain"]]);
  assert.deepEqual(ev.capped_types, ["job_ad"]);
  assert.ok(ev.notes.some((n: string) => n.includes("4 of 30 evidence record(s) are listed")));
  assert.equal(targets(byId(result).h1).hubspot.matches[0].evidence.status, "none_returned");
});

test("errors are kept and 402 stops the run", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.tech_intel.error.status, 500);
  assert.equal(rows.e2.status, "matched");
  assert.equal(rows.e2.tech_intel.error.kind, "timeout");
  assert.equal(rows.e3.tech_intel.error.kind, "invalid_response");
  assert.equal(rows.e4.tech_intel.error.kind, "insufficient_credits");
  assert.equal(rows.e5.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 5);
});

test("forbidden means access denied and stops", async () => {
  const [result, transport] = await run("forbidden");
  const rows = byId(result);
  assert.equal(rows.f1.status, "access_denied");
  assert.equal(rows.f2.status, "not_processed");
  assert.equal(transport.sent.length, 1);
});

test("budget keeps pages already fetched", async () => {
  const [result, transport] = await run("budget", { maxCredits: 6.5 });
  const rows = byId(result);
  assert.equal(rows["row-1"].tech_intel.stop_reason, "budget");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 2);
});

test("company lookup uses profile domains", async () => {
  const [result, transport] = await run("company");
  const rows = byId(result);
  assert.deepEqual(targets(rows.c1).salesforce.matches[0].evidence.records.map((r: Json) => r.flags), [[], []]);
  assert.equal(rows.c2.status, "company_not_found");
  assert.deepEqual(transport.sent.map((r) => r.path), ["/company", "/tech-intel", "/company"]);
});

test("no evidence mode", async () => {
  const [result, transport] = await run("no-evidence");
  const m = targets(result.rows[0]).hubspot.matches[0];
  assert.deepEqual(m.evidence, { status: "not_requested" });
  assert.deepEqual(transport.sent[0].query, { fqdn: "acme.example" });
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  assert.ok(!readFileSync(join(out, "report.html"), "utf-8").includes("<b>Bold</b>"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts pages per unique domain", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /tech-intel"].requests, 10);
  assert.equal(calls["GET /company"], undefined);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.targets = []; }, "targets must be a non-empty list"],
  [(c) => { c.targets[0].match = {}; }, "needs at least one product slug"],
  [(c) => { c.targets[0].match.fuzzy = true; }, "match has unknown key"],
  [(c) => { c.targets[1].id = "salesforce"; }, "used twice"],
  [(c) => { delete c.targets[0].relationship; }, "relationship must be text"],
  [(c) => { c.targets[0].match.aliases = [""]; }, "non-empty text values"],
  [(c) => { c.max_pages = 0; }, "max_pages must be a whole number"],
  [(c) => { c.with_evidence = "yes"; }, "with_evidence must be true or false"],
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 27);
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
