# Contact health check

Reviews a contact list and reports which contacts Firmable data shows as current and reachable, which look stale, and which need a person to review them.

## Business problem

Contact lists decay. People change employer or role, and an email or mobile that worked last year may not work now. Sales and marketing teams waste effort on stale contacts, and they need to know which contacts carry Do Not Call restrictions before anyone picks up the phone.

This recipe asks Firmable whether each contact still works at the employer you hold, whether they still hold the title you hold, and what Firmable holds about their email deliverability, mobile and the Australian Do Not Call register. It keeps every answer with its supporting facts and refresh dates, and applies a written health policy. An answer Firmable cannot give stays visible as unknown.

The email, mobile and Do Not Call answers are Firmable-held observations as at Firmable's last refresh. They are not live mailbox checks, not live telephone checks and not a legal compliance check. The recipe sends no email or SMS and makes no calls.

## Inputs

**Input file** (CSV or JSON). One row per contact. Every row needs at least one person identifier.

| Column | Required | Meaning |
| - | - | - |
| `person_id` | One identifier | Firmable person ID, `fp` followed by 12 digits. Used first when present. |
| `work_email` | One identifier | The contact's work email. Used when there is no `person_id`. |
| `linkedin_url` | One identifier | The contact's LinkedIn profile URL (`linkedin.com/in/...`). Used when there is neither of the above. |
| `expected_company_id` | No | The Firmable company ID of the employer you hold, `f` followed by 12 digits. |
| `expected_domain` | No | The website domain of the employer you hold. Resolved to a company ID with `GET /company`. Ignored when `expected_company_id` is present. |
| `expected_title` | No | The title you hold for the contact, 2 to 200 characters. |
| `email_to_check` | No | An email address you hold, checked against Firmable's record instead of the email Firmable holds. |
| `mobile_to_check` | No | A mobile you hold, checked against Firmable's record and the Do Not Call register instead of every mobile Firmable holds. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

A supplied identifier that is malformed makes the row `invalid_input`. The recipe does not fall back to another identifier on that row.

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `checks` | All five | The checks to ask: `employment`, `role`, `email`, `mobile`, `dnc`. They are always asked in that order. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source, fact date and refresh date behind each answer. Doubles the Decisions price. |
| `fetch_person` | `false` | Fetches each person profile first and asks checks by Firmable person ID. Costs up to 1 credit per new profile. |

The tools behind the checks are checked against the live tool list before any paid call.

