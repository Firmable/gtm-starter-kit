#!/usr/bin/env node
/**
 * Command line entry point: gtm-inbound-lead-router.
 *
 * `serve ...` starts the local single-lead example server (see server.ts). Every other invocation is
 * the standard batch command line, unchanged: plan mode by default, and --live needs --max-credits
 * and FIRMABLE_API_KEY.
 */
import { runMain } from "firmable-gtm-client";
import { RECIPE } from "./recipe.js";
import { serveMain } from "./server.js";

const argv = process.argv.slice(2);
if (argv[0] === "serve") {
  serveMain(argv.slice(1)).then((code) => { process.exitCode = code; }, (e) => {
    process.stderr.write(`error: ${(e as Error)?.stack ?? String(e)}\n`);
    process.exitCode = 1;
  });
} else {
  runMain(RECIPE);
}
