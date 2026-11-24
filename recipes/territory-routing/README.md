# Territory routing

Assigns each company in a list to a sales team using ordered routing rules, and sends ambiguous or incomplete records to a review queue.

## Business problem

Sales operations needs every new account owned by the right team. Routing by hand is slow and inconsistent. Routing on missing data is worse: a company with no recorded head office country can land with the wrong team, and nobody notices.

This recipe reads each company's Firmable profile, applies your written rules in a fixed order, and records which rule decided the route. A record whose route depends on a fact Firmable did not return goes to a review queue instead of a team. The recipe writes its results to files only. It does not write assignments into a CRM.

## Inputs

**Input file** (CSV or JSON). One row per company.

| Column | Required | Meaning |
| - | - | - |
| `domain` | One of `domain` or `company_id` | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `company_id` | One of `domain` or `company_id` | A Firmable company ID: `f` followed by 12 digits. When both are given, the ID is used for the lookup and the domain is compared with the profile. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `teams` | Required | The teams a rule can route to. |
| `teams[].id` | Required | A short identifier: a lower-case letter, then lower-case letters, digits or underscores. |
| `teams[].name` | The id | The name shown in results. |
| `teams[].owner` | `null` | Free text naming the owner or queue, such as a queue name. Copied to the output unchanged. |
| `rules` | Required | The routing rules, in precedence order. |
| `rules[].id` | Required | A short identifier, same format as team ids. |
| `rules[].label` | The id | The name shown in results. |
| `rules[].team` | Required | The team id this rule routes to. |
| `rules[].when` | Required | The conditions. Every condition must be met for the rule to match. |
| `decision_checks` | `[]` | Decisions questions that rules can use. Each check is asked at most once per company, and only when a rule needs it. |
| `decision_checks[].id` | Required | A short identifier, same format as team ids. |
| `decision_checks[].label` | The id | The name shown in results. |
| `decision_checks[].tool` | Required | A decision tool name from `GET /decisions`. |
| `decision_checks[].inputs` | `{}` | The tool's own inputs. The company ID is added by the recipe. |
| `overlap` | `first_match` | How overlapping rules are handled: `first_match` or `review`. See the routing policy. |
| `default_team` | `null` | A team id for companies that no rule matches. When `null`, they are `unassigned`. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |

Every decision check must be used by at least one rule. Tool names and inputs are checked against the live tool list before any paid call.

**Conditions** inside `rules[].when`.

| Condition | Profile field | Met when |
| - | - | - |
| `hq_country_in` | `hq_country` | The head office country equals one of the listed values. |
| `states_any` | `states` | At least one of the company's states equals one of the listed values. |
| `industries_any` | `industries` | At least one of the company's industries equals one of the listed values. |
| `company_size_in` | `company_size` | The size band equals one of the listed values, such as `"201 - 500"`. |
| `employee_count_min` | `employee_count` | The employee count is at least this number. |
| `employee_count_max` | `employee_count` | The employee count is at most this number. |
| `decisions` | Decisions answer | Each named decision check answered the given `yes` or `no`. |

