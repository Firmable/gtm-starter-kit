# Inbound lead router

Routes each inbound lead, such as a form fill or a sign-up, from a work email or domain to an owner or queue using ordered, explicit rules. It runs as a batch over a file, or one lead at a time through a local example server.

## Business problem

Inbound leads arrive with little more than an email address. Someone has to decide which team follows each one up. Routing by hand is slow, and routing on a guess sends good leads to the wrong queue.

This recipe looks up the company behind the email domain in Firmable, optionally asks whether the company fits your ideal customer profile (ICP), optionally looks up the person, then applies your written rules in a fixed order. It records which rule decided the route and why every other rule did or did not match. A lead whose route depends on a fact Firmable did not return goes to a review queue instead of a guessed queue. The recipe writes its results to files, or returns them from the local server. It does not write to a CRM.

## Inputs

**Input file** (CSV or JSON). One row per lead.

| Column | Required | Meaning |
| - | - | - |
| `work_email` | One of `work_email` or `domain` | The lead's email address. The domain after `@` is used when no `domain` is given. |
| `domain` | One of `work_email` or `domain` | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. When both columns are given and the domains differ, the given `domain` is used and the lead is flagged. |
| `lead_name` | No | Kept as given and shown in the report. |
| `source` | No | Kept as given, such as `webinar` or `demo request`. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `personal_email_domains` | `[]` | Domains of personal mailboxes, such as `gmail.com`. A lead whose domain is on this list goes to `personal_email_queue` with no API call. |
| `icp` | Absent | Your ideal customer profile, passed to the `is_icp_fit` decision tool as `icp`. When absent, `is_icp_fit` is not called. |
| `fetch_person` | `false` | When `true`, looks up the person by work email (`GET /people`). Needed for `seniority_in`. |
| `rules` | Required | The routing rules, in precedence order. |
| `rules[].id` | Required | A short identifier: a lower-case letter, then lower-case letters, digits or underscores. |
| `rules[].queue` | Required | The owner or queue name this rule routes to. Free text, copied to the output unchanged. |
| `rules[].when` | Required | The conditions. Every condition must be met for the rule to match. |
| `review_queue` | Required | The queue for leads that need a person: missing facts, no company profile, invalid input or errors. |
| `default_queue` | Required | The queue for leads that no rule matches. |
| `personal_email_queue` | Required | The queue for leads with a personal email domain only. |
| `with_reasoning` | `true` | Asks `is_icp_fit` for the reason, source and refresh date behind the answer. Doubles the Decisions price. |

**ICP** inside `icp`. The keys follow the `is_icp_fit` input schema in the tool list read on 24 Nov 2026.

| Key | Meaning |
| - | - |
| `min_staff`, `max_staff` | Fewest and most staff. Whole numbers. |
| `staff_in` | Count staff in one market only: one of AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US, CA. |
| `industries` | Industry names or ANZSIC code prefixes. Any one matches. |
| `countries` | Head office country codes. Any one matches. |
| `revenue_bands` | Revenue bands as Firmable writes them, such as `$10M - $50M`. |
| `criteria` | Up to 10 checkable statements, 2,000 characters in all, such as `Sells to other businesses`. |

`icp.description` is rejected, because `is_icp_fit` does not accept it. Use `criteria` instead.

**Conditions** inside `rules[].when`. Conditions combine with AND.

| Condition | Fact | Met when |
| - | - | - |
| `hq_country_in` | Company `hq_country` | The head office country equals one of the listed values. |
| `employee_count_min` | Company `employee_count` | The employee count is at least this number. |
| `employee_count_max` | Company `employee_count` | The employee count is at most this number. |
| `industries_any` | Company `industries` | At least one of the company's industries equals one of the listed values. |
| `icp_fit` | `is_icp_fit` answer | The answer equals `yes`, `no` or `unknown`, as configured. Needs `icp`. |
| `seniority_in` | Person `seniority` | The person's seniority equals one of the listed values. Needs `fetch_person`. For a lead with no work email, or a work email on a personal domain, there is no person to check, so the rule does not match. When the person was looked up and Firmable returned no seniority, the fact is missing and the lead goes to review. |

