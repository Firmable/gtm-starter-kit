# Tech sales opportunities

Finds the technologies you care about, such as your integrations, your competitors or the platforms you implement, in each company's Firmable Tech Intel. Reports each match with its confidence, recency, observation dates and evidence locations, and reports every target that was not observed in retrieved data.

## Business problem

A seller wants to know which target accounts run a product they integrate with, a competitor's product, or a platform their services team implements. Checking each account by hand is slow, and loose name matching produces false matches.

This recipe reads Firmable Tech Intel for each company and matches it against an explicit list of target technologies. A technology that was not found is reported as "not observed in retrieved data", which is different from "not used". When only some pages of a company's technology list were read, an unmatched target is "not observed in the pages retrieved".

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
| `targets` | Required | The technologies to look for. |
| `targets[].id` | Required | A short identifier, lower-case letters, digits and underscores. |
| `targets[].label` | The id | The name shown in reports. |
| `targets[].relationship` | Required | Your relationship to the technology, such as `competitor`, `integration` or `implementation opportunity`. Shown as written. |
| `targets[].match.product_slugs` | `[]` | Tech Intel `product_slug` values that match exactly. |
| `targets[].match.vendor_slugs` | `[]` | Tech Intel `vendor_slug` values that match exactly. |
| `targets[].match.aliases` | `[]` | Names that match a technology's `product` or `vendor` name exactly, ignoring case and surrounding spaces. |
| `with_evidence` | `true` | Requests the evidence behind each technology (`withEvidence=true`). Pages then hold 10 technologies instead of 100, and each page costs 0.2 credits. |
| `max_pages` | `3` | The most Tech Intel pages read per company, from 1 to 100. |
| `fetch_company` | `false` | Fetches the company profile first and reads Tech Intel by Firmable company ID. The profile's domain and website are then also treated as company domains for evidence locations. |

Each target needs at least one slug or alias. Slugs are compared exactly, so copy them from a Tech Intel response rather than guessing them.

## API calls

| Call | When | Cost |
| - | - | - |
| `GET /company?fqdn=` | Once per unique valid domain, when `fetch_company` is true | Up to 1 credit per new profile |
| `GET /tech-intel?fqdn=&withEvidence=true` | First page, once per unique valid domain | Up to 5 credits, paid once per company; plus 0.2 credits with evidence |
| `GET /tech-intel?...&cursor=` | Each further page, up to `max_pages` in total | Free without evidence; 0.2 credits with evidence |

Duplicate domains in the input reuse the earlier responses and are not requested again.

## Workflow

1. Read and validate the input and configuration.
2. For each row: normalise the domain, then fetch the company profile when `fetch_company` is true.
3. Read Tech Intel, following `next_cursor` until the last page or `max_pages`.
4. Match every retrieved technology against every target.
5. Check every evidence record's location against the company domain.
6. Write `report.html`, `results.csv` and `results.json`.

## Matching rules

A technology matches a target when any of these is true:

- its `product_slug` is in `match.product_slugs`;
- its `vendor_slug` is in `match.vendor_slugs`;
- its `product` or `vendor` name equals an alias, ignoring case and surrounding spaces.

There is no fuzzy or partial matching. An alias of `Snowflake` does not match a product named `Snowflake Connector`. Every match records which rule matched it in `match_basis`. One technology can match more than one target.

Each target gets one result per company:

| Result | Rule |
| - | - |
| `observed` | At least one retrieved technology matched. |
| `not_observed` | Nothing matched, and every page of the technology list was retrieved. |
| `not_observed_in_pages_retrieved` | Nothing matched, but the technology list is incomplete. The unread pages were not checked. |
| `not_checked` | No Tech Intel data was retrieved for the company. |

Each company gets one status:

