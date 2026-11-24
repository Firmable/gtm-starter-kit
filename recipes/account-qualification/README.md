# Account qualification

Checks a list of companies against your qualification criteria and reports which accounts qualify, which fail, and which need a person to review them.

## Business problem

A seller has a list of target accounts and a set of criteria an account must meet, such as selling to businesses or having a head office in Australia. Checking each account by hand is slow. Treating missing information as a fail drops accounts that may be a good fit, and treating it as a pass wastes sales time.

This recipe asks Firmable each question, keeps every answer with its supporting facts, and applies a written policy. An answer Firmable cannot give stays visible as unknown.

## Inputs

**Input file** (CSV or JSON). One row per account.

| Column | Required | Meaning |
| - | - | - |
| `domain` | Yes | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `checks` | Required | The qualification checks, in the order they are asked. |
| `checks[].id` | Required | A short identifier, lower-case letters, digits and underscores. |
| `checks[].label` | The id | The name shown in reports. |
| `checks[].tool` | Required | A decision tool name from `GET /decisions`. |
| `checks[].inputs` | `{}` | The tool's own inputs, such as `{"countries": ["AU"]}`. The company is added by the recipe. |
| `checks[].want` | `yes` | The answer that counts as a pass. |
| `checks[].mandatory` | `true` | Whether the account must pass this check. |
| `policy.min_optional_passes` | `0` | How many optional checks must pass for the account to qualify. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `fetch_company` | `true` | Fetches each company profile first and asks checks by Firmable company ID. When false, checks are asked by domain. |

Tool names and inputs are checked against the live tool list before any paid call. Tools change without notice, so read `GET /decisions` rather than relying on the example.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to validate the configured tools | Free |
| `GET /company?fqdn=` | Once per unique valid domain, when `fetch_company` is true | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` | Once per check per unique valid domain | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |

Duplicate domains in the input reuse the earlier response and are not requested again.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list and check every configured tool and input. Stop before paid calls if any is wrong.
3. For each row: normalise the domain, fetch the company profile, then ask each check.
4. Apply the aggregation policy.
5. Write `report.html`, `results.csv` and `results.json`.

## Aggregation policy

Each check result is one of:

- **pass**: the answer equals `want`.
- **fail**: the answer is the other of yes or no.
- **unknown**: Firmable does not hold the fact. This is not a no.
- **error**: the request failed or returned something that is not yes, no or unknown.
- **not_asked**: the run stopped before this check was asked.

Each account gets the first status that applies:

| Status | Rule |
| - | - |
| `disqualified` | A mandatory check failed. |
| `incomplete` | A mandatory check has an error or was not asked. |
| `needs_review` | A mandatory check answered unknown. |
| `qualified` | Every mandatory check passed and at least `min_optional_passes` optional checks passed. |
| `not_qualified` | Every mandatory check passed, but too few optional checks passed. |

Other statuses: `invalid_input` (the domain is not a valid domain), `company_not_found` (no Firmable profile, so no checks were asked), `error` (the row failed before its checks) and `not_processed` (the run stopped before this row).

An unknown optional check never counts as a pass. The status reason says how many optional checks answered unknown.

## Output

- `report.html`: a summary of statuses, then one card per account with the conclusion and company facts on the left and each check's answer, reasoning and missing information on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, reason and a compact check summary.
- `results.json`: everything, including every check's `confidence`, `details`, `reasoning`, `answered_at` and `credits`, and the request ledger.

`confidence` and `reasoning` come from the API and are shown unchanged.

## Estimated cost

For 100 unique domains and 4 checks with reasoning: up to 100 credits for company profiles and 32 credits for answers, 132 credits in total. Profiles your organisation already paid for are free, and unknown answers are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-account-qualification --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-account-qualification --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`.
- Nothing is retried automatically.
- Some decision tools use a decision model inside Firmable's service. The report names them. This code adds no model of its own.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
- The recipe asks one question per request for readability. Decisions also accepts up to 100 records in one request; see the [Decisions docs](https://docs.firmable.com/api-reference/decisions.md).
