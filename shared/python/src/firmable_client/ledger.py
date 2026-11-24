"""Request and response ledger.

Records every call a run makes, including refused, failed and cached calls. It never stores the
Authorization header, and any occurrence of the API key in a body is replaced before storage.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"


def redact(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, REDACTED)
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secret) for k, v in value.items()}
    return value


class Ledger:
    def __init__(self, secret: str | None = None) -> None:
        self.entries: list[dict[str, Any]] = []
        self._secret = secret

    def record(self, **entry: Any) -> dict[str, Any]:
        entry = {"seq": len(self.entries) + 1, **entry}
        entry.pop("headers", None)
        entry = redact(entry, self._secret)
        self.entries.append(entry)
        return entry

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "ledger.json"
        path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path
