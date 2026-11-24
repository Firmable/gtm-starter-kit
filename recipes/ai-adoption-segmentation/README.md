# AI adoption segmentation

Assigns each company in a list to one AI-related campaign segment, using Firmable AI Adoption levels, dimensions and momentum, with optional Tech Intel and Decisions. Every assignment comes with the conditions that produced it.

## Business problem

Marketing and sales teams want to run different campaigns for companies at different stages of AI adoption: leaders, companies expanding their AI work, companies with AI tools but little else, and companies with a low adoption level. Sorting a list by hand is slow and inconsistent. Sorting it with a script that treats missing data as "no AI" quietly drops accounts into the wrong campaign.

This recipe applies written, ordered rules to the levels Firmable returns. It keeps unknown data visible, sends accounts with missing or unfamiliar data to an explicit review segment, and shows which conditions placed each account where it is.

## Inputs

**Input file** (CSV or JSON). One row per company.

| Column | Required | Meaning |
| - | - | - |
| `domain` | Yes | The company's website domain. A full URL also works; the scheme, `www.` and path are removed. |
| `row_id` | No | Your own row identifier. Defaults to `row-1`, `row-2` and so on. Must be unique. |
| Any other column | No | Kept with the row in `results.json`. |

**Configuration** (`config.example.json`).

| Key | Default | Meaning |
| - | - | - |
| `levels` | `NONE, BASIC, MEANINGFUL, MODERATE, STRONG, ADVANCED` | The level names in rank order, lowest first. `at_least` and `at_most` compare positions in this list. |
| `segments` | Required | The segments, in the order they are tried. |
| `segments[].id` | Required | A short identifier, lower-case letters, digits and underscores. |
| `segments[].label` | The id | The name shown in reports. |
| `segments[].description` | `""` | A note for people reading the configuration. |
| `segments[].match` | `all` | `all`: every condition must be met. `any`: one met condition is enough. |
| `segments[].conditions` | Required | The conditions; see the table below. |
| `review_segment` | `review`, "Needs review" | Where accounts go when a segment cannot be evaluated. |
| `unmatched_segment` | `unmatched`, "No segment matched" | Where accounts go when no segment matches. |
| `with_evidence` | `false` | Asks AI Adoption for evidence. The evidence is summarised in the output; the rules do not use it. Raises the price from 0.2 to 3 credits per company found. |
| `tech_intel_max_pages` | `1` | Pages of up to 100 technologies to fetch per company for technology conditions, from 1 to 20. |
| `decision_checks` | `[]` | Decisions checks that segments can use: `id`, `label`, `tool`, `inputs`, `want` (`yes` or `no`). Each check must be used by at least one segment. |
| `with_reasoning` | `true` | Asks Decisions for the reason, source and refresh date behind each answer. |

Condition types:

| Type | Keys | Met when |
| - | - | - |
| `overall_level` | One of `at_least`, `at_most`, `in` | The overall AI Adoption level compares as stated. |
| `dimension_level` | `dimension` and one of `at_least`, `at_most`, `in` | The level of `architecture`, `footprint`, `hiring`, `initiatives` or `workforce` compares as stated. |
| `momentum` | `in` | The momentum label is one of the listed labels, such as `EXPANDING`. |
| `momentum_signal` | `kind`, `min_count` (default 1) | A momentum signal of that kind, such as `AI hiring`, has at least that count. |
| `technology` | One or more of `category_slug`, `filter_slug`, `product_slug`, `vendor_slug` (lists); `min_confidence` (default `low`); `min_count` (default 1) | At least `min_count` Tech Intel technologies match every given slug list with at least that confidence. |
| `decision` | `check` | The Decisions check gave its wanted answer. |

Level names in conditions must appear in `levels`. Decision tool names and inputs are checked against the live tool list before any paid call.

## API calls

| Call | When | Cost |
| - | - | - |
| `POST /ai-adoption` | Once per unique valid domain, by domain | 1 credit per 5 companies found; 3 credits per company found with `withEvidence=true`; not found is free |
| `GET /tech-intel?id=` | Up to `tech_intel_max_pages` pages per unique domain, only when a segment being tried has a technology condition | Up to 5 credits per company, paid once per company |
| `GET /decisions` | Once per run, only when `decision_checks` is not empty | Free |
| `POST /decisions?withReasoning=true` | Once per check per unique domain, only when a segment being tried uses the check | 0.08 credits per yes or no answer with reasoning, 0.04 without; unknown answers are free |

Tech Intel and Decisions are asked by the Firmable company ID from AI Adoption when it came back, otherwise by domain. Duplicate domains reuse the earlier responses. Plan mode counts every possible call; a live run usually makes fewer, because an account that matches an early segment never reaches the conditions that need Tech Intel or Decisions.

## Workflow

