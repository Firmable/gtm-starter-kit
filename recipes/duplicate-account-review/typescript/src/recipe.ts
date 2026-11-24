/**
 * Duplicate account review: reviews candidate duplicate pairs of company records that you supply.
 * Mirrors recipes/duplicate-account-review/python/src/duplicate_account_review/recipe.py; see that
 * file and the recipe README for the outcome rules. Nothing is merged or deleted.
 */
import { basename } from "node:path";
import {
  ConfigError, FirmableError, InputError, Json, NotFoundError, PlannedCall, Raw, Recipe, Report, RunContext, Row,
  badge, bulletList, card, decisions, detailsJson, esc, facts, findDuplicates, kv, normaliseDomain, note,
  pricing, pyRepr, readRows, table,
} from "firmable-gtm-client";

export const NAME = "duplicate-account-review";
export const TITLE = "Duplicate account review";
export const DESCRIPTION = "Reviews candidate duplicate pairs of company records that you supply, using Firmable company " +
  "profiles and the is_same_business decision tool, and explains which identifiers agree or " +
  "conflict. Nothing is merged or deleted.";
const TOOL = "is_same_business";

const STATUS_TONE: Record<string, string> = {
  likely_duplicate: "good", distinct: "neutral", needs_review: "review", incomplete: "error",
  invalid_input: "error", error: "error", not_processed: "unknown",
};
const COMPARISON_TONE: Record<string, string> = { same: "good", different: "bad", missing: "unknown" };
const COMPANY_ID = /^f\d{12}$/;
const TOP_KEYS = new Set(["with_reasoning"]);
export const IDENTIFIERS = ["abn", "acn", "nzbn", "fqdn", "linkedin", "website_domain", "name", "hq_country"];
const REGISTRY = ["abn", "acn", "nzbn"];
const STRONG_SAME = ["abn", "acn", "nzbn", "fqdn", "linkedin"];
const SIDES = ["left", "right"] as const;
const isObj = (x: unknown): x is Record<string, any> => !!x && typeof x === "object" && !Array.isArray(x);

export function loadConfig(raw: Json): Json {
  const unknown = Object.keys(raw).filter((k) => !TOP_KEYS.has(k)).sort();
  if (unknown.length) throw new ConfigError(`Unknown configuration key(s): ${unknown.join(", ")}`);
  const withReasoning = raw.with_reasoning ?? true;
  if (typeof withReasoning !== "boolean") throw new ConfigError("with_reasoning must be true or false.");
  return { with_reasoning: withReasoning };
}

export function readInput(path: string): Row[] {
  const rows = readRows(path, { anyOf: ["left_domain", "left_company_id"] });
  const problems = rows.flatMap((r, i) => (r.right_domain || r.right_company_id) ? []
    : [`row ${i + 1} (${r.row_id}): needs one of right_domain, right_company_id`]);
  if (problems.length) throw new InputError(`${basename(path)} has problems:\n  ` + problems.join("\n  "));
  return rows;
}

interface Side { by: string | null; value: string | null; input_domain: string | null; problem: string | null }

function sideOf(row: Row, side: string): Side {
  const cid = (row[`${side}_company_id`] ?? "").trim();
  const rawDomain = row[`${side}_domain`];
  const domain = normaliseDomain(rawDomain);
  if (cid) {
    if (COMPANY_ID.test(cid)) return { by: "id", value: cid, input_domain: domain, problem: null };
    return { by: null, value: null, input_domain: domain,
      problem: `${side}_company_id ${pyRepr(cid)} is not a Firmable company ID (f followed by 12 digits).` };
  }
  if (domain) return { by: "fqdn", value: domain, input_domain: domain, problem: null };
  return { by: null, value: null, input_domain: null, problem: `${side}_domain ${pyRepr(rawDomain ?? null)} is not a valid domain.` };
}

const sideKey = (s: Side): string => (s.by ? `${s.by}:${s.value}` : "");

function pairKey(row: Row): string {
  const keys = SIDES.map((side) => sideKey(sideOf(row, side)));
  if (!keys.every((k) => k)) return "";
  return [...keys].sort().join("|");
}

