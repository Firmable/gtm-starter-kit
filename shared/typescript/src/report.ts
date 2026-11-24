/**
 * Standalone HTML reports. Mirrors shared/python/src/firmable_client/report.py.
 * Reports load no external scripts, fonts, images or trackers. Every value from an input file or
 * an API response is HTML-escaped. Links are rendered only for http and https URLs.
 */

export const LOGO = String.raw`<svg width="110" height="23" viewBox="0 0 143 30" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Firmable"><path d="M13.94 17.69a1.5 1.5 0 0 1 2.12 0l4.24 4.23a1.5 1.5 0 0 1 0 2.12l-4.24 4.24a1.5 1.5 0 0 1-2.12 0L9.7 24.04a1.5 1.5 0 0 1 0-2.12z" fill="#F15B06"/><path d="M12.17 2.5a4 4 0 0 1 5.66 0l9.88 9.89-3.53-3.53a4 4 0 0 0-5.65 0L15 12.39 7.58 19.8a1.5 1.5 0 0 1-2.12 0l-4.23-4.23a1.5 1.5 0 0 1 0-2.12z" fill="#F15B06"/><path d="M28.77 13.45a1.5 1.5 0 0 1 0 2.12l-4.23 4.23a1.5 1.5 0 0 1-2.12 0L15 12.39l3.53-3.53a4 4 0 0 1 5.66 0z" fill="#A94004"/><text x="38" y="23" fill="#fff" font-family="-apple-system,Segoe UI,Roboto,sans-serif" font-size="21" font-weight="700">Firmable</text></svg>`;

export const CSS = String.raw`
:root{--ember:#F15B06;--deep:#913704;--dark:#181D27;--shale:#E9EAEB;--page:#F4F4F5;--muted:#5B6170}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--dark);font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
header{background:var(--dark);color:#fff;padding:20px 24px}
header .row{max-width:1100px;margin:0 auto;display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;justify-content:space-between}
header h1{margin:6px 0 0;font-size:24px;font-weight:800;letter-spacing:-.5px}
header .meta{color:#C9CCD3;font-size:13px}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
section{background:#fff;border-radius:14px;box-shadow:0 1px 8px rgba(24,29,39,.07);border-top:3px solid var(--ember);padding:18px 20px;margin:0 0 18px;overflow-wrap:break-word}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:1.2px;font-weight:700;color:var(--ember);margin:0 0 4px}
h2{margin:0 0 10px;font-size:19px;font-weight:800;letter-spacing:-.3px}
h3{margin:0 0 6px;font-size:16px;font-weight:700}
p{margin:0 0 8px}
.muted{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:6px 0 10px}
th,td{text-align:left;vertical-align:top;padding:7px 8px;border-bottom:1px solid var(--shale)}
th{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700;white-space:nowrap}
td{overflow-wrap:break-word;word-break:normal}
.scroll{overflow-x:auto}
.scroll td{min-width:6.5em}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:700;white-space:nowrap;border:1px solid}
.t-good{background:#E8F5EC;color:#1E6B3A;border-color:#B7DFC4}
.t-bad{background:#FCEBEA;color:#9B2C22;border-color:#F2C1BC}
.t-unknown{background:#F1F2F4;color:#454B57;border-color:#D5D8DD}
.t-review{background:#FFF4E5;color:#8A4B00;border-color:#F7D3A1}
.t-error{background:#FDE8EF;color:#8C1D46;border-color:#F3BCD0}
.t-info{background:#FEEFE6;color:var(--deep);border-color:#F9C9AA}
.t-neutral{background:#fff;color:var(--dark);border-color:var(--shale)}
.card{border:1px solid var(--shale);border-radius:12px;padding:14px 16px;margin:0 0 14px}
.card-head{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;justify-content:space-between;margin-bottom:8px}
.cols{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,3fr);gap:16px}
@media (max-width:760px){.cols{grid-template-columns:minmax(0,1fr)}}
.note{border-left:4px solid var(--ember);background:#FFF7F2;padding:8px 12px;margin:8px 0;font-size:14px;border-radius:0}
.note.t-unknown{border-left-color:#8A909C;background:#F7F8F9}
.note.t-bad,.note.t-error{border-left-color:#C0392B;background:#FDF2F1}
.template{border:1px dashed var(--ember);border-radius:10px;padding:10px 14px;background:#FFFBF8}
ul{margin:4px 0 8px;padding-left:20px}
details{margin:6px 0}
summary{cursor:pointer;color:var(--deep);font-weight:600;font-size:14px}
pre{background:#0F131B;color:#E6E8EC;padding:12px;border-radius:10px;overflow:auto;max-height:420px;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
a{color:var(--ember)}
.kv{table-layout:fixed}
.kv td{overflow-wrap:anywhere}
.kv td:first-child{color:var(--muted);width:34%}
.card-head>div{min-width:0;overflow-wrap:anywhere}
footer{max-width:1100px;margin:0 auto;padding:0 16px 32px;color:var(--muted);font-size:12px}
`;

/** Marks a string as already-safe HTML for kv(), table() and bulletList(). */
export class Raw {
  constructor(readonly html: string) {}
  toString(): string { return this.html; }
}
export const raw = (html: string): Raw => new Raw(html);

export function esc(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (value instanceof Raw) return value.html;
  let s: string;
  if (typeof value === "boolean") s = value ? "yes" : "no";
  else if (typeof value === "object") s = JSON.stringify(value);
  else s = String(value);
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}

