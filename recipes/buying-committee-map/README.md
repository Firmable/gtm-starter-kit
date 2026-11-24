# Buying committee map

Shows who fills each buying-committee role at each account, and which roles have no one observed.

## Business problem

A deal usually needs several people to agree: someone who signs, someone who runs the budget, someone who owns the system. A seller working an account needs to know which of those seats have a named person and which are empty. Building that map by hand from titles is slow and inconsistent between sellers.

This recipe defines each role once as a set of Firmable departments and seniorities. It searches each account, asks Decisions for each person's department and seniority, and assigns roles by a fixed rule. It lists the roles nobody retrieved fills, and it keeps people whose facts Firmable does not hold apart from people who do not match.

## Inputs

**Input file** (CSV or JSON). One row per account. Each row needs `domain` or `company_id`.

| Column | Required | Meaning |
| - | - | - |
| `domain` | When `company_id` is empty | The company's website domain. A full URL also works. The recipe resolves it with `GET /company`. |
| `company_id` | No | A Firmable company ID, such as `f000002437529`. When present, the company lookup is skipped and costs nothing. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`, for example `account_name`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `roles` | Required | 1 to 10 roles, in the order they appear in the outputs. At least one must be required. |
| `roles[].id` | Required | A short identifier, lower-case letters, digits and underscores. Used in CSV column names (`role_<id>`). |
| `roles[].label` | The id | The name shown in reports. |
| `roles[].departments` | Required | One or more department labels, such as `Finance`. Compared exactly with the department Decisions returns. |
| `roles[].seniorities` | Required | One or more of Firmable's seniority labels: `Owner / Founder`, `C-Suite / Partner`, `Board Member / Company Director`, `VP/Director / Head of`, `Manager`, `Other`. Other text is rejected. |
| `roles[].required` | `true` | Whether the committee is complete only when this role is filled. |
| `search.selected_country` | `AU` | `selectedCountry` for people search. One of AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US, CA. |
| `search.position` | None | Position text sent as `position`. |
| `search.seniority` | None | One seniority to narrow the search, as its people search id (`1` to `7`) or label. |
| `search.department` | None | One department to narrow the search, as its people search id (`1` to `17`) or label. |
| `search.max_people` | `25` | People retrieved and checked per account (`size`), 1 to 100. |
| `with_reasoning` | `false` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `decision_maker` | Absent | `{"seller": "...", "buyer": "..."}`. When set, `is_decision_maker` is also asked for each person who fills a role. `seller` is 3 to 2000 characters; `buyer` is optional. |

Department labels Firmable uses are: Consulting, Customer Service, Education & Training, Engineering & Technical, Finance, General Management, Human Resources, Legal, Marketing, Medicine & Healthcare, Operations, Product, Research & Analysis, Sales, Trades, Other. The recipe checks that each department is non-empty text. It does not reject other text, because the `matches_department` schema accepts any string.

All roles together may name at most 20 departments, because `matches_department` accepts up to 20.

Example `decision_maker` block:

```json
"decision_maker": { "seller": "Accounts payable automation software", "buyer": "CFO and finance team" }
```

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run | Free |
| `GET /company?fqdn=` | Once per unique valid domain on rows without `company_id` | Up to 1 credit per new profile |
| `POST /people/search` | Once per unique account, with `from` `0` and `size` `max_people` | Free |
| `POST /decisions` `matches_department` | Once per unique person | 0.04 credits per yes or no answer, 0.08 with reasoning; unknown answers are free |
| `POST /decisions` `matches_seniority` | Once per unique person, unless Decisions does not hold the person | As above |
| `POST /decisions` `is_decision_maker` | Once per person who fills a role, only when `decision_maker` is set | As above |

People search sends only `companyId`, `selectedCountry`, `position`, `seniority`, `department`, `from` and `size`. `matches_department` gets the union of every role's departments. `matches_seniority` gets the union of every role's seniorities. Repeated accounts and repeated people reuse the earlier response.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list and check that `matches_department`, `matches_seniority` and, when configured, `is_decision_maker` exist and accept the inputs sent. Stop before paid calls if any is missing.
3. For each account: take `company_id`, or resolve the domain with `GET /company`.
4. Run one people search. Keep each person ID once.
5. For each person, ask `matches_department`, then `matches_seniority`.
6. Assign roles with the rules below.
7. When `decision_maker` is set, ask `is_decision_maker` for each person who fills a role.
8. Write `report.html`, `results.csv` and `results.json`.

## Role rules

Each answer gives one side of the match for each role:

| Side | Rule |
| - | - |
| yes | The answer is yes and the returned `details.department` or `details.seniority` is in the role's list. The comparison is exact after trimming spaces. |
| no | The answer is no, or the answer is yes and the returned value is not in the role's list. |
| unclear | The answer is unknown, or the answer is yes without a returned value. |
| error | The question has no answer because of an error, or it was not asked because the run stopped. |

For one person and one role:

- The person fills the role when both sides are yes.
- A no on either side means the person cannot fill the role.
- Otherwise, an error on either side makes the role undecided for that person.
- Otherwise, the person could fill the role and is unclassified for it.

Each person gets one classification:

| Classification | Rule |
| - | - |
| `assigned` | Fills at least one role. |
| `incomplete` | Fills no role, and an error leaves at least one role undecided. |
| `unclassified` | Fills no role, and could fill at least one role if the unknown fact were known. The reason says which answer was unknown or had no value. |
| `no_role` | Matches no role on the returned department and seniority. |
| `person_not_found` | Decisions answered 404 `person_not_found`. The remaining questions are not asked, and this is not recorded as a run error. |

Each role at each account gets the first status that applies:

| Role status | Rule |
| - | - |
| `filled` | At least one person fills it. The people are listed. |
| `incomplete` | No one fills it, and an error leaves it undecided for at least one person. |
| `possible` | Only unclassified people could fill it. |
| `not_observed` | No one among the people retrieved fills it or could fill it. |

Each account gets the first status that applies:

| Status | Rule |
| - | - |
| `no_people_found` | The search returned no people with a person ID. |
| `committee_complete` | Every required role is filled. |
| `gaps_found` | A required role is `not_observed`. |
| `incomplete` | A required role is `incomplete`. |
| `needs_review` | A required role is only `possible`. |

Other statuses: `invalid_input` (no valid domain, or a `company_id` that is not a Firmable company ID), `company_not_found`, `error` (the company lookup or the search failed) and `not_processed` (the run stopped earlier, or the tool check failed).

`is_decision_maker` does not change role assignment. Its answer is shown beside the person as returned.

## Output

- `results.csv`: one line per input row. Columns are `account_status`, `account_status_reason`, `search_total`, `people_checked`, `required_roles_not_observed`, `required_roles_possible`, `required_roles_undecided`, `role_statuses`, then one `role_<id>` column per role listing the names of the people who fill it.
- `report.html`: a summary, a role-by-account grid, then one card per account. The left side shows the company, search coverage and the role table. The right side shows the department and seniority facts Decisions returned for each person, their classification, the `is_decision_maker` answer, reasoning when requested, and any conflicting answers.
- `results.json`: everything, including each answer's `answer`, `confidence`, `details` and `reasoning`, each role's `filled_by`, `possible` and `undecided` people, and the request ledger.

`notes` on a person record two kinds of conflicting answers, shown as returned and not corrected:

- An answer is yes but the returned value is not a configured value. No role is assigned on it.
- An answer is no but the returned value is a configured value. The answer no is used.

## Estimated cost

For 100 accounts by domain with `max_people` 25 and no reasoning: up to 100 credits for company profiles and 200 credits for 5,000 department and seniority answers, 300 credits in total. With `decision_maker` set, the upper bound adds 100 credits, because every person could fill a role. People search is free. Supplying `company_id` removes the company cost. Profiles your organisation already paid for are free, and unknown answers are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-buying-committee-map --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-buying-committee-map --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- Roles are inferred from each person's main title by Firmable's rules. A person whose main title does not show their department or seniority can be assigned wrongly or not at all.
- Only people retrieved by one search are checked, up to `max_people` per account. When the search reports a larger total, the account reason says how many people were not checked.
- "Not observed" means not among the people retrieved. It does not mean the account has no such person.
- People search has no name, location or date filter, and covers one country, set by `search.selected_country`.
- How people search matches `position` text is not documented. Results are shown as returned.
- When no one matches, people search answers `success: false` without a `records` list. The shared client returns it with an empty `records` list. This recipe counts it as zero people and says so in the coverage notes.
- Search records without a person ID cannot be checked. They are counted in the account reason and left out.
- Department matching uses the exact text Decisions returns. If Decisions returns a department label that differs from the configured label, the person is not assigned on it and a note shows both.
- `matches_department`, `matches_seniority` and `is_decision_maker` use a decision model inside Firmable's service. The report names every such tool used. This code adds no model of its own and applies only the configured role rules.
- An unknown answer is never a no. A person with an unknown fact is unclassified, and a required role that only unclassified people could fill makes the account `needs_review`.
- An error is not an unknown. A required role left undecided by an error makes the account `incomplete`.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge".
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written. Questions not yet asked are `not_asked`, and later rows are `not_processed`. Nothing is retried automatically.
