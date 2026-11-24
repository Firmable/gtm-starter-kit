"""Command line shared by every recipe.

Plan mode is the default. It checks the input and configuration, lists the calls a live run could
make and their published upper-bound cost, and makes no network request. Live mode needs --live,
--max-credits and the FIRMABLE_API_KEY environment variable.

Exit codes: 0 finished; 2 bad arguments, input or configuration; 3 the run stopped early
(bad key, no credits, credit limit reached, or a blocked redirect). Partial results are still written.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .budget import CreditBudget
from .client import DEFAULT_TIMEOUT, FirmableClient
from .errors import ConfigError, InputError
from .io import read_config, write_json
from .recipe import Recipe, RunContext, build_result, plan_summary, write_outputs


def build_parser(recipe: Recipe) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"gtm-{recipe.name}", description=recipe.description)
    p.add_argument("--input", required=True, help="CSV or JSON input file")
    p.add_argument("--config", required=True, help="JSON configuration file")
    p.add_argument("--out", default="out", help="output folder (default: out)")
    p.add_argument("--live", action="store_true", help="call the Firmable API (spends credits)")
    p.add_argument("--max-credits", type=float, help="credit limit for a live run (required with --live)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait for each response")
    return p


def main(recipe: Recipe, argv: list[str] | None = None) -> int:
    args = build_parser(recipe).parse_args(argv)
    out_dir = Path(args.out)
    try:
        config = recipe.load_config(read_config(args.config))
        rows = recipe.read_input(Path(args.input))
        if args.live and args.max_credits is None:
            raise ConfigError("--live needs --max-credits, the most credits this run may reserve.")
        if args.max_credits is not None and args.max_credits < 0:
            raise ConfigError("--max-credits must be zero or more.")
        if args.timeout <= 0:
            raise ConfigError("--timeout must be positive.")
    except (ConfigError, InputError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    plan = plan_summary(recipe, rows, config)
    _print_plan(plan)
    if not args.live:
        path = write_json(out_dir / "plan.json", plan)
        print(f"\nPlan only: no API calls were made. Saved {path}.")
        print("Run again with --live --max-credits N to call the API.")
        return 0

    api_key = os.environ.get("FIRMABLE_API_KEY", "").strip()
    if not api_key:
        print("error: set FIRMABLE_API_KEY to your developer API key (it starts with fbl_dev_).", file=sys.stderr)
        return 2
    if plan["reservation_upper_bound"] > args.max_credits:
        print(f"\nNote: the upper bound ({plan['reservation_upper_bound']}) is above your limit "
              f"({args.max_credits}). The run stops before any call that would pass the limit.")

    client = FirmableClient(api_key, budget=CreditBudget(args.max_credits), timeout=args.timeout)
    ctx = RunContext(client, config, rows)
    output = recipe.run(ctx)
    result = build_result(recipe, ctx, output, input_path=Path(args.input), config=config, mode="live")
    paths = write_outputs(recipe, result, out_dir)
    c = result["credits"]
    print(f"\nDone. Report: {paths['html']}\nResults: {paths['csv']} and {paths['json']}")
    print(f"Credits: published estimate {c['published_estimate_upper_bound']}, reported in responses "
          f"{c['response_reported_credits']}, uncertain requests {c['requests_with_uncertain_charge']}.")
    if ctx.errors:
        print(f"{len(ctx.errors)} error(s) recorded; see the report.")
    return 3 if ctx.stopped else 0


def _print_plan(plan: dict) -> None:
    print(f"{plan['recipe']}: {plan['rows']} input row(s)")
    for c in plan["calls"]:
        print(f"  {c['endpoint']:<22} up to {c['requests_upper_bound']:>4} request(s), "
              f"up to {c['published_estimate_upper_bound']} credits  {c['purpose']}")
    print(f"Published estimate, upper bound: {plan['published_estimate_upper_bound']} credits")
    print(f"Reservation this code would hold, upper bound: {plan['reservation_upper_bound']} credits")
    print(f"Prices as of {plan['pricing_as_of']}.")
