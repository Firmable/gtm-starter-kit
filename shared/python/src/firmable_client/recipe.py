"""The structure every recipe follows, and the run envelope written to results.json.

A recipe supplies:
- `load_config(raw)`: checks and normalises the configuration; raises ConfigError;
- `read_input(path)`: reads and checks the input rows; raises InputError;
- `plan(rows, config)`: the calls a live run could make, with published upper-bound costs;
- `run(ctx)`: the workflow; returns {"rows": [...], "summary": {...}, "coverage": [...]};
- `csv_columns` and `csv_rows(result)`: the flat CSV export;
- `render(report, result)`: the recipe's own report sections.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import pricing
from .client import FirmableClient
from .errors import FirmableError
from .io import write_csv, write_json
from .report import Report

SCHEMA_VERSION = 1
FATAL_KINDS = {"unauthorized", "insufficient_credits", "budget_exceeded", "redirect_blocked"}


@dataclass
class PlannedCall:
    endpoint: str
    purpose: str
    requests: int
    parts: list[tuple[str, float]]
    row_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "purpose": self.purpose,
            "requests_upper_bound": self.requests,
            "published_estimate_upper_bound": pricing.total(self.parts),
            "row_ids": self.row_ids,
        }


@dataclass
class Recipe:
    name: str
    title: str
    description: str
    load_config: Callable[[dict[str, Any]], dict[str, Any]]
    read_input: Callable[[Path], list[dict[str, str]]]
    plan: Callable[[list[dict[str, str]], dict[str, Any]], list[PlannedCall]]
    run: Callable[["RunContext"], dict[str, Any]]
    csv_columns: list[str]
    csv_rows: Callable[[dict[str, Any]], list[dict[str, Any]]]
    render: Callable[[Report, dict[str, Any]], None]


class RunContext:
    def __init__(self, client: FirmableClient, config: dict[str, Any], rows: list[dict[str, str]],
                 today: _dt.date | None = None) -> None:
        self.client = client
        self.config = config
        self.rows = rows
        self.errors: list[dict[str, Any]] = []
        self.stopped: dict[str, Any] | None = None
        self.today = today or _dt.date.today()

    def record_error(self, row_id: str | None, err: FirmableError) -> dict[str, Any]:
        entry = {"row_id": row_id, **err.to_dict()}
        self.errors.append(entry)
        if err.kind in FATAL_KINDS and self.stopped is None:
            self.stopped = entry
        return entry

    def run_rows(self, rows: list[dict[str, str]], fn: Callable[[dict[str, str]], dict[str, Any]]) -> list[dict[str, Any]]:
        """Runs fn for each row in order and keeps partial results.

        A row-level API error is recorded on that row. An error that makes further calls pointless
        (bad key, no credits, budget reached, blocked redirect) stops the run; later rows are
        returned with status "not_processed" so nothing is silently dropped.
        """
        out: list[dict[str, Any]] = []
        for row in rows:
            if self.stopped is not None:
                out.append({"row_id": row["row_id"], "input": row, "status": "not_processed",
                            "reason": f"Run stopped earlier: {self.stopped['message']}"})
                continue
            try:
                out.append(fn(row))
            except FirmableError as e:
                entry = self.record_error(row["row_id"], e)
                out.append({"row_id": row["row_id"], "input": row, "status": "error", "error": entry})
        return out


def plan_summary(recipe: Recipe, rows: list[dict[str, str]], config: dict[str, Any]) -> dict[str, Any]:
    calls = recipe.plan(rows, config)
    return {
        "recipe": recipe.name,
        "rows": len(rows),
        "calls": [c.to_dict() for c in calls],
        "requests_upper_bound": sum(c.requests for c in calls),
        "published_estimate_upper_bound": pricing.round4(sum(pricing.total(c.parts) for c in calls)),
        "reservation_upper_bound": pricing.reservation_for([c.parts for c in calls]),
        "pricing_as_of": pricing.PRICING_AS_OF,
        "pricing_sources": pricing.PRICING_SOURCES,
        "note": "Upper bounds assume every profile is new to your organisation and every row needs every "
                "call. Profiles your organisation already paid for are free. Wallet deductions are not "
                "exposed by the API.",
    }


def build_result(recipe: Recipe, ctx: RunContext, output: dict[str, Any], *, input_path: Path,
                 config: dict[str, Any], mode: str) -> dict[str, Any]:
    credits = ctx.client.budget.summary()
    credits["pricing_as_of"] = pricing.PRICING_AS_OF
    coverage = list(output.get("coverage") or [])
    if ctx.stopped:
        coverage.insert(0, f"The run stopped early: {ctx.stopped['message'].rstrip('.')}. Rows after that "
                           "point are marked not_processed.")
    return {
        "recipe": recipe.name,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "mode": mode,
        "input": {"file": input_path.name, "row_count": len(ctx.rows), "config": config},
        "summary": output.get("summary") or {},
        "rows": output.get("rows") or [],
        "coverage": coverage,
        "errors": ctx.errors,
        "credits": credits,
        "ledger": ctx.client.ledger.entries,
    }


def write_outputs(recipe: Recipe, result: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": write_json(out_dir / "results.json", result),
        "csv": write_csv(out_dir / "results.csv", recipe.csv_columns, recipe.csv_rows(result)),
    }
    report = Report(recipe.title, recipe.name, result)
    report.add_request_section(recipe.description)
    recipe.render(report, result)
    report.add_standard_sections()
    html_path = out_dir / "report.html"
    html_path.write_text(report.render(), encoding="utf-8")
    paths["html"] = html_path
    return paths


def parse_date(value: Any, what: str) -> _dt.date:
    from .errors import ConfigError
    try:
        return _dt.date.fromisoformat(str(value))
    except ValueError as e:
        raise ConfigError(f"{what} must be a date as YYYY-MM-DD, not {value!r}") from e
