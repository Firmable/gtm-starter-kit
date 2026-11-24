# Getting started

## Requirements

| Tool | Version |
| - | - |
| Python | 3.11, 3.12 or 3.13 |
| Node.js | 22 or 24 |
| npm | 10 or later (ships with Node.js) |

You need one language, not both.

## Install

Clone the repository and work from its root.

### Python

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # shared client, every recipe, pytest
```

To install one recipe only, install the shared client with it. Recipes depend on `shared/python`, which is not published to PyPI:

```bash
pip install -e shared/python -e recipes/account-qualification/python
```

### TypeScript

```bash
npm ci          # installs TypeScript and links the shared client to every recipe (npm workspaces)
npm run build   # compiles shared/typescript and every recipe to dist/
```

## Run a recipe in plan mode

Plan mode checks your input and configuration and lists the calls a live run could make with their maximum cost. It makes no API call and needs no key.

```bash
cd recipes/account-qualification
gtm-account-qualification --input examples/input.csv --config config.example.json --out out   # Python
node typescript/dist/src/cli.js --input examples/input.csv --config config.example.json --out out   # TypeScript
```

## Run a recipe live

1. Create a developer API key; see [authentication.md](authentication.md).
2. Export it: `export FIRMABLE_API_KEY=fbl_dev_...`
3. Run with `--live` and a credit limit:

```bash
gtm-account-qualification --input examples/input.csv --config config.example.json --out out --live --max-credits 10
```

The run stops before any call that would take it past `--max-credits`. Open `out/report.html` to read the results.

## Use your own data

Replace `examples/input.csv` with your own CSV or JSON file and copy `config.example.json` to adjust the rules. Each recipe README lists its input columns and configuration keys.

## Command-line options

Every recipe accepts the same options in both languages:

| Option | Meaning |
| - | - |
| `--input FILE` | CSV or JSON input file. Required. |
| `--config FILE` | JSON configuration file. Required. |
| `--out DIR` | Output folder. Default `out`. |
| `--live` | Call the Firmable API. Without it, the run is plan mode. |
| `--max-credits N` | The most credits the run may reserve. Required with `--live`. |
| `--timeout S` | Seconds to wait for each response. Default 30. |

Exit codes: `0` finished, `2` bad arguments, input or configuration, `3` the run stopped early (bad key, no credits, credit limit reached or a blocked redirect). Partial results are written in every case.
