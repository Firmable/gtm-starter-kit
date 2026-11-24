# Duplicate account review

Reviews candidate duplicate pairs of company records that you supply, and reports each pair as a likely duplicate, distinct, or needing a person to review it.

## Business problem

A CRM collects duplicate accounts over time: the same company entered under two domains, or a subsidiary entered as its parent. Merging on a guess loses history, and leaving duplicates splits ownership and activity. Your CRM or a matching script can produce candidate pairs. Someone still has to decide which pairs are real duplicates.

This recipe looks up both records in each pair, compares the identifiers Firmable holds for them, and asks Firmable's `is_same_business` decision tool. Each outcome comes with the identifiers that agree, the identifiers that differ, and any contradiction between the answer and the identifiers. The recipe compares only the pairs you supply. It never merges or deletes anything.

## Inputs

**Input file** (CSV or JSON). One row per candidate pair.

| Column | Required | Meaning |
| - | - | - |
| `left_domain` | One of `left_domain` or `left_company_id` | The left company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `left_company_id` | One of `left_domain` or `left_company_id` | The left company's Firmable company ID: `f` followed by 12 digits. When both are given, the ID is used for the lookup and the domain is compared with the profile. |
| `right_domain` | One of `right_domain` or `right_company_id` | The right company's website domain. |
| `right_company_id` | One of `right_domain` or `right_company_id` | The right company's Firmable company ID. |
| `left_row_id` | No | Your own identifier for the left record, such as a CRM account ID. Copied to the output. |
| `right_row_id` | No | Your own identifier for the right record. Copied to the output. |
| `row_id` | No | An identifier for the pair. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. Doubles the Decisions price. |

The recipe checks that `is_same_business` is in the live tool list before any paid call.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /decisions` | Once per run, to check that `is_same_business` is available | Free |
| `GET /company?fqdn=` or `GET /company?id=` | Once per unique valid side | Up to 1 credit per new profile |
| `POST /decisions?withReasoning=true` with tool `is_same_business` | Once per unique pair of different Firmable records | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |

`is_same_business` takes the left company as `company_id` and the right company's Firmable ID as `other_company_id`, so both sides are resolved to a profile first. A side that appears in several pairs is looked up once. A pair repeated in either order reuses the earlier answer.

## Workflow

1. Read and validate the input and configuration. Every row needs an identifier on both sides.
2. Read the decision tool list and check `is_same_business`. Stop before paid calls if it is missing or its inputs have changed.
3. For each pair: read both identifiers and fetch both company profiles.
4. Compare the identifiers the two profiles hold.
5. Ask `is_same_business`, unless an earlier rule already settles the pair.
6. Apply the outcome rules.
7. Write `report.html`, `results.csv` and `results.json`.

## Outcome rules

**Identifiers compared.** Each is `same`, `different` or `missing`. Missing means one or both profiles did not return it; it was not observed in retrieved data, which does not mean the company has none.

| Identifier | Profile field | Compared as |
| - | - | - |
| `abn` | `industry_codes.abn` | Spaces removed |
| `acn` | `industry_codes.acn` | Spaces removed |
| `nzbn` | `industry_codes.nzbn` | Spaces removed |
| `fqdn` | `fqdn` | Normalised domain |
| `linkedin` | `linkedin` | Case ignored |
| `website_domain` | `website` | Domain of the URL |
| `name` | `name` | Case ignored, otherwise exact |
| `hq_country` | `hq_country` | Case ignored |

`supporting_identifiers` lists the identifiers that are the same. `conflicting_identifiers` lists those that differ. `missing_identifiers` lists those not observed. Name and head office country are context only; no outcome rule uses them.

**Contradictions.**

- An answer of yes is contradicted when the two profiles hold different ABN, ACN or NZBN values. Different registry numbers mean different legal entities, even when the records share a root domain.
- An answer of no is contradicted when the two profiles share an ABN, ACN, NZBN, domain or LinkedIn handle.

Each pair gets the first status that applies:

| Status | Rule |
| - | - |
| `invalid_input` | A side has no valid domain or company ID. |
| `needs_review` | A side has no Firmable profile. |
| `needs_review` | A side's input domain differs from its profile's `fqdn`. `is_same_business` is not asked. |
| `likely_duplicate` | Both sides resolve to the same Firmable company record. `is_same_business` is not asked. |
| `incomplete` | `is_same_business` returned an error or was not asked because the run stopped. |
| `needs_review` | `is_same_business` answered unknown. Unknown is never treated as distinct. |
| `needs_review` | The answer is contradicted by the profile identifiers. |
| `likely_duplicate` | `is_same_business` answered yes. |
| `distinct` | `is_same_business` answered no. |

Other statuses: `error` (the pair failed before an answer, for example a company lookup error) and `not_processed` (the run stopped before this pair).

## Output

- `report.html`: a summary of outcomes, then one card per pair with the conclusion, both inputs and CRM row IDs, contradictions and notes on the left, and the identifier comparison and the `is_same_business` answer with its `details` and reasoning on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input pair, in input order, with the status, reason, both Firmable IDs and names, the answer, its confidence and the identifier lists.
- `results.json`: everything, including both company profiles' common fields and `industry_codes`, every identifier comparison with both values as returned, and the full answer with `confidence`, `details`, `reasoning` and `credits`, and the request ledger.

`confidence`, `details` and `reasoning` come from the API and are shown unchanged.

## Estimated cost

For 100 pairs with 200 distinct sides, with reasoning: up to 200 credits for company profiles and 8 credits for answers, 208 credits in total. Profiles your organisation already paid for are free, and unknown answers are free. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-duplicate-account-review --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-duplicate-account-review --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- The recipe compares only the pairs in the input file. It does not generate pairs, so it never runs an all-pairs comparison across a large file. Produce candidate pairs with your own blocking rules, such as a shared domain stem or name.
- Nothing is merged or deleted. Each outcome is a recommendation for a person to act on in the CRM.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later pairs are marked `not_processed`.
- Nothing is retried automatically.
- A company lookup error on either side marks the pair `error`, and the other side may not have been looked up.
- `is_same_business` compares two Firmable records by record, by a shared ABN or NZBN with the same country and name, or by a shared root domain, as its tool summary describes. It cannot compare a record that has no Firmable profile.
- The `is_same_business` tool summary refers to an `owns_domain` tool for domain ownership questions. That tool was not in the tool list read on 24 November 2026, so this recipe does not use it.
- The recipe does not send the `country` parameter to `GET /company`, so profiles return Australian-region data. Country-specific identifiers such as NZBN may be absent from those profiles. A missing NZBN is reported as missing, not as different.
- `GET /decisions` lists `is_same_business` as not using a decision model. Other Firmable decision tools do. This code adds no model of its own.
- Decisions answer from Firmable data as of the `last_refreshed` date in the reasoning. A yes or no reflects that data, not a live check.
