import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("territory-routing");
const tmp = () => mkdtempSync(join(tmpdir(), "tr-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));

test("first match routes in rule order", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h1.status, "assigned");
  assert.equal(rows.h1.team_id, "enterprise");
  assert.equal(rows.h1.owner, "Enterprise queue");
  assert.equal(rows.h2.team_id, "commercial");
  assert.equal(rows.h3.team_id, "nz");
  assert.equal(rows.h4.team_id, "public_sector");
  assert.deepEqual(rows.h4.rules.map((r: Json) => r.outcome), ["match", "not_evaluated", "not_evaluated", "not_evaluated"]);
  assert.equal(rows.h10.status, "unassigned");
  assert.deepEqual(transport.unused(), []);
});

test("unknown goes to review, never to a team", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.equal(rows.h5.status, "review");
  assert.equal(rows.h5.team_id, null);
  assert.equal(rows.h5.decisions[0].answer, "unknown");
  assert.deepEqual(rows.h5.candidate_teams, ["public_sector"]);
  const nz = rows.h6.rules[1];
  assert.equal(nz.outcome, "unknown");
  assert.equal(nz.conditions[0].result, "unknown");
  assert.equal(nz.conditions[0].observed, null);
});

test("invalid, missing and duplicate rows", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h8.status, "invalid_input");
  assert.ok(rows.h13.status_reason.includes("f followed by 12 digits"));
  assert.equal(rows.h9.status, "company_not_found");
  assert.equal(rows.h7.duplicate_of, "h1");
  assert.equal(rows.h7.team_id, "enterprise");
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 10);
  assert.equal(transport.sent.filter((r) => r.path === "/decisions" && r.method === "POST").length, 9);
  assert.equal(result.ledger.filter((e: Json) => e.outcome === "cache_hit").length, 2);
});

test("data conflicts are surfaced, not corrected", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.equal(rows.h11.status, "review");
  assert.deepEqual(rows.h11.matched_rules, ["au_commercial"]);
  assert.ok(rows.h11.data_conflicts[0].includes("does not correct"));
  assert.equal(rows.h12.status, "review");
  assert.deepEqual(rows.h12.lookup, { by: "id", value: "f900000000009" });
  assert.ok(rows.h12.status_reason.includes("other.example differs"));
});

test("review overlap mode", async () => {
  const [result, transport] = await run("review", { config: "review.config.json" });
  const rows = byId(result);
  assert.equal(rows.o1.status, "review");
  assert.deepEqual(rows.o1.candidate_teams, ["enterprise", "finance"]);
  assert.deepEqual(rows.o2.matched_rules, ["big", "big_au"]);
  assert.equal(rows.o2.status, "assigned");
  assert.equal(rows.o3.status, "assigned_default");
  assert.deepEqual(rows.o4.candidate_teams, ["finance", "enterprise"]);
  assert.equal(rows.o5.review_reasons.length, 2);
  assert.equal(rows.o6.status, "assigned");
  assert.equal(rows.o7.team_id, "vic");
  assert.deepEqual(result.summary.review_queue, ["o1", "o4", "o5"]);
  assert.deepEqual([...new Set(transport.sent.map((r) => r.path))], ["/company"]);
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.status, "error");
  assert.equal(rows.e1.error.status, 500);
  assert.equal(rows.e2.decisions[0].error.status, 502);
  assert.equal(rows.e3.decisions[0].error.kind, "timeout");
  assert.equal(rows.e4.decisions[0].error.kind, "invalid_response");
  assert.equal(rows.e5.status, "incomplete");
  assert.equal(rows.e6.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 4);
  assert.equal(transport.sent.length, 10);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 2.5 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "assigned");
  assert.equal(rows["row-2"].status, "error");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(rows["row-3"].status, "not_processed");
  assert.equal(transport.sent.length, 3);
});

test("bad tool stops before paid calls", async () => {
  const [result, transport] = await run("bad-tool", { config: "bad-tool.config.json" });
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>Theta</b>") && html.includes("&lt;b&gt;Theta&lt;/b&gt;"));
  assert.ok(!html.includes("<script>alert"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique valid identifiers", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 10);
  assert.equal(calls["POST /decisions"].requests, 10);
  const review = loadConfig(JSON.parse(readFileSync(join(F, "review.config.json"), "utf-8")));
  assert.deepEqual(plan(rows, review).map((c) => c.endpoint), ["GET /company"]);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.teams = []; }, "teams must be a non-empty list"],
  [(c) => { c.rules[0].team = "nobody"; }, "team must be one of the team ids"],
  [(c) => { c.rules[1].id = "public_sector"; }, "used twice"],
  [(c) => { c.rules[1].when = {}; }, "at least one condition"],
  [(c) => { c.rules[1].when.colour = ["red"]; }, "unknown key"],
  [(c) => { c.rules[2].when.employee_count_max = 10; }, "more than employee_count_max"],
  [(c) => { c.rules[0].when.decisions = { nope: "yes" }; }, "not a decision_checks id"],
  [(c) => { c.rules[0].when.decisions = { public_sector: "maybe" }; }, "must be yes or no"],
  [(c) => { c.rules.shift(); }, "is not used by any rule"],
  [(c) => { c.overlap = "random"; }, "overlap must be"],
  [(c) => { c.default_team = "nobody"; }, "default_team must be"],
  [(c) => { c.teams[0].id = "1team"; }, "must start with a lower-case letter"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 10.8);
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
