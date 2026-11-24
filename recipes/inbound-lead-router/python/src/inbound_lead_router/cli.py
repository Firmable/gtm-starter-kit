"""Command line entry point: gtm-inbound-lead-router.

`gtm-inbound-lead-router serve ...` starts the local single-lead example server (see server.py).
Every other invocation is the standard batch command line, unchanged: plan mode by default, and
--live needs --max-credits and FIRMABLE_API_KEY.
"""
from __future__ import annotations

import sys

from firmable_client.cli import main as run_cli

from .recipe import RECIPE


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "serve":
        from .server import main as serve_main
        sys.exit(serve_main(args[1:]))
    sys.exit(run_cli(RECIPE, args))


if __name__ == "__main__":
    main()
