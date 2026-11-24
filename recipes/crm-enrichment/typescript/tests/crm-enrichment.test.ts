import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, normaliseIdentifier, plan, valueText } from "../src/recipe.js";

const F = fixturesDir("crm-enrichment");
const tmp = () => mkdtempSync(join(tmpdir(), "ce-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string; ext?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.${o.ext ?? "csv"}`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const changes = (row: Json): Record<string, Json> => Object.fromEntries(row.changes.map((c: Json) => [c.column, c]));

test("happy path statuses", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.deepEqual(result.rows.map((r: Json) => r.row_id), ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]);
  assert.equal(rows.h1.status, "enriched");
  assert.equal(rows.h1.fields_filled, 8);
  assert.equal(rows.h2.lookups.person.status, "no_identifier");
  assert.equal(rows.h3.status, "not_matched");
  assert.equal(rows.h4.duplicate_of, "h1");
  assert.equal(rows.h5.status, "invalid_input");
  assert.ok(rows.h5.status_reason.includes("'not a domain' is not a valid domain"));
  assert.equal(rows.h6.status, "no_change");
  assert.equal(rows.h7.lookups.company.status, "no_identifier");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 4);
  assert.equal(transport.sent.filter((r) => r.path === "/people").length, 4);
});

test("fill_empty keeps conflicting values", async () => {
  const [result] = await run("happy");
  const c = changes(byId(result).h2);
  assert.equal(c.company_type.action, "conflict_kept");
  assert.equal(c.company_type.output, "Government");
  assert.equal(c.company_type.api_value, "Private company");
  assert.equal(c.year_founded.api_value, "2031");
  assert.equal(c.employee_count.output, "4");
  assert.deepEqual(result.summary.conflicts.map((x: Json) => `${x.row_id}:${x.column}`).sort(),
    ["h2:company_type", "h2:year_founded", "h4:contact_title", "h4:industry"]);
});

test("comparison rules treat case, spaces and domains as equal", async () => {
  const [result] = await run("happy");
  const c = changes(byId(result).h1);
  assert.equal(c.company_name.action, "unchanged");
  assert.equal(c.company_name.output, "ACME   example");
  assert.equal(c.website.action, "unchanged");
  assert.equal(c.industry.output, "Computer software; Information technology");
  assert.equal(changes(byId(result).h6).contact_title.action, "unchanged");
});

test("missing values and do-not-call phones are not written", async () => {
  const [result] = await run("happy");
  const c = changes(byId(result).h6);
  assert.equal(c.website.action, "no_api_value");
  assert.equal(c.website.api_value, null);
  assert.equal(c.mobile.action, "skipped_dnd");
  assert.equal(c.mobile.is_dnd, true);
  assert.equal(c.mobile.output, "");
  assert.equal(changes(byId(result).h1).mobile.is_dnd, false);
  assert.deepEqual([...new Set(Object.values(changes(byId(result).h3)).map((x: Json) => x.action))], ["source_unavailable"]);
});

test("overwrite policy replaces and reports", async () => {
  const [result] = await run("overwrite", { config: "overwrite.config.json" });
  const row = byId(result).o1;
  const c = changes(row);
  assert.equal(c.company_name.action, "overwritten");
  assert.equal(c.company_name.output, "Beta Example");
  assert.equal(c.website.action, "unchanged");
  assert.equal(row.fields_overwritten, 4);
  assert.ok(result.summary.conflicts.every((x: Json) => x.action === "overwritten"));
});

test("errors are kept per lookup and the run stops on 402", async () => {
  const [result, transport] = await run("errors", { config: "errors.config.json" });
  const rows = byId(result);
  assert.equal(rows.e1.status, "incomplete");
  assert.equal(rows.e1.lookups.company.error.status, 500);
  assert.equal(changes(rows.e1).contact_name.action, "filled");
  assert.equal(rows.e2.lookups.company.error.kind, "timeout");
  assert.equal(rows.e3.lookups.company.error.kind, "invalid_response");
  assert.equal(rows.e4.lookups.person.error.kind, "insufficient_credits");
  assert.equal(rows.e5.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 6);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { config: "budget.config.json", maxCredits: 1.5 });
  const rows = byId(result);
  assert.equal(rows.b1.lookups.person.error.kind, "budget_exceeded");
  assert.equal(rows.b2.status, "not_processed");
  assert.equal(transport.sent.length, 1);
});

test("ABN identifiers from JSON input", async () => {
  const [result, transport] = await run("abn", { config: "abn.config.json", ext: "json" });
  const rows = byId(result);
  assert.equal(rows.a1.lookups.company.value, "51900000001");
  assert.equal(changes(rows.a1).hq.action, "conflict_kept");
  assert.equal(rows.a2.status, "invalid_input");
  assert.deepEqual(transport.sent.map((r) => r.query), [{ abn: "51900000001" }]);
});

test("enriched CSV keeps input columns and order", async () => {
  const out = tmp();
  await run("happy", { out });
  const table = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  const header = table[0];
  const source = readFileSync(join(F, "happy.input.csv"), "utf-8").split("\n")[0].split(",");
  assert.deepEqual(header.slice(0, source.length), source);
  assert.deepEqual(header.slice(-8), ["enrichment_status", "enrichment_reason", "company_match", "person_match",
    "fields_filled", "fields_overwritten", "conflicts", "duplicate_of"]);
  assert.deepEqual(table.slice(1).map((r) => r[0]), ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]);
  assert.ok(table[7][header.indexOf("company_name")].startsWith("'="));
  assert.ok(!readFileSync(join(out, "report.html"), "utf-8").includes("<script>alert"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("value text and identifiers", () => {
  assert.equal(valueText(["a", null, "b"]), "a; b");
  assert.equal(valueText(4.0), "4");
  assert.equal(valueText({}), "");
  assert.equal(valueText([{ value: "x" }]), '[{"value":"x"}]');
  assert.equal(normaliseIdentifier("work_email", " Jane.Citizen@Example.com "), "jane.citizen@example.com");
  assert.equal(normaliseIdentifier("abn", "51 824 753 556"), "51824753556");
  assert.equal(normaliseIdentifier("id", "f9 0"), null);
});

test("plan counts unique valid identifiers", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 4);
  assert.equal(calls["GET /people"].requests, 4);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.mappings = []; }, "mappings must be a non-empty list"],
  [(c) => { c.merge_policy = "replace"; }, "merge_policy must be fill_empty or overwrite"],
  [(c) => { c.mappings[1].column = "company_name"; }, "is mapped twice"],
  [(c) => { c.mappings[0].column = "domain"; }, "is an identifier column"],
  [(c) => { c.mappings[0].field = "nickname"; }, "does not start with a documented company field"],
  [(c) => { c.mappings[6].field = "gender"; }, "does not start with a documented person field"],
  [(c) => { c.identifiers.person.type = "phone"; }, "identifiers.person.type must be one of"],
  [(c) => { delete c.identifiers.person; }, "identifiers.person is not set"],
  [(c) => { c.comparison = { trim: true }; }, "comparison has unknown key"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 8);
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await cliMain(RECIPE, [...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects bad config", async () => {
  const d = tmp();
  writeFileSync(join(d, "config.json"), JSON.stringify({ identifiers: {}, mappings: [] }));
  assert.equal(await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(d, "config.json"), "--out", d]), 2);
});
