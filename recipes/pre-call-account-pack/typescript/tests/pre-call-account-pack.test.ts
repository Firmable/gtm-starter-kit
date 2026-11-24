import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError, main as cliMain, Json } from "firmable-gtm-client";
import { fixturesDir, runWithFixtures } from "firmable-gtm-client/testing";
import { RECIPE, loadConfig, plan } from "../src/recipe.js";

const F = fixturesDir("pre-call-account-pack");
const tmp = () => mkdtempSync(join(tmpdir(), "pcap-"));
const readJson = (p: string) => JSON.parse(readFileSync(p, "utf-8"));

function run(name: string, o: { config?: string; maxCredits?: number; out?: string } = {}) {
  return runWithFixtures(RECIPE, {
    inputPath: join(F, `${name}.input.csv`), configPath: join(F, o.config ?? "config.json"),
    scenario: join(F, `${name}.scenario.json`), maxCredits: o.maxCredits ?? 100, outDir: o.out,
  });
}
const byId = (result: Json): Record<string, Json> => Object.fromEntries(result.rows.map((r: Json) => [r.row_id, r]));
const entry = (result: Json, source: Json) => result.ledger[source.ledger_seq - 1];

test("happy path statuses and duplicate copy", async () => {
  const [result, transport] = await run("happy");
  const rows = byId(result);
  assert.equal(rows.h1.status, "complete");
  assert.equal(rows.h2.duplicate_of, "h1");
  assert.deepEqual(rows.h2.contacts, rows.h1.contacts);
  assert.equal(rows.h3.status, "invalid_input");
  assert.equal(rows.h4.status, "company_not_found");
  assert.equal(rows.h5.status, "complete");
  assert.deepEqual(transport.unused(), []);
  assert.equal(transport.sent.filter((r) => r.path === "/company").length, 3);
  assert.deepEqual(result.errors, []);
});

test("contact selection order, dedupe and company_id", async () => {
  const [result, transport] = await run("happy");
  const contacts = byId(result).h1.contacts;
  assert.deepEqual(contacts.selected.map((p: Json) => p.person_id), ["fp900000000001", "fp900000000002", "fp900000000003"]);
  assert.deepEqual(contacts.selected[1].matched_searches, ["sales_leaders", "executives"]);
  const questions = (tool: string) => transport.sent
    .filter((r) => r.method === "POST" && r.path === "/decisions" && (r.jsonBody as Json).tool === tool)
    .map((r) => (r.jsonBody as Json).question);
  assert.deepEqual(questions("works_at_company")[0], { person_id: "fp900000000001", company_id: "f900000000001" });
  assert.ok(!("company_id" in questions("is_new_to_role")[0]));
});

test("every fact carries its source", async () => {
  const [result] = await run("happy");
  const h1 = byId(result).h1;
  assert.equal(entry(result, h1.company.source).endpoint, "GET /company");
  for (const c of h1.company_checks.checks) assert.equal(entry(result, c.source).body.tool, c.tool);
  for (const p of h1.contacts.selected) {
    assert.equal(entry(result, p.search_source).endpoint, "POST /people/search");
    assert.deepEqual(entry(result, p.profile_source).query, { id: p.person_id });
  }
  assert.deepEqual(h1.tech_intel.items.map((i: Json) => entry(result, i.source).query.cursor ?? null), [null, null, "cur-2"]);
  assert.equal(entry(result, h1.ai_adoption.source).endpoint, "POST /ai-adoption");
});

test("unknown stays unknown and becomes a template question", async () => {
  const [result] = await run("happy");
  const h1 = byId(result).h1;
  const check = h1.company_checks.checks.find((c: Json) => c.check_id === "au_head_office");
  assert.equal(check.answer, "unknown");
  assert.equal(check.result, "unknown");
  const ids = h1.suggested_questions.map((q: Json) => q.template_id);
  assert.equal(ids[0], "confirm_unknown_check");
  assert.equal(ids.filter((x: string) => x === "technology_category").length, 2);
  assert.equal(ids[ids.length - 1], "why_now");
});

