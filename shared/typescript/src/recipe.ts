/**
 * The structure every recipe follows, and the run envelope written to results.json.
 * Mirrors shared/python/src/firmable_client/recipe.py.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import * as pricing from "./pricing.js";
import { FirmableClient, Json } from "./client.js";
import { ConfigError, ErrorDict, FirmableError } from "./errors.js";
import { Row, writeCsv, writeJson } from "./io.js";
import { Report } from "./report.js";

export const SCHEMA_VERSION = 1;
export const FATAL_KINDS = new Set(["unauthorized", "insufficient_credits", "budget_exceeded", "redirect_blocked"]);

export class PlannedCall {
  constructor(
    readonly endpoint: string,
    readonly purpose: string,
    readonly requests: number,
    readonly parts: pricing.Part[],
    readonly rowIds: string[] = [],
  ) {}

  toDict(): Record<string, unknown> {
    return {
      endpoint: this.endpoint,
      purpose: this.purpose,
      requests_upper_bound: this.requests,
      published_estimate_upper_bound: pricing.total(this.parts),
      row_ids: this.rowIds,
    };
  }
}

export interface RecipeOutput {
  rows: Json[];
  summary: Record<string, unknown>;
  coverage: string[];
}

export interface Recipe {
  name: string;
  title: string;
  description: string;
  loadConfig(raw: Record<string, unknown>): Json;
  readInput(path: string): Row[];
  plan(rows: Row[], config: Json): PlannedCall[];
  run(ctx: RunContext): Promise<RecipeOutput>;
  csvColumns: string[];
  csvRows(result: Json): Record<string, unknown>[];
  render(report: Report, result: Json): void;
}

export type ErrorEntry = ErrorDict & { row_id: string | null };

export class RunContext {
  errors: ErrorEntry[] = [];
  stopped: ErrorEntry | null = null;
  readonly today: string; // YYYY-MM-DD

  constructor(readonly client: FirmableClient, readonly config: Json, readonly rows: Row[], today?: string) {
    this.today = today ?? new Date().toISOString().slice(0, 10);
  }

  recordError(rowId: string | null, err: FirmableError): ErrorEntry {
    const entry: ErrorEntry = { row_id: rowId, ...err.toDict() };
    this.errors.push(entry);
    if (FATAL_KINDS.has(err.kind) && this.stopped === null) this.stopped = entry;
    return entry;
  }

  /**
   * Runs fn for each row in order and keeps partial results. A row-level API error is recorded on
   * that row. A fatal error (bad key, no credits, budget reached, blocked redirect) stops the run;
   * later rows are returned with status "not_processed".
   */
  async runRows(rows: Row[], fn: (row: Row) => Promise<Json>): Promise<Json[]> {
    const out: Json[] = [];
    for (const row of rows) {
      if (this.stopped !== null) {
        out.push({ row_id: row.row_id, input: row, status: "not_processed", reason: `Run stopped earlier: ${this.stopped.message}` });
        continue;
      }
      try {
        out.push(await fn(row));
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        const entry = this.recordError(row.row_id, e);
        out.push({ row_id: row.row_id, input: row, status: "error", error: entry });
      }
    }
    return out;
  }
}

export function planSummary(recipe: Recipe, rows: Row[], config: Json): Json {
  const calls = recipe.plan(rows, config);
  return {
    recipe: recipe.name,
    rows: rows.length,
    calls: calls.map((c) => c.toDict()),
    requests_upper_bound: calls.reduce((s, c) => s + c.requests, 0),
    published_estimate_upper_bound: pricing.round4(calls.reduce((s, c) => s + pricing.total(c.parts), 0)),
    reservation_upper_bound: pricing.reservationFor(calls.map((c) => c.parts)),
    pricing_as_of: pricing.PRICING_AS_OF,
    pricing_sources: pricing.PRICING_SOURCES,
    note: "Upper bounds assume every profile is new to your organisation and every row needs every " +
      "call. Profiles your organisation already paid for are free. Wallet deductions are not " +
      "exposed by the API.",
  };
}

export function buildResult(recipe: Recipe, ctx: RunContext, output: RecipeOutput, o: {
  inputPath: string; config: Json; mode: string;
}): Json {
  const credits: Json = ctx.client.budget.summary();
  credits.pricing_as_of = pricing.PRICING_AS_OF;
  const coverage = [...(output.coverage ?? [])];
  if (ctx.stopped) {
    coverage.unshift(`The run stopped early: ${ctx.stopped.message.replace(/\.+$/, "")}. Rows after that point are marked not_processed.`);
  }
  return {
    recipe: recipe.name,
    schema_version: SCHEMA_VERSION,
    generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
    mode: o.mode,
    input: { file: basename(o.inputPath), row_count: ctx.rows.length, config: o.config },
    summary: output.summary ?? {},
    rows: output.rows ?? [],
    coverage,
    errors: ctx.errors,
    credits,
    ledger: ctx.client.ledger.entries,
  };
}

export function writeOutputs(recipe: Recipe, result: Json, outDir: string): Record<string, string> {
  mkdirSync(outDir, { recursive: true });
  const paths: Record<string, string> = {
    json: writeJson(join(outDir, "results.json"), result),
    csv: writeCsv(join(outDir, "results.csv"), recipe.csvColumns, recipe.csvRows(result)),
  };
  const report = new Report(recipe.title, recipe.name, result);
  report.addRequestSection(recipe.description);
  recipe.render(report, result);
  report.addStandardSections();
  const htmlPath = join(outDir, "report.html");
  writeFileSync(htmlPath, report.render(), "utf-8");
  paths.html = htmlPath;
  return paths;
}

/** Parses YYYY-MM-DD strictly; raises ConfigError with the same message as Python. */
export function parseDate(value: unknown, what: string): string {
  const s = String(value);
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) {
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    if (d.getUTCFullYear() === +m[1] && d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3]) return s;
  }
  throw new ConfigError(`${what} must be a date as YYYY-MM-DD, not ${pyRepr(value)}`);
}

export function pyRepr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "string") return `'${v}'`;
  if (typeof v === "boolean") return v ? "True" : "False";
  return String(v);
}