export function plan(rows: Row[], config: Json): PlannedCall[] {
  const sides: string[] = [];
  const pairs: string[] = [];
  for (const r of rows) {
    const keys = SIDES.map((side) => sideKey(sideOf(r, side)));
    if (!keys.every((k) => k)) continue;
    for (const k of keys) if (!sides.includes(k)) sides.push(k);
    const pk = [...keys].sort().join("|");
    if (keys[0] !== keys[1] && !pairs.includes(pk)) pairs.push(pk);
  }
  return [
    new PlannedCall("GET /decisions", `Read the decision tool list and check that ${TOOL} is available`, 1, []),
    new PlannedCall("GET /company", "Company profile for each unique valid side", sides.length,
      [["company", pricing.round4(pricing.COMPANY_PROFILE * sides.length)]]),
    new PlannedCall("POST /decisions", `${TOOL} for each unique valid pair of different identifiers`, pairs.length,
      pricing.estimateDecision(pairs.length, config.with_reasoning)),
  ];
}

function text(value: unknown): string | null {
  if (value === null || value === undefined || typeof value === "object" || typeof value === "boolean") return null;
  const s = String(value).trim();
  return s || null;
}

function identifierValues(c: Json): Record<string, Json> {
  const codes = isObj(c.industry_codes) ? c.industry_codes : {};
  return { abn: codes.abn ?? null, acn: codes.acn ?? null, nzbn: codes.nzbn ?? null, fqdn: c.fqdn ?? null,
    linkedin: c.linkedin ?? null, website_domain: c.website ?? null, name: c.name ?? null, hq_country: c.hq_country ?? null };
}

function comparable(identifier: string, value: unknown): string | null {
  let s = text(value);
  if (s === null) return null;
  if (REGISTRY.includes(identifier)) s = s.replace(/\s+/g, "");
  if (identifier === "fqdn" || identifier === "website_domain") return normaliseDomain(s);
  return s.toLowerCase();
}

function compareIdentifiers(left: Json, right: Json): Json[] {
  const lv = identifierValues(left);
  const rv = identifierValues(right);
  return IDENTIFIERS.map((ident) => {
    const a = comparable(ident, lv[ident]);
    const b = comparable(ident, rv[ident]);
    const comparison = a === null || b === null ? "missing" : a === b ? "same" : "different";
    return { identifier: ident, left: lv[ident], right: rv[ident], comparison };
  });
}

const names = (items: string[]) => (items.length ? items.join(", ") : "none");

