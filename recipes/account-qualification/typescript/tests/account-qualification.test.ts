import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("account-qualification");
const tmp = () => mkdtempSync(join(tmpdir(), "aq-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));

test("happy path statuses follow the policy", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.r1.status, "qualified");
  assert.equal(rows.r2.status, "disqualified");
  assert.equal(rows.r3.status, "needs_review");
  assert.deepEqual(rows.r3.missing_information, ["Head office in Australia"]);
  assert.equal(rows.r4.duplicate_of, "r1");
  assert.equal(rows.r5.status, "invalid_input");
  assert.equal(rows.r6.status, "company_not_found");
  assert.equal(rows.r7.status, "not_qualified");
  assert.ok(rows.r8.status_reason.includes("1 optional check(s) answered unknown"));
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 6);
});

test("reasoning and answer metadata are preserved", async () => {
  const [result] = await run("happy");
  const check = byId(result).r2.checks[0];
  assert.equal(check.result, "fail");
  assert.equal(check.reasoning.reason, "Sells to consumers.");
  assert.equal(check.credits, 0.08);
});

test("unknown is distinct from no", async () => {
  const [result] = await run("happy");
  const c = byId(result).r3.checks[1];
  assert.equal(c.answer, "unknown");
  assert.equal(c.result, "unknown");
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  const e1 = Object.fromEntries(rows.e1.checks.map((c: Json) => [c.check_id, c]));
  assert.equal(e1.au_head_office.error.status, 502);
  assert.equal(e1.size_200_plus.error.kind, "timeout");
  assert.equal(rows.e1.status, "incomplete");
  assert.equal(rows.e2.checks[0].error.kind, "invalid_response");
  assert.equal(rows.e3.checks[1].result, "not_asked");
  assert.equal(rows.e4.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 13);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 2.1 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "incomplete");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
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
  assert.ok(!html.includes("<b>Eps</b>") && html.includes("&lt;b&gt;Eps&lt;/b&gt;"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique valid domains", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 6);
  assert.equal(calls["POST /decisions"].requests, 24);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.checks = []; }, "checks must be a non-empty list"],
  [(c) => { c.checks[0].want = "maybe"; }, "want must be yes or no"],
  [(c) => { c.checks[1].id = "b2b"; }, "used twice"],
  [(c) => { c.policy = { min_optional_passes: 5 }; }, "only 2 optional"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 7.92);
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
