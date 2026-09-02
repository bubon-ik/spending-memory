"""The decision: pay, ask, or refuse.

Every branch below is decided by something read out of Sibyl Memory. Delete the
memory and the function has no inputs — which is the point. The five rules run
in order, and the first one that fires wins, because the ordering encodes how
bad each case is:

    1. never paid this merchant     -> ESCALATE  (we know nothing)
    1b. an alert is open on them    -> BLOCK     (someone already saw this)
    2. payout address changed       -> BLOCK     (we know something is wrong)
    3. owner rejected them before   -> ESCALATE  (we were told no)
    4. price unlike the usual price -> ESCALATE  (we know what it costs, and
                                                  how well we know it)
    5. over the daily cap           -> ESCALATE  (we know what today looks like)
                                    -> PAY

Rules 1, 1b, 2 and 4 read what the whole fleet has learned about the merchant.
Rules 3 and 5 read one owner's own record, because being told no and running
out of allowance belong to a person rather than to a merchant.

Rule 1b sits above the address check on purpose: an alert someone else already
raised is the strongest thing we can know about a merchant. It is also the one
rule that reads a conclusion rather than a fact — another agent, acting for
another owner, was asked this same question and refused.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from datetime import datetime, timezone
from typing import Any

from .store import SpendingMemory
from .types import Action, Decision, MerchantMemory, Payment

PRICE_SPIKE_FACTOR = Decimal("3")
"""How far above the median a price may sit before it is worth asking about.

This is the `new` band, and it stays the default for a policy that pins one
factor explicitly.
"""

PRICE_SPIKE_FACTORS = {
    "new": PRICE_SPIKE_FACTOR,
    "established": Decimal("2"),
    "trusted": Decimal("1.5"),
}
"""The band **tightens** as evidence accumulates, which is backwards from what
most people expect, so: with two payments the median is barely a number and the
slack is there to absorb how little we know. With twenty, the price is
genuinely known — and a spike against a well-measured baseline is more
suspicious than one against a guess, not less.

