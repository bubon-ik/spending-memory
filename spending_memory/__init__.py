"""Spending Memory — an agent decides whether a payment needs its owner.

    from decimal import Decimal
    from spending_memory import Payment, SpendingMemory, SpendingPolicy

    memory = SpendingMemory.local()
    policy = SpendingPolicy(memory, daily_cap_usd=Decimal("50"))

    decision = policy.decide(Payment("bitrefill", "0xabc...", Decimal("25")))
    if decision.needs_human:
        ask_the_owner(decision.reason)
    else:
        settle()
        memory.remember_settlement(payment, tx_id=tx)
"""

from .policy import PRICE_SPIKE_FACTOR, SpendingPolicy
from .store import SpendingMemory
from .types import DEFAULT_OWNER, Action, Decision, MerchantMemory, Payment

__all__ = [
    "Action",
    "DEFAULT_OWNER",
    "Decision",
    "MerchantMemory",
    "PRICE_SPIKE_FACTOR",
    "Payment",
    "SpendingMemory",
    "SpendingPolicy",
]

__version__ = "0.5.0"
