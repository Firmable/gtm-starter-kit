#!/usr/bin/env python3
"""Runs every recipe's documented plan-mode command in Python and TypeScript.

For each recipe it runs, from the recipe folder:
    gtm-<recipe> --input examples/input.<csv|json> --config config.example.json --out <tmp>
    node typescript/dist/src/cli.js --input ... --config config.example.json --out <tmp>
and checks that both exit 0, write plan.json, and agree on the plan.

Plan mode makes no network call and needs no API key. FIRMABLE_API_KEY is removed from the
environment for these runs. Needs the Python packages installed and `npm run build` done first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def norm(x):
    """Compares numbers by value, so 3.0 from Python equals 3 from TypeScript."""
    if isinstance(x, dict):
        return {k: norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [norm(v) for v in x]
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def main(argv: list[str]) -> int:
    recipes = argv or sorted(p.name for p in (ROOT / "recipes").iterdir() if (p / "config.example.json").exists())
    env = {k: v for k, v in os.environ.items() if k != "FIRMABLE_API_KEY"}
    failures = 0
    for r in recipes:
        d = ROOT / "recipes" / r
        inp = next((p for p in (d / "examples" / "input.csv", d / "examples" / "input.json") if p.exists()), None)
        if inp is None:
            print(f"FAIL {r}: no examples/input.csv or input.json")
            failures += 1
            continue
        args = ["--input", str(inp.relative_to(d)), "--config", "config.example.json"]
        with tempfile.TemporaryDirectory() as tmp:
            py_out, ts_out = Path(tmp) / "py", Path(tmp) / "ts"
            exe = shutil.which(f"gtm-{r}", path=str(Path(sys.executable).parent)) or shutil.which(f"gtm-{r}")
            py_cmd = [exe] if exe else [sys.executable, "-m", f"{r.replace('-', '_')}.cli"]
            py = subprocess.run(py_cmd + args + ["--out", str(py_out)], cwd=d, env=env, capture_output=True, text=True)
            ts = subprocess.run(["node", "typescript/dist/src/cli.js", *args, "--out", str(ts_out)], cwd=d, env=env,
                                capture_output=True, text=True)
            problems = []
            for name, proc, out in (("python", py, py_out), ("typescript", ts, ts_out)):
                if proc.returncode != 0:
                    problems.append(f"{name} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}")
                elif not (out / "plan.json").exists():
                    problems.append(f"{name} wrote no plan.json")
            if not problems:
                a = json.loads((py_out / "plan.json").read_text())
                b = json.loads((ts_out / "plan.json").read_text())
                if norm(a) != norm(b):
                    problems.append("plan.json differs between Python and TypeScript")
            if problems:
                failures += 1
                print(f"FAIL {r}")
                for p in problems:
                    print(f"     {p}")
            else:
                print(f"ok   {r}: up to {a['published_estimate_upper_bound']} credits, "
                      f"{a['requests_upper_bound']} request(s)")
    print(f"\nplan mode: {len(recipes) - failures} of {len(recipes)} recipe(s) ran in both languages")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
