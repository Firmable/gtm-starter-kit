# firmable-gtm-client (Python)

The shared Python library behind every gtm-starter-kit recipe. It is not published to PyPI; recipes install it from this folder.

| Module | What it does |
| - | - |
| `client.py` | Calls the Firmable developer REST API at `https://api.firmable.com`. Bearer key, explicit timeouts, no automatic retries, redirects refused, one response reused for identical requests in a run. |
| `budget.py` | Checks every request against the run's credit limit before it is sent. |
| `ledger.py` | Records every request and response without the key. |
| `pricing.py` | Published prices, dated, in one place. |
| `validate.py` | Rejects responses that would silently produce a wrong report. |
| `decisions.py` | Validates configured decision tools, normalises answers, compares answers with the wanted answer. |
| `facts.py` | Picks common company and person fields without changing them. |
| `io.py` | Reads CSV and JSON input, normalises domains, writes CSV with formula protection. |
| `report.py` | Builds standalone HTML reports with escaping and safe links. |
| `recipe.py`, `cli.py` | The recipe structure, run envelope and shared command line. |
| `testing.py` | Offline fixture transport for tests. Not used by the command line. |

```bash
pip install -e "shared/python[test]"
pytest shared/python/tests
```
