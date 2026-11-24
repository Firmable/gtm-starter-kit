# Account change monitor

Takes a new snapshot of company, contact, Tech Intel and AI Adoption data for a list of accounts, compares it with the snapshot saved by an earlier run, and reports what differs in the retrieved data.

## Business problem

Account teams want to know when something about a target account moves: headcount, a new technology, a contact's new title, a higher AI adoption level. Checking each account by hand on a schedule is slow, and a naive comparison raises false alarms. A request that failed looks like every field was deleted, and a refresh on Firmable's side looks like a change at the company.

This recipe keeps a snapshot of the fields you choose and compares each run with the last one. It labels every difference by type, never reports a failed or missing response as a deletion, and never calls a difference a confirmed real-world change.

## Inputs

**Input file** (CSV or JSON). One row per monitored account. Each row needs at least one identifier.

| Column | Required | Meaning |
| - | - | - |
| `company_id` | One identifier | A Firmable company ID. Used before `domain` when both are given. |
| `domain` | One identifier | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `person_id` | No | A Firmable person ID for one contact at the account. |
| `work_email` | No | The contact's work email. Used when `person_id` is empty. Lower-cased before lookup. |
| `linkedin_url` | No | The contact's LinkedIn profile URL. Used when `person_id` and `work_email` are empty. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

A row is matched to the previous snapshot by its identifiers, not by its position or `row_id`. The match key is built from the company identifier and the contact identifier, for example `company:fqdn=acme.example|person:work_email=alex@acme.example`. Changing an identifier starts a new baseline for that account.

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `previous_snapshot` | `null` | The `snapshot.json` from an earlier run, as a file path relative to the folder you run the command from. `null` means this run creates a baseline. A snapshot object can also be given inline. |
| `evaluation_date` | Today | The date recorded as `taken_on` in the new snapshot, as `YYYY-MM-DD`. Set it only to reproduce a run. |
| `company_fields` | See the example | Company profile fields to monitor. Dotted paths reach nested values, such as `hq_location`. `[]` skips `GET /company`. |
| `person_fields` | See the example | Person profile fields to monitor, such as `position` or `current_company.name`. `[]` skips `GET /people`. |
| `ai_adoption_fields` | See the example | AI Adoption fields to monitor, such as `overall_level` or `dimensions.workforce.level`. `[]` skips `POST /ai-adoption`. |
| `tech_intel.enabled` | `true` | Fetches the technology list for each company. |
| `tech_intel.max_pages` | `1` | Pages of up to 100 technologies to fetch for each company, from 1 to 20. |

Field names are checked against the documented response fields. A name the API does not document is rejected before any call, which catches typos such as `employee_cuont`. The company ID, person ID and AI Adoption `firmable_id` are always kept so a change of identity is visible.

