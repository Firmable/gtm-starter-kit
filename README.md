# gtm-starter-kit

Working sales and go-to-market workflows built on the [Firmable developer API](https://docs.firmable.com). Each recipe takes a list you already have, such as a CSV of account domains, calls Firmable's REST endpoints, applies rules you can read and change, and produces a report a salesperson can use.

Every recipe has a Python and a TypeScript implementation that produce the same results.

**This is API code with no LLM integration.** The recipes call Firmable's REST endpoints directly and apply deterministic rules from their configuration. They do not call OpenAI, Anthropic or any other model provider, and they use no agent framework. Some Firmable Decisions tools use a decision model inside Firmable's service; each report names those tools.

## Recipes

| Recipe | What it does | APIs |
| - | - | - |
| [account-qualification](recipes/account-qualification) | Checks companies against your qualification criteria and keeps unknown answers visible | Company, Decisions |
| [tech-sales-opportunities](recipes/tech-sales-opportunities) | Finds technologies relevant to your integrations, competitors or services, with evidence | Tech Intel, Company |
| [ai-account-brief](recipes/ai-account-brief) | Turns AI adoption and technology data into a factual account brief | Company, AI Adoption, Tech Intel |
| [crm-enrichment](recipes/crm-enrichment) | Fills empty fields in a company or contact CSV and reports conflicts | Company, People |
| [account-contact-finder](recipes/account-contact-finder) | Finds relevant people at your accounts | Company, People search, Decisions, People |
| [contact-health-check](recipes/contact-health-check) | Reviews whether contacts still work where you think and whether their details are usable | People, Decisions |
| [new-role-outreach](recipes/new-role-outreach) | Finds contacts whose role changed inside your outreach window | People search, People, Decisions |
| [account-change-monitor](recipes/account-change-monitor) | Compares a new snapshot of your accounts with the previous one | Company, People, Tech Intel, AI Adoption |
| [ai-adoption-segmentation](recipes/ai-adoption-segmentation) | Assigns accounts to AI-related campaign segments with explained rules | AI Adoption, Tech Intel (optional), Decisions (optional) |
| [territory-routing](recipes/territory-routing) | Assigns accounts to teams with ordered rules and a review queue | Company, Decisions (optional) |
| [duplicate-account-review](recipes/duplicate-account-review) | Reviews candidate duplicate pairs and explains the evidence | Company, Decisions |
| [pre-call-account-pack](recipes/pre-call-account-pack) | Assembles company facts, contacts, technologies and checks before a call | Company, People search, People, Tech Intel, Decisions, AI Adoption (optional) |

Each recipe folder has a README with inputs, rules, costs and commands, plus `python/README.md` and `typescript/README.md` for language-specific setup.

## Quick start

Requires Python 3.11 to 3.13 or Node.js 22 or 24.

```bash
git clone <this repository> && cd gtm-starter-kit

# Python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# TypeScript
npm ci && npm run build
```

Plan a run. Plan mode checks the input and configuration and shows the calls and the most credits a live run could use. It makes no API call and needs no key.

```bash
cd recipes/account-qualification
gtm-account-qualification --input examples/input.csv --config config.example.json --out out
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out
```

## Live runs

1. Create a developer key at [platform.firmable.com](https://platform.firmable.com). See [docs/authentication.md](docs/authentication.md).
2. `export FIRMABLE_API_KEY=fbl_dev_...`
3. Add `--live --max-credits N`:

```bash
gtm-account-qualification --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

Open `out/report.html`. `out/results.csv` and `out/results.json` hold the same results for spreadsheets and code.

## Credits and data quality

- Live runs spend developer credits. Search and listing decision tools are free; profiles, Decisions answers, Tech Intel and AI Adoption are charged. See [docs/credits-and-pricing.md](docs/credits-and-pricing.md).
- `--max-credits` is a limit enforced by this code before each request. It is not a cap on your Firmable account.
- A timeout or server error on a paid call may still have been charged. Reports count these separately. Nothing is retried automatically.
- `unknown` means Firmable does not hold the fact. It is never treated as no.
- Not observed in the retrieved data does not mean it does not exist.
- A detected technology, an AI adoption level or a new role does not prove buying intent.
- Recipes flag data problems they can detect. They do not correct upstream data.

## Documentation

- [Getting started](docs/getting-started.md)
- [Authentication](docs/authentication.md)
- [Credits and pricing](docs/credits-and-pricing.md)
- [API reference](docs/api-reference.md)
- [Understanding results](docs/understanding-results.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture](docs/architecture.md)
- [Testing](docs/testing.md)

## Supported versions

| Runtime | Versions tested in CI |
| - | - |
| Python | 3.11, 3.12, 3.13 |
| Node.js | 22, 24 |

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
