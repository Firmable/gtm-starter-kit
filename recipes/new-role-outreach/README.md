# New role outreach

Finds people at your target accounts, or in your contact list, whose new role, new executive role or job change falls inside an outreach window you set, and retrieves contact details only for the people selected.

## Business problem

People who have just started a role often review suppliers and set new priorities, so sellers want to reach them early. Finding them by hand means checking every contact's job history. Treating missing history as "not new" drops people who may be in the window, and treating it as "new" wastes outreach.

This recipe asks Firmable about each person's role change, keeps every answer with the start date Firmable returns, and applies written eligibility rules. An answer Firmable cannot give stays visible as unknown. Contact details are retrieved only for eligible people, so the recipe does not pay for profiles it will not use.

A new role is a timing signal for outreach. It does not show buying intent. The recipe sends no outreach.

## Inputs

**Input file** (CSV or JSON). One row per account or contact. A row with a person identifier is a contact. Any other row is an account.

| Column | Required | Meaning |
| - | - | - |
| `person_id` | One identifier for a contact | Firmable person ID, `fp` followed by 12 digits. Used first when present. |
| `work_email` | One identifier for a contact | The contact's work email. Used when there is no `person_id`. |
| `linkedin_url` | One identifier for a contact | The contact's LinkedIn profile URL (`linkedin.com/in/...`). Used when there is neither of the above. |
| `company_id` | One identifier for an account | Firmable company ID, `f` followed by 12 digits. Used first when present. |
| `domain` | One identifier for an account | The company's website domain. A full URL also works. Resolved with `GET /company`. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

