import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, monthsBefore, plan } from "../src/recipe.js";

const F = fixturesDir("new-role-outreach");
const tmp = () => mkdtempSync(join(tmpdir(), "nro-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const people = (row: Json): Record<string, Json> => Object.fromEntries(row.people.map((p: Json) => [p.person_id, p]));

test("happy path statuses follow the rules", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.a1.status, "eligible_contacts_found");
  const a1 = people(rows.a1);
  assert.equal(a1.fp900000000001.status, "eligible");
  assert.equal(a1.fp900000000002.status, "not_eligible");
  assert.equal(a1.fp900000000003.status, "needs_review");
  assert.equal(rows.a2.status, "no_people_found");
  assert.equal(rows.a3.duplicate_of, "a1");
  assert.equal(rows.c1.status, "eligible");
  assert.equal(rows.c2.status, "needs_review");
  assert.equal(rows.c3.status, "invalid_input");
  assert.equal(rows.a4.status, "invalid_input");
  assert.equal(rows.a5.status, "company_not_found");
  assert.equal(result.summary.unique_eligible_people, 4);
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.length, 30);
});

test("search limits and skipped records are reported", async () => {
  const rows = byId((await run("happy"))[0]);
  const s = rows.a1.search;
  assert.equal(s.total, 6);
  assert.equal(s.not_retrieved, 2);
  assert.equal(s.skipped[0].id, "f900000000099");
  assert.deepEqual(s.request, { companyId: "f900000000001", selectedCountry: "AU", from: "0", size: "4", seniority: "4" });
});

test("a no-match search counts as zero people", async () => {
  const [result] = await run("happy");
  const a2 = byId(result).a2;
  assert.equal(a2.search.no_match_response, true);
  assert.deepEqual(a2.people, []);
  assert.ok(result.coverage.some((c: string) => c.includes("success false")));
  assert.deepEqual(result.errors, []);
});

test("contact details only for eligible people", async () => {
  const [result, transport] = await run("happy");
  const fetched = transport.sent.filter((r) => r.path === "/people").map((r) => r.query.id).sort();
  assert.deepEqual(fetched, ["fp900000000001", "fp900000000010", "fp900000000011", "fp900000000012"]);
  const rows = byId(result);
  assert.equal(people(rows.a1).fp900000000002.contact, null);
  assert.deepEqual(people(rows.c1).fp900000000010.contact.phones, [{ value: "+61 400 000 010", is_dnd: true }]);
});

test("source dates and conflicts are kept as returned", async () => {
  const rows = byId((await run("happy"))[0]);
  const c1 = people(rows.c1).fp900000000010;
  assert.deepEqual(c1.triggers.map((t: Json) => t.started), ["2026-09", "2026-10", "2026-09"]);
  assert.ok(c1.conflicts[0].startsWith("Triggers that answered yes returned different start dates"));
  assert.ok(people(rows.c4).fp900000000011.conflicts[0].includes("before the window start 2026-05-24"));
  assert.equal(people(rows.c5).fp900000000012.conflicts.length, 1);
});

test("coverage names model tools and the intent limit", async () => {
  const [result] = await run("happy");
  assert.ok(result.coverage.some((c: string) => c.includes("is_new_exec") && c.includes("decision model")));
  assert.ok(result.coverage.some((c: string) => c.includes("does not show buying intent")));
});

test("executive only uses new_exec alone", async () => {
  const [result, transport] = await run("exec-only", { config: "exec-only.config.json" });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "not_eligible");
  assert.ok(rows["row-1"].status_reason.includes("executive_only counts only New executive"));
  assert.equal(rows["row-2"].status, "eligible");
  assert.equal(result.summary.window_start, "2026-02-28");
  assert.equal(transport.sent.filter((r) => r.path === "/people").length, 0);
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  const x1 = rows.x1.people[0].triggers;
  assert.equal(x1[0].error.status, 500);
  assert.equal(x1[1].error.kind, "timeout");
  assert.equal(rows.x2.status, "error");
  assert.ok(rows.x3.people[0].contact_note.includes("could not be retrieved"));
  assert.equal(rows.x4.people[0].triggers[2].result, "not_asked");
  assert.equal(rows.x5.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 4);
  assert.equal(transport.sent.length, 11);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 1.2 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "eligible");
  assert.equal(rows["row-1"].people[0].contact_note, "Not retrieved, because the run stopped.");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 3);
});

test("a changed tool stops before paid calls", async () => {
  const [result, transport] = await run("bad-tool");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.ok(result.errors.some((e: Json) => e.message.includes("count_internal_moves")));
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<script>alert") && !html.includes("<b>Alex</b>") && html.includes("&lt;b&gt;Alex&lt;/b&gt;"));
  const csv = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  assert.deepEqual(csv.slice(1, 6).map((r) => r[0]), ["a1", "a1", "a1", "a2", "a3"]);
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("monthsBefore clamps to the month end", () => {
  assert.equal(monthsBefore("2026-11-24", 6), "2026-05-24");
  assert.equal(monthsBefore("2026-03-31", 1), "2026-02-28");
  assert.equal(monthsBefore("2026-01-15", 13), "2024-12-15");
});

test("plan counts upper bounds", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 2);
  assert.equal(calls["POST /people/search"].requests, 3);
  assert.equal(calls["POST /decisions"].requests, 48);
  assert.equal(calls["GET /people"].requests, 16);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.window_months = 0; }, "window_months must be a whole number from 1 to 60"],
  [(c) => { c.window_months = 48; }, "36 or less when new_exec"],
  [(c) => { c.triggers = ["new_to_role", "promoted"]; }, "triggers[2] must be one of"],
  [(c) => { c.triggers = ["new_to_role"]; c.executive_only = true; }, "executive_only needs new_exec"],
  [(c) => { c.executive_seniorities = ["CEO"]; }, "executive_seniorities must be"],
  [(c) => { c.evaluation_date = "24/11/2026"; }, "evaluation_date must be a date"],
  [(c) => { c.search.seniority = 4; }, "search.seniority must be one seniority id"],
  [(c) => { c.search.max_people = 500; }, "search.max_people"],
  [(c) => { c.search.name = "Alex"; }, "Unknown search key"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 21.84);
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await cliMain(RECIPE, [...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects rows without an account or contact", async () => {
  const d = tmp();
  writeFileSync(join(d, "in.csv"), "name\nAcme\n");
  assert.equal(await cliMain(RECIPE, ["--input", join(d, "in.csv"), "--config", join(F, "config.json"), "--out", d]), 2);
});
