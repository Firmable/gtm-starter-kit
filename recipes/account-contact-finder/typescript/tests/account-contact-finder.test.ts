import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json, parseCsv } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("account-contact-finder");
const tmp = () => mkdtempSync(join(tmpdir(), "acf-"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string; ext?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.${o.ext ?? "csv"}`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const cands = (row: Json): Record<string, Json> => Object.fromEntries(row.candidates.map((c: Json) => [c.person_id, c]));

test("happy path account statuses", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.r1.status, "contacts_selected");
  assert.equal(rows.r2.status, "no_contacts_selected");
  assert.equal(rows.r3.status, "company_not_found");
  assert.equal(rows.r4.duplicate_of, "r1");
  assert.equal(rows.r5.status, "invalid_input");
  assert.equal(rows.r6.status, "no_candidates");
  assert.equal(rows.r7.status, "contacts_selected");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 4);
});

test("selection policy and limits", async () => {
  const [result, transport] = await run("happy");
  const c = cands(byId(result).r1);
  assert.deepEqual(Object.keys(c), ["fp900000000001", "fp900000000002", "fp900000000003", "fp900000000004", "fp900000000005"]);
  assert.equal(c.fp900000000001.details_status, "fetched");
  assert.equal(c.fp900000000002.selection, "rejected");
  assert.ok(c.fp900000000003.selection_reason.includes("1 optional check(s) answered unknown"));
  assert.equal(c.fp900000000004.details_status, "not_fetched_limit");
  assert.equal(c.fp900000000005.selection, "not_checked");
  assert.deepEqual(c.fp900000000003.matched_queries, ["finance_exec", "finance_heads"]);
  assert.deepEqual(transport.sent.filter((r) => r.path === "/people").map((r) => r.query.id).sort(),
    ["fp900000000001", "fp900000000003", "fp900000000007"]);
});

test("unknown is needs review, not rejected", async () => {
  const [result] = await run("happy");
  const c = cands(byId(result).r2).fp900000000006;
  assert.equal(c.selection, "needs_review");
  const senior = c.checks.find((x: Json) => x.check_id === "senior");
  assert.equal(senior.answer, "unknown");
  assert.equal(senior.result, "unknown");
});

test("search coverage and paging", async () => {
  const [result, transport] = await run("happy");
  const s = Object.fromEntries(byId(result).r1.searches.map((x: Json) => [x.query_id, x]));
  assert.equal(s.finance_exec.total, 4);
  assert.equal(s.finance_exec.pages_fetched, 2);
  assert.equal(s.finance_exec.complete, true);
  const allowed = new Set(["companyId", "selectedCountry", "position", "seniority", "department", "from", "size"]);
  const bodies = transport.sent.filter((r) => r.path === "/people/search").map((r) => r.jsonBody as Json);
  assert.ok(bodies.every((b) => Object.keys(b).every((k) => allowed.has(k))));
  assert.equal(bodies[0].seniority, "3");
  const r2 = byId(result).r2;
  assert.equal(r2.company_id_source, "input");
  assert.equal(r2.searches[0].status, "no_matches");
  assert.ok(result.coverage.some((c: string) => c.includes("success false")));
  assert.ok(byId(result).r7.status_reason.includes("1 search record(s) had no person ID"));
});

test("disagreements and company association are surfaced", async () => {
  const [result] = await run("happy");
  const c = cands(byId(result).r1);
  assert.ok(c.fp900000000002.notes[0].includes("filtered on seniority C-Suite / Partner"));
  assert.equal(c.fp900000000003.current_company_matches_account, "no");
  assert.deepEqual(c.fp900000000001.contact.phones, [{ value: "+61 400 000 101", is_dnd: false }, { value: "+61 400 000 102", is_dnd: true }]);
  assert.equal(byId(result).r7.candidates[0].details_status, "not_found");
});

test("works_at_company gets the account ID", async () => {
  const [, transport] = await run("happy");
  const q = transport.sent.filter((r) => r.path === "/decisions" && r.method === "POST" && (r.jsonBody as Json).tool === "works_at_company")
    .map((r) => (r.jsonBody as Json).question);
  assert.deepEqual(q[0], { person_id: "fp900000000001", company_id: "f900000000001" });
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors", { config: "errors.config.json" });
  const rows = byId(result);
  assert.equal(rows.e1.status, "incomplete");
  assert.equal(rows.e1.searches[0].error.status, 500);
  const checks = Object.fromEntries(rows.e1.candidates[0].checks.map((x: Json) => [x.check_id, x]));
  assert.equal(checks.in_finance.error.status, 502);
  assert.equal(checks.senior.error.kind, "timeout");
  assert.equal(rows.e2.candidates[0].checks[0].error.kind, "invalid_response");
  assert.equal(rows.e3.candidates[0].checks[1].result, "not_asked");
  assert.equal(rows.e4.status, "not_processed");
  assert.equal(result.credits.requests_with_uncertain_charge, 3);
  assert.equal(transport.sent.length, 15);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { config: "budget.config.json", maxCredits: 2.1 });
  const rows = byId(result);
  const c = cands(rows["row-1"]);
  assert.equal(c.fp900000000001.details_status, "not_requested");
  assert.equal(c.fp900000000002.selection, "incomplete");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(result.errors[0].kind, "budget_exceeded");
  assert.equal(transport.sent.length, 6);
});

test("bad tool stops before any search", async () => {
  const [result, transport] = await run("bad-tool", { config: "bad-tool.config.json" });
  assert.deepEqual(result.rows.map((r: Json) => r.status), ["not_processed"]);
  const msgs = result.errors.map((e: Json) => e.message).join(" ");
  assert.ok(msgs.includes("is_imaginary") && msgs.includes("colour") && msgs.includes("not a person"));
  assert.deepEqual(transport.sent.map((r) => r.path), ["/decisions"]);
});

test("no checks selects on search alone", async () => {
  const [result, transport] = await run("nochecks", { config: "nochecks.config.json", ext: "json" });
  const c = cands(result.rows[0]);
  assert.ok(c.fp900000000001.selection_reason.startsWith("Selected on the search criteria alone"));
  assert.equal(c.fp900000000008.details_status, "not_fetched_limit");
  assert.deepEqual(transport.sent.map((r) => r.path), ["/people/search", "/people"]);
});

test("outputs are safe and flat", async () => {
  const out = tmp();
  await run("happy", { out });
  const html = readFileSync(join(out, "report.html"), "utf-8");
  assert.ok(!html.includes("<b>Pat</b>") && html.includes("&lt;b&gt;Pat&lt;/b&gt;"));
  const table = parseCsv(readFileSync(join(out, "results.csv"), "utf-8"));
  const header = table[0];
  assert.deepEqual(table.slice(1).map((r) => r[0]), [...Array(5).fill("r1"), "r2", "r3", ...Array(5).fill("r4"), "r5", "r6", "r7"]);
  assert.equal(table[1][header.indexOf("phones")], "'+61 400 000 101; '+61 400 000 102 (do not call)");
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan upper bounds", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 4);
  assert.equal(calls["POST /people/search"].requests, 20);
  assert.equal(calls["POST /decisions"].requests, 80);
  assert.equal(calls["GET /people"].requests, 10);
});

test("labels and ids are both accepted", () => {
  const cfg = loadConfig(JSON.parse(readFileSync(join(F, "config.json"), "utf-8")));
  const q = cfg.search.queries;
  assert.deepEqual([q[0].department, q[0].seniority, q[1].department, q[1].seniority], ["14", "3", "14", "4"]);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.search.queries = []; }, "search.queries must be a non-empty list"],
  [(c) => { c.search.queries[0].seniority = "Senior"; }, "seniority must be a seniority id 1 to 7"],
  [(c) => { c.search.queries[0].department = "99"; }, "department must be a department id 1 to 17"],
  [(c) => { c.search.queries[0].name = "Alex"; }, "has unknown key"],
  [(c) => { c.search.country = "GB"; }, "search.country must be one of"],
  [(c) => { c.search.page_size = 500; }, "page_size must be a whole number from 1 to 100"],
  [(c) => { c.search.queries.push({ id: "empty" }); }, "needs at least one of position, seniority"],
  [(c) => { c.checks[0].want = "maybe"; }, "want must be yes or no"],
  [(c) => { c.policy = { min_optional_passes: 3 }; }, "only 2 optional"],
  [(c) => { c.max_details_per_account = -1; }, "max_details_per_account must be a whole number"],
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
  assert.equal(JSON.parse(readFileSync(join(out, "plan.json"), "utf-8")).published_estimate_upper_bound, 17.2);
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
