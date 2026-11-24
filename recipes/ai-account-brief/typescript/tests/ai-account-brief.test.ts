import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("ai-account-brief");
const tmp = () => mkdtempSync(join(tmpdir(), "aab-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  const own = `${name}.config.json`;
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? (existsSync(join(F, own)) ? own : "config.json")),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const areas = (row: Json): Record<string, Json> => Object.fromEntries(row.ai_adoption.areas.map((a: Json) => [a.area, a]));

test("happy path statuses", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.a1.status, "brief_ready");
  assert.equal(rows.a2.status, "no_ai_adoption_data");
  assert.equal(rows.a3.status, "brief_ready");
  assert.equal(rows.a4.duplicate_of, "a1");
  assert.equal(rows.a5.status, "invalid_input");
  assert.equal(rows.a6.status, "company_not_found");
  assert.equal(rows.a7.status, "brief_ready");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/ai-adoption").length, 4);
});

test("AI Adoption is shown as returned", async () => {
  const [result] = await run("happy");
  const ai = byId(result).a1.ai_adoption;
  assert.equal(ai.overall_level, "STRONG");
  assert.deepEqual(ai.momentum, { label: "EXPANDING", signals: [{ kind: "AI hiring", count: 2, latest: "2026-10-13" }] });
  const a = areas(byId(result).a1);
  assert.deepEqual(Object.keys(a), ["architecture", "footprint", "hiring", "initiatives", "workforce"]);
  assert.equal(a.initiatives.observability, "LOW");
  assert.deepEqual(a.hiring.detail, { sub_dimensions: { hiring_scale: { roles_12m: 3, roles_90d: 0 } } });
});

test("evidence is split and gaps are flagged", async () => {
  const [result] = await run("happy");
  const a = areas(byId(result).a1);
  assert.deepEqual(a.architecture.evidence.qualifying.map((i: Json) => i.technology), ["Model Server Example"]);
  assert.deepEqual(a.architecture.evidence.not_qualifying.map((i: Json) => i.technology), ["Old Queue Example"]);
  assert.deepEqual(a.architecture.flags, ["Evidence is truncated: 2 of 33 item(s) were returned."]);
  assert.equal(a.footprint.evidence.qualification_unstated.length, 1);
  assert.deepEqual(a.initiatives.flags, ["Firmable holds 5 evidence item(s) for this area, but none were returned."]);
});

test("workforce item naming another organisation is flagged, not removed", async () => {
  const [result] = await run("happy");
  const wf = areas(byId(result).a1).workforce;
  assert.deepEqual(wf.evidence.other_organisation_items, [
    { ref: { type: "person", id: "fp900000000002" }, fields: [{ field: "company_name", value: "Other Org Example" }] }]);
  assert.equal(wf.evidence.qualifying.length + wf.evidence.not_qualifying.length, 3);
});

test("unknown level, conflicts and missing area", async () => {
  const [result] = await run("happy");
  const g = byId(result).a3;
  const a = areas(g);
  assert.equal(a.architecture.level_recognised, false);
  assert.deepEqual(a.workforce.flags, ["The API returned no data for this area."]);
  assert.equal(g.data_conflicts.length, 3);
  assert.equal(g.tech_intel.complete, false);
});

test("questions are templates filled from facts and skipped when missing", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  const qs = Object.fromEntries(rows.a1.suggested_questions.map((q: Json) => [q.id, q]));
  assert.equal(qs.tech_rollout.text, "You use Claude (Anthropic). How is it rolled out across Acme Example?");
  assert.equal(qs.tech_rollout.kind, "suggested_question");
  assert.equal(qs.offering.filled_from[0].source, "seller context");
  const skipped = Object.fromEntries(rows.a3.skipped_questions.map((s: Json) => [s.id, s.missing]));
  assert.deepEqual(skipped.tech_rollout, ["technology"]);
  assert.deepEqual(rows.a2.skipped_questions.map((s: Json) => s.id), ["tech_rollout", "ai_role", "ai_tool", "momentum", "offering"]);
});

test("errors are kept and 402 stops the run", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.ai_adoption.error.status, 500);
  assert.equal(rows.e1.tech_intel.status, "retrieved");
  assert.equal(rows.e2.ai_adoption.error.kind, "invalid_response");
  assert.equal(rows.e3.tech_intel.error.kind, "timeout");
  assert.equal(rows.e4.tech_intel.status, "not_requested");
  assert.equal(rows.e5.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 11);
});

test("forbidden Tech Intel is not asked again", async () => {
  const [result, transport] = await run("forbidden");
  const rows = byId(result);
  assert.equal(rows.f1.tech_intel.status, "not_on_plan");
  assert.equal(rows.f2.tech_intel.status, "not_on_plan");
  assert.equal(transport.sent.filter((r) => r.path === "/tech-intel").length, 1);
});

test("budget stops before Tech Intel", async () => {
  const [result, transport] = await run("budget", { maxCredits: 8 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "incomplete");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 2);
});

test("lean config skips evidence and Tech Intel", async () => {
  const [result, transport] = await run("lean");
  const row = result.rows[0];
  assert.ok(row.ai_adoption.areas.every((a: Json) => a.evidence.status === "not_requested"));
  assert.equal(row.tech_intel.status, "not_requested");
  assert.deepEqual(transport.sent.map((r) => r.path), ["/company", "/ai-adoption"]);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<script>alert") && html.includes('class="template"'));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique valid domains", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 5);
  assert.equal(calls["POST /ai-adoption"].requests, 5);
  assert.equal(calls["GET /tech-intel"].requests, 5);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.discovery_questions.push({ id: "x", template: "Hello {nobody}" }); }, "not an API fact placeholder"],
  [(c) => { c.discovery_questions.push({ id: "x", template: "Hello {company_name" }); }, "unmatched brace"],
  [(c) => { c.discovery_questions.push({ id: "x", template: "Hello {Bad Name}" }); }, "invalid placeholder"],
  [(c) => { c.discovery_questions.push({ id: "tech_rollout", template: "Hi" }); }, "used twice"],
  [(c) => { c.seller_context = { company_name: "Me" }; }, "reserved for an API fact"],
  [(c) => { c.seller_context = { offering: "" }; }, "must be non-empty text"],
  [(c) => { c.tech_intel_max_pages = 0; }, "tech_intel_max_pages must be a whole number"],
  [(c) => { c.ai_evidence = "yes"; }, "ai_evidence must be true or false"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 45);
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
