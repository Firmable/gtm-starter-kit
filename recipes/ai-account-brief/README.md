# AI account brief

Builds a factual brief for each account from its Firmable company profile, AI Adoption and Tech Intel. The brief shows the overall AI adoption level, explanation and momentum, the five AI adoption areas with their evidence, the technologies observed, and suggested discovery questions filled from fixed templates.

## Business problem

Before a meeting, a seller wants to know how far an account has adopted AI and what supports that view. Reading raw API responses is slow, and a summary written by hand or by a model can drift from what the data says.

This recipe lays out the API's own levels, explanations and headlines without changing them. It separates evidence that counts towards a level from evidence that does not, and it says where evidence is missing, truncated or questionable. Discovery questions come from templates you write, filled only with API facts and your own seller context, and they are marked as suggestions.

## Inputs

**Input file** (CSV or JSON). One row per account.

| Column | Required | Meaning |
| - | - | - |
| `domain` | Yes | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `ai_evidence` | `true` | Requests AI Adoption evidence (`withEvidence=true`). Costs 3 credits per company found instead of 0.2. |
| `tech_intel` | `true` | Reads Tech Intel for each company. |
| `tech_intel_with_evidence` | `false` | Requests Tech Intel evidence. Pages then hold 10 technologies instead of 100, and each page costs 0.2 credits. |
| `tech_intel_max_pages` | `1` | The most Tech Intel pages read per company, from 1 to 100. |
| `seller_context` | `{}` | Your own values for templates, such as `{"offering": "our data platform"}`. Keys are lower-case letters, digits and underscores, starting with a letter. A key cannot reuse an API fact placeholder name. |
| `discovery_questions` | `[]` | The question templates, in order. |
| `discovery_questions[].id` | Required | A short identifier, lower-case letters, digits and underscores. |
| `discovery_questions[].template` | Required | The question text with `{placeholder}` names. |

Templates can use these placeholders, plus any `seller_context` key:

| Placeholder | Value | Source |
| - | - | - |
| `company_name` | `name` | Company profile |
| `industry` | The first entry of `industries` | Company profile |
| `hq_country` | `hq_country` | Company profile |
| `overall_level` | `overall_level` | AI Adoption |
| `momentum_label` | `momentum.label` | AI Adoption |
| `architecture_level`, `footprint_level`, `hiring_level`, `initiatives_level`, `workforce_level` | The area's `level` | AI Adoption |
| `ai_tool` | The `technology` of the first qualifying footprint evidence item | AI Adoption evidence |
| `ai_role` | The `title` of the first qualifying hiring evidence item | AI Adoption evidence |
| `technology` | The `product` of the first Tech Intel technology with `confidence` `high`, in the order the API returned | Tech Intel |

