# Contributing

## Ground rules

- Recipes call the Firmable REST API directly. Do not add LLM, agent-framework or generative-AI dependencies.
- Business rules are deterministic and written in configuration or code, not inferred.
- Keep `yes`, `no` and `unknown` distinct. Unknown is never treated as no.
- Every recipe has a Python and a TypeScript implementation that produce the same `results.json` and `results.csv` for the same input. A change to one language needs the same change in the other.
- Fixtures are synthetic. Use `.example` domains, IDs such as `f900000000001` and fictional names. Never commit real API responses that contain personal or customer data.
- Do not add runtime dependencies without a strong reason. The shared clients use only the standard library and Node built-ins.

## Set up

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
npm ci
npm run build
```

## Before opening a pull request

```bash
pytest -q shared/python/tests recipes/*/python/tests
npm test
python tests/parity/run_parity.py
python tests/smoke/plan_all.py
```

All four must pass. CI runs the same commands without an API key.

## Adding a recipe

Copy the structure of `recipes/account-qualification`. See [docs/architecture.md](docs/architecture.md) and [docs/testing.md](docs/testing.md). Add the recipe to `requirements-dev.txt`, the root `tsconfig.json` references and the table in the root README.

## Writing style

Australian English. Plain, factual sentences. Say whether a behaviour is built, proposed or undecided.
