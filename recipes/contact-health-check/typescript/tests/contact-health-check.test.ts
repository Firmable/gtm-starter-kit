import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("contact-health-check");
const tmp = () => mkdtempSync(join(tmpdir(), "chc-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const checks = (row: Json): Record<string, Json> => Object.fromEntries(row.checks.map((c: Json) => [c.check_id, c]));

test("happy path statuses follow the policy", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h1.status, "healthy");
  assert.deepEqual(rows.h1.channels, ["email", "mobile"]);
  assert.equal(rows.h2.status, "stale");
  assert.equal(rows.h3.status, "needs_review");
  assert.deepEqual(rows.h3.missing_information, ["Still in the expected role", "Mobile on the Australian Do Not Call register"]);
  assert.equal(rows.h4.duplicate_of, "h1");
  assert.equal(rows.h5.status, "reach_unconfirmed");
  assert.equal(rows.h5.phone_channel, "held_on_dnc");
  assert.equal(rows.h6.status, "invalid_input");
  assert.equal(rows.h7.status, "needs_review");
  assert.ok(rows.h8.status_reason.includes("No employment or role check"));
  assert.equal(rows.h10.status, "stale");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 2);
  assert.equal(transport.sent.length, 34);
});

test("unknown is distinct from no and from not checked", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.equal(checks(rows.h3).role.answer, "unknown");
  assert.equal(checks(rows.h3).role.result, "unknown");
  assert.equal(checks(rows.h7).employment.result, "not_checked");
  assert.equal(checks(rows.h7).employment.answer, null);
  assert.ok(rows.h7.status_reason.includes("no company profile"));
  assert.equal(checks(rows.h8).employment.result, "not_supplied");
});

test("role check sends the title and the resolved company", async () => {
  const [, transport] = await run("happy");
  const bodies = transport.sent.filter((r) => r.path === "/decisions" && r.method === "POST").map((r) => r.jsonBody as Json);
  const roles = bodies.filter((b) => b.tool === "is_contact_current").map((b) => b.question);
  assert.ok(roles.some((q) => JSON.stringify(q) === JSON.stringify({ person_id: "fp900000000007", title: "Office Manager" })));
  const works = bodies.filter((b) => b.tool === "works_at_company").map((b) => b.question);
  assert.ok(works.some((q) => q.work_email === "jordan.example@beta.example" && q.company_id === "f900000000002"));
});

test("flags show API facts without correcting them", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.ok(rows.h2.flags[0].includes("Grade returned: Unsure."));
  assert.ok(rows.h5.flags[0].includes("as at 2026-10-15"));
  assert.ok(rows.h10.flags[0].startsWith("works_at_company answered yes and is_contact_current answered no"));
  assert.equal(checks(rows.h10).role.details.current_title, "Chief Data Officer");
});

test("last refresh information is kept", async () => {
  const rows = byId((await run("happy"))[0]);
  const email = checks(rows.h1).email;
  assert.equal(email.as_at, "2026-10-30");
  assert.equal(email.last_refreshed, "2026-11-01");
  assert.ok(rows.h1.last_refresh.includes("Work email likely to deliver: as at 2026-10-30, last refreshed 2026-11-01"));
});

test("model tools are named in coverage", async () => {
  const [result] = await run("happy");
  assert.ok(result.coverage.some((c: string) => c.includes("is_contact_current") && c.includes("decision model")));
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  const e1 = checks(rows.e1);
  assert.equal(e1.employment.error.status, 502);
  assert.equal(e1.role.error.kind, "timeout");
  assert.equal(rows.e1.status, "incomplete");
  assert.equal(checks(rows.e2).email.error.kind, "invalid_response");
  assert.equal(rows.e2.status, "healthy");
  assert.equal(checks(rows.e3).email.result, "not_asked");
  assert.equal(rows.e4.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.ok(result.coverage[0].startsWith("The run stopped early"));
  assert.equal(transport.sent.length, 11);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 1.35 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "healthy");
  assert.equal(rows["row-2"].status, "incomplete");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 5);
});

test("fetch person uses the person ID and handles a missing profile", async () => {
  const [result, transport] = await run("fetch-person", { config: "fetch-person.config.json" });
  const rows = byId(result);
  assert.equal(rows.f1.status, "healthy");
  assert.equal(rows.f1.person.id, "fp900000000001");
  assert.equal(rows.f2.status, "person_not_found");
  assert.deepEqual(transport.unused(), []);
});

test("reachability off gives current", async () => {
  const [result] = await run("current-only", { config: "current-only.config.json" });
  assert.equal(result.rows[0].status, "current");
  assert.deepEqual(result.rows[0].checks.map((c: Json) => c.check_id), ["employment", "role"]);
});

test("a missing tool stops before paid calls", async () => {
  const [result, transport] = await run("bad-tool");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<script>alert") && !html.includes("<b>Data</b>") && html.includes("&lt;b&gt;Data&lt;/b&gt;"));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts unique questions", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 2);
  assert.equal(calls["GET /people"], undefined);
  assert.equal(calls["POST /decisions"].requests, 32);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.checks = []; }, "checks must be a non-empty list"],
  [(c) => { c.checks = ["email", "phone"]; }, "checks[2] must be one of"],
  [(c) => { c.checks = ["email", "email"]; }, "'email' is listed twice"],
  [(c) => { c.fetch_person = "yes"; }, "fetch_person must be true or false"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 4.56);
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await cliMain(RECIPE, [...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects rows without an identifier", async () => {
  const d = tmp();
  writeFileSync(join(d, "in.csv"), "name,expected_title\nAlex Example,Manager\n");
  assert.equal(await cliMain(RECIPE, ["--input", join(d, "in.csv"), "--config", join(F, "config.json"), "--out", d]), 2);
});
