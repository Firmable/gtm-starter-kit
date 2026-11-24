# Account contact finder

Finds relevant people at each account, checks each candidate against your criteria, and retrieves contact details only for the people who pass.

## Business problem

A seller has a list of target accounts and needs the right people at each one, such as the finance leaders who approve an accounts payable tool. Pulling every profile at a company spends credits on people who will never be contacted. Taking the first search results without checking them puts the wrong people into a sequence.

This recipe searches each account with the documented people search filters, asks Decisions about each candidate, applies a written selection policy, and buys person profiles only for the people it selects. It shows how many people the search reported and how many were retrieved, so coverage is never implied.

## Inputs

**Input file** (CSV or JSON). One row per account. Each row needs `domain` or `company_id`.

| Column | Required | Meaning |
| - | - | - |
| `domain` | When `company_id` is empty | The company's website domain. A full URL also works. The recipe resolves it with `GET /company`. |
| `company_id` | No | A Firmable company ID, such as `f000002437529`. When present, the company lookup is skipped and costs nothing. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `search.country` | `AU` | `selectedCountry` for people search. One of AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US, CA. |
| `search.queries` | Required | The searches to run at each account, in order. |
| `search.queries[].id` | Required | A short identifier, lower-case letters, digits and underscores. |
| `search.queries[].label` | The id | The name shown in reports. |
| `search.queries[].position` | None | Position text sent as `position`. |
| `search.queries[].seniority` | None | One seniority, as its documented id (`1` to `7`) or label, such as `C-Suite / Partner`. |
| `search.queries[].department` | None | One department, as its documented id (`1` to `17`) or label, such as `Finance`. |
| `search.page_size` | `25` | Records per search page (`size`), 1 to 100. |
| `search.max_pages` | `1` | Pages per search (`from` advances by `page_size`), 1 to 10. |
| `checks` | `[]` | Decisions checks asked about each candidate. Each has `id`, `label`, `tool`, `inputs`, `want` (`yes` or `no`) and `mandatory`. |
| `policy.min_optional_passes` | `0` | How many optional checks must pass for a candidate to be selected. |
| `with_reasoning` | `false` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `max_candidates_checked` | `10` | Candidates checked per account, in the order search returned them. |
| `max_details_per_account` | `3` | Selected candidates per account that get `GET /people`. `0` fetches no profiles. |

Each search needs at least one of `position`, `seniority` or `department`. Labels are converted to the documented ids before the request is sent.

Check tools must answer about a person. Tool names and inputs are checked against the live tool list before any paid call. For `works_at_company`, leave `inputs` empty: the recipe adds the account's `company_id`.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, when checks are configured | Free |
| `GET /company?fqdn=` | Once per unique valid domain on rows without `company_id` | Up to 1 credit per new profile |
| `POST /people/search` | Once per search per page per account | Free |
| `POST /decisions` | Once per check per checked candidate | 0.04 credits per yes or no answer, 0.08 with reasoning; unknown answers are free |
| `GET /people?id=` | Once per selected candidate, up to `max_details_per_account` per account | Up to 1 credit per new profile |

People search sends only `companyId`, `selectedCountry`, `position`, `seniority`, `department`, `from` and `size`. Repeated accounts and repeated people reuse the earlier response.

## Workflow

1. Read and validate the input and configuration.
2. When checks are configured, read the decision tool list and check every tool and input. Stop before paid calls if any is wrong.
3. For each account: take `company_id`, or resolve the domain with `GET /company`.
4. Run each search, page by page, until a short page, the reported total or `max_pages`. A person found by several searches is kept once, with every search that found them.
5. Ask each check about the first `max_candidates_checked` candidates.
6. Apply the selection policy.
7. Fetch the person profile for selected candidates, in search order, up to `max_details_per_account`.
8. Write `report.html`, `results.csv` and `results.json`.

## Selection policy

Each check result is one of `pass`, `fail`, `unknown` (Firmable does not hold the fact; not a no), `error` and `not_asked` (the run stopped first).

