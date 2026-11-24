#!/usr/bin/env python3
"""Python and TypeScript output parity check.

For every scenario in tests/fixtures/<recipe>/<name>.scenario.json it runs the Python and the
TypeScript implementation of that recipe against the same input, configuration and responses, then
compares results.json and results.csv.

Files for a scenario <name>:
- <name>.scenario.json  responses (required)
- <name>.input.csv or <name>.input.json  input rows (required)
- <name>.config.json    configuration (optional; falls back to config.json)
- <name>.meta.json      optional {"max_credits": 2.5, "today": "2026-11-24"}

Only deliberately volatile fields are ignored: generated_at and duration_ms.
Needs the Python packages installed in the active environment and `npm run build` done first.

Usage: python tests/parity/run_parity.py [recipe ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
VOLATILE = {"generated_at", "duration_ms"}


def scrub(x):
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if k not in VOLATILE}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def diff(a, b, path="$"):
    if type(a) is not type(b):
        return [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only in {'python' if k in a else 'typescript'}")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def scenario_files(recipe_dir: Path, name: str) -> dict:
    inp = next((p for p in (recipe_dir / f"{name}.input.csv", recipe_dir / f"{name}.input.json") if p.exists()), None)
    if inp is None:
        raise SystemExit(f"{recipe_dir.name}/{name}: no input file")
    cfg = recipe_dir / f"{name}.config.json"
    if not cfg.exists():
        cfg = recipe_dir / "config.json"
    meta_p = recipe_dir / f"{name}.meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    return {"input": inp, "config": cfg, "scenario": recipe_dir / f"{name}.scenario.json", "meta": meta}


def run_one(recipe: str, name: str) -> list[str]:
    f = scenario_files(FIXTURES / recipe, name)
    extra = []
    if "max_credits" in f["meta"]:
        extra += ["--max-credits", str(f["meta"]["max_credits"])]
    if "today" in f["meta"]:
        extra += ["--today", f["meta"]["today"]]
    common = ["--input", str(f["input"]), "--config", str(f["config"]), "--scenario", str(f["scenario"])]
    with tempfile.TemporaryDirectory() as tmp:
        py_out, ts_out = Path(tmp) / "py", Path(tmp) / "ts"
        module = recipe.replace("-", "_") + ".recipe"
        subprocess.run([sys.executable, "-m", "firmable_client.testing", "--recipe-module", module, *common,
                        "--out", str(py_out), *extra], check=True, cwd=ROOT)
        ts_module = ROOT / "recipes" / recipe / "typescript" / "dist" / "src" / "recipe.js"
        subprocess.run(["node", str(ROOT / "shared/typescript/dist/src/testing-cli.js"), "--recipe-module",
                        str(ts_module), *common, "--out", str(ts_out), *extra], check=True, cwd=ROOT)
        a = scrub(json.loads((py_out / "results.json").read_text()))
        b = scrub(json.loads((ts_out / "results.json").read_text()))
        problems = diff(a, b)
        if (py_out / "results.csv").read_text() != (ts_out / "results.csv").read_text():
            problems.append("results.csv differs")
        for p in ("report.html",):
            if not (py_out / p).exists() or not (ts_out / p).exists():
                problems.append(f"{p} missing")
        return problems


def main(argv: list[str]) -> int:
    recipes = argv or sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())
    failures = 0
    total = 0
    for recipe in recipes:
        for sc in sorted((FIXTURES / recipe).glob("*.scenario.json")):
            name = sc.name[: -len(".scenario.json")]
            total += 1
            problems = run_one(recipe, name)
            if problems:
                failures += 1
                print(f"FAIL {recipe}/{name}")
                for p in problems[:20]:
                    print(f"     {p}")
            else:
                print(f"ok   {recipe}/{name}")
    print(f"\nparity: {total - failures} of {total} scenario(s) match")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
