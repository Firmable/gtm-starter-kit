import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("buying-committee-map");
const tmp = () => mkdtempSync(join(tmpdir(), "bcm-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string; ext?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.${o.ext ?? "csv"}`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const people = (row: Json): Record<string, Json> => Object.fromEntries(row.people.map((p: Json) => [p.person_id, p]));
const roles = (row: Json): Record<string, Json> => Object.fromEntries(row.roles.map((x: Json) => [x.role_id, x]));
const posts = (sent: { path: string; method: string; jsonBody: unknown }[]) =>
  sent.filter((r) => r.path === "/decisions" && r.method === "POST").map((r) => r.jsonBody as Json);

test("happy path account statuses", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h1.status, "committee_complete");
  assert.equal(rows.h2.status, "needs_review");
  assert.equal(rows.h3.status, "company_not_found");
  assert.equal(rows.h4.duplicate_of, "h1");
  assert.equal(rows.h4.status, "committee_complete");
  assert.equal(rows.h5.status, "invalid_input");
  assert.equal(rows.h6.status, "no_people_found");
  assert.equal(rows.h7.status, "gaps_found");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 4);
});

test("roles are assigned from returned details", async () => {
  const [result] = await run("happy");
  const r = roles(byId(result).h1);
  assert.equal(r.economic_buyer.status, "filled");
  assert.equal(r.economic_buyer.filled_by[0].name, "Alex Example");
  assert.equal(r.finance_lead.filled_by[0].name, "Bo Example");
  assert.equal(r.it_owner.status, "possible");
  const p = people(byId(result).h1);
  assert.deepEqual(p.fp900000000001.roles_filled, ["economic_buyer"]);
  assert.equal(p.fp900000000003.status, "unclassified");
  assert.ok(p.fp900000000003.reason.includes("matches_seniority answered unknown"));
});

test("union of role values is asked once per person", async () => {
  const [, transport] = await run("happy");
  const qs = posts(transport.sent);
  const dep = qs.filter((q) => q.tool === "matches_department").map((q) => q.question);
  const sen = qs.filter((q) => q.tool === "matches_seniority").map((q) => q.question);
  assert.deepEqual(dep[0], { person_id: "fp900000000001", departments: ["Finance", "Engineering & Technical"] });
  assert.deepEqual(sen[0], { person_id: "fp900000000001", seniorities: ["C-Suite / Partner", "VP/Director / Head of", "Manager"] });
  assert.equal(dep.filter((q) => q.person_id === "fp900000000006").length, 1);
});

test("yes without a value is unclassified", async () => {
  const [result] = await run("happy");
  const p = people(byId(result).h2);
  assert.equal(p.fp900000000004.status, "unclassified");
  assert.deepEqual(p.fp900000000004.roles_possible, ["economic_buyer", "finance_lead"]);
  assert.ok(p.fp900000000004.reason.includes("returned no seniority value"));
  assert.equal(p.fp900000000005.status, "no_role");
});

test("conflicting answers are surfaced, not corrected", async () => {
  const [result] = await run("happy");
  const p = people(byId(result).h7);
  assert.ok(p.fp900000000007.notes[0].includes("returned department 'Accounting', which is not a configured department"));
  assert.equal(p.fp900000000007.status, "no_role");
  assert.ok(p.fp900000000008.notes[0].includes("answered no but returned department 'Finance'"));
});

test("person not found is not an error", async () => {
  const [result, transport] = await run("happy");
  const ivy = people(byId(result).h7).fp900000000009;
  assert.equal(ivy.status, "person_not_found");
  assert.equal(ivy.seniority_check.result, "not_asked");
  assert.deepEqual(result.errors, []);
  assert.ok(!posts(transport.sent).some((q) => q.tool === "matches_seniority" && q.question.person_id === "fp900000000009"));
  assert.ok(byId(result).h7.status_reason.includes("1 person(s) were not found by Decisions"));
});

test("decision maker only for people who fill a role", async () => {
  const [result, transport] = await run("happy");
  const asked = posts(transport.sent).filter((q) => q.tool === "is_decision_maker").map((q) => q.question.person_id);
  assert.deepEqual(asked, ["fp900000000001", "fp900000000002", "fp900000000006"]);
  const p = people(byId(result).h1);
  assert.equal(p.fp900000000001.decision_maker.answer, "yes");
  assert.equal(p.fp900000000002.decision_maker.result, "unknown");
  assert.equal(p.fp900000000003.decision_maker, null);
  assert.ok(result.coverage.some((c: string) => c.includes("is_decision_maker, matches_department, matches_seniority")));
});

