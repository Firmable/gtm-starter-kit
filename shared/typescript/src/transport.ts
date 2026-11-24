/**
 * HTTP transport using Node's built-in fetch. Redirects are not followed (redirect: "manual"), so
 * the bearer token is never forwarded, and every request has an explicit timeout.
 * Tests replace it with FixtureTransport from ./testing.js; no test calls production.
 */
import { TransportError, TransportTimeoutError } from "./errors.js";
import { fmt } from "./budget.js";

export const BASE_URL = "https://api.firmable.com";

export interface HttpRequest {
  method: string;
  path: string;
  query: Record<string, string>;
  jsonBody: unknown;
  headers: Record<string, string>;
  timeout: number; // seconds
}

export interface HttpResponse {
  status: number;
  headers: Record<string, string>;
  text: string;
}

export interface Transport {
  send(request: HttpRequest): Promise<HttpResponse>;
}

export function requestUrl(req: HttpRequest): string {
  const qs = new URLSearchParams(req.query).toString();
  return `${BASE_URL}${req.path}${qs ? `?${qs}` : ""}`;
}

export class FetchTransport implements Transport {
  async send(req: HttpRequest): Promise<HttpResponse> {
    const headers: Record<string, string> = { ...req.headers };
    let body: string | undefined;
    if (req.jsonBody !== null && req.jsonBody !== undefined) {
      body = JSON.stringify(req.jsonBody);
      headers["Content-Type"] = "application/json";
    }
    let res: Response;
    try {
      res = await fetch(requestUrl(req), {
        method: req.method,
        headers,
        body,
        redirect: "manual",
        signal: AbortSignal.timeout(req.timeout * 1000),
      });
    } catch (e) {
      const name = (e as { name?: string })?.name;
      if (name === "TimeoutError" || name === "AbortError") {
        throw new TransportTimeoutError(`No response within ${fmt(req.timeout)} seconds.`);
      }
      throw new TransportError(`Could not reach the API: ${(e as Error)?.message ?? String(e)}`);
    }
    const out: Record<string, string> = {};
    res.headers.forEach((v, k) => (out[k.toLowerCase()] = v));
    let text = "";
    try {
      text = await res.text();
    } catch (e) {
      const name = (e as { name?: string })?.name;
      if (name === "TimeoutError" || name === "AbortError") {
        throw new TransportTimeoutError(`No response within ${fmt(req.timeout)} seconds.`);
      }
      throw new TransportError(`Could not read the response: ${(e as Error)?.message ?? String(e)}`);
    }
    return { status: res.status, headers: out, text };
  }
}
