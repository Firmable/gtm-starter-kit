# Testing

All tests run offline. No test calls the Firmable API or needs a key.

## Commands

```bash
pytest -q shared/python/tests recipes/*/python/tests   # Python unit and recipe tests
npm run build && npm test                              # TypeScript build, type check and tests
python tests/parity/run_parity.py                      # Python and TypeScript produce the same results
python tests/smoke/plan_all.py                         # every documented plan-mode command runs in both languages
```

## Fixtures

Each recipe's scenarios live in `tests/fixtures/<recipe>/`:

| File | Purpose |
| - | - |
| `<name>.scenario.json` | The requests the run is expected to make and the responses to return |
| `<name>.input.csv` or `.json` | Input rows |
| `<name>.config.json` | Configuration; falls back to `config.json` |
| `<name>.meta.json` | Optional `max_credits` and `today` |

A scenario lists exchanges: a request to match (method, path, exact query, exact body) and a response (status and JSON or text, a timeout, or headers). A request with no matching exchange fails the test, so tests prove exactly which calls a run makes and that nothing is retried.

Fixture data is synthetic: `.example` domains, IDs such as `f900000000001`, fictional names. Scenarios include known data problems without personal data, such as contradictory company classification, evidence with no source location, evidence that does not qualify and truncated evidence.

## Parity

`tests/parity/run_parity.py` runs both implementations of each recipe against every scenario and compares `results.json` and `results.csv`. Only `generated_at` and `duration_ms` are ignored. Numbers are compared by value, so `3.0` and `3` match.

## What the shared tests cover

Status mapping for 400, 401, 402, 403, 404, 429 and 5xx; no retries; timeouts marked as uncertain charges; non-JSON and malformed responses; blocked redirects; key redaction; budget exhaustion before a request; paging to the last page, page limits, repeated cursors and partial results after an error; CSV formula protection; HTML escaping and safe links; input validation and row identity.