export async function run(ctx: RunContext): Promise<{ rows: Json[]; summary: Json; coverage: string[] }> {
  const cfg = ctx.config;
  const client = ctx.client;
  const coverage: string[] = [
    "Only the pairs in the input file are compared. The recipe does not compare every row with every other row.",
    "Nothing is merged or deleted. Each outcome is a recommendation for a person to act on.",
    "A missing identifier was not observed in retrieved data. It does not mean the company has none.",
    "An unknown answer from is_same_business is never treated as distinct.",
  ];

  const stopAll = (reason: string) => {
    const rows = ctx.rows.map((r) => ({ row_id: r.row_id, input: r, status: "not_processed", reason }));
    return { rows, summary: { status_counts: { not_processed: rows.length }, answer_counts: {} }, coverage };
  };

  let catalogue: Json;
  try {
    catalogue = await client.listDecisionTools();
  } catch (e) {
    if (!(e instanceof FirmableError)) throw e;
    ctx.recordError(null, e);
    return stopAll("The decision tool list could not be read, so no pairs were reviewed.");
  }
  const problems = decisions.checkTools(catalogue, [{ tool: TOOL, inputs: { other_company_id: "" } }], "company");
  if (problems.length) {
    for (const p of problems) {
      ctx.errors.push({ row_id: null, kind: "config" as never, status: null, code: null, endpoint: "GET /decisions",
        message: p, charge_uncertain: false, retry_after: null });
    }
    coverage.push(...problems);
    return stopAll(`The decision tool list does not offer ${TOOL} as this recipe uses it.`);
  }
  if (decisions.usesModel(catalogue, TOOL)) {
    coverage.push(`${TOOL} uses a decision model inside Firmable's service, according to GET /decisions. ` +
      "This code applies only the rules in this recipe.");
  } else {
    coverage.push(`${TOOL} does not use a decision model, according to GET /decisions. Other Firmable tools may.`);
  }

  const keyed = ctx.rows.map((r) => ({ ...r, _pair: pairKey(r) })) as Row[];
  const dupes = findDuplicates(keyed, "_pair");
  const nDupes = Object.keys(dupes).length;
  if (nDupes) coverage.push(`${nDupes} row(s) repeat an earlier pair, in either order; their API responses were reused, not requested again.`);
  const asked = new Map<string, Json>();

  const one = async (row: Row): Promise<Json> => {
    const sides: Record<string, Side> = { left: sideOf(row, "left"), right: sideOf(row, "right") };
    const base: Json = {
      row_id: row.row_id, input: row, left_row_id: row.left_row_id || null, right_row_id: row.right_row_id || null,
      duplicate_of: dupes[row.row_id] ?? null, left: null, right: null, identifiers: [], supporting_identifiers: [],
      conflicting_identifiers: [], missing_identifiers: [], contradictions: [], notes: [], decision: null,
    };
    for (const side of SIDES) {
      const s = sides[side];
      base[side] = { lookup: s.by ? { by: s.by, value: s.value } : null, found: null, company: null };
    }
    const problems = SIDES.map((side) => sides[side].problem).filter((p) => p);
    if (problems.length) return { ...base, status: "invalid_input", status_reason: problems.join(" ") };

    const profiles: Record<string, Json> = {};
    for (const side of SIDES) {
      const s = sides[side];
      try {
        profiles[side] = await client.getCompany({ [s.by as string]: s.value as string });
      } catch (e) {
        if (!(e instanceof NotFoundError)) throw e;
        profiles[side] = null;
      }
      const c = profiles[side];
      const f = facts.companyFacts(c);
      if (f !== null) f.industry_codes = isObj(c.industry_codes) ? c.industry_codes : null;
      base[side] = { ...base[side], found: c !== null, company: f };
    }
    const missing = SIDES.filter((side) => profiles[side] === null);
    if (missing.length) {
      const which = missing.length === 2 ? "either side" : `the ${missing[0]} side (${sides[missing[0]].value})`;
      return { ...base, status: "needs_review", status_reason: `Firmable has no company profile for ${which}, so the pair was not compared.` };
    }

    const left = profiles.left;
    const right = profiles.right;
    const idents = compareIdentifiers(left, right);
    base.identifiers = idents;
    base.supporting_identifiers = idents.filter((i) => i.comparison === "same").map((i) => i.identifier);
    base.conflicting_identifiers = idents.filter((i) => i.comparison === "different").map((i) => i.identifier);
    base.missing_identifiers = idents.filter((i) => i.comparison === "missing").map((i) => i.identifier);
    const agree = `Identifiers that agree: ${names(base.supporting_identifiers)}. ` +
      `Identifiers that differ: ${names(base.conflicting_identifiers)}.`;

    const mismatches: string[] = [];
    for (const side of SIDES) {
      const given = sides[side].input_domain;
      const held = normaliseDomain(text(profiles[side].fqdn));
      if (given && held && given !== held) mismatches.push(`The ${side} input domain ${given} differs from the profile's fqdn ${held}.`);
    }
    base.notes = mismatches;
    if (mismatches.length) return { ...base, status: "needs_review", status_reason: mismatches.join(" ") + ` ${TOOL} was not asked.` };

    if (left.id === right.id) {
      return { ...base, status: "likely_duplicate",
        status_reason: `Both sides resolve to the same Firmable company record, ${left.id}, so ${TOOL} was not asked.` };
    }

    const pair = [left.id, right.id].sort().join("|");
    let dec: Json;
    if (asked.has(pair)) {
      dec = { ...asked.get(pair), reused_from_row: asked.get(pair).row_id };
    } else if (ctx.stopped !== null) {
      dec = { row_id: row.row_id, asked: false, answer: null, error: null };
    } else {
      const entry = { row_id: row.row_id, asked: true, company_id: left.id, other_company_id: right.id };
      try {
        const resp = await client.makeDecision(TOOL, { company_id: left.id, other_company_id: right.id }, { withReasoning: cfg.with_reasoning });
        const ans = decisions.answerFrom(resp);
        const bad = !["yes", "no", "unknown"].includes(ans.answer);
        dec = { ...entry, ...ans, error: bad ? ans.answer_error : null };
        asked.set(pair, dec);
      } catch (e) {
        if (!(e instanceof FirmableError)) throw e;
        dec = { ...entry, answer: null, error: ctx.recordError(row.row_id, e) };
      }
    }
    const { row_id: _drop, ...decision } = dec;
    void _drop;
    base.decision = decision;
    const answer = decision.answer;
    if (!decision.asked) return { ...base, status: "incomplete", status_reason: `The run stopped before ${TOOL} was asked.` };
    if ((decision.error !== null && decision.error !== undefined) || !["yes", "no", "unknown"].includes(answer)) {
      return { ...base, status: "incomplete", status_reason: `${TOOL} returned an error, so the pair has no answer.` };
    }
    if (answer === "unknown") {
      return { ...base, status: "needs_review",
        status_reason: `${TOOL} answered unknown. Unknown means Firmable could not settle the question; it is not a no. ${agree}` };
    }
    const byId: Record<string, string> = Object.fromEntries(idents.map((i) => [i.identifier, i.comparison]));
    const contradictions = answer === "yes"
      ? REGISTRY.filter((i) => byId[i] === "different").map((i) => `${i} differs`)
      : STRONG_SAME.filter((i) => byId[i] === "same").map((i) => `${i} is the same`);
    base.contradictions = contradictions;
    if (contradictions.length) {
      return { ...base, status: "needs_review",
        status_reason: `${TOOL} answered ${answer}, but profile identifiers contradict it: ${contradictions.join(", ")}.` };
    }
    if (answer === "yes") return { ...base, status: "likely_duplicate", status_reason: `${TOOL} answered yes. ${agree}` };
    return { ...base, status: "distinct", status_reason: `${TOOL} answered no. ${agree}` };
  };

  const rows = await ctx.runRows(ctx.rows, one);
  const counts: Record<string, number> = {};
  const answers: Record<string, number> = { yes: 0, no: 0, unknown: 0, error: 0, not_asked: 0 };
  for (const r of rows) {
    counts[r.status] = (counts[r.status] ?? 0) + 1;
    const d = r.decision;
    if (d) {
      if (!d.asked) answers.not_asked += 1;
      else if ((d.error !== null && d.error !== undefined) || !["yes", "no", "unknown"].includes(d.answer)) answers.error += 1;
      else answers[d.answer] += 1;
    }
  }
  return { rows, summary: { status_counts: counts, answer_counts: answers }, coverage };
}