Text comparisons ignore case and surrounding spaces. Otherwise they are exact. `hq_country` was returned as a two-letter code, such as `AU`, in production responses checked on 24 Nov 2026. Write country rules with codes. Industry and size values must match the strings the company profile returns; read one profile before you write these rules.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, only when `decision_checks` is not empty | Free |
| `GET /company?fqdn=` or `GET /company?id=` | Once per unique valid identifier | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` | At most once per decision check per unique valid identifier, only when a rule needs the answer | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |

Duplicate identifiers in the input reuse the earlier responses and are not requested again.

## Workflow

1. Read and validate the input and configuration.
2. When decision checks are configured, read the decision tool list and check every tool and input. Stop before paid calls if any is wrong.
3. For each row: read the identifier, fetch the company profile, then evaluate the rules in order.
4. Within a rule, evaluate profile conditions first. Ask a decision check only when no profile condition in that rule has already failed.
5. Apply the overlap policy and the review rules.
6. Write `report.html`, `results.csv` and `results.json`.

## Routing policy

Each condition is one of:

- **met**: the fact satisfies the condition.
- **not_met**: the fact does not satisfy the condition.
- **unknown**: the profile field is missing or empty, or the decision answered unknown. This is never treated as not met.
- **error**: the decision request failed or returned an answer that is not yes, no or unknown.
- **not_asked**: the run stopped before the decision was asked.
- **skipped**: a profile condition in the same rule already failed, so the decision was not needed.

Each rule outcome is the first that applies:

| Outcome | Rule |
| - | - |
| `no_match` | A condition is not met. |
| `error` | A condition has an error or was not asked. |
| `unknown` | A condition is unknown. |
| `match` | Every condition is met. |

Rules after the deciding rule show `not_evaluated` in `first_match` mode.

**Overlap handling.**

- `first_match`: rules are read in order. The first rule that is not `no_match` settles the row. A match routes the company to that rule's team. An unknown or error sends the row to the review queue, because that rule might have matched.
- `review`: every rule is evaluated. Matching rules that route to different teams are a conflict, and the row goes to the review queue. Matching rules that route to the same team are not a conflict. An unknown or error rule that routes to the same team as the match does not block the assignment.

Each row gets the first status that applies:

| Status | Rule |
| - | - |
| `review` | Matching rules route to different teams. |
| `incomplete` | A rule that could change the route has an error or was not asked. |
| `review` | A rule that could change the route is unknown. |
| `review` | The input domain differs from the profile's `fqdn`. |
| `review` | A matching rule uses a size condition, and the profile's `employee_count` is outside its own `company_size` band. |
| `assigned` | A rule matched. |
| `assigned_default` | No rule matched and `default_team` is set. |
| `unassigned` | No rule matched and `default_team` is `null`. |

Other statuses: `invalid_input` (the domain or company ID is not valid), `company_not_found` (no Firmable profile, so no rules were evaluated), `error` (the row failed before its rules) and `not_processed` (the run stopped before this row).

The review queue holds every row with status `review`, `incomplete`, `company_not_found`, `invalid_input` or `error`. `candidate_teams` lists the teams the matching and unsettled rules point to. `review_reasons` lists every reason that applies; `status_reason` is the first.

## Output

- `report.html`: a routing summary with counts per team and per rule, the review queue, then one card per company with the conclusion, profile facts and data conflicts on the left and each rule's conditions and decision answers on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, team, owner, matched rules, review reasons and the profile facts the rules read.
- `results.json`: everything, including every condition's expected and observed value, every decision answer with its `confidence`, `details`, `reasoning` and `credits`, and the request ledger.

Decision `confidence` and `reasoning` come from the API and are shown unchanged. `data_conflicts` lists disagreements the recipe detected. The recipe reports them and does not correct upstream data.

## Estimated cost

For 100 unique companies and 1 decision check with reasoning: up to 100 credits for company profiles and 8 credits for answers, 108 credits in total. Profiles your organisation already paid for are free, unknown answers are free, and a decision check is not asked when a profile condition in its rule has already failed. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-territory-routing --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-territory-routing --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`.
- Nothing is retried automatically.
- The recipe does not write assignments into a CRM. Load `results.csv` into your CRM through your own process.
- `employee_count` counts staff in the requested country region, which is Australia by default. The recipe does not send the `country` parameter, so `employee_count` reflects the Australian region. For staff counts in another market, use a decision check such as `matches_company_size` with `staff_in`.
- `employee_count` and `company_size` can disagree, because `company_size` may describe the whole company. The recipe flags the disagreement and sends rows to review only when a matching rule uses size.
- An empty `states` or `industries` list is treated as unknown, because an empty list in the profile is not evidence that the company has no state or industry.
- Firmable company IDs are validated as `f` followed by 12 digits, the pattern the Decisions tool list uses. An ID in another format is reported as `invalid_input`.
- Some decision tools use a decision model inside Firmable's service. The report names them. This code adds no model of its own.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