test("contact details exclude personal email and keep the DND flag", async () => {
  const [result] = await run("happy");
  const profile = byId(result).h1.contacts.selected[0].profile;
  assert.deepEqual(profile.work_emails, ["alex.example@acme.example"]);
  assert.deepEqual(profile.phones[0], { value: "+61 400 000 000", is_dnd: true });
  assert.ok(!JSON.stringify(result.rows).includes("personal"));
});

test("data quality notes are surfaced, not corrected", async () => {
  const [result] = await run("happy");
  const rows = byId(result);
  assert.ok(rows.h1.data_quality_notes.some((n: string) => n.includes("current company ID (f900000000099) differs")));
  assert.ok(rows.h1.data_quality_notes.some((n: string) => n.includes("workforce evidence is truncated")));
  assert.equal(rows.h1.contacts.selected[2].profile.current_company_id, "f900000000099");
  assert.ok(rows.h5.data_quality_notes.some((n: string) => n.includes("about entity f900000000099")));
  const ev = rows.h1.ai_adoption.evidence_summary.workforce;
  assert.deepEqual([ev.qualifying, ev.not_qualifying, ev.qualifies_missing], [1, 1, 1]);
  assert.equal(rows.h1.ai_adoption.dimensions.governance.level, "EMERGING");
});

test("valid empty answers are not errors", async () => {
  const [result] = await run("happy");
  const h5 = byId(result).h5;
  assert.equal(h5.contacts.status, "no_match");
  assert.equal(h5.tech_intel.status, "no_data");
  assert.equal(h5.ai_adoption.status, "not_found");
  assert.deepEqual(h5.suggested_questions.map((q: Json) => q.template_id), ["who_else", "why_now"]);
  assert.ok(result.coverage.some((c: string) => c.includes("success false without a records list")));
});

test("limits: page limit, contact limit and Tech Intel not on plan", async () => {
  const [result, transport] = await run("limits", { config: "limits.config.json" });
  const rows = byId(result);
  assert.equal(rows.l1.status, "complete");
  assert.equal(rows.l1.tech_intel.complete, false);
  assert.ok(rows.l1.status_reason.includes("Tech Intel is incomplete: 2 of 150"));
  assert.equal(rows.l1.contacts.selected.length, 1);
  assert.equal(rows.l1.contacts.not_selected, 1);
  assert.equal(rows.l1.contacts.selected[0].profile_status, "not_requested");
  assert.ok(rows.l1.data_quality_notes.some((n: string) => n.includes("Marketo: 1 record(s) have no source location")));
  assert.equal(rows.l1.suggested_questions.length, 2);
  assert.equal(rows.l2.status, "partial");
  assert.equal(rows.l2.tech_intel.status, "not_available");
  assert.ok(!transport.sent.some((r) => r.path === "/people" || r.path === "/decisions"));
});

test("errors are not answers and the run stops on 402", async () => {
  const [result, transport] = await run("errors");
  const rows = byId(result);
  assert.equal(rows.e1.status, "error");
  const checks = Object.fromEntries(rows.e2.company_checks.checks.map((c: Json) => [c.check_id, c]));
  assert.equal(checks.b2b.error.kind, "timeout");
  assert.equal(checks.au_head_office.error.kind, "invalid_response");
  assert.deepEqual(rows.e2.contacts.searches.map((s: Json) => s.status), ["error", "ok"]);
  assert.equal(rows.e2.contacts.selected[0].profile_status, "not_found");
  assert.equal(rows.e2.tech_intel.status, "error");
  assert.equal(rows.e2.ai_adoption.status, "ok");
  assert.equal(rows.e3.company_checks.checks[1].result, "not_asked");
  assert.equal(rows.e3.contacts.status, "not_asked");
  assert.equal(rows.e4.status, "not_processed");
  assert.ok(!result.errors.some((e: Json) => e.kind === "not_found"));
  assert.equal(result.credits.requests_with_uncertain_charge, 4);
  assert.equal(transport.sent.length, 14);
});