Text comparisons ignore case and surrounding spaces. Otherwise they are exact. `hq_country` was returned as a two-letter code, such as `AU`, in production responses checked on 24 Nov 2026. Industry and seniority values must match the strings the profiles return, such as `Computer software` or `C-Suite / Partner`.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /company?fqdn=` | Once per unique business domain | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` with `is_icp_fit` | Once per company, only when `icp` is configured | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |
| `GET /people?work_email=` | Once per unique work email, only when `fetch_person` is `true` and the email is not on a personal domain | Up to 1 credit per new profile; 404 when Firmable has no profile |

No call is made for a personal email domain, an invalid row, or a lead after the company lookup returned no profile. Duplicate leads reuse the earlier responses.

## Workflow

1. Read and validate the input and configuration.
2. For each lead, take the domain from `domain`, or from `work_email` when no domain is given.
3. Send a lead whose domain is on `personal_email_domains` to `personal_email_queue`. No call is made.
4. Fetch the company profile by domain. No profile sends the lead to `review_queue` with status `company_not_found`.
5. When `icp` is configured, ask `is_icp_fit` about the company.
6. When `fetch_person` is `true` and a business work email is given, fetch the person.
7. Evaluate every rule in order and pick the route by the routing policy below.
8. Write `report.html`, `results.csv` and `results.json`.

## Routing policy

Each condition is one of:

- **met**: the fact satisfies the condition.
- **not_met**: the fact does not satisfy the condition.
- **missing**: the fact was not returned, the list was empty, or `is_icp_fit` answered unknown against a `yes` or `no` condition.
- **error**: the request for the fact failed.
- **not_asked**: the run stopped before the request was made.

Each rule outcome is the first that applies:

| Outcome | Rule |
| - | - |
| `not_matched` | A condition is not met, or a `seniority_in` condition does not apply because the lead has no business work email. `failing_condition` names the first one. |
| `error` | A condition has an error or was not asked. |
| `skipped_missing_fact` | A condition's fact is missing. `missing_facts` names them. |
| `matched` | Every condition is met. |

The first rule, in configuration order, whose outcome is not `not_matched` decides the lead:

| Deciding rule | Status | Queue |
| - | - | - |
| `matched` | `routed` | The rule's queue |
| `skipped_missing_fact` | `needs_review` | `review_queue` |
| `error` | `error` | `review_queue` |
| None: every rule is `not_matched` | `routed_default` | `default_queue` |

A missing fact never lets a later rule or the default queue take the lead, because the skipped rule might have matched. An `is_icp_fit` answer of unknown never matches an `icp_fit` condition of `yes` or `no`, and it is never treated as no. To route unknown answers to a queue of their own, put a rule with `"icp_fit": "unknown"` before the rules that need `yes` or `no`.

Other statuses, set before the rules:

| Status | Queue | Rule |
| - | - | - |
| `personal_email` | `personal_email_queue` | The domain used is on `personal_email_domains`. |
| `company_not_found` | `review_queue` | Firmable has no company profile for the domain. |
| `invalid_input` | `review_queue` | The work email or domain is not valid. |
| `error` | `review_queue` | The company request failed. |
| `not_processed` | None | The run stopped before this lead. |

**Flags.** A lead is flagged, and still routed, when:

- the given domain differs from the work email domain;
- the work email is on a personal domain but a business domain was given;
- the company profile's `fqdn` differs from the domain looked up;
- the person's current company in Firmable is not the company found for the domain.

The recipe reports these and does not correct upstream data.

## Output

- `report.html`: a routing summary with counts per status, queue and rule, the review queue, then one card per lead. Each card shows the route, the company and person facts, flags, the `is_icp_fit` answer with its reasoning as returned, and every rule's outcome with each condition's expected and observed value. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, queue, matched rule, domain used, company facts, ICP answer, person seniority, flags and each rule's outcome.
- `results.json`: everything, including every condition, the full `is_icp_fit` answer with `confidence`, `details` (`criteria` and `filters`), `reasoning` and `credits`, and the request ledger.

