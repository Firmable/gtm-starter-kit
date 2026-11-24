# Campaign audience check

Checks a list of accounts against up to five campaign audiences and says include, exclude or review for each account in each campaign, with Firmable's reason shown as returned.

## Business problem

A marketing or sales team plans several campaigns, each with its own audience and message. Deciding which accounts belong in which campaign is slow by hand. Sending a campaign to an account it does not fit wastes budget and can annoy an existing customer. Dropping an account because a fact is missing loses a possible fit.

This recipe asks Firmable's `matches_campaign_audience` decision tool about every account and campaign pair. It keeps each answer with its reason and filter details. An answer Firmable cannot give stays visible as review. Existing customers can be excluded before any credit is spent on them.

## Inputs

**Input file** (CSV or JSON). One row per account. Each row needs `domain` or `company_id`.

| Column | Required | Meaning |
| - | - | - |
| `domain` | One of `domain` or `company_id` | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `company_id` | One of `domain` or `company_id` | A Firmable company ID, `f` followed by 12 digits. When both are given, `company_id` is used. |
| `account_name` | No | Your name for the account. Shown in the report and the CSV. |
| `existing_customer` | No | `true`, `false` or empty. Any other value makes the row `invalid_input`. Case does not matter. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `campaigns` | Required | 1 to 5 campaigns, asked in this order. |
| `campaigns[].id` | Required | A short identifier, lower-case letters, digits and underscores. Used for the CSV column names. |
| `campaigns[].label` | The id | The name shown in reports. |
| `campaigns[].campaign` | Required | Sent unchanged as the `campaign` input of `matches_campaign_audience`. Its keys are listed below. |
| `exclude_existing_customers` | `true` | Excludes rows with `existing_customer` = `true` from every campaign before any paid call for that row. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `fetch_company` | `false` | Fetches each company profile for display. When a profile is found, campaigns are asked by its Firmable company ID. |

Keys of `campaign`, from the `$defs.Campaign` schema in the decision tool list (24 Nov 2026):

| Key | Meaning |
| - | - |
| `message` | Required. Who the campaign is for and what it says, 3 to 2,000 characters. Firmable judges it against the company. |
| `min_staff`, `max_staff` | Fewest and most staff, whole numbers. Worldwide unless `staff_in` names a country. |
| `staff_in` | Counts staff in one country only: AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US or CA. |
| `industries` | Industry names or ANZSIC code prefixes. Any one matches. |
| `countries` | Head office countries as two-letter ISO codes. Any one matches. |
| `revenue_bands` | Revenue bands as Firmable writes them: `<$1M`, `$1M - $5M`, `$5M - $10M`, `$10M - $50M`, `$50M - $100M`, `$100M - $250M`, `$250M - $500M`, `$500M - $1B`, `>$1B+`. |

The configuration is checked when it loads. An unknown key, a missing or short message, a negative staff count, `min_staff` above `max_staff`, an unlisted `staff_in` value, a lower-case country code or an unlisted revenue band is rejected. At run time the recipe also checks the tool and the campaign keys against the live tool list, and stops before any paid call if they no longer match.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to check the tool and its campaign schema | Free |
| `GET /company?fqdn=` or `GET /company?id=` | Once per unique account asked, only when `fetch_company` is true | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` | Once per campaign per unique account asked | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |

An account is asked when its identifier is valid and it is not an excluded existing customer. Duplicate accounts in the input reuse the earlier response and are not requested again. Two rows count as the same account when they have the same `company_id`, or no `company_id` and the same normalised domain.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list. Check that `matches_campaign_audience` exists, answers about companies, and accepts every configured campaign key. Stop before paid calls if not.
3. For each row: check the identifier and the `existing_customer` value.
4. Exclude an existing customer from every campaign when `exclude_existing_customers` is true. No call is made for that row.
5. Fetch the company profile when `fetch_company` is true.
6. Ask each campaign and record the answer, reason and `details.filters` as returned.
7. Apply the status policy.
8. Write `report.html`, `results.csv` and `results.json`.

## Status policy

Each campaign result for an account is one of:

- **include**: Firmable answered yes.
- **exclude**: Firmable answered no.
- **review**: Firmable answered unknown because it does not hold the facts needed to answer. This is not an exclude.
- **excluded_customer**: the row is an existing customer and `exclude_existing_customers` is true.
- **error**: the request failed, or the answer was not yes, no or unknown.
- **not_asked**: the run stopped before this campaign was asked.

Each account gets the first status that applies:

| Status | Rule |
| - | - |
| `in_campaigns` | At least one campaign answered include. The reason also names any campaign in review or with an error. |
| `incomplete` | No include, and at least one campaign has an error or was not asked. |
| `needs_review` | No include, and at least one campaign answered unknown. |
| `no_campaign` | Every campaign answered exclude. |

Other statuses: `invalid_input` (no valid `company_id` or domain, or an `existing_customer` value other than true, false or empty), `excluded_customer` (an existing customer excluded before any call), `error` (the row failed before its campaigns) and `not_processed` (the run stopped before this row).

## Output

- `report.html`: a status summary, the configured campaigns, an account-by-campaign grid with each result and Firmable's reason, then one card per account. Each card shows the conclusion, the identifier used, company facts when fetched, and each campaign's answer, reason, source, refresh date and filter details. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order. Columns: `row_id`, `account_name`, `company_id`, `domain`, `existing_customer`, `duplicate_of`, `status`, `status_reason`, `company_name`, `included_in`, `needs_review`, then `campaign_<id>` and `campaign_<id>_reason` for each campaign. `company_id` is the Firmable ID from the profile or the answer when one came back, otherwise the value given.
- `results.json`: everything, including every answer's `confidence`, `details`, `filters`, `reasoning`, `answered_at` and `credits`, and the request ledger.

`confidence`, `reasoning` and `details.filters` come from the API and are shown unchanged. The shape of `details.filters` is not documented, so the recipe does not interpret it.

## Estimated cost

For 100 unique accounts and 3 campaigns with reasoning: up to 24 credits for answers. With `fetch_company` true, add up to 100 credits for company profiles. Unknown answers are free, and profiles your organisation already paid for are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-campaign-audience-check --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-campaign-audience-check --input examples/input.csv --config config.example.json --out out --live --max-credits 5
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 5
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- `matches_campaign_audience` uses a decision model inside Firmable's service to judge the campaign message. The report names it. This code adds no model of its own and applies only the rules in the configuration.
- The same account and campaign asked twice with slightly different wording can get different answers. The recipe shows each answer as returned and does not reconcile them.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written. Later campaigns are `not_asked` and later rows are `not_processed`.
- Nothing is retried automatically.
- With `fetch_company` true, a company with no Firmable profile is still asked with the identifier given. The row shows "Company profile: not found".
- Company profile facts are shown as returned. The recipe does not correct a profile whose facts conflict, such as a `company_type` or `year_founded` that contradicts other fields.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
- The `existing_customer` column comes from your own records. The recipe does not check it against Firmable or your CRM.
- The recipe asks one question per request for readability. Decisions also accepts up to 100 records in one request; see the [Decisions docs](https://docs.firmable.com/api-reference/decisions.md).