test("budget stops before the next paid call", async () => {
  const [result, transport] = await run("budget", { config: "budget.config.json", maxCredits: 1.5 });
  const rows = byId(result);
  assert.equal(rows["row-1"].status, "partial");
  assert.equal(rows["row-1"].contacts.selected[0].profile_error.kind, "budget_exceeded");
  assert.equal(rows["row-2"].status, "not_processed");
  assert.equal(transport.sent.length, 2);
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
  assert.ok(!html.includes("<b>Beta</b>") && html.includes("&lt;b&gt;Beta&lt;/b&gt;"));
  assert.ok(!html.includes("<script>alert"));
  assert.ok(html.includes('<div class="template">'));
  assert.ok(!readFileSync(join(out, "results.json"), "utf-8").includes("fbl_dev_"));
});

test("plan counts upper bounds", () => {
  const cfg = loadConfig(readJson(join(F, "config.json")));
  const rows = RECIPE.readInput(join(F, "happy.input.csv"));
  const calls = Object.fromEntries(plan(rows, cfg).map((c) => [c.endpoint, c]));
  assert.equal(calls["GET /company"].requests, 3);
  assert.equal(calls["POST /people/search"].requests, 6);
  assert.equal(calls["GET /people"].requests, 9);
  assert.equal(calls["POST /decisions"].requests, 24);
  assert.equal(calls["GET /tech-intel"].requests, 6);
  assert.equal(calls["POST /ai-adoption"].requests, 3);
});

test("plan omits disabled sections", () => {
  const cfg = loadConfig(readJson(join(F, "limits.config.json")));
  const rows = RECIPE.readInput(join(F, "limits.input.csv"));
  assert.deepEqual(plan(rows, cfg).map((c) => c.endpoint), ["GET /company", "POST /people/search", "GET /tech-intel"]);
});

const bad: [(c: Json) => void, string][] = [
  [(c) => { c.colour = "orange"; }, "Unknown configuration key"],
  [(c) => { c.company_checks[0].want = "maybe"; }, "want must be yes or no"],
  [(c) => { c.contact_checks[0].id = "b2b"; }, "used twice"],
  [(c) => { c.company_checks[0].extra = 1; }, "has unknown key"],
  [(c) => { c.contacts.selected_country = "GB"; }, "selected_country must be one of"],
  [(c) => { c.contacts.searches[0].seniority = 4; }, "seniority must be a seniority id"],
  [(c) => { c.contacts.searches[0].department = "18"; }, "department must be a department id"],
  [(c) => { delete c.contacts.searches[1].seniority; }, "needs at least one of"],
  [(c) => { c.contacts.max_contacts = 51; }, "max_contacts must be a whole number from 0 to 50"],
  [(c) => { c.contacts.searches = []; }, "contact_checks need at least one"],
  [(c) => { c.tech_intel.max_pages = 0; }, "max_pages must be a whole number from 1 to 20"],
  [(c) => { c.ai_adoption.enabled = "yes"; }, "ai_adoption.enabled must be true or false"],
  [(c) => { c.tech_intel.cursor = "x"; }, "Unknown tech_intel key"],
];
for (const [mutate, message] of bad) {
  test(`invalid config: ${message}`, () => {
    const cfg = readJson(join(F, "config.json"));
    mutate(cfg);
    assert.throws(() => loadConfig(cfg), (e: Error) => e instanceof ConfigError && e.message.includes(message));
  });
}

test("config defaults", () => {
  const cfg = loadConfig({});
  assert.deepEqual(cfg.tech_intel, { enabled: true, with_evidence: false, max_pages: 1 });
  assert.deepEqual(cfg.ai_adoption, { enabled: false, with_evidence: false });
  assert.equal(cfg.contacts.max_contacts, 3);
});

test("CLI plan mode makes no calls", async () => {
  const out = tmp();
  delete process.env.FIRMABLE_API_KEY;
  const code = await cliMain(RECIPE, ["--input", join(F, "happy.input.csv"), "--config", join(F, "config.json"), "--out", out]);
  assert.equal(code, 0);
  assert.equal(readJson(join(out, "plan.json")).published_estimate_upper_bound, 37.92);
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
