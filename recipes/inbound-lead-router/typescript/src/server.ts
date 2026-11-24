#!/usr/bin/env node
/**
 * Single-lead routing server: a local example, not a production service.
 * Mirrors recipes/inbound-lead-router/python/src/inbound_lead_router/server.py.
 *
 *   node typescript/dist/src/cli.js serve --config config.json --max-credits-per-lead 2 --port 8787
 *   node typescript/dist/src/server.js --config config.json --max-credits-per-lead 2 --port 8787
 *
 * - Binds 127.0.0.1 only and sends no CORS headers.
 * - POST /route with a JSON body holding one lead (16 KB at most, else 413). Other paths answer 404
 *   and other methods 405.
 * - Needs FIRMABLE_API_KEY. Every request gets a fresh client and a fresh credit budget of
 *   --max-credits-per-lead.
 * - Answers with the same row object the batch run returns. The request ledger, the request headers
 *   and the key are never sent back.
 *
 * `handleRequest` holds all request handling and opens no socket, so tests call it directly.
 * Node standard library only.
 */
import { createServer, IncomingMessage, Server, ServerResponse } from "node:http";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { ConfigError, CreditBudget, DEFAULT_TIMEOUT, FirmableClient, Json, Row, RunContext, readConfig } from "firmable-gtm-client";
import { RECIPE } from "./recipe.js";

export const MAX_BODY = 16 * 1024;
export const HOST = "127.0.0.1";
export type ClientFactory = () => FirmableClient;
export interface Answer { status: number; body: Json }

const error = (status: number, code: string, message: string): Answer => ({ status, body: { error: { code, message } } });

/** Turns a JSON object into an input row the same way the batch input reader does. */
export function leadFromJson(data: unknown): [Row | null, string | null] {
  if (!data || typeof data !== "object" || Array.isArray(data)) return [null, "The body must be a JSON object holding one lead."];
  const row: Record<string, string> = {};
  for (const [k, v] of Object.entries(data as Record<string, unknown>)) {
    if (v !== null && typeof v === "object") return [null, `Field '${k}' must be text, a number, true, false or null.`];
    row[k.trim()] = v === null || v === undefined ? "" : typeof v === "boolean" ? (v ? "true" : "false") : String(v).trim();
  }
  if (!row.work_email && !row.domain) return [null, "The lead needs work_email or domain."];
  row.row_id = row.row_id || "row-1";
  return [row as Row, null];
}

/** Routes one lead. Returns the HTTP status and JSON body. Opens no socket. */
export async function handleRequest(method: string, target: string, body: Buffer | string,
  o: { config: Json; clientFactory: ClientFactory }): Promise<Answer> {
  const path = new URL(target, "http://127.0.0.1").pathname;
  if (path !== "/route") return error(404, "not_found", "The only path is POST /route.");
  if (method !== "POST") return error(405, "method_not_allowed", "Use POST /route.");
  const bytes = typeof body === "string" ? Buffer.from(body, "utf-8") : body;
  if (bytes.length > MAX_BODY) return error(413, "body_too_large", `The body must be ${MAX_BODY} bytes or fewer.`);
  let data: unknown = null;
  try {
    data = bytes.length ? JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) : null;
  } catch {
    return error(400, "bad_json", "The body is not valid JSON.");
  }
  const [row, problem] = leadFromJson(data);
  if (row === null) return error(400, "bad_lead", problem ?? "");
  const ctx = new RunContext(o.clientFactory(), o.config, [row]);
  const output = await RECIPE.run(ctx);
  return { status: 200, body: output.rows[0] };
}

function send(res: ServerResponse, answer: Answer, head: boolean): void {
  const data = Buffer.from(JSON.stringify(answer.body), "utf-8");
  const headers: Record<string, string | number> = { "Content-Type": "application/json; charset=utf-8", "Content-Length": data.length };
  if (answer.status === 405) headers.Allow = "POST";
  res.writeHead(answer.status, headers);
  res.end(head ? undefined : data);
}

