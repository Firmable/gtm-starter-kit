"""Command line entry point: gtm-ai-adoption-segmentation."""
import sys

from firmable_client.cli import main as run_cli

from .recipe import RECIPE


def main() -> None:
    sys.exit(run_cli(RECIPE))


if __name__ == "__main__":
    main()
