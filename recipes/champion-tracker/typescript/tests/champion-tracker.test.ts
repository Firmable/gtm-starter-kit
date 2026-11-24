import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, fillTemplates, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("champion-tracker");
const tmp = () => mkdtempSync(join(tmpdir(), "ct-"));

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
  assert.equal(rows.a1.status, "champions_found");
  assert.equal(rows.a1.champion_count, 1);
  const a1 = people(rows.a1);
  assert.equal(a1.fp900000000001.status, "champion");
  assert.equal(a1.fp900000000002.status, "not_alumni");
  assert.equal(a1.fp900000000003.status, "unknown");
  assert.equal(rows.a2.status, "no_people_found");
  assert.equal(rows.a3.duplicate_of, "a1");
  assert.equal(rows.a4.status, "invalid_input");
  assert.equal(rows.a5.status, "company_not_found");
  assert.equal(rows.a8.status, "no_champions_found");
  assert.equal(rows.a9.status, "needs_review");
  assert.equal(people(rows.a9).fp900000000010.status, "person_not_found");
  assert.equal(rows.a10.status, "no_people_found");
  assert.equal(result.summary.unique_champions, 3);
  assert.equal(result.summary.people_checked, 10);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.length, 29);
});

test("the question sends past companies and include_current", async () => {
  const [, transport] = await run("happy");
  const q = transport.sent.find((r) => r.path === "/decisions" && r.method === "POST")!.jsonBody as Json;
  assert.equal(q.tool, "previously_worked_at");
  assert.deepEqual(q.question, { person_id: "fp900000000001", companies: ["Delta Example", "f900000000050"], include_current: false });
  const lookups = transport.sent.filter((r) => r.path === "/company").map((r) => r.query);
  assert.ok(lookups.some((x) => x.id === "f900000000002"));
  assert.ok(lookups.some((x) => x.fqdn === "acme.example"));
});

test("details are kept as returned", async () => {
  const rows = byId((await run("happy"))[0]);
  assert.deepEqual(people(rows.a1).fp900000000001.alumni,
    { employer: "Delta Example", years_there: 3, title: "Operations Lead", past_employers_checked: 4 });
  assert.equal(people(rows.a7).fp900000000006.alumni.years_there, 2.5);
});

test("people not checked are counted", async () => {
  const rows = byId((await run("happy"))[0]);
  const s = rows.a1.search;
  assert.equal(s.total, 6);
  assert.equal(s.not_checked, 2);
  assert.equal(s.skipped[0].id, "f900000000099");
  assert.ok(rows.a1.status_reason.includes("2 more matching person(s) were not checked"));
  assert.ok(rows.a8.status_reason.includes("No champion was observed among the 2 person(s) checked"));
});

test("contact details only for champions", async () => {
  const [result, transport] = await run("happy");
  const fetched = transport.sent.filter((r) => r.path === "/people").map((r) => r.query.id).sort();
  assert.deepEqual(fetched, ["fp900000000001", "fp900000000004", "fp900000000006"]);
  const rows = byId(result);
  assert.equal(people(rows.a1).fp900000000002.contact, null);
  assert.deepEqual(people(rows.a7).fp900000000006.contact.phones, [{ value: "+61 400 000 001", is_dnd: true }]);
  assert.ok(people(rows.a6).fp900000000004.contact_note.includes("no person profile"));
});

test("openers are filled from facts or skipped", async () => {
  const rows = byId((await run("happy"))[0]);
  const alex = people(rows.a1).fp900000000001;
  assert.equal(alex.suggested_openers[0].text,
    "Hi Alex, I noticed you spent time at Delta Example. Would you introduce us to the team at Acme key account?");
  const dana = people(rows.a6).fp900000000004;
  assert.deepEqual(dana.suggested_openers.map((o: Json) => o.template), [3]);
  assert.deepEqual(dana.skipped_openers, [{ template: 1, missing: ["first_name", "past_employer"] },
    { template: 2, missing: ["past_employer"] }]);
  assert.deepEqual(people(rows.a1).fp900000000002.suggested_openers, []);
});

test("conflicts are surfaced, not corrected", async () => {
  const rows = byId((await run("happy"))[0]);
  const frankie = people(rows.a7).fp900000000006;
  assert.equal(frankie.status, "champion");
  assert.ok(frankie.notes[0].startsWith("previously_worked_at returned the account itself (Delta Example)"));
  assert.ok(frankie.notes[1].includes("Delta Example Pty Ltd"));
  assert.ok(people(rows.a6).fp900000000004.notes[0].includes("returned no past employer"));
  assert.ok(people(rows.a6).fp900000000005.notes[0].includes("answered no but returned a past employer"));
});

