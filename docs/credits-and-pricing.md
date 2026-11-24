# Credits and pricing

Prices below are from the public Firmable documentation on **24 November 2026**. They are kept in one place in code: `shared/python/src/firmable_client/pricing.py` and `shared/typescript/src/pricing.ts`. Check the [credits page](https://docs.firmable.com/developer/credits.md) for current prices.

## Published prices

| Call | Credits |
| - | - |
| `GET /company` | Up to 1 per company profile new to your organisation; free after that |
| `GET /people` | Up to 1 per person profile new to your organisation; free after that |
| `POST /people/search` | Free |
| `GET /decisions` | Free |
| `POST /decisions` | 0.04 per yes or no answer (1 per 25); 0.08 with `withReasoning=true` (2 per 25); unknown answers free |
| `POST /ai-adoption` | 0.2 per company found (1 per 5); 3 per company found with `withEvidence=true`; not found free |
| `GET /tech-intel` | 1 per technology, up to 5 per company, paid once per company |
| `GET /tech-intel?withEvidence=true` | Adds 0.2 per request (1 per 5), on every page, repeats included |

Part-credits carry over between calls and are taken as whole credits.

## Four different numbers

Every run reports four figures. They are not the same thing.

| Figure | Where it comes from |
| - | - |
| **Published estimate** | The upper bound from the published prices, assuming every profile is new to your organisation. Shown in plan mode and in the report. |
| **Reservation** | What this code holds against your `--max-credits` limit. It is the published estimate plus 1 credit for each fractional price category used in the run (decisions, AI adoption without evidence, Tech Intel evidence), because carried-over part-credits can turn a small charge into a whole-credit deduction. |
| **Response-reported credits** | The `credits` field some responses include, such as Decisions answers. Not every endpoint reports credits. |
| **Actual wallet deduction** | Not exposed by the API. Check **Usage** in the developer portal. |

## The credit limit

`--max-credits` is enforced by this code, before each request is sent. It is not a spending cap on your Firmable account. A request that would take the reservation past the limit is not sent, the run stops, and the results so far are written.

## Uncertain charges

A request that times out, or gets a 5xx response from a paid endpoint, may still have been charged. These are counted as "requests with an uncertain charge", and their reservation is kept. Nothing is retried automatically, because a retry could be charged again.

## Free credits

New developer accounts get 100 free credits, once per company email domain, and 40 free credits on the 1st of each month (Sydney time). Monthly credits do not roll over.
