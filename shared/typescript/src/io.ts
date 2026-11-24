/**
 * Input and output helpers shared by every recipe. Mirrors shared/python/src/firmable_client/io.py.
 *
 * Input files are CSV (with a header row) or JSON (an array of objects, or {"rows": [...]}).
 * Every row keeps a `row_id`: the value of a `row_id` column when present, otherwise `row-<n>`.
 */
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, extname } from "node:path";
import { ConfigError, InputError } from "./errors.js";

export type Row = Record<string, string> & { row_id: string };

const FORMULA_PREFIXES = ["=", "+", "-", "@", "\t", "\r"];
const PLAIN_NUMBER = /^-?\d+(\.\d+)?$/;
const PHONE = /^\+\d[\d ()]*$/; // +61 412 345 678: digits, spaces and brackets cannot form a formula
const DOMAIN = /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

/** Parses CSV the way Python's csv module does with the default dialect. Blank lines are skipped. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let fieldStarted = false;
  let i = 0;
  const pushField = () => { row.push(field); field = ""; fieldStarted = false; };
  const pushRow = () => {
    if (row.length === 1 && row[0] === "" ) { row = []; return; }
    rows.push(row); row = [];
  };
  while (i < text.length) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false; i += 1; continue;
      }
      field += ch; i += 1; continue;
    }
    if (ch === '"' && !fieldStarted) { inQuotes = true; fieldStarted = true; i += 1; continue; }
    if (ch === ",") { pushField(); i += 1; continue; }
    if (ch === "\r" || ch === "\n") {
      pushField(); pushRow();
      if (ch === "\r" && text[i + 1] === "\n") i += 1;
      i += 1; continue;
    }
    field += ch; fieldStarted = true; i += 1;
  }
  if (field !== "" || fieldStarted || row.length) { pushField(); pushRow(); }
  return rows;
}

function cellText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return String(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v).trim();
}

function pyRepr(s: string): string {
  return `'${s}'`;
}

/**
 * Reads and checks an input file. `required` columns must exist and be non-empty on every row.
 * When `anyOf` is given, every row needs at least one of those columns filled.
 */
export function readRows(path: string, opts: { required?: string[]; anyOf?: string[] } = {}): Row[] {
  const required = opts.required ?? [];
  const anyOf = opts.anyOf ?? [];
  const name = basename(path);
  if (!existsSync(path) || !statSync(path).isFile()) throw new InputError(`Input file not found: ${path}`);
  let text = readFileSync(path, "utf-8");
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  if (!text.trim()) throw new InputError(`Input file is empty: ${name}`);
  let raw: unknown[];
  const ext = extname(path).toLowerCase();
  if (ext === ".json") {
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch (e) {
      throw new InputError(`${name} is not valid JSON: ${(e as Error).message}`);
    }
    if (data && typeof data === "object" && !Array.isArray(data) && Array.isArray((data as { rows?: unknown }).rows)) {
      data = (data as { rows: unknown[] }).rows;
    }
    if (!Array.isArray(data)) throw new InputError(`${name} must hold an array of objects or {"rows": [...]}.`);
    raw = data;
  } else if (ext === ".csv") {
    const table = parseCsv(text);
    if (!table.length) throw new InputError(`${name} has no header row.`);
    const header = table[0];
    const missing = required.filter((c) => !header.includes(c));
    if (missing.length) throw new InputError(`${name} is missing column(s): ${missing.join(", ")}`);
    raw = table.slice(1).map((cells) => {
      const o: Record<string, string> = {};
      header.forEach((h, idx) => { o[h] = idx < cells.length ? cells[idx] : ""; });
      return o;
    });
  } else {
    throw new InputError("Input must be a .csv or .json file.");
  }

  const rows: Row[] = [];
  const problems: string[] = [];
  const seen = new Map<string, number>();
  raw.forEach((item, idx) => {
    const n = idx + 1;
    if (!item || typeof item !== "object" || Array.isArray(item)) { problems.push(`row ${n}: is not an object`); return; }
    const row: Record<string, string> = {};
    for (const [k, v] of Object.entries(item as Record<string, unknown>)) row[k.trim()] = cellText(v);
    const rowId = row.row_id || `row-${n}`;
    if (seen.has(rowId)) problems.push(`row ${n}: row_id ${pyRepr(rowId)} repeats row ${seen.get(rowId)}`);
    seen.set(rowId, n);
    row.row_id = rowId;
    for (const c of required) if (!row[c]) problems.push(`row ${n} (${rowId}): ${c} is empty`);
    if (anyOf.length && !anyOf.some((c) => row[c])) problems.push(`row ${n} (${rowId}): needs one of ${anyOf.join(", ")}`);
    rows.push(row as Row);
  });
  if (!rows.length && !problems.length) problems.push("no rows found");
  if (problems.length) throw new InputError(`${name} has problems:\n  ${problems.join("\n  ")}`);
  return rows;
}

export function readConfig(path: string): Record<string, unknown> {
  const name = basename(path);
  if (!existsSync(path) || !statSync(path).isFile()) throw new ConfigError(`Config file not found: ${path}`);
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(path, "utf-8"));
  } catch (e) {
    throw new ConfigError(`${name} is not valid JSON: ${(e as Error).message}`);
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new ConfigError(`${name} must hold a JSON object.`);
  return data as Record<string, unknown>;
}

/** Returns a bare lower-case domain, or null when the value is not a plausible domain. */
export function normaliseDomain(value: string | null | undefined): string | null {
  if (!value) return null;
  let v = value.trim().toLowerCase();
  v = v.replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
  v = v.split("/")[0].split("?")[0].split("#")[0];
  const at = v.split("@");
  v = at[at.length - 1].split(":")[0].replace(/\.+$/, "");
  if (v.startsWith("www.")) v = v.slice(4);
  return DOMAIN.test(v) ? v : null;
}

/** Maps row_id to the row_id of the first earlier row with the same non-empty key value. */
export function findDuplicates(rows: Row[], key: string): Record<string, string> {
  const first = new Map<string, string>();
  const dupes: Record<string, string> = {};
  for (const r of rows) {
    const k = r[key] || "";
    if (!k) continue;
    if (first.has(k)) dupes[r.row_id] = first.get(k)!;
    else first.set(k, r.row_id);
  }
  return dupes;
}

/** Formats a value for CSV and neutralises spreadsheet formulas. */
export function safeCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) value = value.map((v) => safeCell(v)).join("; ");
  const s = typeof value === "object" ? JSON.stringify(value) : String(value);
  if (FORMULA_PREFIXES.some((p) => s.startsWith(p)) && !PLAIN_NUMBER.test(s) && !PHONE.test(s)) return "'" + s;
  return s;
}

function csvQuote(s: string): string {
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function writeCsv(path: string, header: string[], rows: Record<string, unknown>[]): string {
  mkdirSync(dirname(path), { recursive: true });
  const lines = [header.map(csvQuote).join(",")];
  for (const r of rows) lines.push(header.map((h) => csvQuote(safeCell(r[h]))).join(","));
  writeFileSync(path, lines.join("\n") + "\n", "utf-8");
  return path;
}

export function writeJson(path: string, data: unknown): string {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(data, null, 2) + "\n", "utf-8");
  return path;
}
