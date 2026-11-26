# Troubleshooting

| Symptom | Cause | What to do |
| - | - | - |
| `error: set FIRMABLE_API_KEY ...` | `--live` without a key in the environment | `export FIRMABLE_API_KEY=fbl_dev_...` |
| `error: --live needs --max-credits` | Live runs need a credit limit | Add `--max-credits 10` or the upper bound that plan mode printed |
| Exit code 2 with a list of row problems | The input file has empty required columns, repeated `row_id`s or invalid JSON | Fix the rows listed; every problem is reported at once |
| `Unknown configuration key(s)` | A typo in the configuration | Compare with `config.example.json` |
| `Decision tool '...' is not in the current tool list` | The tool was renamed or switched off | Run `curl/requests.sh` or `GET /decisions` to see current tools |
| The run stops with `Stopped before calling ...` | The next request would pass `--max-credits` | Raise the limit, or reduce rows or checks; results so far are written |
| `HTTP 401` | Invalid or revoked key | Create a new key in the developer portal |
| `HTTP 402` | Not enough credits | Buy credits in the developer portal, or wait for monthly free credits |
| `HTTP 403` | The key cannot use this endpoint | Check the key in the developer portal, or create a new one |
| `HTTP 429` | Over 15 requests a second | Wait for the `retry-after` seconds and run again; the client paces requests, so this is rare |
| "Requests with an uncertain charge" above 0 | A timeout or 5xx on a paid call | Check **Usage** in the developer portal before re-running |
| `command not found: gtm-...` | Python package not installed, or the virtual environment is not active | `source .venv/bin/activate` and `pip install -r requirements-dev.txt` |
| `Cannot find module .../dist/src/cli.js` | TypeScript not built | `npm ci && npm run build` from the repository root |
