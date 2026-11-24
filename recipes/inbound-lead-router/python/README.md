# Inbound lead router: Python

Requires Python 3.11 or later. This package depends on the shared client in [`shared/python`](../../../shared/python), which is not published to PyPI, so install both from a clone of the repository.

```bash
# from the repository root
python -m venv .venv
source .venv/bin/activate
pip install -e shared/python -e "recipes/inbound-lead-router/python[test]"
```

Run from the recipe folder:

```bash
cd recipes/inbound-lead-router
gtm-inbound-lead-router --input examples/input.csv --config config.example.json --out out
```

Add `--live --max-credits N` with `FIRMABLE_API_KEY` set to call the API. See the [recipe README](../README.md) for inputs, routing rules and costs.

Start the local single-lead example server (needs `FIRMABLE_API_KEY`; binds 127.0.0.1 only):

```bash
gtm-inbound-lead-router serve --config config.example.json --max-credits-per-lead 3.08 --port 8787
# the same server without the wrapper command:
python -m inbound_lead_router.server --config config.example.json --max-credits-per-lead 3.08 --port 8787
```

Tests use synthetic responses from `tests/fixtures/inbound-lead-router/` and make no network calls. The server test binds 127.0.0.1 on a free port and answers from fixtures:

```bash
pytest recipes/inbound-lead-router/python/tests
```
