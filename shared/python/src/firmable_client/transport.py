"""HTTP transport.

The default transport uses the standard library. It refuses redirects, so the bearer token is never
forwarded to another location, and it applies an explicit timeout to every request.
Tests replace it with `firmable_client.testing.FixtureTransport`; no test calls production.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import RedirectBlockedError, TransportError, TransportTimeoutError

BASE_URL = "https://api.firmable.com"


@dataclass
class HttpRequest:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    def url(self) -> str:
        qs = urllib.parse.urlencode(self.query)
        return f"{BASE_URL}{self.path}" + (f"?{qs}" if qs else "")


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    text: str


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise RedirectBlockedError(
            f"The API answered with a redirect ({code}). Redirects are not followed, so the API key "
            "is not sent to another location.",
            status=code,
        )


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def send(self, request: HttpRequest) -> HttpResponse:
        data = None
        headers = dict(request.headers)
        if request.json_body is not None:
            data = json.dumps(request.json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(request.url(), data=data, headers=headers, method=request.method)
        try:
            with self._opener.open(req, timeout=request.timeout) as resp:
                return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                                    resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return HttpResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
        except RedirectBlockedError:
            raise
        except (socket.timeout, TimeoutError) as e:
            raise TransportTimeoutError(f"No response within {request.timeout} seconds.") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeoutError(f"No response within {request.timeout} seconds.") from e
            raise TransportError(f"Could not reach the API: {e.reason}") from e
