#!/usr/bin/env node
/**
 * Command line entry point: gtm-account-change-monitor.
 *
 * Runs the shared command line, then, after a live run, copies the new snapshot from results.json
 * (summary.snapshot) to snapshot.json in the output folder. Point previous_snapshot in the
 * configuration at that file for the next run. Nothing is scheduled.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { main, parseArgs, writeJson } from "firmable-gtm-client";
import { RECIPE } from "./recipe.js";

/** Writes snapshot.json from results.json in outDir. Returns the path, or null if there is no snapshot. */
export function writeSnapshot(outDir: string): string | null {
  const results = join(outDir, "results.json");
  if (!existsSync(results)) return null;
  const snapshot = JSON.parse(readFileSync(results, "utf-8"))?.summary?.snapshot;
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return null;
  return writeJson(join(outDir, "snapshot.json"), snapshot);
}

export async function runCli(argv: string[] = process.argv.slice(2)): Promise<number> {
  const code = await main(RECIPE, argv);
  let args;
  try {
    args = parseArgs(argv);
  } catch {
    return code;
  }
  if (args.live && (code === 0 || code === 3)) {
    const path = writeSnapshot(args.out);
    if (path) console.log(`Snapshot for the next run: ${path}`);
  }
  return code;
}

const invoked = process.argv[1] ?? "";
if (invoked.endsWith("cli.js") || invoked.endsWith("gtm-account-change-monitor")) {
  runCli().then((code) => { process.exitCode = code; }, (e) => {
    process.stderr.write(`error: ${(e as Error)?.stack ?? String(e)}\n`);
    process.exitCode = 1;
  });
}