Each candidate gets the first selection that applies:

| Selection | Rule |
| - | - |
| `rejected` | A mandatory check failed. |
| `incomplete` | A mandatory check has an error or was not asked. |
| `needs_review` | A mandatory check answered unknown. The candidate is not selected and no profile is bought. |
| `selected` | Every mandatory check passed and at least `min_optional_passes` optional checks passed. |
| `not_selected` | Every mandatory check passed, but too few optional checks passed. |
| `not_checked` | The account had already reached `max_candidates_checked`. |

With no checks configured, every checked candidate is `selected` on the search criteria alone.

A selected candidate's profile status is `fetched`, `not_fetched_limit` (over `max_details_per_account`), `not_found`, `error` or `not_requested` (the run stopped). Other candidates show `not_selected`.

Each account gets the first status that applies:

| Status | Rule |
| - | - |
| `incomplete` | A search failed, a candidate is `incomplete`, or a selected candidate's profile failed or was not requested. |
| `no_candidates` | The searches returned nobody. |
| `contacts_selected` | At least one candidate is selected. |
| `no_contacts_selected` | Candidates were found, but none is selected. |

Other statuses: `invalid_input` (no valid domain, or a `company_id` that is not a Firmable company ID), `company_not_found`, `error` and `not_processed`.

## Output

- `results.csv`: one line per candidate, with the account's status and search totals on every line. An account with no candidates gets one line. Columns include `selection`, `selection_reason`, `check_results`, `details_status`, `work_emails`, `personal_emails`, `phones` (a do-not-call phone is marked `(do not call)`), `current_company_matches_account` and `notes`.
- `report.html`: a summary, then one card per account. The left side shows the company and a search coverage table: total reported, records retrieved, pages and whether paging finished. The right side shows each candidate's selection and checks, and contact details for fetched profiles.
- `results.json`: everything, including every check's `answer`, `confidence`, `details` and `reasoning`, each search's request coverage, and the request ledger.

`notes` record two kinds of disagreement, shown as returned and not resolved:

- A search filtered on a seniority or department, and the matching `matches_seniority` or `matches_department` check answered no.
- The fetched person profile names a current company other than the account (`current_company_matches_account` is `no`).

## Estimated cost

For 100 accounts by domain, with 3 checks without reasoning on up to 10 candidates and up to 3 profiles per account: up to 100 credits for company profiles, 120 for answers and 300 for person profiles, 520 credits in total. People search is free. Supplying `company_id` removes the company cost. Profiles your organisation already paid for are free, and unknown answers are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-account-contact-finder --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-account-contact-finder --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- People search has no name, location or date filter. Each documented search field takes one string, so each query sends one seniority and one department. Run several queries to cover several values.
- How people search matches `position` text is not documented. Results are shown as returned.
- Searches cover one country, set by `search.country`.
- A person not in the search results is not observed in retrieved data. It does not mean no such person works at the account.
- When `total` is larger than the records retrieved, the account was not fully searched. The report shows both numbers for every search.
- When no one matches, people search answers `success: false` without a `records` list. The shared client returns it with an empty `records` list. This recipe counts it as zero matches and says so in the coverage notes.
- Search records without a person ID cannot be checked or fetched. They are counted in the account reason and left out.
- `matches_seniority`, `matches_department` and `is_decision_maker` use a decision model inside Firmable's service. The report names every such tool used. This code adds no model of its own and applies only the configured rules.
- A search filter and a Decisions check about the same attribute can disagree. Both are kept and the disagreement is noted.
- Contact details are shown as returned. A phone flagged `is_dnd` is marked do not call; the recipe does not check any register itself.
- The shared CSV writer adds a leading apostrophe to any cell that starts with `=`, `+`, `-` or `@`, so spreadsheets do not run it as a formula. Phone numbers made only of `+`, digits, spaces and brackets, such as `+61 412 345 678`, are written unchanged.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge".
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`. Nothing is retried automatically.
