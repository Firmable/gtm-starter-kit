/**
 * Tech sales opportunities: finds the technologies a seller cares about in Firmable Tech Intel.
 * Mirrors recipes/tech-sales-opportunities/python/src/tech_sales_opportunities/recipe.py; see that
 * file and the recipe README for the matching rules and target results.
 */
import {
  ConfigError, FATAL_KINDS, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note, pricing,
  pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "tech-sales-opportunities";
export const TITLE = "Tech sales opportunities";
export const DESCRIPTION = "Finds the technologies in the configuration in each company's Firmable Tech Intel, with " +
  "confidence, recency, observation dates and evidence locations, and reports what was not " +
  "observed in retrieved data.";

const STATUS_TONE: Record<string, string> = {
  matched: "good", not_observed: "neutral", not_observed_incomplete: "review", no_technology_data: "unknown",
  company_not_found: "unknown", access_denied: "error", invalid_input: "error", error: "error", not_processed: "unknown",
};
const RESULT_TONE: Record<string, string> = {
  observed: "good", not_observed: "neutral", not_observed_in_pages_retrieved: "review", not_checked: "error",
};
const RESULT_TEXT: Record<string, string> = {
  observed: "Observed in retrieved data.",
  not_observed: "Not observed in retrieved data.",
  not_observed_in_pages_retrieved: "Not observed in the pages retrieved. The technology list is incomplete.",
  not_checked: "Not checked: no Tech Intel data was retrieved.",
};
const FLAG_TEXT: Record<string, string> = {
  no_location: "Evidence record has no location.",
  location_differs_from_company_domain: "Evidence location differs from the company domain.",
  location_not_a_domain: "Evidence location is not a domain or URL, so it could not be compared with " +
    "the company domain.",
};
const STOP_TEXT: Record<string, string> = {
  page_limit: "the page limit was reached",
  error: "a later page failed",
  budget: "the credit limit was reached",
  repeated_cursor: "the API returned a cursor it had already returned",
};
const ID = /^[a-z0-9_]{1,40}$/;
const TOP_KEYS = new Set(["targets", "with_evidence", "max_pages", "fetch_company"]);
const TARGET_KEYS = new Set(["id", "label", "relationship", "match"]);
const MATCH_KEYS = new Set(["product_slugs", "vendor_slugs", "aliases"]);
const MAX_PAGES_LIMIT = 100;

const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x);
const s = (v: unknown): string => (v === null || v === undefined ? "" : String(v));

function textList(value: unknown, where: string): string[] {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value) || !value.every((v) => typeof v === "string" && v.trim())) {
    throw new ConfigError(`${where} must be a list of non-empty text values.`);
  }
  return value.map((v: string) => v.trim());
}

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const targetsRaw = raw.targets;
  if (!Array.isArray(targetsRaw) || !targetsRaw.length) throw new ConfigError("targets must be a non-empty list.");
  const targets: Json[] = [];
  const seen = new Set<string>();
  targetsRaw.forEach((t: Json, idx: number) => {
    const i = idx + 1;
    if (!isObj(t)) throw new ConfigError(`targets[${i}] must be an object.`);
    let extra = Object.keys(t).filter((k) => !TARGET_KEYS.has(k)).sort();
    if (extra.length) throw new ConfigError(`targets[${i}] has unknown key(s): ${extra.join(", ")}`);
    const tid = t.id;
    if (typeof tid !== "string" || !ID.test(tid)) throw new ConfigError(`targets[${i}].id must be 1 to 40 lower-case letters, digits or underscores.`);
    if (seen.has(tid)) throw new ConfigError(`targets[${i}].id ${pyRepr(tid)} is used twice.`);
    seen.add(tid);
    const label = t.label ?? tid;
    if (typeof label !== "string" || !label.trim()) throw new ConfigError(`targets[${i}].label must be text.`);
    const rel = t.relationship;
    if (typeof rel !== "string" || !rel.trim()) throw new ConfigError(`targets[${i}].relationship must be text, such as competitor or integration.`);
    const match = t.match;
    if (!isObj(match)) throw new ConfigError(`targets[${i}].match must be an object.`);
    extra = Object.keys(match).filter((k) => !MATCH_KEYS.has(k)).sort();
    if (extra.length) throw new ConfigError(`targets[${i}].match has unknown key(s): ${extra.join(", ")}`);
    const m: Record<string, string[]> = {};
    for (const k of ["product_slugs", "vendor_slugs", "aliases"]) m[k] = textList(match[k], `targets[${i}].match.${k}`);
    if (!(m.product_slugs.length || m.vendor_slugs.length || m.aliases.length)) {
      throw new ConfigError(`targets[${i}].match needs at least one product slug, vendor slug or alias.`);
    }
    targets.push({ id: tid, label: label.trim(), relationship: rel.trim(), match: m });
  });
  for (const [key, def] of [["with_evidence", true], ["fetch_company", false]] as [string, boolean][]) {
    if (typeof (raw[key] ?? def) !== "boolean") throw new ConfigError(`${key} must be true or false.`);
  }
  const maxPages = raw.max_pages ?? 3;
  if (typeof maxPages !== "number" || !Number.isInteger(maxPages) || maxPages < 1 || maxPages > MAX_PAGES_LIMIT) {
    throw new ConfigError(`max_pages must be a whole number from 1 to ${MAX_PAGES_LIMIT}.`);
  }
  return { targets, with_evidence: raw.with_evidence ?? true, max_pages: maxPages, fetch_company: raw.fetch_company ?? false };
}

