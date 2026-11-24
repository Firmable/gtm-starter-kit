# CRM enrichment

Fills empty fields in an existing company or contact file from Firmable profiles, keeps existing values that differ, and reports every field-level change and conflict.

## Business problem

A CRM export has gaps: missing websites, industries, head counts and job titles. Filling the gaps by hand is slow. Overwriting the file with a data provider's values loses what the sales team already knows, and a silent overwrite cannot be reviewed.

This recipe looks up each row by an identifier you choose, fills empty cells, keeps existing values by default, and writes a conflict report so a person can decide what to change. It does not connect to Salesforce, HubSpot or any other CRM.

## Inputs

**Input file** (CSV or JSON). One row per company or contact.

| Column | Required | Meaning |
| - | - | - |
| The company identifier column | When a mapping uses `company` | Named in `identifiers.company.column`, such as `domain`. |
| The person identifier column | When a mapping uses `person` | Named in `identifiers.person.column`, such as `work_email`. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept unchanged in the enriched file, in the same order. |

A row can have a company identifier, a person identifier or both. A row with neither is reported as `invalid_input` and kept.

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `identifiers.company.column` | None | The input column that identifies the company. |
| `identifiers.company.type` | Required with the column | One of `fqdn` (domain), `id` (Firmable company ID), `website`, `abn`, `ln_slug`, `ln_url`. |
| `identifiers.person.column` | None | The input column that identifies the person. |
| `identifiers.person.type` | Required with the column | One of `id` (Firmable person ID), `work_email`, `personal_email`, `ln_slug`, `ln_url`. |
| `mappings` | Required | The columns to enrich, in order. |
| `mappings[].column` | Required | The output column. It can be an existing input column or a new one. It cannot be an identifier column or `row_id`. |
| `mappings[].source` | Required | `company` (from `GET /company`) or `person` (from `GET /people`). |
| `mappings[].field` | Required | A dotted path into the response, such as `name`, `industries`, `current_company.name` or `phones.0.value`. The first part must be a documented response field. |
| `mappings[].compare_as` | `text` | `domain` compares two values as domains, so `https://www.acme.com/` equals `acme.com`. |
| `merge_policy` | `fill_empty` | `fill_empty` keeps every non-empty cell. `overwrite` replaces a non-empty cell that differs. |
| `comparison.case_insensitive` | `true` | Ignores letter case when comparing an existing value with the Firmable value. |
| `comparison.collapse_whitespace` | `true` | Ignores leading, trailing and repeated spaces when comparing. |
| `skip_dnd_phones` | `true` | Does not write a person phone that Firmable flags `is_dnd` (do not call). Applies to `phones.N...` mappings with source `person`. |

Every identifier configured must be used by at least one mapping, so no profile is bought without a use. Person `gender` cannot be mapped.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /company?<type>=` | Once per unique valid company identifier | Up to 1 credit per new profile |
| `GET /people?<type>=` | Once per unique valid person identifier | Up to 1 credit per new profile |

Repeated identifiers reuse the earlier response and are not requested again.

## Workflow

1. Read and validate the input and configuration.
2. For each row, normalise the identifiers. Domains lose the scheme, `www.` and path. Emails are lower-cased. ABNs lose spaces and must have 11 digits.
3. Look up the company, then the person.
4. For each mapping, read the Firmable value at the dotted path and apply the merge policy.
5. Write `report.html`, `results.csv` and `results.json`.

## Merge policy

Each mapped field gets one action:

| Action | Rule | Cell in the output |
| - | - | - |
| `filled` | The cell was empty and Firmable has a value. | The Firmable value |
| `unchanged` | The cell equals the Firmable value under the comparison rules. | The existing value |
| `conflict_kept` | The cell differs and `merge_policy` is `fill_empty`. | The existing value |
| `overwritten` | The cell differs and `merge_policy` is `overwrite`. | The Firmable value |
| `no_api_value` | The retrieved profile has no value at that path. | The existing value |
| `skipped_dnd` | A person phone flagged do not call, with `skip_dnd_phones` true. | The existing value |
| `source_unavailable` | The profile for that source was not retrieved. | The existing value |

`conflict_kept` and `overwritten` both appear in the conflict report with the existing value and the Firmable value.

Values are written as text. A list of plain values is joined with `; `. A list of objects or an object is written as compact JSON. Numbers are written without a trailing `.0`.

Each row gets the first status that applies:

| Status | Rule |
| - | - |
| `invalid_input` | No configured identifier is usable on this row. |
| `incomplete` | A lookup failed or was not made because the run stopped. Fields from successful lookups are still applied. |
| `not_matched` | Firmable has no profile for any identifier on the row. |
| `enriched` | At least one field was filled or overwritten. |
| `no_change` | Profiles were found but no field changed. |

Other statuses: `error` (the row failed outside a lookup) and `not_processed` (the run stopped before this row).

## Output

- `results.csv`: the enriched file. It has `row_id`, every input column in input order, any new mapped columns, then `enrichment_status`, `enrichment_reason`, `company_match`, `person_match`, `fields_filled`, `fields_overwritten`, `conflicts` and `duplicate_of`. Every input row is present, matched or not, in input order.
- `report.html`: a status summary, the conflict report, then one card per row with its lookups and a field-by-field table. The raw requests and responses are at the end.
- `results.json`: everything, including each row's `lookups`, `changes` (column, source field, existing value, Firmable value, action, written value, do-not-call flag), the company and person facts, and the request ledger.

## Estimated cost

For 100 rows with a unique domain and a unique work email each: up to 100 credits for company profiles and 100 for person profiles, 200 credits in total. Profiles your organisation already paid for are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-crm-enrichment --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-crm-enrichment --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- No CRM is connected in this version. The recipe reads one file and writes files.
- Rows are matched only by the configured identifier. The recipe never matches by company or person name.
- A value Firmable did not return is `no_api_value`: not observed in the retrieved profile. It does not mean the value does not exist.
- A conflict is reported, not resolved. The recipe does not decide which value is correct, and it does not correct Firmable data. For example, a `company_type` or `year_founded` that contradicts your record is listed as a conflict with both values.
- With `merge_policy` `overwrite`, the previous value of every replaced cell is kept in the conflict report and in `results.json`.
- A person lookup by work email or LinkedIn returns the profile Firmable holds now. If the person changed jobs, `current_company` fields describe the new employer.
- The shared CSV writer adds a leading apostrophe to any cell that starts with `=`, `+`, `-` or `@`, so spreadsheets do not run it as a formula. Phone numbers made only of `+`, digits, spaces and brackets, such as `+61 412 345 678`, are written unchanged.
- The CSV header depends on the input file. An input column with the same name as a status column, such as `conflicts`, is replaced by the status value in `results.csv`; its original value stays in `results.json` under `input`.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge".
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`. Nothing is retried automatically.