1. Read and validate the input and configuration.
2. When `decision_checks` is not empty, read the decision tool list and check every configured tool and input. Stop before paid calls if any is wrong.
3. For each row: normalise the domain and fetch AI Adoption.
4. Check the levels the rules read against `levels`.
5. Try the segments in order, fetching Tech Intel or asking a Decisions check the first time a condition needs it.
6. Write `report.html`, `results.csv` and `results.json`.

## Segment rules

Each condition has one outcome:

- **met**: the data satisfies the condition.
- **not_met**: the data does not satisfy the condition. A momentum signal or technology that is absent from a complete list counts as not met, and the explanation says it was not observed in retrieved data.
- **unknown**: the data needed is missing, such as AI Adoption not found, an area or level not returned, a decision answered unknown, or a technology list that stopped before the end.
- **error**: a request failed, so there is no data to judge.
- **not_evaluated**: an earlier condition already decided the segment, so this one was skipped.

Each segment combines its conditions:

| Match | Outcome |
| - | - |
| `all` | Not matched if any condition is not met. Otherwise error if any condition errored, unknown if any is unknown, and matched if every condition is met. |
| `any` | Matched if any condition is met. Otherwise error if any condition errored, unknown if any is unknown, and not matched if every condition is not met. |

Each account gets the first assignment that applies:

| Status | Segment | Rule |
| - | - | - |
| `invalid_input` | None | The domain is not a valid domain. |
| `needs_review` | Review | A level that a rule reads is not in `levels`. The account is not ranked. |
| `assigned` | The matched segment | The first segment, in order, whose outcome is matched. |
| `needs_review` | Review | The first segment that is not "not matched" is unknown. Later segments are not tried. |
| `incomplete` | Review | The first segment that is not "not matched" is error. Later segments are not tried. |
| `no_segment_matched` | Unmatched | Every segment is not matched. |

Other statuses: `error` (the row failed outside the rules) and `not_processed` (the run stopped before this row).

Why an unknown segment stops the walk: if an account might belong to a higher-priority segment but the data to decide is missing, assigning it to a lower segment would rank it on missing information. The review segment keeps that decision with a person.

An unfamiliar level in an area that no rule reads is listed in `unfamiliar_levels` and in the coverage notes, but does not stop the account being segmented. Add a new level to `levels` in the right position once you know where it ranks.

When a condition reads an area whose `observability` is `LOW`, the explanation says so. The level is still used as returned.

## Output

- `report.html`: a summary of statuses and segment counts, the segment rules in order, then one card per account with the segment, the reason and the AI Adoption data as returned on the left, and the rule trace, unknowns, decision reasons and evidence summary on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, segment, reason, the overall and area levels, unknowns and a compact trace.
- `results.json`: everything, including the full trace with each condition's outcome and explanation, the AI Adoption summary, technology counts, decision answers with `reasoning`, the evidence summary, and the request ledger with every raw response.

The AI Adoption `explanation` and `headline` values and the decision reasons are shown as returned. The explanation text each condition produces is fixed template text built from the returned values.

## Estimated cost

For 100 unique domains with the example configuration: up to 20 credits for AI Adoption and 500 credits for Tech Intel, 520 credits in total. In practice, accounts that match the first two segments never reach the technology condition, so Tech Intel is fetched only for the rest. Tech Intel companies your organisation already paid for are free. With `with_evidence` set to true, AI Adoption rises to up to 300 credits. Run in plan mode to see the upper bound for your own file and configuration.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-ai-adoption-segmentation --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-ai-adoption-segmentation --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- A segment is a campaign grouping from your rules. An AI adoption level, a momentum label or a detected technology does not prove buying intent.
- AI Adoption not found means Firmable returned no AI Adoption data for the company. It is not evidence that the company has no AI adoption, and the account goes to review if a rule needs that data.
- AI Adoption levels come from Firmable's scoring framework (`framework_version` in the output). A framework change can move levels without any change at the company.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`.
- Nothing is retried automatically.
- Tech Intel returns 403 when it is not on your plan. Accounts that reach a technology condition then get `incomplete`.
- Technology conditions use the Tech Intel list without evidence, so they rely on Firmable's `confidence` value and cannot check the source of each detection. Tech Intel evidence is not requested.
- The rules do not use AI Adoption evidence. With `with_evidence` set to true, the output counts qualifying and non-qualifying items and flags truncated areas. The code cannot tell whether an evidence item, such as a workforce member, belongs to a different organisation; check the evidence in `results.json` before relying on it.
- Some decision tools use a decision model inside Firmable's service. The report names them. This code adds no model of its own.
- AI Adoption is asked one company per request for readability. The API also accepts up to 100 companies per request; see the [AI Adoption docs](https://docs.firmable.com/api-reference/ai-adoption.md).