test("include_current is passed through", async () => {
  const [result, transport] = await run("include-current", { config: "include-current.config.json" });
  const row = result.rows[0];
  assert.equal(row.status, "champions_found");
  assert.deepEqual(row.people[0].notes, []);
  const last = transport.sent[transport.sent.length - 1];
  assert.equal((last.jsonBody as Json).question.include_current, true);
  assert.deepEqual(last.query, {});
});

test("coverage states the limits", async () => {
  const [result] = await run("happy");
  const cov = result.coverage.join(" ");
  assert.ok(cov.includes("no past-employer filter") && cov.includes("does not show buying intent"));
  assert.ok(cov.includes("names Firmable holds") && cov.includes("not observed in retrieved data"));
  assert.ok(cov.includes("success false"));
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.x1.status, "error");
  const x2 = people(rows.x2);
  assert.equal(x2.fp900000000020.error.status, 500);
  assert.equal(x2.fp900000000021.error.kind, "timeout");
  assert.equal(x2.fp900000000022.status, "error");
  assert.equal(rows.x2.status, "incomplete");
  assert.equal(rows.x3.status, "champions_found");
  assert.ok(rows.x3.people[0].contact_note.includes("could not be retrieved"));
  assert.equal(people(rows.x4).fp900000000025.status, "not_asked");
  assert.equal(rows.x5.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 5);
  assert.equal(transport.sent.length, 14);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { maxCredits: 2.5 });
  const rows = byId(result);
  assert.equal(rows.b1.status, "champions_found");
  assert.equal(people(rows.b1).fp900000000027.status, "not_asked");
  assert.ok(people(rows.b1).fp900000000026.contact_note.includes("Stopped before calling GET /people"));
  assert.equal(rows.b2.status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 4);
});

test("a changed tool stops before paid calls", async () => {
  const [result, transport] = await run("bad-tool");
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  assert.ok(result.errors.some((e: Json) => e.message.includes("include_current")));
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("outputs are safe", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<script>alert") && !html.includes("<b>Alex</b>") && html.includes("&lt;b&gt;Alex&lt;/b&gt;"));
  assert.ok(html.includes('class="template"'));
  const csv = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  assert.deepEqual(csv.slice(1, 6).map((r) => r[0]), ["a1", "a1", "a1", "a2", "a3"]);
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("fillTemplates fills or skips", () => {
  const [filled, skipped] = fillTemplates(["{title} {title}", "{first_name}!"], { title: "CTO", first_name: null });
  assert.deepEqual(filled, [{ template: 1, text: "CTO CTO" }]);
  assert.deepEqual(skipped, [{ template: 2, missing: ["first_name"] }]);
});

test("plan counts upper bounds", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 8);
  assert.equal(calls["POST /people/search"].requests, 8);
  assert.equal(calls["POST /decisions"].requests, 32);
  assert.equal(calls["GET /people"].requests, 32);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { delete c.past_companies; }, "past_companies is required"],
  [(c) => { c.past_companies = []; }, "past_companies must be a list of 1 to 50"],
  [(c) => { c.past_companies = Array(51).fill("x"); }, "past_companies must be a list of 1 to 50"],
  [(c) => { c.past_companies = ["Delta", " "]; }, "past_companies[2] must be text"],
  [(c) => { c.past_companies = ["Delta", "delta"]; }, "past_companies[2] 'delta' is listed twice"],
  [(c) => { c.include_current = "yes"; }, "include_current must be true or false"],
  [(c) => { c.opener_templates = "Hi"; }, "opener_templates must be a list"],
  [(c) => { c.opener_templates = ["Hi {name}"]; }, "unknown placeholder(s): {name}"],
  [(c) => { c.search.seniority = 4; }, "search.seniority must be one seniority id"],
  [(c) => { c.search.max_people = 500; }, "search.max_people"],
  [(c) => { c.search.selected_country = "GB"; }, "search.selected_country"],
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

test("defaults", () => {
  const cfg = loadConfig({ past_companies: ["Delta"] });
  assert.equal(cfg.include_current, false);
  assert.equal(cfg.with_reasoning, false);
  assert.equal(cfg.fetch_contact_details, false);
  assert.deepEqual(cfg.opener_templates, []);
  assert.deepEqual(cfg.search, { selected_country: "AU", position: null, seniority: null, department: null, max_people: 25 });
});

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 42.56);
});

test("CLI live needs a limit and a key", async () => {
  const out = tmp();
  const args = ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out];
  assert.equal(await cliMain(RECIPE, [...args, "--live"]), 2);
  delete process.env.FIRMABLE_API_KEY;
  assert.equal(await cliMain(RECIPE, [...args, "--live", "--max-credits", "5"]), 2);
});

test("CLI rejects rows without an account", async () => {
  const d = tmp();
  writeFileSync(join(d, "in.csv"), "name\nAcme\n");
  assert.equal(await cliMain(RECIPE, ["--input", join(d, "in.csv"), "--config", join(F, "config.json"), "--out", d]), 2);
});
