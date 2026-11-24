"""Offline test support. Never used by the command line.

`FixtureTransport` answers requests from a scenario file instead of the network. A scenario is JSON:

    {
      "description": "...",
      "exchanges": [
        {"request": {"method": "GET", "path": "/company", "query": {"fqdn": "acme.example"}},
         "response": {"status": 200, "json": {...}}},
        {"request": {"method": "POST", "path": "/decisions", "body": {...}},
         "response": {"timeout": true}},
        {"request": {...}, "response": {"status": 200, "text": "<html>"}, "times": 2}
      ]
    }

`query` and `body`, when given, must match exactly. Each exchange answers once unless `times` says
otherwise. A request with no matching exchange fails the test, so tests prove which calls were made.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from .budget import CreditBudget
from .client import FirmableClient
from .errors import TransportTimeoutError
from .io import read_config
from .recipe import Recipe, RunContext, build_result, write_outputs
from .transport import HttpRequest, HttpResponse

TEST_KEY = "fbl_dev_TESTKEY0000000000000000"


def fixtures_dir(recipe: str, start: str | Path | None = None) -> Path:
    """tests/fixtures/<recipe> in the repository that contains `start` (default: this file)."""
    here = Path(start or __file__).resolve()
    for d in [here, *here.parents]:
        if (d / "tests" / "fixtures").is_dir() and (d / "recipes").is_dir():
            return d / "tests" / "fixtures" / recipe
    raise FileNotFoundError("Could not find the repository's tests/fixtures folder.")


class FixtureError(AssertionError):
    pass


class FixtureTransport:
    def __init__(self, scenario: dict[str, Any] | str | Path, expected_key: str = TEST_KEY) -> None:
        if not isinstance(scenario, dict):
            scenario = json.loads(Path(scenario).read_text(encoding="utf-8"))
        self.exchanges = [dict(e, used=0) for e in scenario.get("exchanges", [])]
        self.sent: list[HttpRequest] = []
        self.expected_key = expected_key

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.headers.get("Authorization") != f"Bearer {self.expected_key}":
            raise FixtureError("request did not carry the expected bearer token")
        self.sent.append(request)
        for e in self.exchanges:
            if e["used"] >= e.get("times", 1):
                continue
            want = e["request"]
            if want.get("method", "GET") != request.method or want["path"] != request.path:
                continue
            if "query" in want and {k: str(v) for k, v in want["query"].items()} != request.query:
                continue
            if "body" in want and want["body"] != request.json_body:
                continue
            e["used"] += 1
            resp = e["response"]
            if resp.get("timeout"):
                raise TransportTimeoutError(f"No response within {request.timeout} seconds.")
            text = resp["text"] if "text" in resp else json.dumps(resp.get("json"))
            headers = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
            return HttpResponse(resp.get("status", 200), headers, text)
        raise FixtureError(f"no fixture for {request.method} {request.path} query={request.query} "
                           f"body={json.dumps(request.json_body)}")

    def unused(self) -> list[dict[str, Any]]:
        return [e["request"] for e in self.exchanges if e["used"] < e.get("times", 1)]


def make_client(scenario: dict[str, Any] | str | Path, max_credits: float | None = 100.0,
                timeout: float = 30.0) -> tuple[FirmableClient, FixtureTransport]:
    transport = FixtureTransport(scenario)
    client = FirmableClient(TEST_KEY, budget=CreditBudget(max_credits), transport=transport,
                            timeout=timeout, min_interval=0)
    return client, transport


def run_with_fixtures(recipe: Recipe, *, input_path: str | Path, config_path: str | Path,
                      scenario: dict[str, Any] | str | Path, out_dir: str | Path | None = None,
                      max_credits: float | None = 100.0, today: str | None = None) -> tuple[dict[str, Any], FixtureTransport]:
    config = recipe.load_config(read_config(config_path))
    rows = recipe.read_input(Path(input_path))
    client, transport = make_client(scenario, max_credits)
    ctx = RunContext(client, config, rows, today=_dt.date.fromisoformat(today) if today else None)
    output = recipe.run(ctx)
    result = build_result(recipe, ctx, output, input_path=Path(input_path), config=config, mode="test-fixture")
    if out_dir is not None:
        write_outputs(recipe, result, Path(out_dir))
    return result, transport


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a recipe against a fixture scenario (tests and parity only).")
    p.add_argument("--recipe-module", required=True, help="module exposing RECIPE, e.g. account_qualification.recipe")
    p.add_argument("--input", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--scenario", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--max-credits", type=float, default=100.0)
    p.add_argument("--today")
    a = p.parse_args(argv)
    recipe = importlib.import_module(a.recipe_module).RECIPE
    run_with_fixtures(recipe, input_path=a.input, config_path=a.config, scenario=a.scenario,
                      out_dir=a.out, max_credits=a.max_credits, today=a.today)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
