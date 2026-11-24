import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("expansion-signals");
const tmp = () => mkdtempSync(join(tmpdir(), "es-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const signals = (row: Json): Record<string, Json> => Object.fromEntries(row.signals.map((s: Json) => [s.signal_id, s]));
const cfgJson = () => JSON.parse(readFileSync(join(F, "config.json"), "utf-8"));

test("happy path tiers follow the policy", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.r1.tier_id, "global_expand");
  assert.equal(rows.r2.tier_id, "multi_site");
  assert.equal(rows.r4.duplicate_of, "r1");
  assert.equal(rows.r5.status, "invalid_input");
  assert.equal(rows.r6.status, "invalid_input");
  assert.equal(rows.r7.status, "no_tier");
  assert.ok(rows.r7.status_reason.includes("1 signal(s) answered unknown"));
  assert.equal(rows.r9.tier_id, "regional");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.length, 38);
});

test("unknown sends to review, never to a lower tier", async () => {
  const [result] = await run("happy");
  const r3 = byId(result).r3;
  assert.equal(r3.status, "needs_review");
  assert.equal(r3.tier_id, null);
  assert.equal(r3.deciding_tier_id, "global_expand");
  assert.deepEqual(r3.tiers.map((t: Json) => t.outcome), ["needs_review", "not_considered", "not_considered"]);
  assert.equal(signals(r3).multiple_sites.result, "yes");
  assert.equal(byId(result).r8.deciding_tier_id, "regional");
});

test("signal details are kept as returned", async () => {
  const [result] = await run("happy");
  const s = signals(byId(result).r1);
  assert.deepEqual(s.multinational.details, { countries: ["AU", "NZ", "US"] });
  assert.deepEqual(s.multiple_sites.fact, { key: "locations_held", value: 4 });
  assert.equal(s["local_team:SG"].result, "unknown");
  assert.equal(s["local_team:SG"].fact, null);
});

test("company_id wins and both identifiers dedupe", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.deepEqual(rows.r2.lookup, { by: "id", value: "f900000000002" });
  assert.equal(rows.r11.duplicate_of, "r2");
  assert.ok(!transport.sent.some((r) => r.method === "POST" && (r.jsonBody as Json)?.question?.domain === "other.example"));
});

test("conflicting answers are surfaced, not corrected", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  assert.ok(rows.r2.data_conflicts[0].includes("is_multinational answered no"));
  assert.ok(rows.r9.data_conflicts[0].includes("has_multiple_sites answered no"));
});

test("Decisions company_not_found stops that row", async () => {
  const [result] = await run("happy");
  assert.equal(byId(result).r10.status, "company_not_found");
  assert.deepEqual(result.errors, []);
});

test("fetch_company with reasoning", async () => {
  const [result, transport] = await run("company", { config: "company.config.json" });
  const rows = byId(result);
  assert.equal(rows.c1.status, "tier_assigned");
  assert.equal(signals(rows.c1).multinational.reasoning.reason, "Synthetic reason for a yes answer.");
  assert.equal(rows.c2.status, "company_not_found");
  assert.ok(rows.c3.data_conflicts[0].includes("differs from the profile's fqdn gamma.example"));
  assert.equal(rows.c4.status, "error");
  assert.deepEqual(transport.unused(), []);
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  const e1 = signals(rows.e1);
  assert.equal(e1.multinational.error.status, 502);
  assert.equal(e1.multiple_sites.error.kind, "timeout");
  assert.equal(rows.e1.status, "incomplete");
  assert.equal(signals(rows.e2).multiple_sites.error.kind, "invalid_response");
  assert.equal(rows.e2.status, "tier_assigned");
  assert.equal(signals(rows.e3).multiple_sites.result, "not_asked");
  assert.equal(rows.e4.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 14);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 1.1 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "incomplete");
  assert.equal(signals(rows["row-1"])["local_team:AU"].error.kind, "budget_exceeded");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(transport.sent.length, 3);
});

test("tool list drift stops before paid calls", async () => {
  const [result, transport] = await run("drift");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.equal(result.errors.length, 2);
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("outputs are safe and the CSV has a column per signal", async () => {
  const out = tmp();
  await run("happy", { out });
  assert.ok(!readFileSync(join(out, "report.html"), "utf-8").includes("<script>alert"));
  const csv = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  for (const col of ["multinational", "multiple_sites", "local_team:AU", "local_team:SG", "office_in:NZ", "office_in:US"]) {
    assert.ok(csv[0].includes(col));
  }
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique valid identifiers and warns", () => {
  const cfg = loadConfig(cfgJson());
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"], undefined);
  assert.equal(calls["POST /decisions"].requests, 42);
  assert.ok(calls["POST /decisions"].purpose.includes("Config warning: has_local_team answers only for AU and NZ"));
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.checks = {}; }, "checks must turn on at least one signal"],
  [(c) => { c.checks = []; }, "checks must be an object"],
  [(c) => { c.checks.colour = true; }, "Unknown key(s) in checks: colour"],
  [(c) => { c.checks.multiple_sites = { min_sites: 1 }; }, "min_sites must be a whole number, 2 or more"],
  [(c) => { c.checks.local_teams.push({ country: "GB" }); }, "country must be one of"],
  [(c) => { c.checks.local_teams.push({ country: "AU" }); }, "'AU' is listed twice"],
  [(c) => { c.checks.offices_in = ["nz"]; }, "two-letter upper-case country code"],
  [(c) => { c.tiers = []; }, "tiers must be a list of 1 to 20 tiers"],
  [(c) => { c.tiers[0].when.all = ["office_in:SG"]; }, "names signal 'office_in:SG'"],
  [(c) => { c.tiers[1].id = "global_expand"; }, "used twice"],
  [(c) => { c.tiers[1].when = {}; }, "needs at least one signal"],
  [(c) => { c.with_reasoning = "yes"; }, "with_reasoning must be true or false"],
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
];
for (const [mutate, message] of bad) {
  test(`invalid config: ${message}`, () => {
    const cfg = cfgJson();
    mutate(cfg);
    assert.throws(() => loadConfig(cfg), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("defaults", () => {
  const cfg = loadConfig({ checks: { multiple_sites: true, local_teams: [{ country: "NZ" }] },
    tiers: [{ id: "t", when: { any: ["multiple_sites", "local_team:NZ"] } }] });
  assert.deepEqual(cfg.signals[0].inputs, { min_sites: 2 });
  assert.deepEqual(cfg.signals[1].inputs, { country: "NZ", min_staff: 5 });
  assert.deepEqual(cfg.warnings, []);
  assert.equal(cfg.tiers[0].label, "t");
});

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 1.68);
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