test("search coverage", async () => {
  const [result, transport] = await run("happy");
  const h1 = byId(result).h1;
  assert.equal(h1.search.total, 6);
  assert.equal(h1.search.not_checked, 2);
  assert.ok(h1.status_reason.includes("2 were not checked"));
  assert.ok(h1.status_reason.includes("1 search record(s) had no person ID"));
  const bodies = transport.sent.filter((r) => r.path === "/people/search").map((r) => r.jsonBody);
  assert.deepEqual(bodies[0], { companyId: "f900000000001", selectedCountry: "AU", from: "0", size: "5" });
  assert.equal(byId(result).h2.company_id_source, "input");
  assert.ok(result.coverage.some((c: string) => c.includes("success false")));
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors", { config: "errors.config.json" });
  const rows = byId(result);
  assert.equal(rows.e1.status, "error");
  assert.equal(rows.e1.error.status, 500);
  assert.equal(rows.e2.status, "incomplete");
  const p = people(rows.e2);
  assert.equal(p.fp900000000001.department_check.error.status, 502);
  assert.equal(p.fp900000000001.seniority_check.error.kind, "timeout");
  assert.equal(p.fp900000000002.department_check.result, "error");
  assert.deepEqual(p.fp900000000002.roles_undecided, ["economic_buyer", "it_owner"]);
  assert.equal(roles(rows.e2).economic_buyer.status, "incomplete");
  assert.equal(people(rows.e3).fp900000000003.seniority_check.result, "not_asked");
  assert.equal(rows.e4.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 12);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { config: "budget.config.json", maxCredits: 2.1 });
  const rows = byId(result);
  const p = people(rows["row-1"]);
  assert.equal(p.fp900000000001.status, "assigned");
  assert.equal(p.fp900000000002.status, "incomplete");
  assert.equal(rows["row-1"].status, "incomplete");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 5);
});

test("missing tool stops before any search", async () => {
  const [result, transport] = await run("bad-tool", { config: "bad-tool.config.json" });
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.ok(result.errors[0].message.includes("is_decision_maker"));
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("reasoning and search filters", async () => {
  const [result, transport] = await run("reasoning", { config: "reasoning.config.json", ext: "json" });
  const row = result.rows[0];
  assert.equal(row.status, "committee_complete");
  const search = transport.sent.find((r) => r.path === "/people/search")!.jsonBody as Json;
  assert.equal(search.department, "14");
  assert.equal(search.seniority, "3");
  assert.ok(transport.sent.filter((r) => r.path === "/decisions" && r.method === "POST")
    .every((r) => JSON.stringify(r.query) === JSON.stringify({ withReasoning: "true" })));
  assert.equal(row.people[0].decision_maker.reasoning.source, "Firmable person profile");
  const dmQ = posts(transport.sent).find((q) => q.tool === "is_decision_maker")!.question;
  assert.equal(dmQ.buyer, "CFO and finance team");
});

test("outputs are safe and flat", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>Pat</b>") && html.includes("&lt;b&gt;Pat&lt;/b&gt;"));
  assert.ok(html.includes("Roles by account"));
  const tbl = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  const header = tbl[0];
  assert.deepEqual(tbl.slice(1).map((r) => r[0]), ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]);
  assert.equal(tbl[1][header.indexOf("role_economic_buyer")], "Alex Example");
  assert.equal(tbl[2][header.indexOf("required_roles_possible")], "Economic buyer; Finance lead");
  assert.equal(tbl[7][header.indexOf("required_roles_not_observed")], "Finance lead");
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan upper bounds", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 4);
  assert.equal(calls["POST /people/search"].requests, 5);
  assert.equal(calls["POST /decisions"].requests, 75);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.roles = []; }, "roles must be a list of 1 to 10 roles"],
  [(c) => { c.roles[0].seniorities = ["Senior"]; }, "must use Firmable's seniority labels"],
  [(c) => { c.roles[0].seniorities = ["c-suite / partner"]; }, "must use Firmable's seniority labels"],
  [(c) => { c.roles[0].departments = [""]; }, "departments must hold non-empty text"],
  [(c) => { c.roles[0].departments = []; }, "departments must be a non-empty list"],
  [(c) => { c.roles[1].id = "economic_buyer"; }, "is used twice"],
  [(c) => { c.roles[0].colour = "orange"; }, "has unknown key"],
  [(c) => { for (const r of c.roles) r.required = false; }, "At least one role must be required"],
  [(c) => { c.roles[0].departments = Array.from({ length: 21 }, (_, n) => `Dept ${n}`); }, "more than 20 departments"],
  [(c) => { c.search = { selected_country: "GB" }; }, "search.selected_country must be one of"],
  [(c) => { c.search = { max_people: 500 }; }, "max_people must be a whole number from 1 to 100"],
  [(c) => { c.search = { seniority: "Senior" }; }, "search.seniority must be a seniority id"],
  [(c) => { c.decision_maker = { seller: "x" }; }, "decision_maker.seller must be text"],
  [(c) => { c.decision_maker = { seller: "Software", team: "x" }; }, "decision_maker has unknown key"],
  [(c) => { c.with_reasoning = "yes"; }, "with_reasoning must be true or false"],
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
];
for (const [mutate, message] of bad) {
  test(`invalid config: ${message}`, () => {
    const cfg = JSON.parse(readFileSync(join(F, "config.json"), "utf-8"));
    mutate(cfg);
    assert.throws(() => loadConfig(cfg), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("config defaults", () => {
  const cfg = loadConfig({ roles: [{ id: "cfo", departments: [" Finance "], seniorities: ["C-Suite / Partner"] }] });
  assert.deepEqual(cfg.roles[0], { id: "cfo", label: "cfo", departments: ["Finance"], seniorities: ["C-Suite / Partner"], required: true });
  assert.deepEqual(cfg.search, { selected_country: "AU", position: null, seniority: null, department: null, max_people: 25 });
  assert.equal(cfg.decision_maker, null);
  assert.equal(cfg.with_reasoning, false);
});

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 7);
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
