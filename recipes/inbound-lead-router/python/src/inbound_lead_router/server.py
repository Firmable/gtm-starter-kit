"""Single-lead routing server: a local example, not a production service.

    gtm-inbound-lead-router serve --config config.json --max-credits-per-lead 2 --port 8787
    python -m inbound_lead_router.server --config config.json --max-credits-per-lead 2 --port 8787

- Binds 127.0.0.1 only and sends no CORS headers.
- POST /route with a JSON body holding one lead (16 KB at most, else 413). Other paths answer 404 and
  other methods 405.
- Needs FIRMABLE_API_KEY. Every request gets a fresh client and a fresh credit budget of
  --max-credits-per-lead.
- Answers with the same row object the batch run returns. The request ledger, the request headers and
  the key are never sent back.

`handle_request` holds all request handling and opens no socket, so tests call it directly.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from firmable_client.budget import CreditBudget
from firmable_client.client import DEFAULT_TIMEOUT, FirmableClient
from firmable_client.errors import ConfigError
from firmable_client.io import read_config
from firmable_client.recipe import RunContext

from .recipe import RECIPE

MAX_BODY = 16 * 1024
HOST = "127.0.0.1"
ClientFactory = Callable[[], FirmableClient]


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


def lead_from_json(data: Any) -> tuple[dict[str, str] | None, str | None]:
    """Turns a JSON object into an input row the same way the batch input reader does."""
    if not isinstance(data, dict):
        return None, "The body must be a JSON object holding one lead."
    row: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            return None, f"Field {k!r} must be text, a number, true, false or null."
        if v is None:
            text = ""
        elif isinstance(v, bool):
            text = "true" if v else "false"
        elif isinstance(v, float) and v.is_integer():
            text = str(int(v))
        else:
            text = str(v).strip()
        row[str(k).strip()] = text
    if not row.get("work_email") and not row.get("domain"):
        return None, "The lead needs work_email or domain."
    row["row_id"] = row.get("row_id") or "row-1"
    return row, None


def handle_request(method: str, target: str, body: bytes, *, config: dict[str, Any],
                   client_factory: ClientFactory) -> tuple[int, dict[str, Any]]:
    """Routes one lead. Returns (HTTP status, JSON body). Opens no socket."""
    path = urlsplit(target).path
    if path != "/route":
        return _error(404, "not_found", "The only path is POST /route.")
    if method != "POST":
        return _error(405, "method_not_allowed", "Use POST /route.")
    if len(body) > MAX_BODY:
        return _error(413, "body_too_large", f"The body must be {MAX_BODY} bytes or fewer.")
    try:
        data = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        return _error(400, "bad_json", "The body is not valid JSON.")
    row, problem = lead_from_json(data)
    if row is None:
        return _error(400, "bad_lead", problem or "")
    ctx = RunContext(client_factory(), config, [row])
    output = RECIPE.run(ctx)
    return 200, output["rows"][0]


def make_server(config: dict[str, Any], client_factory: ClientFactory, port: int = 8787) -> HTTPServer:
    """An HTTPServer bound to 127.0.0.1. Port 0 picks a free port (see server.server_address)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "inbound-lead-router"
        sys_version = ""

        def _answer(self) -> None:
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header) if length_header else 0
            except ValueError:
                length = -1
            if length < 0:
                status, payload = _error(400, "bad_length", "Content-Length is not a number.")
            elif length > MAX_BODY:
                # The body is not read; the connection closes after the answer.
                status, payload = _error(413, "body_too_large", f"The body must be {MAX_BODY} bytes or fewer.")
                self.close_connection = True
            else:
                body = self.rfile.read(length) if length else b""
                try:
                    status, payload = handle_request(self.command, self.path, body, config=config,
                                                     client_factory=client_factory)
                except Exception:  # noqa: BLE001 - never echo internals to the caller
                    status, payload = _error(500, "internal_error", "The lead could not be routed.")
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if status == 405:
                self.send_header("Allow", "POST")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_POST = do_GET = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _answer

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    return HTTPServer((HOST, port), Handler)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gtm-inbound-lead-router serve",
                                description="Local example server: routes one lead per POST /route.")
    p.add_argument("--config", required=True, help="JSON configuration file")
    p.add_argument("--max-credits-per-lead", type=float, required=True,
                   help="credit limit for each request (each request gets a fresh budget)")
    p.add_argument("--port", type=int, default=8787, help="port on 127.0.0.1 (default: 8787)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait for each API response")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = RECIPE.load_config(read_config(args.config))
        if args.max_credits_per_lead < 0:
            raise ConfigError("--max-credits-per-lead must be zero or more.")
        if args.timeout <= 0:
            raise ConfigError("--timeout must be positive.")
        if not 0 <= args.port <= 65535:
            raise ConfigError("--port must be between 0 and 65535.")
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    api_key = os.environ.get("FIRMABLE_API_KEY", "").strip()
    if not api_key:
        print("error: set FIRMABLE_API_KEY to your developer API key (it starts with fbl_dev_).", file=sys.stderr)
        return 2
    limit = args.max_credits_per_lead
    timeout = args.timeout

    def factory() -> FirmableClient:
        return FirmableClient(api_key, budget=CreditBudget(limit), timeout=timeout)

    server = make_server(config, factory, args.port)
    host, port = server.server_address[:2]
    print(f"Routing leads at http://{host}:{port}/route (local example, not a production service). "
          f"Each lead may use up to {limit} credits. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
