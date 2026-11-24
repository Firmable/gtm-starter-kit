import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("ai-adoption-segmentation");
const tmp = () => mkdtempSync(join(tmpdir(), "aas-"));

function run(name: string, o: { maxCredits?: number; out?: string } = {}) {
  const cfg = existsSync(join(F, `${name}.config.json`)) ? join(F, `${name}.config.json`) : join(F, "config.json");
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: cfg, scenario: join(F, `${name}.scenario.json`),
    maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));

test("first matching segment wins", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.deepEqual([rows.h1.status, rows.h1.segment_id], ["assigned", "ai_leaders"]);
  assert.deepEqual(rows.h2.trace.map((t: Json) => t.outcome), ["not_matched", "matched"]);
  assert.equal(rows.h3.segment_id, "ai_tooling");
  assert.ok(rows.h3.status_reason.includes("Claude (Anthropic)"));
  assert.equal(rows.h4.segment_id, "low_adoption");
  assert.equal(rows.h8.duplicate_of, "h1");
  assert.equal(rows.h9.status, "invalid_input");
  assert.deepEqual([rows.h12.status, rows.h12.segment_id], ["no_segment_matched", "unmatched"]);
  assert.deepEqual(transport.unused(), []);
  assert.deepEqual(result.summary.segment_counts, { ai_leaders: 3, ai_expanding: 1, ai_tooling: 1, low_adoption: 1, review: 4, unmatched: 1 });
});

test("Tech Intel is requested only when a technology condition is tried", async () => {
  const [result, transport] = await run("happy");
  assert.deepEqual(transport.sent.filter((r) => r.path === "/tech-intel").map((r) => r.query.id),
    ["f910000000003", "f910000000004", "f910000000005", "f910000000010"]);
  assert.equal(byId(result).h1.technologies, null);
});

test("missing data goes to review, not a lower segment", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.deepEqual([rows.h5.status, rows.h5.segment_id], ["needs_review", "review"]);
  assert.ok(rows.h5.status_reason.includes("only 1 of 230 technologies were retrieved"));
  assert.equal(rows.h6.ai_adoption_found, false);
  assert.ok(rows.h10.status_reason.includes("workforce area was not returned"));
  assert.equal(rows.h4.trace[2].conditions[1].outcome, "not_met");
});

test("unfamiliar levels", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h7.status, "needs_review");
  assert.deepEqual(rows.h7.trace, []);
  assert.ok(rows.h7.status_reason.includes("overall_level EXTREME"));
  assert.deepEqual(rows.h12.unfamiliar_levels, ["dimensions.hiring.level=EXTREME"]);
  assert.equal(rows.h12.status, "no_segment_matched");
});

test("low observability is surfaced", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.ok(rows.h11.status_reason.includes("observability for this area is LOW"));
});

test("evidence summary separates qualifying and truncated", async () => {
  const [result, transport] = await run("evidence");
  assert.deepEqual(transport.sent[0].query, { withEvidence: "true" });
  assert.deepEqual(result.rows[0].evidence_summary.workforce,
    { item_count: 40, items_returned: 2, qualifying: 1, not_qualifying: 1, truncated: true, as_of: "2026-11-05" });
});

test("decisions are asked only when needed", async () => {
  const [result, transport] = await run("decisions");
  const rows = byId(result);
  assert.equal(rows.d1.segment_id, "b2b_ai_leaders");
  assert.deepEqual([rows.d2.status, rows.d2.segment_id], ["needs_review", "manual_review"]);
  assert.deepEqual(rows.d3.decisions, []);
  assert.deepEqual([rows.d4.status, rows.d4.segment_id], ["incomplete", "manual_review"]);
  assert.equal(transport.sent.filter((r) => r.path === "/decisions" && r.method === "POST").length, 3);
});

test("bad tool stops before paid calls", async () => {
  const [result, transport] = await run("bad-tool");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("errors are incomplete and 402 stops the run", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  for (const id of ["e1", "e2", "e3", "e4", "e5", "e6"]) assert.deepEqual([rows[id].status, rows[id].segment_id], ["incomplete", "review"]);
  assert.deepEqual(result.errors.map((e: Json) => e.kind),
    ["server_error", "timeout", "invalid_response", "answer_error", "forbidden", "insufficient_credits"]);
  assert.equal(rows.e7.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 7);
});

test("budget stops before Tech Intel", async () => {
  const [result, transport] = await run("budget", { maxCredits: 1.5 });
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(byId(result)["row-2"].status, "not_processed");
  assert.equal(transport.sent.length, 1);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>Hotel</b>") && html.includes("&lt;b&gt;Hotel&lt;/b&gt;"));
  assert.ok(!html.includes("<script>alert"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique valid domains", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const calls = Object.fromEntries(plan(RECIPE.readInput(join(F, "happy.input.csv")), cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["POST /ai-adoption"].requests, 10);
  assert.equal(calls["GET /tech-intel"].requests, 10);
  assert.ok(!("POST /decisions" in calls));
  const dcfg = loadConfig(JSON.parse(readFileSync(join(F, "decisions.config.json"), "utf-8")));
  const dcalls = Object.fromEntries(plan(RECIPE.readInput(join(F, "decisions.input.csv")), dcfg).map((c) => [c.endpoint, c]));
  assert.equal(dcalls["POST /decisions"].requests, 4);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
  [(c) => { c.segments = []; }, "segments must be a non-empty list"],
  [(c) => { c.levels = ["NONE", "NONE"]; }, "distinct level names"],
  [(c) => { c.segments[0].conditions[0].at_least = "HUGE"; }, "not in levels: HUGE"],
  [(c) => { c.segments[0].conditions[0].at_most = "BASIC"; }, "exactly one of at_least, at_most or in"],
  [(c) => { c.segments[0].conditions.push({ type: "vibes" }); }, "type must be one of"],
  [(c) => { c.segments[0].conditions.push({ type: "technology", min_confidence: "high" }); }, "needs at least one of"],
  [(c) => { c.segments[0].conditions.push({ type: "decision", check: "b2b" }); }, "must name an id from decision_checks"],
  [(c) => { c.segments[1].id = "ai_leaders"; }, "used twice"],
  [(c) => { c.segments[1].id = "review"; }, "clashes with the review"],
  [(c) => { c.segments[0].match = "most"; }, "match must be all or any"],
  [(c) => { c.decision_checks = [{ id: "b2b", tool: "is_b2b" }]; }, "is not used by any segment"],
  [(c) => { c.tech_intel_max_pages = 0; }, "tech_intel_max_pages"],
  [(c) => { c.with_evidence = "yes"; }, "with_evidence must be true or false"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 52);
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