| Check | Decision tool | Answer that counts as a pass | Needs |
| - | - | - | - |
| `employment` | `works_at_company` | `yes` | An expected employer |
| `role` | `is_contact_current` | `yes` | An expected title. The expected employer's company ID is sent when known. |
| `email` | `is_email_valid` | `yes` | Nothing extra |
| `mobile` | `is_mobile_valid` | `yes` | Nothing extra |
| `dnc` | `is_mobile_on_dnc` | `no` | Nothing extra |

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to validate the tools | Free |
| `GET /company?fqdn=` | Once per unique `expected_domain`, when the employment or role check is on | Up to 1 credit per new profile |
| `GET /people` | Once per unique identifier, when `fetch_person` is true | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` | Once per check that has the inputs it needs, per unique question | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |

Identical requests in one run are answered from memory and not sent again, so duplicate contacts cost nothing extra.

## Workflow

1. Read and validate the input and configuration.
2. Read the decision tool list and check every tool the recipe uses. Stop before paid calls if any is missing or no longer accepts the inputs.
3. For each row: validate the identifiers, resolve the expected employer domain, fetch the person profile when configured, then ask each check.
4. Apply the health policy.
5. Write `report.html`, `results.csv` and `results.json`.

## Health policy

Each check result is one of:

- **pass**: the answer is the one in the table above.
- **fail**: the answer is the other of yes or no.
- **unknown**: Firmable does not hold the fact. This is not a no.
- **error**: the request failed or returned something that is not yes, no or unknown.
- **not_asked**: the run stopped before this check was asked.
- **not_checked**: a prerequisite was missing. Today this means Firmable has no company profile for `expected_domain`.
- **not_supplied**: the row has no expected employer (employment) or no expected title (role).

Each contact gets the first status that applies:

| Status | Rule |
| - | - |
| `stale` | The employment or role check failed. |
| `incomplete` | The employment or role check has an error or was not asked. |
| `needs_review` | The employment or role check answered unknown or was not checked, or neither check was asked. |
| `healthy` | The employment and role checks that were asked passed, and at least one channel is confirmed. |
| `incomplete` | No channel is confirmed, and a reachability check has an error or was not asked. |
| `reach_unconfirmed` | The employment and role checks that were asked passed, but no channel is confirmed. |
| `current` | The employment and role checks that were asked passed, and every reachability check is turned off. |

A channel is confirmed when:

- **email**: the email check answered yes;
- **mobile**: the mobile check answered yes, and the Do Not Call check answered no or is turned off.

A mobile that Firmable records on the Do Not Call register is not a confirmed channel. A Do Not Call answer of unknown is not a confirmed channel either; `is_mobile_on_dnc` answers unknown for numbers outside Australia.

Other statuses: `invalid_input` (an identifier or expected value is malformed), `person_not_found` (`fetch_person` is true and Firmable has no profile, so no checks were asked), `error` (the row failed before its checks) and `not_processed` (the run stopped before this row).

`phone_channel` summarises the mobile and Do Not Call answers: `held_not_on_dnc`, `held_on_dnc`, `held_dnc_unknown`, `held_dnc_not_checked`, `held_dnc_error`, `not_held`, `unknown`, `error` or `not_checked`.

## Flags

The recipe adds a flag, without changing any answer, when:

- `works_at_company` and `is_contact_current` answer yes and no between them;
- the email check answers no; the flag shows the `deliverability_grade` as returned;
- the Do Not Call check answers yes; the flag shows the `as_at` date as returned.

## Output

- `report.html`: a summary of statuses, then one card per contact with the conclusion, flags and expected values on the left and each check's answer, reasoning, notes, refresh dates and missing information on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, reason, each check as `result(answer)`, the phone channel, flags and refresh dates.
- `results.json`: everything, including every check's `confidence`, `details`, `reasoning`, `answered_at`, `credits`, `as_at`, `last_refreshed` and `fact_date`, and the request ledger with every raw response.

`confidence`, `details` and `reasoning` come from the API and are shown unchanged.

## Estimated cost

For 100 unique contacts with an expected employer domain and title, all five checks with reasoning: up to 100 credits for company profiles (fewer when contacts share an employer) and 40 credits for answers, 140 credits in total. With `fetch_person` true, add up to 100 credits for person profiles. Profiles your organisation already paid for are free, and unknown answers are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-contact-health-check --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-contact-health-check --input examples/input.csv --config config.example.json --out out --live --max-credits 10
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
- `is_contact_current` uses a decision model inside Firmable's service. The report names it. This code adds no model of its own.
- Every answer reflects Firmable data as at its refresh date. `is_email_valid` answers from Firmable's deliverability grade; a no is a low grade, not a bounce. `is_mobile_valid` answers whether Firmable holds a mobile, not whether the number is live. `is_mobile_on_dnc` reflects the register as at Firmable's last refresh, covers Australian numbers only, and is not a legal compliance check.
- `works_at_company` needs a Firmable company ID. When only `expected_domain` is given, the recipe resolves it with `GET /company`, which costs up to 1 credit per new company profile. When Firmable has no profile for that domain, employment is `not_checked` and the role check is asked without a company ID.
- A LinkedIn URL can match the wrong person when a record carries someone else's link. Prefer `person_id` where you have it.
- Person answers are advisory: a fact to check, not proof.
- The recipe asks one question per request for readability. Decisions also accepts up to 100 records in one request; see the [Decisions docs](https://docs.firmable.com/api-reference/decisions.md).
