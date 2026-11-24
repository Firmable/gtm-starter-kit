import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, SNAPSHOT_FORMAT, loadConfig, plan } from "../src/recipe.js";
import { runCli, writeSnapshot } from "../src/cli.js";

const F = fixturesDir("account-change-monitor");
const K1 = "company:fqdn=acme.example|person:work_email=alex.example@acme.example";
const tmp = () => mkdtempSync(join(tmpdir(), "acm-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  const cfg = o.config ?? (existsSync(join(F, `${name}.config.json`)) ? join(F, `${name}.config.json`) : join(F, "config.json"));
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: cfg, scenario: join(F, `${name}.scenario.json`),
    maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const changes = (row: Json): Record<string, Json> => Object.fromEntries(row.changes.map((c: Json) => [`${c.source} ${c.field}`, c]));

test("first run creates a baseline without change claims", async () => {
  const [result, transport] = await run("baseline");
  const rows = byId(result);
  assert.deepEqual(["b1", "b2", "b3", "b6"].map((k) => rows[k].status), Array(4).fill("baseline_created"));
  assert.ok(result.rows.every((r: Json) => r.changes.length === 0));
  assert.equal(rows.b3.duplicate_of, "b1");
  assert.equal(rows.b4.status, "invalid_input");
  assert.equal(rows.b5.status, "invalid_input");
  assert.equal(rows.b6.sources.company.result, "not_requested");
  assert.equal(rows.b2.sources.ai_adoption.result, "not_found");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 2);
  const snap = result.summary.snapshot;
  assert.equal(snap.snapshot_format, SNAPSHOT_FORMAT);
  assert.equal(snap.taken_on, "2026-11-24");
  assert.deepEqual(Object.keys(snap.accounts).sort(), [K1, "company:fqdn=beta.example", "person:work_email=sam.sample@other.example"]);
  assert.equal(snap.accounts[K1].sources.tech_intel.values.complete, false);
  assert.ok(result.coverage.some((c: string) => c.startsWith("1 Tech Intel list(s) are incomplete")));
});

test("change types are distinct and never confirmed", async () => {
  const [result, transport] = await run("changes");
  const rows = byId(result);
  const c1 = changes(rows.c1);
  assert.equal(rows.c1.status, "changes_detected");
  assert.equal(c1["company employee_count"].change_type, "value_changed");
  assert.equal(c1["company industries"].change_type, "newly_observed");
  assert.equal(c1["company revenue"].change_type, "no_longer_observed_in_retrieved_data");
  assert.equal(c1["company year_founded"].current, 2024);
  assert.ok(c1["ai_adoption overall_level"].note.includes("framework version changed from 2026.09 to 2026.10"));
  assert.equal(c1["ai_adoption momentum.label"].note, null);
  assert.equal(c1["tech_intel technology:marketing-automation/marketo"].change_type, "no_longer_observed_in_retrieved_data");
  assert.equal(c1["tech_intel technology:help-desk/zendesk.confidence"].current, "high");
  assert.deepEqual(rows.c1.baseline_fields, ["ai_adoption.size_band"]);
  assert.equal(rows.c2.status, "no_changes_detected");
  assert.equal(rows.c4.status, "baseline_created");
  assert.deepEqual(transport.unused(), []);
});

test("failed and missing responses are not deletions", async () => {
  const [result] = await run("changes");
  const c3 = byId(result).c3;
  assert.equal(c3.status, "partially_compared");
  assert.equal(changes(c3)["company *"].change_type, "not_compared_fetch_failed");
  assert.equal(changes(c3)["ai_adoption *"].change_type, "not_compared_not_found");
  const gamma = result.summary.snapshot.accounts["company:fqdn=gamma.example"].sources;
  assert.equal(gamma.company.values.name, "Gamma Example");
  assert.equal(gamma.company.last_result, "error");
  assert.equal(gamma.ai_adoption.values.overall_level, "BASIC");
});

test("incomplete technology list is not read as removal", async () => {
  const [result] = await run("changes");
  const c6 = byId(result).c6;
  assert.ok(!c6.changes.some((c: Json) => c.source === "tech_intel"));
  assert.ok(c6.status_reason.includes("1 technology(ies) from the previous snapshot were not in the retrieved pages"));
  assert.equal(changes(c6)["ai_adoption dimensions.hiring.level"].current, "EXTREME");
});

test("identity change and source baselines", async () => {
  const [result] = await run("changes");
  const rows = byId(result);
  assert.ok(changes(rows.c7)["company id"].note.includes("different Firmable company ID"));
  assert.equal(rows.c8.sources.company.comparison, "baseline");
  assert.equal(changes(rows.c8)["person id"].note, null);
  assert.ok("company:fqdn=gone.example" in result.summary.snapshot.accounts);
});

test("errors are recorded and 402 stops the run", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.status, "fetch_failed");
  const kinds = new Set(Object.values(rows.e1.sources).map((s: Json) => s.error.kind));
  assert.deepEqual([...kinds].sort(), ["forbidden", "invalid_response", "server_error", "timeout"]);
  assert.equal(rows.e2.sources.tech_intel.result, "skipped");
  assert.equal(rows.e3.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(result.summary.snapshot.accounts[K1].sources.company.values.employee_count, 320);
  assert.equal(transport.sent.length, 6);
});

