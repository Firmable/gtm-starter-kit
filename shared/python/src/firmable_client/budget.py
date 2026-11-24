"""Client-side credit budget.

The budget is checked before each request. A request whose reservation would take the run over the
limit is not sent. This is a safeguard in this code only: the Firmable API does not enforce it.
"""
from __future__ import annotations

from typing import Any

from .errors import BudgetExceededError
from .pricing import FRACTIONAL_CATEGORIES, ROUNDING_ALLOWANCE, round4


class CreditBudget:
    def __init__(self, limit: float | None) -> None:
        if limit is not None and limit < 0:
            raise ValueError("credit limit must be zero or more")
        self.limit = limit
        self.reserved = 0.0           # reservations currently held (sent or uncertain requests)
        self.published = 0.0          # published estimate of everything sent
        self.reported = 0.0           # credits reported in responses
        self.uncertain_requests = 0   # requests that may have been charged without a usable response
        self._allowances: set[str] = set()

    def reserve(self, parts: list[tuple[str, float]], endpoint: str) -> float:
        """Reserves credits for one request and returns the amount held.

        Raises BudgetExceededError, without changing state, when the limit would be passed.
        """
        amount = sum(v for _, v in parts)
        new_allowances = {c for c, v in parts
                          if v > 0 and c in FRACTIONAL_CATEGORIES and c not in self._allowances}
        hold = round4(amount + ROUNDING_ALLOWANCE * len(new_allowances))
        if self.limit is not None and self.reserved + hold > self.limit + 1e-9:
            remaining = round4(max(self.limit - self.reserved, 0.0))
            raise BudgetExceededError(
                f"Stopped before calling {endpoint}: it needs a reservation of {hold} credits "
                f"and {remaining} of the {self.limit} credit limit remain.",
                endpoint=endpoint,
            )
        self._allowances |= new_allowances
        self.reserved = round4(self.reserved + hold)
        self.published = round4(self.published + amount)
        return hold

    def release(self, hold: float, published: float) -> None:
        """Returns a reservation for a request the server refused, which was not charged."""
        self.reserved = round4(max(self.reserved - hold, 0.0))
        self.published = round4(max(self.published - published, 0.0))

    def add_reported(self, credits: float | None) -> None:
        if credits is not None:
            self.reported = round4(self.reported + credits)

    def mark_uncertain(self) -> None:
        self.uncertain_requests += 1

    def summary(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "published_estimate_upper_bound": self.published,
            "reserved_by_this_code": self.reserved,
            "response_reported_credits": self.reported,
            "requests_with_uncertain_charge": self.uncertain_requests,
            "wallet_deduction": "Not exposed by the API. Check Usage in the developer portal.",
        }
