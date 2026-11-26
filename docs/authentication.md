# Authentication

## Get a developer key

1. Sign up at the [Firmable developer portal](https://platform.firmable.com) with a company email.
2. Create an API key. Developer keys start with `fbl_dev_`.
3. Copy it once; the portal does not show it again.

Customer keys from a Firmable subscription start with `fbl_` and also work with these endpoints. They spend subscription credits. Partner keys do not work with these endpoints.

See the [API keys documentation](https://docs.firmable.com/developer/api-keys.md).

## Give the key to a recipe

Recipes read the key from `FIRMABLE_API_KEY` and nowhere else:

```bash
export FIRMABLE_API_KEY=fbl_dev_...
```

Or copy `.env.example` to `.env`, fill it in, and load it with your shell or a tool such as `direnv`. Recipes do not read `.env` files themselves.

## How the key is used

- Sent as `Authorization: Bearer <key>` to `https://api.firmable.com` only.
- Never written to the ledger, reports, results or logs.
- Never forwarded on a redirect; redirects are refused.

## Errors

| Status | Meaning |
| - | - |
| `401` | The key is missing or invalid. The run stops. |
| `403` | The key cannot use this endpoint. Check the key in the developer portal. |
| `402` | Not enough credits. The run stops. |