test("budget stops before Tech Intel", async () => {
  const [result, transport] = await run("budget", { maxCredits: 2.5 });
  const rows = byId(result);
  assert.equal(rows["row-1"].sources.tech_intel.error.kind, "budget_exceeded");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(transport.sent.length, 2);
});

test("snapshot round trip reports no changes", async () => {
  const out = tmp();
  const [first] = await run("baseline", { out });
  const path = writeSnapshot(out) as string;
  assert.deepEqual(JSON.parse(readFileSync(path, "utf-8")), first.summary.snapshot);
  const cfg = JSON.parse(readFileSync(join(F, "config.json"), "utf-8"));
  cfg.previous_snapshot = path;
  writeFileSync(join(out, "second.config.json"), JSON.stringify(cfg));
  const [second] = await run("baseline", { config: join(out, "second.config.json") });
  const rows = byId(second);
  assert.deepEqual(["b1", "b2", "b3", "b6"].map((k) => rows[k].status), Array(4).fill("no_changes_detected"));
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("baseline", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>html</b>") && html.includes("&lt;b&gt;html&lt;/b&gt;"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique valid subjects", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "baseline.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  for (const e of ["GET /company", "GET /people", "POST /ai-adoption", "GET /tech-intel"]) assert.equal(calls[e].requests, 2);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
  [(c) => { c.company_fields = ["request_id"]; }, "unknown field 'request_id'"],
  [(c) => { c.person_fields = ["position", "position"]; }, "lists 'position' twice"],
  [(c) => { c.tech_intel = { enabled: true, max_pages: 0 }; }, "max_pages must be a whole number"],
  [(c) => { c.tech_intel = { evidence: true }; }, "Unknown tech_intel key"],
  [(c) => { c.company_fields = []; c.person_fields = []; c.ai_adoption_fields = []; c.tech_intel = { enabled: false }; }, "Nothing to monitor"],
  [(c) => { c.evaluation_date = "24/11/2026"; }, "evaluation_date must be a date"],
  [(c) => { c.previous_snapshot = { accounts: {} }; }, "is not a gtm-account-change-monitor/1 snapshot"],
  [(c) => { c.previous_snapshot = "/nonexistent/snapshot.json"; }, "previous_snapshot file not found"],
  [(c) => { c.previous_snapshot = 7; }, "must be a file path"],
];
for (const [mutate, message] of bad) {
  test(`invalid config: ${message}`, () => {
    const cfg = JSON.parse(readFileSync(join(F, "config.json"), "utf-8"));
    mutate(cfg);
    assert.throws(() => loadConfig(cfg), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("previous snapshot file must be JSON", () => {
  const d = tmp();
  writeFileSync(join(d, "snap.json"), "{not json");
  assert.throws(() => loadConfig({ previous_snapshot: join(d, "snap.json") }), (e: Error) => e instanceof ConfigError && e.message.includes("is not valid JSON"));
});

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await runCli(["--input", join(F, "baseline.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 14.4);
  assert.ok(!existsSync(join(out, "snapshot.json")));
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "baseline.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await runCli([...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects bad input", async () => {
  const d = tmp();
  writeFileSync(join(d, "in.csv"), "name\nAcme\n");
  assert.equal(await runCli(["--input", join(d, "in.csv"), "--config", join(F, "config.json"), "--out", d]), 2);
});
