/**
 * Command line shared by every recipe. Mirrors shared/python/src/firmable_client/cli.py.
 *
 * Plan mode is the default: it checks input and configuration, lists the calls a live run could
 * make and their published upper-bound cost, and makes no network request. Live mode needs --live,
 * --max-credits and the FIRMABLE_API_KEY environment variable.
 *
 * Exit codes: 0 finished; 2 bad arguments, input or configuration; 3 the run stopped early.
 */
import { join } from "node:path";
import { CreditBudget } from "./budget.js";
import { DEFAULT_TIMEOUT, FirmableClient, Json } from "./client.js";
import { ConfigError, InputError } from "./errors.js";
import { readConfig, writeJson } from "./io.js";
import { Recipe, RunContext, buildResult, planSummary, writeOutputs } from "./recipe.js";

interface Args { input?: string; config?: string; out: string; live: boolean; maxCredits: number | null; timeout: number; help: boolean }

function usage(recipe: Recipe): string {
  return `usage: gtm-${recipe.name} --input FILE --config FILE [--out DIR] [--live --max-credits N] [--timeout S]\n\n` +
    `${recipe.description}\n\n` +
    "  --input FILE       CSV or JSON input file\n" +
    "  --config FILE      JSON configuration file\n" +
    "  --out DIR          output folder (default: out)\n" +
    "  --live             call the Firmable API (spends credits)\n" +
    "  --max-credits N    credit limit for a live run (required with --live)\n" +
    "  --timeout S        seconds to wait for each response (default: 30)\n";
}

export function parseArgs(argv: string[]): Args {
  const a: Args = { out: "out", live: false, maxCredits: null, timeout: DEFAULT_TIMEOUT, help: false };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const next = (): string => {
      const v = argv[++i];
      if (v === undefined) throw new ConfigError(`${k} needs a value.`);
      return v;
    };
    const num = (v: string): number => {
      const n = Number(v);
      if (v.trim() === "" || !Number.isFinite(n)) throw new ConfigError(`${k} must be a number, not '${v}'.`);
      return n;
    };
    if (k === "--input") a.input = next();
    else if (k === "--config") a.config = next();
    else if (k === "--out") a.out = next();
    else if (k === "--live") a.live = true;
    else if (k === "--max-credits") a.maxCredits = num(next());
    else if (k === "--timeout") a.timeout = num(next());
    else if (k === "--help" || k === "-h") a.help = true;
    else throw new ConfigError(`Unknown argument: ${k}`);
  }
  return a;
}

export async function main(recipe: Recipe, argv: string[] = process.argv.slice(2)): Promise<number> {
  let args: Args;
  let config: Json;
  let rows;
  try {
    args = parseArgs(argv);
    if (args.help) { process.stdout.write(usage(recipe)); return 0; }
    if (!args.input || !args.config) throw new ConfigError("--input and --config are required.");
    config = recipe.loadConfig(readConfig(args.config));
    rows = recipe.readInput(args.input);
    if (args.live && args.maxCredits === null) throw new ConfigError("--live needs --max-credits, the most credits this run may reserve.");
    if (args.maxCredits !== null && args.maxCredits < 0) throw new ConfigError("--max-credits must be zero or more.");
    if (args.timeout <= 0) throw new ConfigError("--timeout must be positive.");
  } catch (e) {
    if (e instanceof ConfigError || e instanceof InputError) {
      process.stderr.write(`error: ${e.message}\n`);
      return 2;
    }
    throw e;
  }

  const plan = planSummary(recipe, rows, config);
  printPlan(plan);
  if (!args.live) {
    const path = writeJson(join(args.out, "plan.json"), plan);
    console.log(`\nPlan only: no API calls were made. Saved ${path}.`);
    console.log("Run again with --live --max-credits N to call the API.");
    return 0;
  }

  const apiKey = (process.env.FIRMABLE_API_KEY ?? "").trim();
  if (!apiKey) {
    process.stderr.write("error: set FIRMABLE_API_KEY to your developer API key (it starts with fbl_dev_).\n");
    return 2;
  }
  if (plan.reservation_upper_bound > (args.maxCredits as number)) {
    console.log(`\nNote: the upper bound (${plan.reservation_upper_bound}) is above your limit (${args.maxCredits}). ` +
      "The run stops before any call that would pass the limit.");
  }
  const client = new FirmableClient(apiKey, { budget: new CreditBudget(args.maxCredits), timeout: args.timeout });
  const ctx = new RunContext(client, config, rows);
  const output = await recipe.run(ctx);
  const result = buildResult(recipe, ctx, output, { inputPath: args.input, config, mode: "live" });
  const paths = writeOutputs(recipe, result, args.out);
  const c = result.credits;
  console.log(`\nDone. Report: ${paths.html}\nResults: ${paths.csv} and ${paths.json}`);
  console.log(`Credits: published estimate ${c.published_estimate_upper_bound}, reported in responses ` +
    `${c.response_reported_credits}, uncertain requests ${c.requests_with_uncertain_charge}.`);
  if (ctx.errors.length) console.log(`${ctx.errors.length} error(s) recorded; see the report.`);
  return ctx.stopped ? 3 : 0;
}

function printPlan(plan: Json): void {
  console.log(`${plan.recipe}: ${plan.rows} input row(s)`);
  for (const c of plan.calls) {
    console.log(`  ${String(c.endpoint).padEnd(22)} up to ${String(c.requests_upper_bound).padStart(4)} request(s), ` +
      `up to ${c.published_estimate_upper_bound} credits  ${c.purpose}`);
  }
  console.log(`Published estimate, upper bound: ${plan.published_estimate_upper_bound} credits`);
  console.log(`Reservation this code would hold, upper bound: ${plan.reservation_upper_bound} credits`);
  console.log(`Prices as of ${plan.pricing_as_of}.`);
}

/** Entry point helper for a recipe's cli.ts. */
export function runMain(recipe: Recipe): void {
  main(recipe).then((code) => { process.exitCode = code; }, (e) => {
    process.stderr.write(`error: ${(e as Error)?.stack ?? String(e)}\n`);
    process.exitCode = 1;
  });
}