export const CSV_COLUMNS = ["row_id", "left_row_id", "right_row_id", "left_input", "right_input", "duplicate_of", "status",
  "status_reason", "left_company_id", "left_company_name", "right_company_id", "right_company_name",
  "is_same_business", "confidence", "supporting_identifiers", "conflicting_identifiers",
  "missing_identifiers", "contradictions", "notes"];

const inputText = (row: Json, side: string): string | null => row[`${side}_company_id`] || row[`${side}_domain`] || null;

export function csvRows(result: Json): Record<string, unknown>[] {
  return result.rows.map((r: Json) => {
    const left = r.left?.company ?? {};
    const right = r.right?.company ?? {};
    const d = r.decision ?? {};
    return {
      row_id: r.row_id, left_row_id: r.input.left_row_id || null, right_row_id: r.input.right_row_id || null,
      left_input: inputText(r.input, "left"), right_input: inputText(r.input, "right"),
      duplicate_of: r.duplicate_of ?? null, status: r.status,
      status_reason: r.status_reason || r.reason || (r.error ?? {}).message || null,
      left_company_id: left.id ?? null, left_company_name: left.name ?? null,
      right_company_id: right.id ?? null, right_company_name: right.name ?? null,
      is_same_business: d.answer ?? null, confidence: d.confidence ?? null,
      supporting_identifiers: r.supporting_identifiers ?? [], conflicting_identifiers: r.conflicting_identifiers ?? [],
      missing_identifiers: r.missing_identifiers ?? [], contradictions: r.contradictions ?? [], notes: r.notes ?? [],
    };
  });
}