export function readInput(path: string): Row[] {
  return readRows(path, { required: ["domain"] });
}

function validUniqueDomains(rows: Row[]): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const d = normaliseDomain(r.domain);
    if (d && !out.includes(d)) out.push(d);
  }
  return out;
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const n = validUniqueDomains(rows).length;
  const pages: number = config.max_pages;
  const calls: PlannedCall[] = [];
  if (config.fetch_company) {
    calls.push(new PlannedCall("GET /company", "Company profile for each unique valid domain", n,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * n)]]));
  }
  const parts: [string, number][] = [["tech_intel", pricing.round4(pricing.TECH_INTEL_COMPANY * n)]];
  if (config.with_evidence) parts.push(["tech_intel_evidence", pricing.round4(pricing.TECH_INTEL_EVIDENCE_REQUEST * n * pages)]);
  calls.push(new PlannedCall("GET /tech-intel", `Up to ${pages} page(s) of Tech Intel for each unique valid domain`, n * pages, parts));
  return calls;
}

function matchBasis(item: Json, match: Record<string, string[]>): string[] {
  const basis: string[] = [];
  if (typeof item.product_slug === "string" && match.product_slugs.includes(item.product_slug)) basis.push(`product_slug ${item.product_slug}`);
  if (typeof item.vendor_slug === "string" && match.vendor_slugs.includes(item.vendor_slug)) basis.push(`vendor_slug ${item.vendor_slug}`);
  const aliases = match.aliases.map((a) => a.toLowerCase());
  for (const field of ["product", "vendor"]) {
    const value = item[field];
    if (typeof value === "string" && aliases.includes(value.trim().toLowerCase())) basis.push(`alias of ${field} name ${value}`);
  }
  return basis;
}

const sameDomain = (host: string, domains: string[]) => domains.some((d) => host === d || host.endsWith("." + d));

function evidence(item: Json, withEvidence: boolean, domains: string[]): Json {
  if (!withEvidence) return { status: "not_requested" };
  const ev = item.evidence_detail;
  if (!isObj(ev)) return { status: "none_returned", notes: ["No evidence was returned for this technology."] };
  const records: Json[] = [];
  for (const rec of ev.records ?? []) {
    if (!isObj(rec)) continue;
    const location = rec.location;
    const flags: string[] = [];
    let host: string | null = null;
    if (typeof location !== "string" || !location.trim()) {
      flags.push("no_location");
    } else {
      host = normaliseDomain(location);
      if (host === null) flags.push("location_not_a_domain");
      else if (!sameDomain(host, domains)) flags.push("location_differs_from_company_domain");
    }
    records.push({ api_record: rec, location_host: host, flags, flag_messages: flags.map((f) => FLAG_TEXT[f]) });
  }
  const byType: Json[] = (ev.evidence_by_type ?? []).filter((t: unknown) => isObj(t));
  const capped: string[] = byType.filter((t) => t.capped === true && typeof t.type === "string").map((t) => t.type);
  const count = ev.evidence_count ?? null;
  const notes: string[] = [];
  if (typeof count === "number" && Number.isInteger(count) && records.length < count) {
    notes.push(`${records.length} of ${count} evidence record(s) are listed. The API lists up to 25 records per type.`);
  }
  if (capped.length) notes.push(`The source reported more evidence than it sent for: ${capped.join(", ")}. Those counts are minimums.`);
  return { status: "returned", confidence: ev.confidence ?? null, first_seen: ev.first_seen ?? null,
    last_seen: ev.last_seen ?? null, evidence_count: count, evidence_by_type: byType, records_listed: records.length,
    capped_types: capped, records, notes };
}

