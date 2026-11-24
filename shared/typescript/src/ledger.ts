/**
 * Request and response ledger. Never stores the Authorization header; any occurrence of the API key
 * in a body is replaced before storage.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const REDACTED = "[REDACTED]";

export function redact<T>(value: T, secret: string | null): T {
  if (!secret) return value;
  if (typeof value === "string") return value.split(secret).join(REDACTED) as unknown as T;
  if (Array.isArray(value)) return value.map((v) => redact(v, secret)) as unknown as T;
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = redact(v, secret);
    return out as T;
  }
  return value;
}

export type LedgerEntry = Record<string, unknown> & { seq: number };

export class Ledger {
  entries: LedgerEntry[] = [];
  constructor(private readonly secret: string | null = null) {}

  record(entry: Record<string, unknown>): LedgerEntry {
    const e: Record<string, unknown> = { seq: this.entries.length + 1, ...entry };
    delete e.headers;
    const clean = redact(e, this.secret) as LedgerEntry;
    this.entries.push(clean);
    return clean;
  }

  write(directory: string): string {
    mkdirSync(directory, { recursive: true });
    const path = join(directory, "ledger.json");
    writeFileSync(path, JSON.stringify(this.entries, null, 2) + "\n", "utf-8");
    return path;
  }
}