export function render(report: Report, result: Json): void {
  const counts: Record<string, number> = result.summary.status_counts ?? {};
  report.add("Result", "Pair review summary",
    "<p>" + Object.entries(counts).map(([k, v]) => badge(`${k.replace(/_/g, " ")}: ${v}`, STATUS_TONE[k] ?? "neutral")).join(" ") + "</p>" +
    note("These are recommendations. Nothing was merged or deleted. An unknown answer is " +
      "<strong>needs review</strong>, never distinct.", "unknown"));
  const cards: string[] = [];
  for (const r of result.rows) {
    const status: string = r.status;
    const lc = r.left?.company ?? {};
    const rc = r.right?.company ?? {};
    const lname = lc.name || inputText(r.input, "left") || "left";
    const rname = rc.name || inputText(r.input, "right") || "right";
    const reason = r.status_reason || r.reason || r.error?.message || "";
    let leftHtml = `<p>${esc(reason)}</p>`;
    leftHtml += kv([["Left input", inputText(r.input, "left")], ["Left CRM row", r.input.left_row_id], ["Left Firmable ID", lc.id],
      ["Right input", inputText(r.input, "right")], ["Right CRM row", r.input.right_row_id], ["Right Firmable ID", rc.id]]);
    if ((r.contradictions ?? []).length) leftHtml += "<h3>Contradictions</h3>" + bulletList(r.contradictions);
    if ((r.notes ?? []).length) leftHtml += "<h3>Notes</h3>" + bulletList(r.notes);
    if (r.duplicate_of) leftHtml += note(`Same pair as row ${esc(r.duplicate_of)}; responses were reused.`, "info");
    let rightHtml = "";
    const idents: Json[] = r.identifiers ?? [];
    if (idents.length) {
      rightHtml += table(["Identifier", "Left", "Right", "Comparison"], idents.map((i) => [
        i.identifier, i.left ?? null, i.right ?? null,
        new Raw(badge(i.comparison !== "missing" ? i.comparison : "not observed", COMPARISON_TONE[i.comparison] ?? "neutral"))]));
      rightHtml += '<p class="muted">Not observed means the identifier was not in the retrieved profile. It does not mean the company has none.</p>';
    }
    const d = r.decision;
    if (d) {
      const rs = d.reasoning ?? {};
      rightHtml += `<p><strong>${esc(TOOL)}:</strong> ${esc(d.answer || (!d.asked ? "not asked" : "no answer"))}` +
        (d.confidence !== null && d.confidence !== undefined ? `, confidence ${esc(d.confidence)}` : "") + "</p>";
      if (isObj(d.details) && Object.keys(d.details).length) rightHtml += kv(Object.entries(d.details));
      if (rs.reason) {
        rightHtml += `<p>${esc(rs.reason)}</p><p class="muted">Source: ${esc(rs.source)}. Last refreshed: ${esc(rs.last_refreshed)}.` +
          (rs.would_change_if ? ` Would change if: ${esc(rs.would_change_if)}.` : "") + "</p>";
      }
      if (d.error) rightHtml += note(esc(isObj(d.error) ? d.error.message : d.error), "error");
      rightHtml += detailsJson("Decision as JSON", d);
    }
    cards.push(card(`${lname} and ${rname}`, badge(status.replace(/_/g, " "), STATUS_TONE[status] ?? "neutral"), leftHtml,
      rightHtml || '<p class="muted">The pair was not compared.</p>', `Row ${r.row_id}`));
  }
  report.add("Pairs", "Each candidate pair with its evidence", cards.join(""));
}

export const RECIPE: Recipe = {
  name: NAME, title: TITLE, description: DESCRIPTION, loadConfig, readInput, plan, run,
  csvColumns: CSV_COLUMNS, csvRows, render,
};