A supplied identifier that is malformed makes the row `invalid_input`. The recipe does not fall back to another identifier on that row.

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `evaluation_date` | Today | The date the window ends, as `YYYY-MM-DD`. Set it for repeatable runs. |
| `window_months` | `6` | The outreach window in months, 1 to 60, or 1 to 36 when `new_exec` is a trigger. |
| `triggers` | All three | Which triggers to ask: `new_to_role`, `new_exec`, `changed_jobs`. They are always asked in that order. |
| `executive_only` | `false` | When true, only `new_exec` decides eligibility. Needs `new_exec` in `triggers`. |
| `executive_seniorities` | Not sent | Narrows `new_exec` to some of `C-Suite`, `VP`, `Director`, `Head of`, `Founder`, `Owner`. |
| `count_internal_moves` | `true` | Sent to `is_new_to_role`. Counts a new role at the same company as new. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `fetch_contact_details` | `true` | Fetches the person profile for eligible people, to return emails, phones and the do-not-call flag on each phone. |
| `search.selected_country` | `AU` | The people search country: AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US or CA. |
| `search.position` | Not sent | A position filter for people search, as text. |
| `search.seniority` | Not sent | One seniority id, as text: `1` Board Member / Company Director, `2` Owner / Founder, `3` C-Suite / Partner, `4` VP/Director / Head of, `5` Manager, `6` Other, `7` No Data Available. |
| `search.department` | Not sent | One department id from 1 to 17, as text. See the [people search docs](https://docs.firmable.com/api-reference/endpoint/people-search.md) for the list. |
| `search.max_people` | `25` | The most people retrieved and checked per account, 1 to 100. |

| Trigger | Decision tool | Inputs the recipe sends |
| - | - | - |
| `new_to_role` | `is_new_to_role` | `within_months` = `window_months`, `count_internal_moves` |
| `new_exec` | `is_new_exec` | `within_months` = `window_months`, `seniorities` when set |
| `changed_jobs` | `has_changed_jobs_since` | `since` = `evaluation_date` minus `window_months` |

The window start is the same day `window_months` calendar months before `evaluation_date`, moved to the last day of the month when that month is shorter. For example, one month before 31 March 2026 is 28 February 2026.

The tools and inputs are checked against the live tool list before any paid call.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to validate the tools | Free |
| `GET /company?fqdn=` | Once per unique account domain | Up to 1 credit per new profile |
| `POST /people/search` | Once per unique account | Free |
| `POST /decisions?withReasoning=true` | Once per trigger for each person found or listed | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |
| `GET /people?id=` | Once per eligible person, when `fetch_contact_details` is true | Up to 1 credit per new profile |

Identical requests in one run are answered from memory and not sent again, so duplicate accounts and contacts cost nothing extra.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list and check every trigger tool and input. Stop before paid calls if any is missing or no longer accepts the inputs.
3. For each account: resolve the domain to a company ID, then search people with the configured filters, up to `search.max_people`.
4. For each person found, and for each contact row: ask each trigger.
5. Apply the eligibility rules.
6. For each eligible person: fetch the person profile, when `fetch_contact_details` is true.
7. Write `report.html`, `results.csv` and `results.json`.

## Eligibility rules

Each trigger result is one of:

- **pass**: the answer is yes.
- **fail**: the answer is no.
- **unknown**: Firmable does not hold the fact. This is not a no.
- **error**: the request failed or returned something that is not yes, no or unknown.
- **not_asked**: the run stopped before this trigger was asked.

Qualifying triggers are every configured trigger, or only `new_exec` when `executive_only` is true. Each person gets the first status that applies:

| Status | Rule |
| - | - |
| `eligible` | A qualifying trigger passed. |
| `incomplete` | A qualifying trigger has an error or was not asked. |
| `needs_review` | A qualifying trigger answered unknown. |
| `not_eligible` | Every qualifying trigger failed. |

A contact row takes the status of its person. An account row gets the first status that applies:

| Status | Rule |
| - | - |
| `no_people_found` | People search returned nobody, or nobody the recipe could check. |
| `eligible_contacts_found` | At least one person is eligible. |
| `incomplete` | No person is eligible, and at least one person is `incomplete`. |
| `needs_review` | No person is eligible, and at least one person is `needs_review`. |
| `no_eligible_contacts` | Every person is `not_eligible`. |

Other statuses: `invalid_input` (an identifier is malformed), `company_not_found` (Firmable has no profile for the account domain, so no people were searched), `error` (the row failed, for example people search returned an error) and `not_processed` (the run stopped before this row).

## Conflicts

The recipe adds a conflict note, without changing any answer, when:

- `count_internal_moves` is true, `is_new_to_role` answered no, and `is_new_exec` or `has_changed_jobs_since` answered yes;
- `is_new_to_role` answered yes with `inside_same_company` false, and `has_changed_jobs_since` answered no;
- triggers that answered yes returned different `started` values;
- a trigger answered yes with a `started` value before the window start or after the evaluation date. A `started` value given as a month is compared by month.

## Output

- `report.html`: a summary with the evaluation date and window, then one card per input row. Each card shows the row's conclusion, the people search totals and any skipped search records on the left, and each person's status, trigger answers, start dates, reasoning, conflicts and contact details on the right. The raw requests and responses are at the end.
- `results.csv`: one row per person evaluated, in input order, plus one row for each input row with nobody evaluated. Each row carries the input row's status, the person's status, trigger results, trigger reasons, start dates as returned, conflicts, work emails and phones.
- `results.json`: everything, including every trigger's `confidence`, `details`, `reasoning`, `answered_at` and `credits`, the people search request and totals, and the request ledger with every raw response.

`confidence`, `details`, `started` and `reasoning` come from the API and are shown unchanged.

## Estimated cost

For 20 account domains, 10 people per account and three triggers with reasoning: up to 20 credits for company profiles, 48 credits for answers and 200 credits for contact details if everyone were eligible, 268 credits in total. In practice contact details are fetched only for eligible people, unknown answers are free, and profiles your organisation already paid for are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-new-role-outreach --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-new-role-outreach --input examples/input.csv --config config.example.json --out out --live --max-credits 20
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
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`. An eligible person found before the stop keeps its status, without contact details.
- Nothing is retried automatically.
- `is_new_exec` uses a decision model inside Firmable's service. The report names it. This code adds no model of its own.
- `is_new_to_role` and `is_new_exec` take no reference date. They count back from Firmable's current date, not from `evaluation_date`. Only `has_changed_jobs_since` uses the window start you set. When `evaluation_date` is not today, the triggers can disagree, and the conflict notes show it.
- People search has no start-date filter, so every person retrieved is checked with Decisions. Decisions cost grows with `search.max_people`.
- People search accepts one seniority, one department and one position per request. The recipe sends one search per account and retrieves one page.
- When more people match than `search.max_people`, the rest are not retrieved or checked. The row's reason gives the number.
- People search answers `success: false` with no `records` list when nobody matches. The shared client returns it with an empty `records` list, and the recipe counts it as zero people.
- The people search docs show a person `id` example in company ID format (`f` followed by 12 digits). Decisions accepts only `fp` followed by 12 digits, so a search record without such an ID is skipped and listed in the report.
- A LinkedIn URL can match the wrong person when a record carries someone else's link. Prefer `person_id` where you have it.
- Person answers are advisory: a fact to check, not proof.
- The recipe asks one question per request for readability. Decisions also accepts up to 100 records in one request; see the [Decisions docs](https://docs.firmable.com/api-reference/decisions.md).
