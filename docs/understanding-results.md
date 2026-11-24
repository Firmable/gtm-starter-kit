# Understanding results

Every live run writes three files to the output folder.

| File | For | Contents |
| - | - | - |
| `report.html` | People | A readable report. Open it in a browser. |
| `results.csv` | Spreadsheets | One row per input row, in input order. |
| `results.json` | Code and audit | Everything, including the raw request and response ledger. |

Plan mode writes `plan.json` only.

## The report

Each report has the same parts:

1. **Input and request**: the input file, row count, mode and the configuration used.
2. **The result**: a summary, then one card per input row. The conclusion and key facts sit on the left. The evidence, answers and missing information sit on the right.
3. **Coverage, limitations and errors**: what the run could not see, and every error with its endpoint and HTTP status.
4. **Credit use**: the four credit figures; see [credits-and-pricing.md](credits-and-pricing.md).
5. **Every request and response**: the ledger, expandable, without the API key.

## Words with a fixed meaning

| Term | Meaning |
| - | - |
| `yes`, `no` | The Decisions API answered from Firmable data. |
| `unknown` | Firmable does not hold the fact needed to answer. It is never counted as no, pass or fail. |
| `error` | The request failed or returned something unusable. Different from unknown. |
| `not_asked` | The run stopped before this question was asked. |
| `not_processed` | The run stopped before this row was reached. |
| Not observed in retrieved data | The data the run fetched does not show it. It may still exist. |
| Suggested question | A fixed template filled from facts. It is not a fact and was not written by a model. |

## What results do not prove

- A detected technology or an AI adoption level does not prove buying intent, spend, licence count or active use.
- A new role does not prove a purchase is coming.
- Decisions answers reflect Firmable data as of the `last_refreshed` date, not a live check.
- Email deliverability and do-not-call statuses are Firmable-held observations, not live mailbox, telephone or legal-compliance checks.
- Recipes report data problems they can detect, such as evidence with no source location. They do not correct upstream data.

## `results.json` structure

```json
{
  "recipe": "account-qualification",
  "schema_version": 1,
  "generated_at": "2026-11-24T05:00:00+00:00",
  "mode": "live",
  "input": {"file": "input.csv", "row_count": 3, "config": {}},
  "summary": {},
  "rows": [],
  "coverage": [],
  "errors": [],
  "credits": {},
  "ledger": []
}
```

`rows` has one entry per input row with its `row_id`. Each recipe README describes its row fields.
