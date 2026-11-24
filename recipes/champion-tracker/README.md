# Champion tracker

Finds people at your target accounts who previously worked at your customers or at other companies you name, so sellers can ask for a warm introduction.

## Business problem

A person who used a product at a past employer is often the easiest route into a new account. Finding those people by hand means reading the work history of everyone at every target account. Treating a missing work history as "never worked there" hides people who may be champions.

This recipe searches people at each target account and asks Firmable, for each person found, whether they previously worked at any company on your list. It keeps every answer with the past employer, years there and title Firmable returns, and applies written rules. An answer Firmable cannot give stays visible as unknown.

A past employer is a route to a warm introduction. It does not show buying intent. The recipe sends no outreach.

## Inputs

**Input file** (CSV or JSON). One row per target account.

| Column | Required | Meaning |
| - | - | - |
| `company_id` | One of these two | Firmable company ID, `f` followed by 12 digits. Used first when present. |
| `domain` | One of these two | The company's website domain. A full URL also works. |
| `account_name` | No | Your own name for the account. Kept as given. Used for `{account_name}` in opener templates. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

A supplied identifier that is malformed makes the row `invalid_input`. The recipe does not fall back to the other identifier on that row.

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `past_companies` | Required | 1 to 50 company names or Firmable company IDs. Sent to `previously_worked_at` as `companies`. Any one matches. A name listed twice, ignoring case, is rejected. |
| `include_current` | `false` | Sent to `previously_worked_at`. When true, a person's current employer and earlier roles there also count. |
| `with_reasoning` | `false` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `fetch_contact_details` | `false` | Fetches the person profile for each champion only, to return emails, phones and the do-not-call flag on each phone. |
| `opener_templates` | None | 1 to 10 fixed text templates. Allowed placeholders: `{first_name}`, `{past_employer}`, `{account_name}`, `{title}`. Any other placeholder is rejected. |
| `search.selected_country` | `AU` | The people search country: AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US or CA. |
| `search.position` | Not sent | A position filter for people search, as text. |
| `search.seniority` | Not sent | One seniority id, as text: `1` Board Member / Company Director, `2` Owner / Founder, `3` C-Suite / Partner, `4` VP/Director / Head of, `5` Manager, `6` Other, `7` No Data Available. |
| `search.department` | Not sent | One department id from 1 to 17, as text. See the [people search docs](https://docs.firmable.com/api-reference/endpoint/people-search.md) for the list. |
| `search.max_people` | `25` | The most people retrieved and checked per account, 1 to 100. |

The tool and its inputs are checked against the live tool list before any paid call.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to validate `previously_worked_at` | Free |
| `GET /company?fqdn=` or `GET /company?id=` | Once per unique account | Up to 1 credit per new profile |
| `POST /people/search` | Once per unique account | Free |
| `POST /decisions` (`previously_worked_at`) | Once per unique person ID from the search | 0.04 credits per yes or no answer, 0.08 with reasoning; unknown answers are free |
| `GET /people?id=` | Once per champion, when `fetch_contact_details` is true | Up to 1 credit per new profile |

Identical requests in one run are answered from memory and not sent again, so duplicate accounts cost nothing extra.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list and check `previously_worked_at` and its inputs. Stop before paid calls if the tool is missing or no longer accepts the inputs.
3. For each account: resolve the company with `GET /company`, by `fqdn` for a domain or by `id` for a company ID.
4. Search people at the company with the configured filters, up to `search.max_people`.
5. For each unique person ID from the search: ask `previously_worked_at` with `person_id`, `companies` and `include_current`.
6. Apply the rules below.
7. For each champion: fetch the person profile, when `fetch_contact_details` is true.
8. For each champion: fill the opener templates.
9. Write `report.html`, `results.csv` and `results.json`.

## Rules

Each person gets one result:

| Result | Rule |
| - | - |
| `champion` | `previously_worked_at` answered yes. |
| `not_alumni` | `previously_worked_at` answered no. |
| `unknown` | `previously_worked_at` answered unknown. Firmable does not hold the facts needed to answer. This is not a no. |
| `person_not_found` | Decisions answered 404 `person_not_found`. This is not recorded as a run error. |
| `error` | The request failed, or the answer was not yes, no or unknown. |
| `not_asked` | The run stopped before this person was asked. |

Each account gets the first status that applies:

| Status | Rule |
| - | - |
| `invalid_input` | The identifier is malformed. |
| `company_not_found` | Firmable has no company profile for the domain or company ID. No people were searched. |
| `error` | The company lookup or people search failed. |
| `no_people_found` | People search returned nobody, or nobody with a person ID Decisions accepts. |
| `champions_found` | At least one person is a champion. |
| `incomplete` | No champion, and at least one person has `error` or `not_asked`. |
| `needs_review` | No champion, and at least one person has `unknown` or `person_not_found`. |
| `no_champions_found` | Every person checked answered no. |
| `not_processed` | The run stopped before this row. |

`incomplete` comes before `needs_review`, because a person without an answer could still be a champion. `person_not_found` counts towards `needs_review`, because it is neither a yes nor a no.

A missing champion means none was observed among the people checked. It does not mean none exist. When the search total is larger than the people retrieved, the account's reason states how many were not checked.

`details.employer`, `details.years_there`, `details.title` and `details.past_employers_checked` are kept as returned, under `alumni` in `results.json`. `details.title` is the title at the past employer.

### Conflict notes

The recipe adds a note, without changing any answer, when:

- `previously_worked_at` answered yes but returned no `details.employer`;
- `include_current` is false and the returned past employer equals the account's company name, ignoring case;
- `previously_worked_at` answered no but returned a `details.employer`;
- the people search record's `company_name` differs from the account's company name, ignoring case.

### Suggested openers

Opener templates are predefined text. They are not API facts. They are filled for champions only, from these values:

| Placeholder | Source |
| - | - |
| `{first_name}` | `first_name` from `GET /people`. Empty when `fetch_contact_details` is false or the profile was not retrieved. |
| `{past_employer}` | `details.employer` from `previously_worked_at`. |
| `{account_name}` | `account_name` from the input row, or the company name from `GET /company`. |
| `{title}` | The person's current `position` from people search, or from `GET /people`. |

A template with an empty placeholder is skipped and listed with the missing placeholders. Openers are output under `suggested_openers` and `skipped_openers`, and in a separate block in the report.

## Output

- `report.html`: a summary, then one card per input row. Each card shows the account's conclusion and the people search totals on the left. On the right it shows each person's result, the past employer details as returned, the reasoning, conflict notes, contact details and the suggested openers in a separate block. The raw requests and responses are at the end.
- `results.csv`: one row per person checked, in input order, plus one row for each input row with nobody checked. Each row carries the account status, the person's result, the answer, confidence, past employer details, notes, work emails, phones and openers.
- `results.json`: everything, including each answer's `confidence`, `details`, `reasoning`, `answered_at` and `credits`, the people search request and totals, and the request ledger with every raw response.

## Estimated cost

For 20 account domains and 25 people per account without reasoning: up to 20 credits for company profiles and 20 credits for 500 answers, 40 credits in total. With `fetch_contact_details`, add up to 1 credit per champion, and up to 500 credits if everyone checked were a champion. In practice unknown answers are free and profiles your organisation already paid for are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-champion-tracker --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-champion-tracker --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- People search has no past-employer filter. Only the first `search.max_people` people per account are checked, and the rest are not observed.
- `previously_worked_at` matches the past employer names Firmable holds. A past employer recorded under another name, or a company renamed since, may not match. Firmable company IDs in `past_companies` avoid name ambiguity where you have them.
- People search accepts one seniority, one department and one position per request. The recipe sends one search per account and retrieves one page.
- People search answers `success: false` with no `records` list when nobody matches. The shared client returns it with an empty `records` list, and the recipe counts it as zero people.
- A search record without a person ID of `fp` followed by 12 digits is skipped and listed in the report, because Decisions accepts only that format.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`. A champion found before the stop keeps its result, without contact details.
- `--max-credits` counts the reservation for each call, which can be higher than the published price. Plan mode prints the reservation upper bound.
- Nothing is retried automatically.
- The live tool list marks `previously_worked_at` as not using a decision model. The recipe reads that flag on every run and names the tool in `coverage` if it changes.
- Person answers are advisory: a fact to check, not proof.
- The recipe asks one question per request for readability. Decisions also accepts up to 100 records in one request; see the [Decisions docs](https://docs.firmable.com/api-reference/decisions.md).