A template that names an unknown placeholder, has an unmatched brace or repeats an id is rejected before any call.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /company?fqdn=` | Once per unique valid domain | Up to 1 credit per new profile |
| `POST /ai-adoption?withEvidence=true` | Once per company found | 3 credits per company found with evidence; 0.2 without evidence; free when not found |
| `GET /tech-intel?id=` | First page once per company found, when `tech_intel` is true | Up to 5 credits, paid once per company |
| `GET /tech-intel?...&cursor=` | Each further page, up to `tech_intel_max_pages` | Free without evidence; 0.2 credits with evidence |

Duplicate domains in the input reuse the earlier responses and are not requested again.

## Workflow

1. Read and validate the input and configuration, including every template.
2. For each row: normalise the domain and fetch the company profile. No profile means no brief.
3. Read AI Adoption by Firmable company ID.
4. Read Tech Intel by Firmable company ID, when configured.
5. Lay out the brief, check the evidence and compare company facts between endpoints.
6. Fill the templates.
7. Write `report.html`, `results.csv` and `results.json`.

## Brief rules

**AI Adoption.** The overall level, explanation, momentum label and signals, data date and framework version are shown as returned. Each of the five areas shows its level, headline, observability, whether it counts towards the overall level, how many evidence items Firmable holds, and its area-specific detail. A level that is not one of the documented levels (`NONE`, `BASIC`, `MEANINGFUL`, `MODERATE`, `STRONG`, `ADVANCED`) is shown as returned with a note.

**Evidence.** Each area's evidence items are split by the API's `qualifies` field:

| Group | Rule |
| - | - |
| Qualifying | `qualifies` is `true`. The item counts towards the area's level. |
| Not qualifying | `qualifies` is `false`. |
| Qualification unstated | `qualifies` is missing or not true or false. |

Each area is flagged when:

- the API returned no data for the area;
- `evidence_available` is above 0 but no evidence came back for the area;
- the area's evidence is `truncated`;
- a workforce item names a different organisation in a field the API returned (see below).

**Workforce items from another organisation.** The documented workforce item fields do not name an employer. The recipe checks only fields that name an organisation when the API returns them: `company_id`, `firmable_id` and `organisation_id` are compared with the AI Adoption `firmable_id`; `company_name`, `employer`, `organisation` and `company` are compared, ignoring case, with the company name from AI Adoption and the company profile. A flagged item is shown, not removed. The recipe does not guess from titles or other text.

**Conflicting facts.** The brief lists, without correcting, a different company ID, company name or country between the company profile and AI Adoption.

**Tech Intel.** Technologies are listed as returned, with confidence, recency, last observed date and nature. When only some pages were read, the brief says how many technologies were retrieved out of the total. A technology not listed is not observed in retrieved data.

**Discovery questions.** A template is filled only when every placeholder has a value. A template with any missing value is skipped and listed with the missing placeholder names. Every filled template is marked `suggested_question` and records which placeholder came from which source. Questions are shown in a separate box in the report and never mixed with API facts.

Each account gets one status:

| Status | Rule |
| - | - |
| `brief_ready` | AI Adoption returned the company, and Tech Intel was retrieved or turned off. |
| `no_ai_adoption_data` | AI Adoption has no data for the company (`company` is `null`). The brief shows the profile and Tech Intel. |
| `incomplete` | AI Adoption or Tech Intel failed, Tech Intel returned 403, or the run stopped part way through the row. The reason says which. |
| `company_not_found` | Firmable has no company profile for the domain. No other call is made. |
| `invalid_input` | The domain is not a valid domain. |
| `error` | The company profile request failed. |
| `not_processed` | The run stopped before this row. |

## Output

- `report.html`: a status summary, then one card per account. The left side has the conclusion, company facts, AI Adoption overview and any conflicting facts. The right side has the area table, each area's headline, flags and evidence, the Tech Intel list, and the suggested questions in a separate box. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, levels, Tech Intel totals, flags, suggested questions and skipped template ids.
- `results.json`: everything, including every evidence item as returned, the template sources, and the request ledger.

## Estimated cost

For 100 unique domains with AI Adoption evidence and one Tech Intel page each: up to 100 credits for profiles, 300 credits for AI Adoption and 500 credits for Tech Intel, 900 credits in total. Profiles and technologies your organisation already paid for are free, and companies without AI adoption data cost nothing for AI Adoption. Without AI evidence, AI Adoption costs up to 20 credits for the same 100 companies. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-ai-account-brief --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-ai-account-brief --input examples/input.csv --config config.example.json --out out --live --max-credits 30
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 30
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`.
- Tech Intel must be on your plan. After a 403, Tech Intel is not requested for later rows, and those briefs are `incomplete`.
- Nothing is retried automatically.
- This code computes no AI score and no historical trend. Levels, explanations and headlines are the API's own words and are not rewritten.
- An AI adoption level or a detected technology does not prove buying intent.
- AI Adoption evidence can be truncated. The brief says how many items came back out of `item_count`.
- Workforce evidence that belongs to another organisation is detectable only when the API returns a field naming that organisation. The documented fields do not include one.
- AI Adoption also accepts up to 100 companies in one request. The recipe sends one company per request so each brief has its own response in the ledger.
