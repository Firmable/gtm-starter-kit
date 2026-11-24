#!/usr/bin/env node
/** Runs a recipe against a fixture scenario. Used by tests and the parity check only. */
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { runWithFixtures } from "./testing.js";

const args: Record<string, string> = {};
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i += 2) args[argv[i].replace(/^--/, "")] = argv[i + 1];

const mod = await import(pathToFileURL(resolve(args["recipe-module"])).href);
await runWithFixtures(mod.RECIPE, {
  inputPath: args.input,
  configPath: args.config,
  scenario: args.scenario,
  outDir: args.out,
  maxCredits: args["max-credits"] !== undefined ? Number(args["max-credits"]) : 100.0,
  today: args.today,
});
