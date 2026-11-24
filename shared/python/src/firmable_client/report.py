"""Standalone HTML reports.

Reports load no external scripts, fonts, images or trackers. Every value from an input file or an
API response is HTML-escaped. Links are rendered only for http and https URLs; anything else is
shown as plain text.
"""
from __future__ import annotations

import html
import json
from typing import Any, Iterable
from urllib.parse import urlparse

LOGO = (
    '<svg width="110" height="23" viewBox="0 0 143 30" fill="none" xmlns="http://www.w3.org/2000/svg" '
    'role="img" aria-label="Firmable"><path d="M13.94 17.69a1.5 1.5 0 0 1 2.12 0l4.24 4.23a1.5 1.5 0 0 1 0 '
    '2.12l-4.24 4.24a1.5 1.5 0 0 1-2.12 0L9.7 24.04a1.5 1.5 0 0 1 0-2.12z" fill="#F15B06"/><path d="M12.17 '
    '2.5a4 4 0 0 1 5.66 0l9.88 9.89-3.53-3.53a4 4 0 0 0-5.65 0L15 12.39 7.58 19.8a1.5 1.5 0 0 1-2.12 0l-4.23-4.23a1.5 '
    '1.5 0 0 1 0-2.12z" fill="#F15B06"/><path d="M28.77 13.45a1.5 1.5 0 0 1 0 2.12l-4.23 4.23a1.5 1.5 0 0 1-2.12 '
    '0L15 12.39l3.53-3.53a4 4 0 0 1 5.66 0z" fill="#A94004"/><text x="38" y="23" fill="#fff" '
    'font-family="-apple-system,Segoe UI,Roboto,sans-serif" font-size="21" font-weight="700">Firmable</text></svg>'
)

CSS = """
:root{--ember:#F15B06;--deep:#913704;--dark:#181D27;--shale:#E9EAEB;--page:#F4F4F5;--muted:#5B6170}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--dark);font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
header{background:var(--dark);color:#fff;padding:20px 24px}
header .row{max-width:1100px;margin:0 auto;display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;justify-content:space-between}
header h1{margin:6px 0 0;font-size:24px;font-weight:800;letter-spacing:-.5px}
header .meta{color:#C9CCD3;font-size:13px}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
section{background:#fff;border-radius:14px;box-shadow:0 1px 8px rgba(24,29,39,.07);border-top:3px solid var(--ember);padding:18px 20px;margin:0 0 18px;overflow-wrap:anywhere}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:1.2px;font-weight:700;color:var(--ember);margin:0 0 4px}
h2{margin:0 0 10px;font-size:19px;font-weight:800;letter-spacing:-.3px}
h3{margin:0 0 6px;font-size:16px;font-weight:700}
p{margin:0 0 8px}
.muted{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:6px 0 10px}
th,td{text-align:left;vertical-align:top;padding:7px 8px;border-bottom:1px solid var(--shale)}
th{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700}
.scroll{overflow-x:auto}
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
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
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
.kv td:first-child{color:var(--muted);width:34%}
footer{max-width:1100px;margin:0 auto;padding:0 16px 32px;color:var(--muted);font-size:12px}
"""


def esc(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        value = "yes" if value else "no"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return html.escape(str(value), quote=True)


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    try:
        parsed = urlparse(v)
    except ValueError:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return v
    return None


def link(url: Any, text: Any = None) -> str:
    """An anchor for http(s) URLs only; any other value is shown escaped, without a link."""
    safe = safe_url(url)
    label = esc(text if text is not None else url)
    if not safe:
        return label
    return f'<a href="{esc(safe)}" rel="noopener noreferrer nofollow">{label}</a>'


def badge(text: Any, tone: str = "neutral") -> str:
    return f'<span class="badge t-{esc(tone)}">{esc(text)}</span>'


def note(text_html: str, tone: str = "info") -> str:
    return f'<div class="note t-{esc(tone)}">{text_html}</div>'


def kv(pairs: Iterable[tuple[str, Any]]) -> str:
    rows = "".join(f"<tr><td>{esc(k)}</td><td>{v if isinstance(v, Raw) else esc(v)}</td></tr>" for k, v in pairs)
    return f'<table class="kv">{rows}</table>'


class Raw(str):
    """Marks a string as already-safe HTML for kv() and table()."""


def table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, Raw) else esc(c)}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def details_json(summary: str, data: Any) -> str:
    return (f"<details><summary>{esc(summary)}</summary><pre>"
            f"{esc(json.dumps(data, indent=2, ensure_ascii=False))}</pre></details>")