A merchant is never punished for being new; the agent is only ever cautious in
proportion to how much it actually knows.
"""


def _how_long_ago(timestamp: str | None) -> str:
    """Plain English for a moment in the past, for a sentence a human reads.

    Deliberately coarse. The owner needs to know whether this happened minutes
    or days ago; a millisecond-accurate delta in an approval message is noise.
    """
    if not timestamp:
        return "earlier"
    try:
        raised = datetime.fromisoformat(timestamp)
    except ValueError:
        return "earlier"
    if raised.tzinfo is None:
        raised = raised.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - raised).total_seconds()
    if seconds < 120:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return "an hour ago" if hours == 1 else f"{hours} hours ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


class SpendingPolicy:
    """Decides whether a payment needs its owner.

    The daily cap is a policy input, not a memory one — the owner sets it. What
    memory supplies is how much of it is already gone, and that number has to
    survive a restart or the cap means nothing.
    """

    def __init__(
        self,
        memory: SpendingMemory,
        *,
        daily_cap_usd: Decimal,
        price_spike_factor: Decimal | None = None,
    ) -> None:
        if daily_cap_usd <= 0:
            raise ValueError("daily_cap_usd must be positive")
        self.memory = memory
        self.daily_cap_usd = daily_cap_usd
        self.price_spike_factor = price_spike_factor
        """One band for every merchant, or None to choose it by their status."""

    def _price_spike_factor(self, status: str) -> Decimal:
        if self.price_spike_factor is not None:
            return self.price_spike_factor
        return PRICE_SPIKE_FACTORS.get(status, PRICE_SPIKE_FACTOR)

    def decide(self, payment: Payment, *, record: bool = True) -> Decision:
        known = self.memory.recall_merchant(payment.merchant)
        preference = self.memory.recall_preference(payment.owner, payment.merchant)
        alert = self.memory.open_alert(payment.merchant)
        spent = self.memory.spent_today(payment.owner)
        decision = self._evaluate(payment, known, preference, alert, spent)

        if decision.rule == "payout_address_changed" and known is not None:
            # Raised here rather than inside `_evaluate` so the rules stay a
            # pure reading of memory, and raised whatever `record` says: a
            # journal line is a log, but this is a warning to everyone else.
            self.memory.raise_alert(
                payment.merchant,
                previous_pay_to=known.pay_to,
                requested_pay_to=payment.pay_to_normalised,
                raised_by=payment.owner,
            )

        if record:
            decision = replace(
                decision, journal_id=self.memory.record_decision(payment, decision)
            )
        return decision

    # ------------------------------------------------------------------ rules

    def _evaluate(
        self,
        payment: Payment,
        known: MerchantMemory | None,
        preference: dict[str, Any],
        alert: dict[str, Any] | None,
        spent_today: Decimal,
    ) -> Decision:
        if known is None or known.payment_count == 0:
            return Decision(
                action=Action.ESCALATE,
                rule="unknown_merchant",
                reason=(
                    f"I have never paid {payment.merchant} before. "
                    "Confirm this one and I will handle the next."
                ),
                evidence={"merchant": payment.merchant},
            )

        if alert is not None:
            # Whose refusal it was is not in the sentence and not in the
            # evidence. The owner needs to know the merchant is disputed, not
            # who else is a customer of theirs.
            return Decision(
                action=Action.BLOCK,
                rule="merchant_alert",
                reason=(
                    f"Another agent was asked to pay {payment.merchant} at a "
                    f"different address {_how_long_ago(alert.get('raised_at'))} "
                    "and refused. Until someone resolves that I am not paying "
                    "them either."
                ),
                evidence={
                    "merchant_status": known.status,
                    "alert_raised_at": alert.get("raised_at"),
                    "alert_previous_pay_to": alert.get("previous_pay_to"),
                    "alert_requested_pay_to": alert.get("requested_pay_to"),
                },
            )

        if known.pay_to != payment.pay_to_normalised:
            return Decision(
                action=Action.BLOCK,
                rule="payout_address_changed",
                reason=(
                    f"{payment.merchant} is asking to be paid at "
                    f"{payment.pay_to_normalised}, but the last "
                    f"{known.payment_count} payments went to {known.pay_to}. "
                    "I am not sending this without you."
                ),
                evidence={
                    "merchant_status": known.status,
                    "remembered_pay_to": known.pay_to,
                    "requested_pay_to": payment.pay_to_normalised,
                    "payment_count": known.payment_count,
                },
            )

        if preference["rejected"]:
            return Decision(
                action=Action.ESCALATE,
                rule="previously_rejected",
                reason=(
                    f"You turned down {payment.merchant} before"
                    + (f": {preference['reason']}." if preference["reason"] else ".")
                    + " Asking again rather than assuming that changed."
                ),
                evidence={
                    "merchant_status": known.status,
                    "rejected_reason": preference["reason"],
                },
            )

        typical = known.typical_usd
        factor = self._price_spike_factor(known.status)
        ceiling = typical * factor
        if payment.amount_usd > ceiling:
            return Decision(
                action=Action.ESCALATE,
                rule="price_spike",
                reason=(
                    f"{payment.merchant} normally costs about {typical} USD and "
                    f"this quote is {payment.amount_usd}. After "
                    f"{known.payment_count} payments I hold them to "
                    f"{factor}x the usual, so I stopped."
                ),
                evidence={
                    "merchant_status": known.status,
                    "typical_usd": str(typical),
                    "quoted_usd": str(payment.amount_usd),
                    "ceiling_usd": str(ceiling),
                    "price_spike_factor": str(factor),
                },
            )

        remaining = self.daily_cap_usd - spent_today
        if payment.amount_usd > remaining:
            return Decision(
                action=Action.ESCALATE,
                rule="daily_cap",
                reason=(
                    f"This would take today past the {self.daily_cap_usd} USD "
                    f"limit you set — {spent_today} is already spent, "
                    f"{remaining} left."
                ),
                evidence={
                    "merchant_status": known.status,
                    "spent_today_usd": str(spent_today),
                    "remaining_usd": str(remaining),
                    "daily_cap_usd": str(self.daily_cap_usd),
                },
            )

        times = "once" if known.payment_count == 1 else f"{known.payment_count} times"
        return Decision(
            action=Action.PAY,
            rule="known_good",
            reason=(
                f"Paid {payment.merchant} {times} at this same address, "
                f"usually around {typical} USD. "
                f"{remaining - payment.amount_usd} USD left today."
            ),
            evidence={
                "merchant_status": known.status,
                "payment_count": known.payment_count,
                "pay_to": known.pay_to,
                "typical_usd": str(typical),
                "remaining_after_usd": str(remaining - payment.amount_usd),
            },
        )
