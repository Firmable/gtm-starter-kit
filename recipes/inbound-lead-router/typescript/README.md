# Inbound lead router: TypeScript

Requires Node.js 22 or later. This package depends on the shared client in [`shared/typescript`](../../../shared/typescript) through npm workspaces, so install and build from the repository root.

```bash
# from the repository root
npm ci
npm run build
```

Run from the recipe folder:

```bash
cd recipes/inbound-lead-router
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out
```

Add `--live --max-credits N` with `FIRMABLE_API_KEY` set to call the API. See the [recipe README](../README.md) for inputs, routing rules and costs.

Start the local single-lead example server (needs `FIRMABLE_API_KEY`; binds 127.0.0.1 only):

```bash
node typescript/dist/src/cli.js serve --config config.example.json --max-credits-per-lead 3.08 --port 8787
# the same server without the wrapper command:
node typescript/dist/src/server.js --config config.example.json --max-credits-per-lead 3.08 --port 8787
```

Tests use synthetic responses from `tests/fixtures/inbound-lead-router/` and make no network calls. The server test binds 127.0.0.1 on a free port and answers from fixtures:

```bash
npm test
```
