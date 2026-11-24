# Security

## API keys

- Recipes read the key only from the `FIRMABLE_API_KEY` environment variable.
- The key is sent only as a bearer token to `https://api.firmable.com`. The base URL is fixed in code.
- Redirects are refused, so the key is never sent to another location.
- The request ledger never stores the `Authorization` header. Any occurrence of the key in a response body is replaced with `[REDACTED]` before it is written.
- `.env` files are ignored by git. Do not commit a key. If a key is exposed, revoke it in the [developer portal](https://platform.firmable.com).

## Output files

`results.json`, `results.csv` and `report.html` contain API responses. People endpoints return names, work emails and phone numbers. Treat output folders as personal data:

- `out/` folders are ignored by git.
- Do not attach output files to issues or pull requests.
- Delete output you no longer need.

## Reports

HTML reports load no external scripts, fonts, images or trackers, and set a Content Security Policy that blocks them. Every value from input files and API responses is escaped. Links are rendered only for `http` and `https` URLs.

CSV exports prefix values starting with `=`, `+`, `-`, `@`, tab or carriage return with an apostrophe, so spreadsheet programs do not run them as formulas.

## Reporting a vulnerability

Report security issues privately by email to Samuel Medina at samuel@firmable.com. Do not open a public issue for a security problem.
