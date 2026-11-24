# CRM enrichment: Python

Requires Python 3.11 or later. This package depends on the shared client in [`shared/python`](../../../shared/python), which is not published to PyPI, so install both from a clone of the repository.

```bash
# from the repository root
python -m venv .venv
source .venv/bin/activate
pip install -e shared/python -e "recipes/crm-enrichment/python[test]"
```

Run from the recipe folder:

```bash
cd recipes/crm-enrichment
gtm-crm-enrichment --input examples/input.csv --config config.example.json --out out
```

Add `--live --max-credits N` with `FIRMABLE_API_KEY` set to call the API. See the [recipe README](../README.md) for inputs, rules and costs.

Tests use synthetic responses from `tests/fixtures/crm-enrichment/` and make no network calls:

```bash
pytest recipes/crm-enrichment/python/tests
```