export function safeUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const v = value.trim();
  const m = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/([^/?#]+)/.exec(v);
  if (!m) return null;
  const scheme = m[1].toLowerCase();
  return scheme === "http" || scheme === "https" ? v : null;
}

/** An anchor for http(s) URLs only; any other value is shown escaped, without a link. */
export function link(url: unknown, text?: unknown): string {
  const safe = safeUrl(url);
  const label = esc(text !== undefined && text !== null ? text : url);
  if (!safe) return label;
  return `<a href="${esc(safe)}" rel="noopener noreferrer nofollow">${label}</a>`;
}

export const badge = (text: unknown, tone = "neutral"): string => `<span class="badge t-${esc(tone)}">${esc(text)}</span>`;
export const note = (textHtml: string, tone = "info"): string => `<div class="note t-${esc(tone)}">${textHtml}</div>`;

export const MISSING = '<span class="muted">not returned</span>';
const cell = (c: unknown): string => (c instanceof Raw ? c.html : esc(c));

export function kv(pairs: [string, unknown][]): string {
  const value = (v: unknown): string => (v instanceof Raw ? v.html : v === null || v === undefined || v === "" ? MISSING : esc(v));
  return `<table class="kv">${pairs.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${value(v)}</td></tr>`).join("")}</table>`;
}

export function table(headers: string[], rows: unknown[][]): string {
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + r.map((c) => `<td>${cell(c)}</td>`).join("") + "</tr>").join("");
  return `<div class="scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export function detailsJson(summary: string, data: unknown): string {
  return `<details><summary>${esc(summary)}</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
}

export function card(title: string, badgeHtml: string, leftHtml: string, rightHtml: string, sub = ""): string {
  const subHtml = sub ? `<div class="muted">${esc(sub)}</div>` : "";
  return `<div class="card"><div class="card-head"><div><h3>${esc(title)}</h3>${subHtml}</div>${badgeHtml}</div>` +
    `<div class="cols"><div>${leftHtml}</div><div>${rightHtml}</div></div></div>`;
}

export function bulletList(items: unknown[]): string {
  const lis = items.map((i) => `<li>${cell(i)}</li>`).join("");
  return lis ? `<ul>${lis}</ul>` : "";
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

export class Report {
  sections: string[] = [];
  constructor(readonly title: string, readonly recipe: string, readonly result: Json) {}

  add(eyebrow: string, heading: string, bodyHtml: string): void {
    this.sections.push(`<section><p class="eyebrow">${esc(eyebrow)}</p><h2>${esc(heading)}</h2>${bodyHtml}</section>`);
  }

  addRequestSection(description: string): void {
    const inp = this.result.input ?? {};
    const body = `<p>${esc(description)}</p>` +
      kv([["Input file", inp.file], ["Rows", inp.row_count], ["Mode", this.result.mode]]) +
      detailsJson("Configuration used", inp.config);
    this.add("What you asked for", "Input and request", body);
  }

  addStandardSections(): void {
    const r = this.result;
    const cov: unknown[] = r.coverage ?? [];
    const errs: Json[] = r.errors ?? [];
    let body = "";
    if (cov.length) body += "<h3>Coverage and limitations</h3>" + bulletList(cov);
    if (errs.length) {
      body += "<h3>Errors</h3>" + table(["Row", "Endpoint", "Error", "Status", "Charge uncertain"],
        errs.map((e) => [e.row_id, e.endpoint, e.message, e.status, e.charge_uncertain ? "yes" : "no"]));
    }
    if (!body) body = "<p>No errors and no coverage limitations were recorded.</p>";
    this.add("Limits", "Coverage, limitations and errors", body);

    const c = r.credits ?? {};
    this.add("Credits", "Credit use for this run", kv([
      ["Your limit for this run", c.limit],
      ["Published estimate (upper bound)", c.published_estimate_upper_bound],
      ["Reserved by this code", c.reserved_by_this_code],
      ["Credits reported in responses", c.response_reported_credits],
      ["Requests with an uncertain charge", c.requests_with_uncertain_charge],
      ["Actual wallet deduction", c.wallet_deduction],
    ]) + `<p class="muted">Prices as of ${esc(c.pricing_as_of)}. The limit is enforced by this code, not by the API.</p>`);

    const parts = (r.ledger ?? []).map((e: Json) => {
      let label = `#${e.seq} ${e.endpoint} · ${e.outcome}`;
      if (e.status !== null && e.status !== undefined) label += ` · HTTP ${e.status}`;
      return detailsJson(label, e);
    });
    this.add("Raw responses", "Every request and response",
      '<p class="muted">The API key is never stored. Responses can include personal data; keep this file private.</p>' +
      (parts.join("") || "<p>No requests were made.</p>"));
  }

  render(): string {
    const r = this.result;
    const meta = `${esc(this.recipe)} · ${esc(r.mode ?? "")} · generated ${esc(r.generated_at)}`;
    return '<!doctype html><html lang="en-AU"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">` +
      `<title>${esc(this.title)}</title><style>${CSS}</style></head><body>` +
      `<header><div class="row"><div>${LOGO}<h1>${esc(this.title)}</h1></div><div class="meta">${meta}</div></div></header><main>` +
      this.sections.join("") +
      "</main><footer>gtm-starter-kit. Results come from the Firmable API and the rules in this " +
      "recipe's configuration. Nothing in this report was written by a language model in this code.</footer></body></html>\n";
  }
}
