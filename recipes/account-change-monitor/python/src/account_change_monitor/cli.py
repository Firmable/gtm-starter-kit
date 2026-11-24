"""Command line entry point: gtm-account-change-monitor.

Runs the shared command line, then, after a live run, copies the new snapshot from
results.json (summary.snapshot) to snapshot.json in the output folder. Point previous_snapshot in
the configuration at that file for the next run. Nothing is scheduled.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from firmable_client.cli import build_parser
from firmable_client.cli import main as run_cli
from firmable_client.io import write_json

from .recipe import RECIPE


def write_snapshot(out_dir: Path) -> Path | None:
    """Writes snapshot.json from results.json in out_dir. Returns the path, or None if there is no snapshot."""
    results = out_dir / "results.json"
    if not results.is_file():
        return None
    snapshot = (json.loads(results.read_text(encoding="utf-8")).get("summary") or {}).get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    return write_json(out_dir / "snapshot.json", snapshot)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    code = run_cli(RECIPE, argv)
    args, _ = build_parser(RECIPE).parse_known_args(argv)
    if args.live and code in (0, 3):
        path = write_snapshot(Path(args.out))
        if path:
            print(f"Snapshot for the next run: {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
