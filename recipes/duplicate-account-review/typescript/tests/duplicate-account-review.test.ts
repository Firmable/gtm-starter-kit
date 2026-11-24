import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, InputError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("duplicate-account-review");
const tmp = () => mkdtempSync(join(tmpdir(), "dar-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));

test("outcomes follow the rules", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.p1.status, "likely_duplicate");
  assert.equal(rows.p1.left_row_id, "crm-001");
  assert.equal(rows.p1.right_row_id, "crm-101");
  assert.equal(rows.p2.status, "distinct");
  assert.equal(rows.p3.status, "likely_duplicate");
  assert.equal(rows.p3.decision, null);
  assert.equal(rows.p6.status, "needs_review");
  assert.equal(rows.p6.decision.answer, "unknown");
  assert.equal(rows.p7.status, "needs_review");
  assert.equal(rows.p7.left.found, false);
  assert.equal(rows.p7.right.found, true);
  assert.deepEqual(transport.unused(), []);
});

test("identifiers explain support and conflict", async () => {
  const rows = byId((await run("happy"))[0]);
  const abn = rows.p1.identifiers.find((i: Json) => i.identifier === "abn");
  assert.deepEqual(abn, { identifier: "abn", left: "11 000 000 001", right: "11000000001", comparison: "same" });
  assert.deepEqual(rows.p1.supporting_identifiers, ["abn", "linkedin", "hq_country"]);
  assert.ok(rows.p1.missing_identifiers.includes("acn"));
  assert.equal(rows.p1.decision.details.matched, "abn");
  assert.equal(rows.p1.decision.reasoning.reason, "The two records share an ABN.");
});

test("contradictions go to review", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.equal(rows.p4.status, "needs_review");
  assert.deepEqual(rows.p4.contradictions, ["abn differs"]);
  assert.equal(rows.p5.status, "needs_review");
  assert.deepEqual(rows.p5.contradictions, ["linkedin is the same"]);
  assert.equal(rows.p10.status, "needs_review");
  assert.equal(rows.p10.decision, null);
  assert.ok(rows.p10.notes[0].includes("beta-old.example differs"));
});

test("invalid sides and duplicate pairs", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.p9.status, "invalid_input");
  assert.ok(rows.p11.status_reason.includes("f followed by 12 digits"));
  assert.equal(rows.p8.duplicate_of, "p2");
  assert.equal(rows.p8.decision.reused_from_row, "p2");
  assert.equal(transport.sent.filter((r) => r.path === "/decisions" && r.method === "POST").length, 5);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 13);
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.status, "incomplete");
  assert.equal(rows.e1.decision.error.status, 502);
  assert.equal(rows.e2.decision.error.kind, "timeout");
  assert.equal(rows.e3.status, "error");
  assert.equal(rows.e4.decision.error.kind, "invalid_response");
  assert.equal(rows.e5.status, "incomplete");
  assert.equal(rows.e6.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 4);
  assert.equal(transport.sent.length, 13);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 3.5 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "distinct");
  assert.equal(rows["row-2"].status, "error");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(rows["row-3"].status, "not_processed");
  assert.equal(transport.sent.length, 4);
});

test("missing tool stops before paid calls", async () => {
  const [result, transport] = await run("no-tool");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("without reasoning", async () => {
  const [result, transport] = await run("no-reasoning", { config: "no-reasoning.config.json" });
  assert.equal(result.rows[0].status, "likely_duplicate");
  assert.equal(result.rows[0].decision.reasoning, null);
  assert.deepEqual(transport.sent[transport.sent.length - 1].query, {});
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>Zeta</b>") && html.includes("&lt;b&gt;Zeta&lt;/b&gt;"));
  assert.ok(readFileSync(join(out, "results.csv"), "utf-8").includes("'=SUM(1)"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique sides and pairs", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 13);
  assert.equal(calls["POST /decisions"].requests, 8);
});

test("input needs both sides", () => {
  const d = tmp();
  writeFileSync(join(d, "pairs.csv"), "left_domain,right_domain\nacme.example,\n");
  assert.throws(() => RECIPE.readInput(join(d, "pairs.csv")),
    (e: Error) => e instanceof InputError && e.message.includes("needs one of right_domain, right_company_id"));
});

const bad: [Json, string][] = [
  [{ with_reasoning: "yes" }, "with_reasoning must be true or false"],
  [{ colour: "orange" }, "Unknown configuration key"],
];
for (const [raw, message] of bad) {
  test(`invalid config: ${message}`, () => {
    assert.throws(() => loadConfig(raw), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 13.64);
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
