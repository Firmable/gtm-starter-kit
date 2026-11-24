/**
 * Offline test support. Never used by the command line. Mirrors firmable_client/testing.py and reads
 * the same scenario format, so Python and TypeScript tests share fixtures.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CreditBudget } from "./budget.js";
import { FirmableClient, Json } from "./client.js";
import { TransportTimeoutError } from "./errors.js";
import { fmt } from "./budget.js";
import { readConfig } from "./io.js";
import { Recipe, RunContext, buildResult, writeOutputs } from "./recipe.js";
import { HttpRequest, HttpResponse, Transport } from "./transport.js";

export const TEST_KEY = "fbl_dev_TESTKEY0000000000000000";

/** tests/fixtures/<recipe> in the repository that contains `start` (default: this file). */
export function fixturesDir(recipe: string, start?: string): string {
  let d = resolve(start ?? dirname(fileURLToPath(import.meta.url)));
  for (;;) {
    if (existsSync(join(d, "tests", "fixtures")) && existsSync(join(d, "recipes"))) return join(d, "tests", "fixtures", recipe);
    const up = dirname(d);
    if (up === d) throw new Error("Could not find the repository's tests/fixtures folder.");
    d = up;
  }
}

export class FixtureError extends Error {}

interface Exchange { request: Json; response: Json; times?: number; used: number }

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null) return false;
  if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  if (typeof a === "object") {
    const ka = Object.keys(a as object), kb = Object.keys(b as object);
    return ka.length === kb.length && ka.every((k) => deepEqual((a as Json)[k], (b as Json)[k]));
  }
  return false;
}

export class FixtureTransport implements Transport {
  exchanges: Exchange[];
  sent: HttpRequest[] = [];

  constructor(scenario: Json | string, readonly expectedKey: string = TEST_KEY) {
    const s = typeof scenario === "string" ? JSON.parse(readFileSync(scenario, "utf-8")) : scenario;
    this.exchanges = (s.exchanges ?? []).map((e: Json) => ({ ...e, used: 0 }));
  }

  async send(request: HttpRequest): Promise<HttpResponse> {
    if (request.headers.Authorization !== `Bearer ${this.expectedKey}`) throw new FixtureError("request did not carry the expected bearer token");
    this.sent.push(request);
    for (const e of this.exchanges) {
      if (e.used >= (e.times ?? 1)) continue;
      const want = e.request;
      if ((want.method ?? "GET") !== request.method || want.path !== request.path) continue;
      if ("query" in want) {
        const q: Record<string, string> = {};
        for (const [k, v] of Object.entries(want.query)) q[k] = String(v);
        if (!deepEqual(q, request.query)) continue;
      }
      if ("body" in want && !deepEqual(want.body, request.jsonBody)) continue;
      e.used += 1;
      const resp = e.response;
      if (resp.timeout) throw new TransportTimeoutError(`No response within ${fmt(request.timeout)} seconds.`);
      const text = "text" in resp ? resp.text : JSON.stringify(resp.json ?? null);
      const headers: Record<string, string> = {};
      for (const [k, v] of Object.entries(resp.headers ?? {})) headers[k.toLowerCase()] = String(v);
      return { status: resp.status ?? 200, headers, text };
    }
    throw new FixtureError(`no fixture for ${request.method} ${request.path} query=${JSON.stringify(request.query)} body=${JSON.stringify(request.jsonBody)}`);
  }

  unused(): Json[] {
    return this.exchanges.filter((e) => e.used < (e.times ?? 1)).map((e) => e.request);
  }
}

export function makeClient(scenario: Json | string, maxCredits: number | null = 100.0, timeout = 30.0): [FirmableClient, FixtureTransport] {
  const transport = new FixtureTransport(scenario);
  const client = new FirmableClient(TEST_KEY, { budget: new CreditBudget(maxCredits), transport, timeout, minInterval: 0 });
  return [client, transport];
}

export async function runWithFixtures(recipe: Recipe, o: {
  inputPath: string; configPath: string; scenario: Json | string; outDir?: string; maxCredits?: number | null; today?: string;
}): Promise<[Json, FixtureTransport]> {
  const config = recipe.loadConfig(readConfig(o.configPath));
  const rows = recipe.readInput(o.inputPath);
  const [client, transport] = makeClient(o.scenario, o.maxCredits === undefined ? 100.0 : o.maxCredits);
  const ctx = new RunContext(client, config, rows, o.today);
  const output = await recipe.run(ctx);
  const result = buildResult(recipe, ctx, output, { inputPath: o.inputPath, config, mode: "test-fixture" });
  if (o.outDir) writeOutputs(recipe, result, o.outDir);
  return [result, transport];
}