## Local single-lead server

The recipe includes a small HTTP server for trying the router one lead at a time from a form or a script on your own machine. It is an example to run locally. It is not a production service: it has no authentication, no rate limiting and no persistence.

```bash
export FIRMABLE_API_KEY=fbl_dev_...

# Python
gtm-inbound-lead-router serve --config config.json --max-credits-per-lead 3.08 --port 8787
# or: python -m inbound_lead_router.server --config config.json --max-credits-per-lead 3.08 --port 8787

# TypeScript
node typescript/dist/src/cli.js serve --config config.json --max-credits-per-lead 3.08 --port 8787
# or: node typescript/dist/src/server.js --config config.json --max-credits-per-lead 3.08 --port 8787

curl -sS -X POST http://127.0.0.1:8787/route -H "Content-Type: application/json" \
  -d '{"work_email": "jane.citizen@example.com", "lead_name": "Jane Citizen", "source": "webinar"}'
```

- The server binds `127.0.0.1` only and sends no CORS headers.
- `POST /route` takes a JSON object holding one lead, with the same fields as an input row. The body may be up to 16 KB; a larger body gets 413.
- Other paths get 404. Other methods on `/route` get 405.
- A body that is not JSON, not an object, or has neither `work_email` nor `domain` gets 400.
- `FIRMABLE_API_KEY` and `--max-credits-per-lead` are required. Every request gets a fresh client and a fresh credit budget of `--max-credits-per-lead`.
- The answer is the same row object a batch run writes to `results.json`, including status, queue, rules and flags. The request ledger is not included. Request headers and the key are never sent back.
- With the example configuration, one lead can cost up to 2.08 credits: 1 for the company, 0.08 for `is_icp_fit` with reasoning, and 1 for the person. The budget check also holds a 1-credit allowance for fractional Decisions charges, so `--max-credits-per-lead 3.08` lets every call through. A lower limit stops the lead before the call that would pass it, and the lead gets status `error`.

The `serve` subcommand does not change the batch command line. Without `serve`, the command runs in plan mode by default, and `--live` still needs `--max-credits` and the key.

## Estimated cost

For 100 unique business domains with `icp` configured, reasoning on, and 100 unique work emails with `fetch_person`: up to 100 credits for company profiles, 8 credits for ICP answers and 100 credits for person profiles, 208 credits in total. Profiles your organisation already paid for are free, unknown answers are free, and a person Firmable does not hold is free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-inbound-lead-router --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-inbound-lead-router --input examples/input.csv --config config.example.json --out out --live --max-credits 10
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
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later leads are marked `not_processed`.
- Nothing is retried automatically.
- The recipe does not write routes into a CRM or assign owners in any system. Load `results.csv` through your own process, or call the local server from your own form handler.
- Personal email detection uses only the `personal_email_domains` list. A personal mailbox domain missing from the list is looked up as a company and may return no profile, or the profile of the mail provider.
- The company is looked up by domain only. A lead whose email domain belongs to a parent, a reseller or a shared mail host is routed on the company Firmable holds for that domain. The flags show disagreements the recipe can detect.
- `employee_count` counts staff in the requested country region, which is Australia by default. The recipe does not send the `country` parameter. For staff counts in another market, use `icp.min_staff` with `icp.staff_in`.
- An empty `industries` list is treated as missing, because an empty list in the profile is not evidence that the company has no industry.
- Person profiles are looked up by work email only. A lead with no work email has no person facts, so a `seniority_in` rule does not match it.
- `is_icp_fit` uses a decision model inside Firmable's service (`uses_decision_model` is true in the tool list read on 24 Nov 2026). The report says so. This code adds no model of its own and applies only the rules in the configuration.
- The recipe validates `icp` against the `is_icp_fit` schema saved on 24 Nov 2026. It does not read the live tool list at run time, so a later schema change can appear as a `bad_request` error on the ICP call.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
- An ICP fit answer is a fit judgement. It is not evidence of buying intent.
