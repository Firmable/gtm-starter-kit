## What changed

## Checks

- [ ] Python tests pass: `pytest -q shared/python/tests recipes/*/python/tests`
- [ ] TypeScript builds and tests pass: `npm run build && npm test`
- [ ] Parity passes: `python tests/parity/run_parity.py`
- [ ] Plan mode runs: `python tests/smoke/plan_all.py`
- [ ] Python and TypeScript changes match each other
- [ ] No API key, personal data or private customer data in code, fixtures or docs
- [ ] Fixtures are synthetic (`.example` domains, fictional names)
- [ ] Docs updated where behaviour, inputs or costs changed