| Status | Rule |
| - | - |
| `matched` | At least one target was observed. The reason says when the list is incomplete. |
| `not_observed` | No target was observed in a complete technology list. |
| `not_observed_incomplete` | No target was observed in the pages retrieved, and the list is incomplete. |
| `no_technology_data` | Firmable knows the company but holds no technology data for it (`total` is 0). |
| `company_not_found` | Tech Intel or the company profile returned 404. |
| `not_on_plan` | Tech Intel returned 403. The run stops, because every later request would also be refused. |
| `invalid_input` | The domain is not a valid domain. |
| `error` | The first Tech Intel page failed. |
| `not_processed` | The run stopped before this row. |

A list is incomplete when the page limit was reached, a later page failed, the credit limit was reached, or the API repeated a cursor. Pages already fetched are kept and matched.

## Evidence checks

With evidence on, every evidence record of a matched technology is shown with its type, what was found, location, record, the reasons it belongs to the company, dates and confidence. Each record is checked:

| Flag | Rule |
| - | - |
| `no_location` | The record has no location. Firmable leaves `location` empty when one finding was seen on more than 25 company sites. |
| `location_differs_from_company_domain` | The location's host is not the company domain or one of its subdomains. The record is shown, not dropped. Job ads and review sites are often on other domains. |
| `location_not_a_domain` | The location is not a domain or URL, so it could not be compared. |

The company domains are the input domain and, when `fetch_company` is true, the profile's `fqdn` and `website`. The recipe also reports when fewer records are listed than `evidence_count`, and which evidence types the API marks as `capped`.

## Output

- `report.html`: a status summary, then one card per company with the Tech Intel totals on the left and each target's result, matched technologies and evidence on the right. The raw requests and responses are at the end.
- `results.csv`: one row per input row, in input order, with the status, reason, totals, target results and a compact list of observed technologies.
- `results.json`: everything, including every matched technology as returned, every evidence record as returned with its flags, and the request ledger.

`confidence`, `recency`, `last_observed`, `nature` and evidence fields come from the API and are shown unchanged.

## Estimated cost

For 100 unique domains with evidence and `max_pages` of 3: up to 500 credits for technologies and 60 credits for evidence pages, 560 credits in total. Technologies are paid once per company, so companies your organisation already paid for cost only the evidence pages. Without evidence, the same run costs up to 500 credits and reads up to 100 technologies a page. Run in plan mode to see the upper bound for your own file.

## Commands

Install first; see [Getting started](../../docs/getting-started.md).

```bash
# Python, plan mode (no API calls)
gtm-tech-sales-opportunities --input examples/input.csv --config config.example.json --out out

# Python, live
export FIRMABLE_API_KEY=fbl_dev_...
gtm-tech-sales-opportunities --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

```bash
# TypeScript, plan mode (no API calls)
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out

# TypeScript, live
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out --live --max-credits 20
```

The underlying requests are in [curl/requests.sh](curl/requests.sh).

## Errors, limitations and unknowns

- Tech Intel must be on your plan. A 403 stops the run and every row is reported.
- A request that times out or gets a 5xx response may still have been charged. It is counted under "Requests with an uncertain charge" and its reservation is kept.
- A bad key, no credits, a blocked redirect or reaching `--max-credits` stops the run. Results so far are written, and later rows are marked `not_processed`.
- Nothing is retried automatically.
- "Not observed" means not observed in retrieved data. It never means the company does not use the technology.
- Tech Intel reports observed signals with Firmable's confidence and recency. It does not report replacement intent, spend, licence count or active usage, and this recipe does not infer them. A detected technology does not prove buying intent.
- With evidence on, each page holds 10 technologies, so a large technology list needs many pages. A low `max_pages` leaves the list incomplete, and unmatched targets are then "not observed in the pages retrieved".
- The API lists up to 25 evidence records per type. `evidence_count` can be higher than the records listed.
- The evidence location check compares host names only. It cannot tell whether a different domain belongs to the same organisation, so it flags the record and leaves the judgement to you.
- Tech Intel accepts one company per request. The recipe reads one company at a time.
