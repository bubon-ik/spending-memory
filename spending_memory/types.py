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

DEFAULT_OWNER = "default"
"""Owner used when the host application has only one.

A single-owner deployment should not have to invent an identifier, but the
identifier has to exist: two owners sharing one budget is a bug that only shows
up once the second one arrives.
"""


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
    owner: str = DEFAULT_OWNER
    """Whose money this is.

    Required in substance, defaulted in form: budgets, rejections and approvals
    all belong to a person, and one process serves several of them. What the
    fleet learns about a merchant is shared; what one owner decided is not.
    """
    resource: str | None = None

    def __post_init__(self) -> None:
        if not self.merchant:
            raise ValueError("merchant is required")
        if not self.pay_to:
            raise ValueError("pay_to is required")
        if not self.owner:
            raise ValueError("owner is required")
        if self.amount_usd <= 0:
            raise ValueError("amount_usd must be positive")

    @property
    def pay_to_normalised(self) -> str:
        return self.pay_to.strip().lower()


@dataclass(frozen=True)
class MerchantMemory:
    """What the fleet knows about one merchant. Built by `store.py`, never by hand.

    Everything here is shared across owners, because it is a fact about the
    merchant rather than an opinion about them: the address they are actually
    paid at, how often they have been paid, what they charge. One owner's
    refusal lives in the preference record instead, so it cannot silence a
    merchant for everybody.
    """

    merchant: str
    pay_to: str
    payment_count: int
    prices_usd: tuple[Decimal, ...]
    last_settled_at: str | None = None

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
    journal_id: str | None = None
    """Id of the COLD-tier journal entry that recorded this decision.

    Carry it into whatever ledger the host application keeps, and a settled
    payment points at the exact remembered facts that authorised it. A purchase
    approved by memory is then more auditable than one approved by a person
    tapping yes, not less.
    """

    @property
    def needs_human(self) -> bool:
        return self.action is not Action.PAY

    def __str__(self) -> str:
        return f"{self.action.value}: {self.reason}"
