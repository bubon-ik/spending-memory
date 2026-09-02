"""The decision: pay, ask, or refuse.

Every branch below is decided by something read out of Sibyl Memory. Delete the
memory and the function has no inputs — which is the point. The five rules run
in order, and the first one that fires wins, because the ordering encodes how
bad each case is:

    1. never paid this merchant     -> ESCALATE  (we know nothing)
    2. payout address changed       -> BLOCK     (we know something is wrong)
    3. owner rejected them before   -> ESCALATE  (we were told no)
    4. price unlike the usual price -> ESCALATE  (we know what it costs)
    5. over the daily cap           -> ESCALATE  (we know what today looks like)
                                    -> PAY
"""

from __future__ import annotations

from decimal import Decimal

from .store import SpendingMemory
from .types import Action, Decision, MerchantMemory, Payment

PRICE_SPIKE_FACTOR = Decimal("3")
"""How far above the remembered median price is still allowed through silently."""


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
        price_spike_factor: Decimal = PRICE_SPIKE_FACTOR,
    ) -> None:
        if daily_cap_usd <= 0:
            raise ValueError("daily_cap_usd must be positive")
        self.memory = memory
        self.daily_cap_usd = daily_cap_usd
        self.price_spike_factor = price_spike_factor

    def decide(self, payment: Payment, *, record: bool = True) -> Decision:
        known = self.memory.recall_merchant(payment.merchant)
        spent = self.memory.spent_today()
        decision = self._evaluate(payment, known, spent)
        if record:
            self.memory.record_decision(payment, decision)
        return decision

    # ------------------------------------------------------------------ rules

    def _evaluate(
        self,
        payment: Payment,
        known: MerchantMemory | None,
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
                    "remembered_pay_to": known.pay_to,
                    "requested_pay_to": payment.pay_to_normalised,
                    "payment_count": known.payment_count,
                },
            )

        if known.rejected:
            return Decision(
                action=Action.ESCALATE,
                rule="previously_rejected",
                reason=(
                    f"You turned down {payment.merchant} before"
                    + (f": {known.rejected_reason}." if known.rejected_reason else ".")
                    + " Asking again rather than assuming that changed."
                ),
                evidence={"rejected_reason": known.rejected_reason},
            )

        typical = known.typical_usd
        ceiling = typical * self.price_spike_factor
        if payment.amount_usd > ceiling:
            return Decision(
                action=Action.ESCALATE,
                rule="price_spike",
                reason=(
                    f"{payment.merchant} normally costs about {typical} USD and "
                    f"this quote is {payment.amount_usd}. That is more than "
                    f"{self.price_spike_factor}x the usual, so I stopped."
                ),
                evidence={
                    "typical_usd": str(typical),
                    "quoted_usd": str(payment.amount_usd),
                    "ceiling_usd": str(ceiling),
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
                "payment_count": known.payment_count,
                "pay_to": known.pay_to,
                "typical_usd": str(typical),
                "remaining_after_usd": str(remaining - payment.amount_usd),
            },
        )