def card(title: str, badge_html: str, left_html: str, right_html: str, sub: str = "") -> str:
    sub_html = f'<div class="muted">{esc(sub)}</div>' if sub else ""
    return (f'<div class="card"><div class="card-head"><div><h3>{esc(title)}</h3>{sub_html}</div>'
            f'{badge_html}</div><div class="cols"><div>{left_html}</div><div>{right_html}</div></div></div>')


def bullet_list(items: Iterable[Any]) -> str:
    lis = "".join(f"<li>{i if isinstance(i, Raw) else esc(i)}</li>" for i in items)
    return f"<ul>{lis}</ul>" if lis else ""


class Report:
    def __init__(self, title: str, recipe: str, result: dict[str, Any]) -> None:
        self.title = title
        self.recipe = recipe
        self.result = result
        self.sections: list[str] = []

    def add(self, eyebrow: str, heading: str, body_html: str) -> None:
        self.sections.append(f'<section><p class="eyebrow">{esc(eyebrow)}</p><h2>{esc(heading)}</h2>{body_html}</section>')

    def add_request_section(self, description: str) -> None:
        inp = self.result.get("input", {})
        body = (f"<p>{esc(description)}</p>"
                + kv([("Input file", inp.get("file")), ("Rows", inp.get("row_count")),
                      ("Mode", self.result.get("mode"))])
                + details_json("Configuration used", inp.get("config")))
        self.add("What you asked for", "Input and request", body)

    def add_standard_sections(self) -> None:
        r = self.result
        cov = r.get("coverage") or []
        errs = r.get("errors") or []
        body = ""
        if cov:
            body += "<h3>Coverage and limitations</h3>" + bullet_list(cov)
        if errs:
            body += "<h3>Errors</h3>" + table(
                ["Row", "Endpoint", "Error", "Status", "Charge uncertain"],
                [[e.get("row_id"), e.get("endpoint"), e.get("message"), e.get("status"),
                  "yes" if e.get("charge_uncertain") else "no"] for e in errs])
        if not body:
            body = "<p>No errors and no coverage limitations were recorded.</p>"
        self.add("Limits", "Coverage, limitations and errors", body)

        c = r.get("credits") or {}
        self.add("Credits", "Credit use for this run", kv([
            ("Your limit for this run", c.get("limit")),
            ("Published estimate (upper bound)", c.get("published_estimate_upper_bound")),
            ("Reserved by this code", c.get("reserved_by_this_code")),
            ("Credits reported in responses", c.get("response_reported_credits")),
            ("Requests with an uncertain charge", c.get("requests_with_uncertain_charge")),
            ("Actual wallet deduction", c.get("wallet_deduction")),
        ]) + f'<p class="muted">Prices as of {esc(c.get("pricing_as_of"))}. The limit is enforced by this code, not by the API.</p>')

        ledger = r.get("ledger") or []
        parts = []
        for e in ledger:
            label = f"#{e.get('seq')} {e.get('endpoint')} · {e.get('outcome')}"
            if e.get("status") is not None:
                label += f" · HTTP {e.get('status')}"
            parts.append(details_json(label, e))
        self.add("Raw responses", "Every request and response",
                 "<p class=\"muted\">The API key is never stored. Responses can include personal data; "
                 "keep this file private.</p>" + ("".join(parts) or "<p>No requests were made.</p>"))

    def render(self) -> str:
        r = self.result
        mode = r.get("mode", "")
        meta = f"{esc(self.recipe)} · {esc(mode)} · generated {esc(r.get('generated_at'))}"
        return (
            "<!doctype html><html lang=\"en-AU\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            f"<title>{esc(self.title)}</title><style>{CSS}</style></head><body>"
            f"<header><div class=\"row\"><div>{LOGO}<h1>{esc(self.title)}</h1></div>"
            f"<div class=\"meta\">{meta}</div></div></header><main>"
            + "".join(self.sections)
            + "</main><footer>gtm-starter-kit. Results come from the Firmable API and the rules in this "
              "recipe's configuration. Nothing in this report was written by a language model in this code.</footer>"
              "</body></html>\n"
        )
