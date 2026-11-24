# API reference

The recipes use seven endpoints of the Firmable developer REST API. This page summarises what the recipes rely on. The [Firmable documentation](https://docs.firmable.com) and its [OpenAPI file](https://docs.firmable.com/api-reference/openapi.json) are the source of truth.

Base URL: `https://api.firmable.com`. Authentication: `Authorization: Bearer <key>`. Rate limit: 15 requests a second per key for each of these endpoints; over the limit the API returns `429` with a `retry-after` header.

| Endpoint | Inputs the recipes use | Notes |
| - | - | - |
| `GET /company` | One of `fqdn`, `id`, `abn`, `website`, `ln_slug`, `ln_url` | `404` when Firmable has no profile. |
| `GET /people` | One of `id`, `work_email`, `personal_email`, `ln_slug`, `ln_url` | Returns emails and phones; costs a credit for a new profile. |
| `POST /people/search` | `companyId` (required), `selectedCountry`, `position`, `seniority`, `department`, `from`, `size` | Free. Seniority and department take numeric IDs. The REST API has no name search and no date filters. |
| `GET /decisions` | None | Free. Lists the decision tools switched on, with each tool's `input_schema`. Tools change without notice. |
| `POST /decisions` | `{tool, question}`; `?withReasoning=true` | `question` holds who the question is about plus the tool's inputs. Up to 100 `records` per request. Each answer is `yes`, `no` or `unknown`. |
| `POST /ai-adoption` | `{company_id}` or `{domain}`, or `{records: [...]}` up to 100; `?withEvidence=true` | A company with no data returns `200` with `company: null` and is free. |
| `GET /tech-intel` | One company identifier; `withEvidence=true`; `cursor` | 100 technologies a page, 10 with evidence. Follow `next_cursor` until it is missing. `403` when Tech Intelligence is not on your plan. |

## Decisions

- A tool's `subject` is `company`, `person` or `company or person`.
- Company questions take `company_id` or `domain`. Person questions take `person_id`, `linkedin_url` or `work_email`.
- `unknown` means Firmable does not hold the fact needed to answer. It is not a no.
- With `withReasoning=true`, each answer carries `reasoning`: `reason`, `source`, `last_refreshed` and, on a no, `would_change_if`.
- Some tools use a decision model inside Firmable's service; the tool list marks them with `uses_decision_model`. The recipes add no model of their own.
- A free-text question at the top level is rejected. Custom questions go through the `evaluate_custom_decision` tool with `question.ask`.

## What the recipes do not use

- MCP tools. Some MCP behaviour, such as searching people by name, has no REST equivalent and is not copied.
- Partner endpoints, which need partner keys.
- `GET /filter-options`. No recipe needs it; the people-search ID lists are in the people-search documentation.