`time_in_current_role` is accepted but changes every month by design. Leave it out unless you want that.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /company?fqdn=` or `?id=` | Once per unique company identifier, when `company_fields` is not empty | Up to 1 credit per new profile; free if your organisation already paid for it |
| `GET /people?work_email=`, `?id=` or `?ln_url=` | Once per unique contact identifier, when `person_fields` is not empty | Up to 1 credit per new profile; free if already paid for |
| `POST /ai-adoption` | Once per unique company identifier, when `ai_adoption_fields` is not empty | 1 credit per 5 companies found; not found is free |
| `GET /tech-intel?id=` | Up to `max_pages` pages per unique company identifier, when `tech_intel.enabled` is true | Up to 5 credits per company, paid once per company; later pages and repeat calls are free |

AI Adoption and Tech Intel are asked by the Firmable company ID from the company profile when it came back, otherwise by the identifier in the row. Duplicate rows reuse the earlier responses and are not requested again. Tech Intel evidence is not requested.

## Workflow

1. Read and validate the input and configuration, including the previous snapshot.
2. For each row: work out the identifiers, then fetch the company profile, the person profile, AI Adoption and Tech Intel.
3. Compare the monitored fields with the same account in the previous snapshot.
4. Build the new snapshot. Accounts in the previous snapshot that are not in this input are copied across unchanged.
5. Write `report.html`, `results.csv` and `results.json`. After a live run, the command line also writes `snapshot.json`.

## Comparison policy

Only the configured fields are compared. These volatile fields are never compared: request IDs, `usage`, AI Adoption `as_of`, and Tech Intel `recency` and `last_observed`.

Each difference has one change type:

| Change type | Meaning |
| - | - |
| `newly_observed` | The field was empty in the previous snapshot and has a value now. |
| `no_longer_observed_in_retrieved_data` | The profile came back, but the field is empty now. It does not mean the thing no longer exists. |
| `value_changed` | Both snapshots hold a value and the values differ. |
| `not_compared_fetch_failed` | The request failed, or the run stopped before it, so nothing was compared for that source. |
| `not_compared_not_found` | Firmable returned no data for the subject in this run, so nothing was compared for that source. |

Rules that keep the comparison honest:

- A difference is a difference in the data Firmable returned. It is never a confirmed real-world change. Firmable may have refreshed, corrected or newly found the data.
- A failed, skipped or not-found response is never reported as a deletion. The new snapshot keeps the last values that were retrieved, with `last_result` and `last_attempt_on` showing what happened this time.
- An account that is not in the previous snapshot gets `baseline_created` and no change claims. On the first run every account gets a baseline.
- A source that had no values last time because its request failed starts a baseline. A source that was not found last time and returns data now is reported as `newly_observed`.
- A field added to the configuration since the previous snapshot starts a baseline and is listed in `baseline_fields`.
- A technology missing from the current list is reported as `no_longer_observed_in_retrieved_data` only when both the previous and the current lists are complete. When either list stopped at the page limit or an error, missing technologies are counted in the status reason instead.
- A technology that is new against an incomplete previous list carries a note that it may have been present but not retrieved.
- When the company ID or person ID behind an identifier changes, the change carries a note that the other fields may compare two different companies or people.
- When the AI Adoption `framework_version` changed between snapshots, each level change carries a note that the new scoring rules may explain it.
- An unfamiliar AI Adoption level is stored and compared as returned. It does not cause an error.

Each account gets the first status that applies:

| Status | Rule |
| - | - |
| `invalid_input` | An identifier in the row is not valid. |
| `not_monitored` | No enabled source applies to the row's identifiers. |
| `fetch_failed` | Every requested source failed in this run. |
| `baseline_created` | The account is not in the previous snapshot, or there is no previous snapshot. |
| `changes_detected` | At least one `newly_observed`, `no_longer_observed_in_retrieved_data` or `value_changed`. |
| `partially_compared` | No differences, but at least one source could not be compared. |
| `no_changes_detected` | No differences in the monitored fields. |

Other statuses: `error` (the row failed outside a source request) and `not_processed` (the run stopped before this row).

## Output

- `report.html`: a summary of statuses and change types, then one card per account with its sources on the left and its differences, gaps and new baseline fields on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, reason and a compact list of changes.
- `results.json`: everything, including each row's `changes` with `previous`, `current` and `note`, the request ledger with every raw response, and the new snapshot under `summary.snapshot`.
- `snapshot.json` (live runs only): the same snapshot, ready to use as `previous_snapshot` next time.

## Snapshots and scheduling

Copy `out/snapshot.json` somewhere stable, such as `snapshots/latest.json`, and set `previous_snapshot` to that path. Each run reads the previous snapshot and writes a new one; it never overwrites the file you pointed at.

This code does not schedule itself and does not install a scheduler. To run it on a schedule, use a scheduler you control. A weekly cron entry on macOS or Linux looks like this:

```bash
# Every Monday at 07:00. Copies the new snapshot forward only when the run finished (exit code 0).
0 7 * * 1 cd /path/to/gtm-starter-kit/recipes/account-change-monitor && \
  FIRMABLE_API_KEY=fbl_dev_... gtm-account-change-monitor --input accounts.csv --config config.json \
  --out out/$(date +\%F) --live --max-credits 50 && cp out/$(date +\%F)/snapshot.json snapshots/latest.json
```

A CI schedule, such as a GitHub Actions `schedule:` trigger, works the same way. Keep the API key in the scheduler's secret store, not in the file. Exit code 3 means the run stopped early; the snapshot it wrote keeps the previous values for every account it did not reach, so it is safe to use, but check the report first.

## Estimated cost

For 100 unique companies with one contact each, on a first run: up to 100 credits for company profiles, 100 for person profiles, 20 for AI Adoption and 500 for Tech Intel, 720 credits in total. Profiles and Tech Intel companies your organisation already paid for are free, so later runs of the same list usually cost the AI Adoption part only, 20 credits here. Run in plan mode to see the upper bound for your own file; plan mode always assumes every profile is new.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-account-change-monitor --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-account-change-monitor --input examples/input.csv --config config.example.json --out out --live --max-credits 40
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 40
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, later rows are marked `not_processed`, and the snapshot keeps their previous values.
- Nothing is retried automatically.
- Tech Intel returns 403 when it is not on your plan. Each affected source is reported as `not_compared_fetch_failed` and the coverage notes say so.
- Firmable data has its own refresh cycle. Two runs a day apart can show no differences even when the company changed, and a difference can appear when Firmable refreshes data that changed long ago. The comparison dates are the snapshot dates, not the dates the facts changed.
- The REST API has no change feed or history endpoint, so this recipe can only compare snapshots it saved itself. It cannot report changes from before the first run.
- Company sources need a company identifier in the row. The recipe does not follow a contact's current employer to a company, because a job change would silently switch the monitored company.
- AI Adoption is asked one company per request for readability. The API also accepts up to 100 companies per request; see the [AI Adoption docs](https://docs.firmable.com/api-reference/ai-adoption.md).
- The snapshot holds personal data from person profiles. Keep it private, as you would `results.json`.
