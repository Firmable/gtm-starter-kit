# Pre-call account pack

Builds a factual pack for each account before a sales call: the company profile, selected contacts, the technologies Firmable observed, Decisions checks and, when requested, AI Adoption. Every fact shows which API response it came from.

## Business problem

Before a call, a seller pieces together who the company is, who to talk to, what technology the company uses and whether it fits the seller's criteria. This takes several tools and the sources get lost. A seller then repeats a detail on the call without knowing where it came from or how old it is.

This recipe makes those calls in one run and keeps each fact next to its source request and response. It keeps the questions it suggests apart from the facts, because those questions are fixed templates, not information about the account.

## Inputs

**Input file** (CSV or JSON). One row per account.

| Column | Required | Meaning |
| - | - | - |
| `domain` | Yes | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`). Every object rejects keys it does not know.

| Key | Default | Meaning |
| - | - | - |
| `company_checks` | `[]` | Decisions checks about the company, asked in order. |
| `contact_checks` | `[]` | Decisions checks about each selected contact, asked in order. Needs at least one people search. |
| `*_checks[].id` | Required | A short identifier, lower-case letters, digits and underscores. Unique across both lists. |
| `*_checks[].label` | The id | The name shown in reports. |
| `*_checks[].tool` | Required | A decision tool name from `GET /decisions`. Company checks need a company tool; contact checks need a person tool. |
| `*_checks[].inputs` | `{}` | The tool's own inputs, such as `{"within_months": 6}`. The subject is added by the recipe. |
| `*_checks[].want` | `yes` | The answer that counts as a pass. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |
| `contacts.selected_country` | `AU` | The country people search covers: AU, NZ, SG, MY, ID, PH, HK, JP, VN, KR, TH, US or CA. |
| `contacts.searches` | `[]` | People searches, run in order. An empty list means no contacts section. |
| `contacts.searches[].id` | Required | A short identifier for the search. |
| `contacts.searches[].label` | The id | The name shown in reports. |
| `contacts.searches[].position` | None | Position text, such as `Marketing`. |
| `contacts.searches[].seniority` | None | One seniority id, written as text: `1` Board Member or Company Director, `2` Owner or Founder, `3` C-Suite or Partner, `4` VP, Director or Head of, `5` Manager, `6` Other, `7` No Data Available. |
| `contacts.searches[].department` | None | One department id from `1` to `17`, written as text. See the [People Search docs](https://docs.firmable.com/api-reference/endpoint/people-search.md) for the list. |
| `contacts.searches[].size` | `5` | The most people one search returns, from 1 to 100. Each search needs at least one of position, seniority or department. |
| `contacts.max_contacts` | `3` | The most contacts selected for each account, from 0 to 50. |
| `contacts.fetch_profiles` | `true` | Fetches each selected contact's full profile. When false, only the search fields are shown. |
| `tech_intel.enabled` | `true` | Retrieves Tech Intel for each account. |
| `tech_intel.with_evidence` | `false` | Adds the evidence behind each technology. Pages hold 10 technologies instead of 100 and each page costs 0.2 credits more. |
| `tech_intel.max_pages` | `1` | The most Tech Intel pages retrieved for each account, from 1 to 20. |
| `ai_adoption.enabled` | `false` | Retrieves AI Adoption for each account. |
| `ai_adoption.with_evidence` | `false` | Adds the evidence behind each AI Adoption area. Costs 3 credits per company found instead of 0.2. |
| `suggested_questions.enabled` | `true` | Fills the predefined question templates. |
| `suggested_questions.max_questions` | `6` | The most suggested questions for each account, from 0 to 20. |

Tool names and inputs are checked against the live tool list before any paid call. Tools change without notice, so read `GET /decisions` rather than relying on the example.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, when any check is configured | Free |
| `GET /company?fqdn=` | Once per unique valid domain | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` | Once per company check, and once per contact check per selected contact | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |
| `POST /people/search` | Once per configured search per account | Free |
| `GET /people?id=` | Once per selected contact, when `fetch_profiles` is true | Up to 1 credit per new profile |
| `GET /tech-intel?id=` | Up to `max_pages` pages per account | Up to 5 credits per company, charged once; 0.2 credits more per page with evidence |
| `POST /ai-adoption` | Once per account, when enabled | 0.2 credits per company found; 3 with evidence; not found is free |

Rows that repeat an earlier domain copy that row's pack. No request is repeated for them.

## Workflow

1. Read and validate the input and configuration.
2. When any check is configured, read the decision tool list and check every tool and input. Stop before paid calls if any is wrong.
3. For each row: normalise the domain and fetch the company profile. A domain with no profile gets no pack.
4. Ask each company check, using the Firmable company ID.
5. Run each people search, select contacts, fetch their profiles and ask each contact check.
6. Retrieve Tech Intel, following pages up to `max_pages`.
7. Retrieve AI Adoption when enabled.
8. Fill the predefined question templates from the facts above.
9. Write `report.html`, `results.csv` and `results.json`.

## Rules

**Contact selection.** Searches run in configuration order. Each returns up to its `size` people, in the order the API returns them. A person found by two searches is kept once, with both search ids in `matched_searches`. The first `max_contacts` people are selected. The rest are counted in `not_selected`.

**Contact checks.** Each contact check is asked with the contact's `person_id`. When the tool accepts a `company_id` input, such as `works_at_company`, the account's company ID is added, so the check is about this account.

**Contact details.** Work email addresses and phone numbers are copied from the person profile. Each phone keeps its `is_dnd` do-not-call flag as returned. Personal email addresses are not copied into the pack. They remain in the raw responses.

