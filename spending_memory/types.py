"""Value types for the spending decision.

Kept free of any Sibyl import on purpose: these are the shapes that cross the
boundary between `store.py` (which is the only module that talks to memory) and
`policy.py` (which only reasons).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class Action(str, Enum):
    """What the gateway should do with a proposed payment."""

    PAY = "PAY"
    """Inside everything the memory knows. Settle without asking a human."""

    ESCALATE = "ESCALATE"
    """Reality does not match memory in a recoverable way. Ask the owner."""

    BLOCK = "BLOCK"
    """The payment looks like it goes somewhere it should not. Refuse, then ask."""


@dataclass(frozen=True)
class Payment:
    """A payment the agent is about to make, before any decision is taken."""

    merchant: str
    pay_to: str
    amount_usd: Decimal
    resource: str | None = None

    def __post_init__(self) -> None:
        if not self.merchant:
            raise ValueError("merchant is required")
        if not self.pay_to:
            raise ValueError("pay_to is required")
        if self.amount_usd <= 0:
            raise ValueError("amount_usd must be positive")

    @property
    def pay_to_normalised(self) -> str:
        return self.pay_to.strip().lower()


@dataclass(frozen=True)
class MerchantMemory:
    """What memory holds about one merchant. Built by `store.py`, never by hand."""

    merchant: str
    pay_to: str
    payment_count: int
    prices_usd: tuple[Decimal, ...]
    rejected: bool = False
    rejected_reason: str | None = None

    @property
    def typical_usd(self) -> Decimal:
        """Median of remembered prices.

        Median rather than mean: one mispriced purchase should not move the
        baseline enough to wave the next one through.
        """
        if not self.prices_usd:
            raise ValueError("a remembered merchant always has at least one price")
        ordered = sorted(self.prices_usd)
        middle = len(ordered) // 2
        if len(ordered) % 2 == 1:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2


@dataclass(frozen=True)
class Decision:
    """The verdict, plus the remembered facts that produced it.

    `reason` is written to be shown to a human verbatim — in Telegram, on the
    dashboard, and in the demo video. A decision the owner cannot read is a
    decision they cannot trust.
    """

    action: Action
    reason: str
    rule: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_human(self) -> bool:
        return self.action is not Action.PAY

    def __str__(self) -> str:
        return f"{self.action.value}: {self.reason}"
