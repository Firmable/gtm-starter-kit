"""Structured errors raised by the Firmable client.

Every error carries the endpoint and, where one exists, the HTTP status. `charge_uncertain` is True
when the server may have charged for the request even though no usable response came back, for
example after a timeout or a 5xx on a billable endpoint.
"""
from __future__ import annotations

from typing import Any


class FirmableError(Exception):
    kind = "error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        endpoint: str | None = None,
        charge_uncertain: bool = False,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.endpoint = endpoint
        self.charge_uncertain = charge_uncertain
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "code": self.code,
            "endpoint": self.endpoint,
            "message": self.message,
            "charge_uncertain": self.charge_uncertain,
            "retry_after": self.retry_after,
        }


class BadRequestError(FirmableError):
    kind = "bad_request"


class AuthenticationError(FirmableError):
    kind = "unauthorized"


class InsufficientCreditsError(FirmableError):
    kind = "insufficient_credits"


class ForbiddenError(FirmableError):
    kind = "forbidden"


class NotFoundError(FirmableError):
    kind = "not_found"


class RateLimitedError(FirmableError):
    kind = "rate_limited"


class ServerError(FirmableError):
    kind = "server_error"


class UnexpectedStatusError(FirmableError):
    kind = "unexpected_status"


class TransportTimeoutError(FirmableError):
    kind = "timeout"


class TransportError(FirmableError):
    kind = "transport_error"


class RedirectBlockedError(FirmableError):
    kind = "redirect_blocked"


class InvalidResponseError(FirmableError):
    kind = "invalid_response"


class BudgetExceededError(FirmableError):
    """Raised before a request is sent when its reservation would exceed the credit limit."""

    kind = "budget_exceeded"


class ConfigError(Exception):
    """Invalid recipe configuration or command-line arguments. Raised before any request."""


class InputError(Exception):
    """Invalid input file. Raised before any request."""


def error_for_status(status: int, message: str, *, code: str | None, endpoint: str,
                     billable: bool, retry_after: str | None) -> FirmableError:
    """Maps an HTTP status to the matching error class.

    4xx responses mean the request was refused, so nothing was charged. A 5xx on a billable
    endpoint is marked charge-uncertain because the docs do not say whether work was done.
    """
    if status == 400:
        return BadRequestError(message, status=status, code=code, endpoint=endpoint)
    if status == 401:
        return AuthenticationError(message, status=status, code=code, endpoint=endpoint)
    if status == 402:
        return InsufficientCreditsError(message, status=status, code=code, endpoint=endpoint)
    if status == 403:
        return ForbiddenError(message, status=status, code=code, endpoint=endpoint)
    if status == 404:
        return NotFoundError(message, status=status, code=code, endpoint=endpoint)
    if status == 429:
        return RateLimitedError(message, status=status, code=code, endpoint=endpoint,
                                retry_after=retry_after)
    if 500 <= status <= 599:
        return ServerError(message, status=status, code=code, endpoint=endpoint,
                           charge_uncertain=billable)
    return UnexpectedStatusError(message, status=status, code=code, endpoint=endpoint,
                                 charge_uncertain=billable)