**Check results.** Each check result is one of:

- **pass**: the answer equals `want`.
- **fail**: the answer is the other of yes or no.
- **unknown**: Firmable does not hold the fact. This is not a no.
- **error**: the request failed or returned something that is not yes, no or unknown.
- **not_asked**: the run stopped before this check was asked.

The recipe does not combine check results into a qualification decision. Use the account-qualification recipe for that.

**Section status.** Each section (`company_checks`, `contacts`, `tech_intel`, `ai_adoption`) has a status:

| Status | Meaning |
| - | - |
| `ok` | Retrieved. |
| `no_match` | People search ran and found nobody. |
| `no_data` | Tech Intel holds no technology for this company. |
| `not_found` | AI Adoption holds no data for this company. |
| `not_available` | Tech Intel answered 403, which means it is not on the API plan. |
| `error` | A request in the section failed. |
| `not_asked` | The run stopped before the section was asked. |
| `not_requested` | The configuration does not ask for the section. |

`no_match`, `no_data` and `not_found` are valid answers, not errors.

**Row status.** Each row gets one status:

| Status | Rule |
| - | - |
| `complete` | No section has the status `error`, `not_asked` or `not_available`. |
| `partial` | At least one section has the status `error`, `not_asked` or `not_available`. The reason names each one. |
| `invalid_input` | The domain is not a valid domain. |
| `company_not_found` | Firmable has no company profile for the domain, so no pack was built. |
| `error` | The company profile request failed. |
| `not_processed` | The run stopped before this row. |

A `complete` row whose Tech Intel stopped at the page limit says so in its status reason, with the number retrieved and the total.

**Suggested questions.** Questions come from five fixed templates, in this order, up to `max_questions`:

| Template | Used when |
| - | - |
| `confirm_unknown_check` | A company check answered unknown. One question per check. |
| `who_else` | The people searches found nobody. |
| `technology_category` | Tech Intel observed a technology with `high` confidence. One question per category, for the first two categories. The question asks about the category, not the product. |
| `ai_adoption` | AI Adoption returned an overall level. |
| `why_now` | Always. It is not based on API data. |

Each question has a `basis` that names the facts it was filled from. Questions are kept in `suggested_questions`, apart from the facts.

**Data quality notes.** The recipe lists these problems where it can detect them, in `data_quality_notes`. It does not correct the data.

- A contact's person profile names a different current company ID from the account.
- A Decisions answer is about a different entity ID from the account.
- A Tech Intel or AI Adoption response is for a different company ID.
- A Tech Intel evidence record has no source location.
- An AI Adoption evidence area is truncated.

## Output

- `report.html`: a summary of statuses, then one card per account. The left side has the status reason, the company profile, data quality notes and company checks. The right side has contacts, Tech Intel, AI Adoption and, in a separate dashed box, the suggested questions. Each fact shows its source as `#<ledger seq> <endpoint>`. The raw requests and responses are at the end, numbered by the same sequence.
- `results.csv`: one row per input row, in input order, with the status, company facts, compact check results, contact names, technology names, the AI Adoption level, data quality notes and suggested questions.
- `results.json`: everything, including every fact's `source` (`endpoint`, `ledger_seq`, `reused`), every check's `confidence`, `details`, `reasoning`, `answered_at` and `credits`, and the request ledger.

`source.ledger_seq` is the position of the response in the `ledger` array, counting from 1. `reused` is true when the client answered an identical request from memory; `ledger_seq` then points to the original response.

`confidence`, `reasoning`, Tech Intel fields and AI Adoption explanations come from the API and are shown unchanged.

## Estimated cost

For 100 unique domains with the example configuration (2 company checks, 2 searches, 3 contacts, 2 contact checks, 1 Tech Intel page, no AI Adoption): up to 100 credits for company profiles, 300 for contact profiles, 64 for 800 answers with reasoning and 500 for Tech Intel, 964 credits in total. Tech Intel is the largest part. Profiles your organisation already paid for are free, and unknown answers are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-pre-call-account-pack --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-pre-call-account-pack --input examples/input.csv --config config.example.json --out out --live --max-credits 35
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 35
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, the rest of the current row is `not_asked`, and later rows are `not_processed`.
- Nothing is retried automatically.
- Company and person profiles carry no date in the response. The only dates in the pack are the API fields that hold them: Decisions `answered_at` and `last_refreshed`, Tech Intel `last_observed`, person `month_joined` and `year_joined`, and AI Adoption `as_of`.
- People search filters by company, country, position, seniority and department only. It has no name search and no date filters. Each search takes one seniority id and one department id. To cover two seniorities, configure two searches.
- People search with no matches returns `success: false` and no `records` list. The shared client returns it with an empty `records` list. This recipe counts it as zero matches and says so in the coverage notes.
- The recipe takes one page of people search results per search (`from` is `0`). People beyond `size` are not retrieved.
- A technology that is not listed was not observed in retrieved data. It does not mean the company does not use it. When Tech Intel stopped at the page limit, more technologies exist than the pack shows.
- A detected technology or an AI Adoption level shows what Firmable observed. It does not show buying intent, spend, licence count or active use.
- Some decision tools use a decision model inside Firmable's service. The report names them. This code adds no model of its own, and the suggested questions are fixed templates.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
- AI Adoption workforce evidence has no field that names the organisation each person belongs to, so the recipe cannot detect workforce evidence that belongs to a different organisation. It reports a different company ID on the AI Adoption answer itself.