/** An HTTP server for 127.0.0.1. Call listen(port, HOST); port 0 picks a free port. */
export function makeServer(config: Json, clientFactory: ClientFactory): Server {
  return createServer((req: IncomingMessage, res: ServerResponse) => {
    const method = req.method ?? "GET";
    const target = req.url ?? "/";
    const declared = Number(req.headers["content-length"] ?? 0);
    if (Number.isFinite(declared) && declared > MAX_BODY) {
      res.setHeader("Connection", "close");
      send(res, error(413, "body_too_large", `The body must be ${MAX_BODY} bytes or fewer.`), method === "HEAD");
      req.resume();
      return;
    }
    const chunks: Buffer[] = [];
    let size = 0;
    let tooBig = false;
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) tooBig = true;
      else chunks.push(c);
    });
    req.on("end", () => {
      if (tooBig) {
        send(res, error(413, "body_too_large", `The body must be ${MAX_BODY} bytes or fewer.`), method === "HEAD");
        return;
      }
      handleRequest(method, target, Buffer.concat(chunks), { config, clientFactory }).then(
        (answer) => {
          send(res, answer, method === "HEAD");
          process.stderr.write(`${method} ${new URL(target, "http://127.0.0.1").pathname} ${answer.status}\n`);
        },
        () => send(res, error(500, "internal_error", "The lead could not be routed."), method === "HEAD"),
      );
    });
  });
}

const USAGE = "usage: gtm-inbound-lead-router serve --config FILE --max-credits-per-lead N [--port P] [--timeout S]\n\n" +
  "Local example server: routes one lead per POST /route.\n\n" +
  "  --config FILE                JSON configuration file\n" +
  "  --max-credits-per-lead N     credit limit for each request (each request gets a fresh budget)\n" +
  "  --port P                     port on 127.0.0.1 (default: 8787)\n" +
  "  --timeout S                  seconds to wait for each API response (default: 30)\n";

/** Starts the server. Resolves with an exit code when argument checks fail; otherwise runs until stopped. */
export async function serveMain(argv: string[]): Promise<number> {
  let configPath: string | undefined;
  let limit: number | null = null;
  let port = 8787;
  let timeout = DEFAULT_TIMEOUT;
  let config: Json;
  try {
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
      if (k === "--config") configPath = next();
      else if (k === "--max-credits-per-lead") limit = num(next());
      else if (k === "--port") port = num(next());
      else if (k === "--timeout") timeout = num(next());
      else if (k === "--help" || k === "-h") { process.stdout.write(USAGE); return 0; }
      else throw new ConfigError(`Unknown argument: ${k}`);
    }
    if (!configPath) throw new ConfigError("--config is required.");
    if (limit === null) throw new ConfigError("--max-credits-per-lead is required.");
    config = RECIPE.loadConfig(readConfig(configPath));
    if (limit < 0) throw new ConfigError("--max-credits-per-lead must be zero or more.");
    if (timeout <= 0) throw new ConfigError("--timeout must be positive.");
    if (!Number.isInteger(port) || port < 0 || port > 65535) throw new ConfigError("--port must be between 0 and 65535.");
  } catch (e) {
    if (e instanceof ConfigError) {
      process.stderr.write(`error: ${e.message}\n`);
      return 2;
    }
    throw e;
  }
  const apiKey = (process.env.FIRMABLE_API_KEY ?? "").trim();
  if (!apiKey) {
    process.stderr.write("error: set FIRMABLE_API_KEY to your developer API key (it starts with fbl_dev_).\n");
    return 2;
  }
  const perLead = limit;
  const server = makeServer(config, () => new FirmableClient(apiKey, { budget: new CreditBudget(perLead), timeout }));
  await new Promise<void>((resolve) => server.listen(port, HOST, resolve));
  const addr = server.address();
  const bound = typeof addr === "object" && addr ? addr.port : port;
  console.log(`Routing leads at http://${HOST}:${bound}/route (local example, not a production service). ` +
    `Each lead may use up to ${perLead} credits. Press Ctrl+C to stop.`);
  process.once("SIGINT", () => server.close());
  await new Promise<void>((resolve) => server.on("close", resolve));
  return 0;
}

const invokedDirectly = (() => {
  try {
    return !!process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
})();
if (invokedDirectly) {
  serveMain(process.argv.slice(2)).then((code) => { process.exitCode = code; }, (e) => {
    process.stderr.write(`error: ${(e as Error)?.stack ?? String(e)}\n`);
    process.exitCode = 1;
  });
}
