# Expansion signals

Flags land-and-expand signals for each account and places the account in the first tier you define. The signals are: recorded in two or more countries, several sites, a local team in a named country, and an office or staff in a named country.

## Business problem

An account manager wants to know which existing or target accounts have room to grow: a company with sites in several places, staff in another market, or an office in a country you sell into. Checking this by hand for a whole book of accounts is slow. Treating missing information as "no presence" hides accounts that may be a good fit.

This recipe asks Firmable one question per signal, keeps every answer with the facts returned beside it, and applies ordered tier rules you write. An answer Firmable cannot give stays visible as unknown and sends the account to review.

## Inputs

**Input file** (CSV or JSON). One row per account.

| Column | Required | Meaning |
| - | - | - |
| `domain` | One of `domain` or `company_id` | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `company_id` | One of `domain` or `company_id` | A Firmable company ID, `f` followed by 12 digits. When both are given, `company_id` is used. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `checks.multinational` | `false` | Asks `is_multinational`. Signal id `multinational`. |
| `checks.multiple_sites` | `false` | `true`, or `{"min_sites": n}` with n of 2 or more (default 2). Asks `has_multiple_sites`. Signal id `multiple_sites`. |
| `checks.local_teams` | `[]` | A list of `{"country": "AU", "min_staff": 5}`. Asks `has_local_team` once per country. Signal id `local_team:<CC>`. `min_staff` defaults to 5. |
| `checks.offices_in` | `[]` | A list of two-letter upper-case country codes. Asks `is_located_in` with `where: any_office` once per country. Signal id `office_in:<CC>`. |
| `tiers` | Required | 1 to 20 tiers, in the order they are tried. |
| `tiers[].id` | Required | A short identifier, lower-case letters, digits and underscores. |
| `tiers[].label` | The id | The name shown in reports. |
| `tiers[].when.all` | `[]` | Signal ids that must all answer yes. |
| `tiers[].when.any` | `[]` | Signal ids of which at least one must answer yes. A tier needs at least one signal in `all` or `any`. |
| `with_reasoning` | `false` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `fetch_company` | `false` | Fetches each company profile first, shows its facts, and asks signals by the profile's Firmable ID. When false, signals are asked by the `company_id` or `domain` given. |

At least one signal must be turned on. Every signal id a tier names must be turned on in `checks`.

`has_local_team` accepts the countries AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US and CA, but answers only for AU and NZ. Other countries answer unknown. The recipe allows them, prints a config warning in plan mode and adds a line to `coverage`.

Tool names and inputs are checked against the live tool list before any paid call.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to validate the tools and inputs | Free |
| `GET /company?fqdn=` or `?id=` | Once per unique valid identifier, only when `fetch_company` is true | Up to 1 credit per new profile |
| `POST /decisions` | Once per signal per unique valid identifier | 0.04 credits per yes or no answer, 0.08 with reasoning; unknown answers are free |

Duplicate identifiers in the input reuse the earlier responses and are not requested again.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list and check every tool and input. Stop before paid calls if any is wrong.
3. For each row: pick the identifier, fetch the company profile when configured, then ask every configured signal.
4. Evaluate the tiers in order.
5. Write `report.html`, `results.csv` and `results.json`.

## Tier rules

Each signal result is one of `yes`, `no`, `unknown`, `error` (the request failed or the answer was not yes, no or unknown) or `not_asked` (the run stopped first).

A signal counts only on a yes.

Each tier is evaluated as follows:

| Outcome | Rule |
| - | - |
| `not_matched` | A signal in `all` answered no, or every signal in `any` answered no. |
| `matched` | Every signal in `all` answered yes and, when `any` is given, at least one signal in `any` answered yes. |
| `incomplete` | Neither of the above, and a signal the tier depends on has an error or was not asked. |
| `needs_review` | Neither of the above, and every signal the tier depends on answered unknown. |

Tiers are tried in order. The first tier that is not `not_matched` decides the account. Later tiers are shown as `not_considered`. An unknown answer in an earlier tier therefore sends the account to review, even when a later tier would match.

Each account gets one status:

| Status | Rule |
| - | - |
| `tier_assigned` | The deciding tier matched. `tier_id` and `tier_label` name it. |
| `needs_review` | The deciding tier depends on unknown answers. `deciding_tier_id` names it. |
| `incomplete` | The deciding tier depends on a signal with an error or one that was not asked. `deciding_tier_id` names it. |
| `no_tier` | Every tier was not matched. The reason says how many signals answered unknown. |
| `company_not_found` | Firmable has no company for the identifier, from `GET /company` or from Decisions answering 404 `company_not_found`. Remaining signals are not asked. |
| `invalid_input` | The domain is not a valid domain, or the `company_id` is not `f` followed by 12 digits. |
| `error` | The row failed before its signals, such as a 5xx on `GET /company`. |
| `not_processed` | The run stopped before this row. |

The recipe also flags disagreements between answers in `data_conflicts`, without changing the status:

- `is_multinational` answered no while `is_located_in` (any office) answered yes for two or more countries.
- `has_multiple_sites` answered no while `is_located_in` (any office) answered yes for two or more countries.
- With `fetch_company`, the input domain differs from the profile's `fqdn`.

These are shown as returned. The recipe does not correct Firmable data.

## Output

- `report.html`: a summary of statuses, tiers and signal answers, the tier rules, then one card per account with the conclusion, the signals with the facts returned, the tier trace and any conflicts. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, reason, tier, one column per signal id holding its result, `signal_facts` and `data_conflicts`.
- `results.json`: everything, including every signal's `details` as returned (`countries`, `locations_held`, `staff_in_country`, `location_held` and any other keys), `confidence`, `reasoning`, `credits`, the tier trace and the request ledger.

`details`, `confidence` and `reasoning` come from the API and are shown unchanged.

## Estimated cost

For 100 unique accounts and the 6 signals in `config.example.json` without reasoning: up to 600 answers at 0.04 credits, 24 credits in total. With reasoning it is 48 credits. With `fetch_company`, add up to 100 credits for profiles. Unknown answers are free, so `has_local_team` outside AU and NZ costs nothing. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-expansion-signals --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-expansion-signals --input examples/input.csv --config config.example.json --out out --live --max-credits 5
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 5
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- Signals come from records Firmable holds. A no means the presence is not recorded in that data. It is not proof that the company has no presence there.
- `has_local_team` counts staff only in AU and NZ. Other countries answer unknown.
- `is_located_in` uses a decision model inside Firmable's service. The report names it. The other three tools do not. This code adds no model of its own and applies only the tier rules in the configuration.
- Expansion signals describe where a company is recorded. They do not show buying intent.
- The Decisions documentation lists no 404 for an unknown company. The recipe treats a 404 with code `company_not_found` as that company not being held, by analogy with `person_not_found`. Any other 404 is recorded as an error.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`.
- Nothing is retried automatically.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
- The recipe asks one question per request for readability. Decisions also accepts up to 100 records in one request; see the [Decisions docs](https://docs.firmable.com/api-reference/decisions.md).