const TECH_KEYS = ["product", "product_slug", "vendor", "vendor_slug", "filter", "filter_slug", "category",
  "category_slug", "pillar_slug", "confidence", "recency", "last_observed", "nature"];
function technology(item: Json): Json {
  const out: Json = {};
  for (const k of TECH_KEYS) out[k] = item[k] ?? null;
  return out;
}

function recordError(ctx: RunContext, rowId: string | null, err: Json, stop = false): Json {
  const entry = { row_id: rowId, ...err };
  ctx.errors.push(entry);
  if ((stop || FATAL_KINDS.has(err.kind)) && ctx.stopped === null) ctx.stopped = entry;
  return entry;
}

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = [
    "A target that was not matched is not observed in retrieved data. This does not mean the company " +
      "does not use it.",
    "Tech Intel reports observed technology signals with Firmable's confidence and recency. It does not " +
      "report replacement intent, spend, licence count or active usage, and a detected technology does not " +
      "prove buying intent.",
    "Matching uses only the product slugs, vendor slugs and exact aliases in the configuration. There is " +
      "no fuzzy matching.",
  ];
  if (!cfg.with_evidence) coverage.push("Evidence was not requested (with_evidence is false), so no evidence locations are shown.");

  const keyed = ctx.rows.map((r) => ({ ...r, _domain: normaliseDomain(r.domain) ?? "" })) as Row[];
  const dupes = findDuplicates(keyed, "_domain");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier domain; their API responses were reused, not requested again.`);

  const one = async (row: Row): Promise<Json> => {
    const domain = normaliseDomain(row.domain);
    const base: Json = { row_id: row.row_id, input: row, domain, duplicate_of: dupes[row.row_id] ?? null, company: null,
      tech_intel: null, targets: [], evidence_flag_counts: {} };
    if (!domain) return { ...base, status: "invalid_input", status_reason: `${pyRepr(row.domain)} is not a valid domain.` };
    let company: Json = null;
    let identifier: Record<string, string> = { fqdn: domain };
    if (cfg.fetch_company) {
      try {
        company = await client.getCompany({ fqdn: domain });
      } catch (e) {
        if (e instanceof NotFoundError) {
          return { ...base, status: "company_not_found", status_reason: "Firmable has no company profile for this domain, so Tech Intel was not requested." };
        }
        throw e;
      }
      base.company = facts.companyFacts(company);
      identifier = { id: company.id };
    }
    const domains = [domain];
    if (company) {
      for (const value of [company.fqdn, company.website]) {
        const d = typeof value === "string" ? normaliseDomain(value) : null;
        if (d && !domains.includes(d)) domains.push(d);
      }
    }

    const ti = await client.getTechIntel(identifier, { withEvidence: cfg.with_evidence, maxPages: cfg.max_pages });
    const err: Json = ti.error;
    let entry: Json = null;
    if (err) entry = recordError(ctx, row.row_id, err, err.kind === "forbidden");
    const items: Json[] = ti.items.filter((i) => isObj(i));
    const total = ti.total;
    base.tech_intel = { company_id: ti.id, total, retrieved: items.length, pages_fetched: ti.pages_fetched,
      complete: ti.complete, stop_reason: ti.stop_reason, error: entry };
    const targetsOut: Json[] = cfg.targets.map((t: Json) => ({ target_id: t.id, label: t.label, relationship: t.relationship }));
    if (ti.pages_fetched === 0) {
      for (const t of targetsOut) Object.assign(t, { result: "not_checked", result_text: RESULT_TEXT.not_checked, matches: [] });
      base.targets = targetsOut;
      const kind = err?.kind;
      if (kind === "not_found") return { ...base, status: "company_not_found", status_reason: "Tech Intel has no company for this identifier (404)." };
      if (kind === "forbidden") {
        return { ...base, status: "access_denied", status_reason: "Firmable returned 403 Forbidden for Tech Intel. This API key cannot use this endpoint. " +
          "No further rows were requested." };
      }
      return { ...base, status: "error", status_reason: `Tech Intel could not be read: ${err?.message}` };
    }

    const complete = ti.complete;
    const flagCounts: Record<string, number> = {};
    for (const k of Object.keys(FLAG_TEXT)) flagCounts[k] = 0;
    cfg.targets.forEach((t: Json, idx: number) => {
      const out = targetsOut[idx];
      const matches: Json[] = [];
      for (const item of items) {
        const basis = matchBasis(item, t.match);
        if (basis.length) {
          const ev = evidence(item, cfg.with_evidence, domains);
          for (const rec of ev.records ?? []) for (const f of rec.flags) flagCounts[f] += 1;
          matches.push({ match_basis: basis, technology: technology(item), evidence: ev });
        }
      }
      const result = matches.length ? "observed" : (complete ? "not_observed" : "not_observed_in_pages_retrieved");
      Object.assign(out, { result, result_text: RESULT_TEXT[result], matches });
    });
    base.targets = targetsOut;
    base.evidence_flag_counts = Object.fromEntries(Object.entries(flagCounts).filter(([, v]) => v));

    const observed = targetsOut.filter((t) => t.result === "observed");
    const n = targetsOut.length;
    const totalText = typeof total === "number" && Number.isInteger(total) ? String(total) : "an unknown number of";
    let tail = "";
    if (!complete) {
      const why = STOP_TEXT[ti.stop_reason] ?? ti.stop_reason;
      tail = ` The technology list is incomplete because ${why}: ${items.length} of ${totalText} ` +
        `technologies were retrieved in ${ti.pages_fetched} page(s).`;
    }
    if (observed.length) {
      const labels = observed.map((t) => `${t.label} (${t.relationship})`).join(", ");
      return { ...base, status: "matched", status_reason: `${observed.length} of ${n} target(s) observed in retrieved data: ${labels}.${tail}` };
    }
    if (complete && total === 0) {
      return { ...base, status: "no_technology_data", status_reason: "Firmable holds no technology data for this company (total 0), so none of the " +
        `${n} target(s) were observed in retrieved data.` };
    }
    if (complete) {
      return { ...base, status: "not_observed", status_reason: `None of the ${n} target(s) were observed in retrieved data. All ${totalText} ` +
        "technologies were retrieved." };
    }
    return { ...base, status: "not_observed_incomplete", status_reason: `None of the ${n} target(s) were observed in the pages retrieved.${tail}` };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const targetCounts: Record<string, Record<string, number>> = {};
  const flagsTotal: Record<string, number> = {};
  for (const k of Object.keys(FLAG_TEXT)) flagsTotal[k] = 0;
  let incomplete = 0;
  for (const r of rows) {
    for (const t of r.targets ?? []) {
      if (!targetCounts[t.target_id]) {
        targetCounts[t.target_id] = {};
        for (const k of Object.keys(RESULT_TEXT)) targetCounts[t.target_id][k] = 0;
      }
      targetCounts[t.target_id][t.result] += 1;
    }
    for (const [k, v] of Object.entries(r.evidence_flag_counts ?? {})) flagsTotal[k] += v as number;
    const ti = r.tech_intel ?? {};
    if (ti.pages_fetched && !ti.complete) incomplete += 1;
  }
  if (incomplete) {
    coverage.push(`${incomplete} row(s) have an incomplete technology list (max_pages is ${cfg.max_pages}). ` +
      "Targets not matched for them are not observed in the pages retrieved.");
  }
  if (flagsTotal.no_location) {
    coverage.push(`${flagsTotal.no_location} evidence record(s) have no location, so their source cannot be shown.`);
  }
  if (flagsTotal.location_differs_from_company_domain) {
    coverage.push(`${flagsTotal.location_differs_from_company_domain} evidence record(s) have a location ` +
      "that differs from the company domain. They are shown as returned, not dropped. Job ads " +
      "and review sites are often on other domains.");
  }
  if (flagsTotal.location_not_a_domain) {
    coverage.push(`${flagsTotal.location_not_a_domain} evidence record(s) have a location that is not a domain or URL.`);
  }
  return { rows, summary: { status_counts: counts, target_counts: targetCounts,
    evidence_flag_counts: Object.fromEntries(Object.entries(flagsTotal).filter(([, v]) => v)) }, coverage };
}

export const CSV_COLUMNS = ["row_id", "domain", "duplicate_of", "status", "status_reason", "company_id", "company_name",
  "technologies_total", "technologies_retrieved", "pages_fetched", "list_complete",
  "targets_observed", "target_results", "observed_technologies", "evidence_flags"];

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const targets: Json[] = r.targets ?? [];
    const company = r.company ?? {};
    const ti = r.tech_intel ?? {};
    const observedTech: string[] = [];
    for (const t of targets) {
      for (const m of t.matches ?? []) {
        const tech = m.technology;
        observedTech.push(`${t.target_id}:${s(tech.product)}(${s(tech.confidence)},${s(tech.recency)},${s(tech.last_observed)})`);
      }
    }
    return {
      row_id: r.row_id, domain: r.domain || r.input.domain, duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      company_id: company.id || ti.company_id || null, company_name: company.name ?? null,
      technologies_total: ti.total ?? null, technologies_retrieved: ti.retrieved ?? null,
      pages_fetched: ti.pages_fetched ?? null, list_complete: ti.complete ?? null,
      targets_observed: targets.filter((t) => t.result === "observed").map((t) => t.target_id),
      target_results: targets.map((t) => `${t.target_id}=${t.result}`),
      observed_technologies: observedTech,
      evidence_flags: Object.entries(r.evidence_flag_counts ?? {}).map(([k, v]) => `${k}=${v}`),
    };
  });
}

function evidenceHtml(ev: Json): string {
  if (ev.status === "not_requested") return '<p class="muted">Evidence was not requested.</p>';
  if (ev.status === "none_returned") return note("No evidence was returned for this technology.", "unknown");
  let html = kv([["Evidence confidence", ev.confidence], ["First seen", ev.first_seen], ["Last seen", ev.last_seen],
    ["Evidence count", ev.evidence_count], ["Records listed", ev.records_listed],
    ["By type", (ev.evidence_by_type ?? []).map((t: Json) => `${t.type}: ${t.count}` + (t.capped ? " (minimum)" : "")).join(", ") || null]]);
  for (const n of ev.notes ?? []) html += note(esc(n), "unknown");
  const recs: Json[] = ev.records ?? [];
  if (recs.length) {
    html += table(["Type", "Found", "Location", "Record", "Belongs because", "Seen", "Confidence", "Flags"], recs.map((rec) => {
      const a = rec.api_record;
      return [a.type, a.found, a.location, a.record, (a.belongs_because ?? []).join(", "),
        `${a.first_seen || "?"} to ${a.last_seen || "?"}`, a.confidence,
        new Raw(rec.flag_messages.map((m: string) => badge(m, "review")).join(" "))];
    }));
  }
  return html;
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Technology match summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("Not observed means not observed in retrieved data. It does not mean the company does not " +
      "use the technology. A detected technology does not prove buying intent, spend, licence " +
      "count or active usage.", "unknown"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const title = r.company?.name || r.domain || r.input.domain || r.row_id;
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let left = `<p>${esc(reason)}</p>`;
    const ti = r.tech_intel;
    if (ti) {
      left += kv([["Firmable ID", ti.company_id], ["Technologies in Firmable", ti.total], ["Technologies retrieved", ti.retrieved],
        ["Pages fetched", ti.pages_fetched], ["List complete", ti.complete], ["Stopped because", ti.stop_reason]]);
      if (ti.error) left += note(esc(ti.error.message), "error");
    }
    if (r.duplicate_of) left += note(`Same domain as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    const targets: Json[] = r.targets ?? [];
    let right = "";
    if (targets.length) {
      right += table(["Target", "Relationship", "Result"], targets.map((t) => [t.label, t.relationship,
        new Raw(badge(t.result.replace(/_/g, " "), RESULT_TONE[t.result] ?? "neutral"))]));
      for (const t of targets) {
        for (const m of t.matches ?? []) {
          const tech = m.technology;
          right += `<h3>${esc(t.label)}: ${esc(tech.product)}</h3>` +
            kv([["Vendor", tech.vendor], ["Category", tech.category], ["Filter", tech.filter], ["Confidence", tech.confidence],
              ["Recency", tech.recency], ["Last observed", tech.last_observed], ["Nature", tech.nature],
              ["Matched by", m.match_basis.join("; ")]]) +
            evidenceHtml(m.evidence);
        }
      }
      const missing = targets.filter((t) => t.result !== "observed").map((t) => t.label);
      if (missing.length) right += "<h3>Not observed in retrieved data</h3>" + bulletList(missing);
      right += detailsJson("Target results as JSON", targets);
    }
    cards.push(card(title, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), left,
      right || '<p class="muted">No Tech Intel was retrieved for this row.</p>', `Row ${r.row_id}`));
  }
  report.add("Accounts", "Each account with its target technologies", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
