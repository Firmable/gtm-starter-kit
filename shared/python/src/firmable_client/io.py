"""Input and output helpers shared by every recipe.

Input files are CSV (with a header row) or JSON (an array of objects, or {"rows": [...]}).
Every row keeps a `row_id`: the value of a `row_id` column when present, otherwise `row-<n>`
counting from 1 in file order. Output order always follows input order.
"""
from __future__ import annotations

import csv
import io as _io
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigError, InputError

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_PHONE = re.compile(r"^\+\d[\d ()]*$")  # +61 412 345 678: digits, spaces and brackets cannot form a formula
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def read_rows(path: str | Path, *, required: Iterable[str] = (), any_of: Iterable[str] = ()) -> list[dict[str, str]]:
    """Reads and checks an input file.

    `required` columns must exist and be non-empty on every row. When `any_of` is given, every
    row needs at least one of those columns filled. All problems are reported together.
    """
    p = Path(path)
    if not p.is_file():
        raise InputError(f"Input file not found: {p}")
    text = p.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise InputError(f"Input file is empty: {p.name}")
    raw: list[Any]
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise InputError(f"{p.name} is not valid JSON: {e}") from e
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            data = data["rows"]
        if not isinstance(data, list):
            raise InputError(f"{p.name} must hold an array of objects or {{\"rows\": [...]}}.")
        raw = data
    elif p.suffix.lower() == ".csv":
        reader = csv.DictReader(_io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise InputError(f"{p.name} has no header row.")
        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            raise InputError(f"{p.name} is missing column(s): {', '.join(missing)}")
        raw = list(reader)
    else:
        raise InputError("Input must be a .csv or .json file.")

    rows: list[dict[str, str]] = []
    problems: list[str] = []
    seen_ids: dict[str, int] = {}
    any_cols = list(any_of)
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"row {n}: is not an object")
            continue
        row = {str(k).strip(): _cell_text(v) for k, v in item.items() if k is not None}
        row_id = row.get("row_id") or f"row-{n}"
        if row_id in seen_ids:
            problems.append(f"row {n}: row_id {row_id!r} repeats row {seen_ids[row_id]}")
        seen_ids[row_id] = n
        row["row_id"] = row_id
        for c in required:
            if not row.get(c):
                problems.append(f"row {n} ({row_id}): {c} is empty")
        if any_cols and not any(row.get(c) for c in any_cols):
            problems.append(f"row {n} ({row_id}): needs one of {', '.join(any_cols)}")
        rows.append(row)
    if not rows and not problems:
        problems.append("no rows found")
    if problems:
        raise InputError(f"{p.name} has problems:\n  " + "\n  ".join(problems))
    return rows


def _cell_text(v: Any) -> str:
    """Turns an input value into text the same way in Python and TypeScript."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v).strip()


def read_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"{p.name} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p.name} must hold a JSON object.")
    return data


def normalise_domain(value: str | None) -> str | None:
    """Returns a bare lower-case domain, or None when the value is not a plausible domain.

    Strips a scheme, credentials, path, query, port, a leading "www." and a trailing dot.
    """
    if not value:
        return None
    v = value.strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.split("@")[-1].split(":")[0].rstrip(".")
    if v.startswith("www."):
        v = v[4:]
    return v if _DOMAIN.match(v) else None


def find_duplicates(rows: list[dict[str, str]], key: str) -> dict[str, str]:
    """Maps row_id to the row_id of the first earlier row with the same non-empty key value."""
    first: dict[str, str] = {}
    dupes: dict[str, str] = {}
    for r in rows:
        k = r.get(key) or ""
        if not k:
            continue
        if k in first:
            dupes[r["row_id"]] = first[k]
        else:
            first[k] = r["row_id"]
    return dupes


def safe_cell(value: Any) -> str:
    """Formats a value for CSV and neutralises spreadsheet formulas.

    A text value starting with =, +, -, @, tab or carriage return gets a leading apostrophe.
    Plain numbers such as -5 and phone numbers such as +61 412 345 678 are left alone.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (list, tuple)):
        value = "; ".join(safe_cell(v) for v in value)
    s = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, dict) else str(value)
    if s.startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.match(s) and not _PHONE.match(s):
        return "'" + s
    return s


def write_csv(path: Path, header: list[str], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([safe_cell(r.get(h)) for h in header])
    return path


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
