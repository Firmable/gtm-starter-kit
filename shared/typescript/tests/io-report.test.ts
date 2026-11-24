import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  InputError, Report, esc, findDuplicates, link, normaliseDomain, parseCsv, readRows, safeCell, safeUrl, writeCsv,
} from "../src/index.js";

const tmp = () => mkdtempSync(join(tmpdir(), "gtm-"));

test("CSV row ids and order", () => {
  const p = join(tmp(), "in.csv");
  writeFileSync(p, "domain,name\nacme.example,Acme\nbeta.example,Beta\n");
  const rows = readRows(p, { required: ["domain"] });
  assert.deepEqual(rows.map((r) => r.row_id), ["row-1", "row-2"]);
  assert.equal(rows[1].name, "Beta");
});

test("CSV parser handles quotes, embedded newlines and blank lines", () => {
  assert.deepEqual(parseCsv('a,b\r\n"x, ""y""","multi\nline"\n\n1,\n'), [["a", "b"], ['x, "y"', "multi\nline"], ["1", ""]]);
});

test("JSON rows object", () => {
  const p = join(tmp(), "in.json");
  writeFileSync(p, JSON.stringify({ rows: [{ row_id: "a", domain: "acme.example", n: 2, ok: true }] }));
  const r = readRows(p, { required: ["domain"] })[0];
  assert.equal(r.row_id, "a");
  assert.equal(r.n, "2");
  assert.equal(r.ok, "true");
});

test("reports all problems", () => {
  const p = join(tmp(), "in.csv");
  writeFileSync(p, "row_id,domain\nx,acme.example\nx,\n");
  assert.throws(() => readRows(p, { required: ["domain"] }), (e: Error) =>
    e instanceof InputError && e.message.includes("repeats row 1") && e.message.includes("domain is empty"));
});

for (const bad of ["", "   \n", "not,a\n"]) {
  test(`rejects empty or missing columns: ${JSON.stringify(bad)}`, () => {
    const p = join(tmp(), "in.csv");
    writeFileSync(p, bad);
    assert.throws(() => readRows(p, { required: ["domain"] }), InputError);
  });
}

test("rejects wrong extension and bad JSON", () => {
  const d = tmp();
  writeFileSync(join(d, "in.txt"), "x");
  assert.throws(() => readRows(join(d, "in.txt")), InputError);
  writeFileSync(join(d, "in.json"), "{nope");
  assert.throws(() => readRows(join(d, "in.json")), InputError);
});

const domains: [string, string | null][] = [
  ["https://www.Acme.example/about?x=1", "acme.example"], ["acme.example.", "acme.example"],
  ["user@acme.example", "acme.example"], ["acme.example:8443", "acme.example"], ["not a domain", null],
  ["localhost", null], ["", null],
];
for (const [raw, expected] of domains) test(`normaliseDomain(${raw})`, () => assert.equal(normaliseDomain(raw), expected));

test("findDuplicates", () => {
  const rows = [{ row_id: "a", d: "x" }, { row_id: "b", d: "x" }, { row_id: "c", d: "" }];
  assert.deepEqual(findDuplicates(rows, "d"), { b: "a" });
});

const cells: [unknown, string][] = [
  ['=HYPERLINK("http://x")', `'=HYPERLINK("http://x")`], ["+cmd", "'+cmd"], ["@SUM(1)", "'@SUM(1)"], ["-2+3", "'-2+3"],
  ["\tx", "'\tx"], ["-5", "-5"], ["3.5", "3.5"], [null, ""], [true, "true"], [2.0, "2"], [["a", "=b"], "a; '=b"],
];
for (const [v, e] of cells) test(`safeCell(${JSON.stringify(v)})`, () => assert.equal(safeCell(v), e));

test("writeCsv neutralises formulas", () => {
  const p = writeCsv(join(tmp(), "o.csv"), ["a"], [{ a: "=1+1" }]);
  assert.equal(readFileSync(p, "utf-8"), "a\n'=1+1\n");
});

test("escape and safe links", () => {
  assert.equal(esc("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;");
  assert.equal(safeUrl("javascript:alert(1)"), null);
  assert.equal(safeUrl("https://acme.example/x"), "https://acme.example/x");
  assert.ok(!link("javascript:alert(1)", "x").includes("<a "));
  assert.ok(link("https://acme.example/?a=1&b=2", "x").includes('href="https://acme.example/?a=1&amp;b=2"'));
});

test("report escapes and loads nothing external", () => {
  const result = { mode: "test-fixture", generated_at: "t", input: { file: "<b>.csv", row_count: 1, config: {} },
    coverage: ["<img src=x onerror=alert(1)>"], errors: [], credits: {}, ledger: [] };
  const r = new Report("T", "recipe", result);
  r.addRequestSection("desc");
  r.addStandardSections();
  const out = r.render();
  assert.ok(!out.includes("<img") && out.includes("&lt;img"));
  assert.ok(!out.includes("<script"));
  assert.ok(!out.includes('src="http'));
});

for (const phone of ["+61 412 345 678", "+1 (415) 555 0100", "+6141234"]) {
  test(`phone ${phone} is not prefixed`, () => assert.equal(safeCell(phone), phone));
}
test("phone-like formula is still prefixed", () => assert.ok(safeCell("+1+cmd|' /C calc'!A0").startsWith("'")));
