"""The journal is read, not only written.

Rule 6 has no other source of truth. It cannot be answered from the merchant
record, from the preference record, or from today's total — only from the
sequence of decisions already taken. That is what makes the COLD tier
load-bearing rather than an archive nobody opens.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy

MERCHANT = "bitrefill-amazon-de"
OTHER_MERCHANT = "api.example.com"
KNOWN_ADDRESS = "0xAbC0000000000000000000000000000000000001"
ALICE = "telegram:1001"
BOB = "telegram:2002"


@pytest.fixture()
def db(tmp_path) -> str:
    return str(tmp_path / "memory.db")


@pytest.fixture()
def memory(db: str) -> SpendingMemory:
    return SpendingMemory.local(db)


@pytest.fixture()
def policy(memory: SpendingMemory) -> SpendingPolicy:
    return SpendingPolicy(memory, daily_cap_usd=Decimal("500"))


def payment(
    owner: str = ALICE, amount: str = "25", merchant: str = MERCHANT
) -> Payment:
    return Payment(merchant, KNOWN_ADDRESS, Decimal(amount), owner=owner)


def escalate(policy: SpendingPolicy, times: int, **kwargs) -> None:
    """Produce escalations the honest way: ask about a merchant nobody knows."""
    for _ in range(times):
        decision = policy.decide(payment(**kwargs))
        assert decision.action is Action.ESCALATE


# ------------------------------------------------------------- the read


def test_recent_decisions_filters_by_merchant_owner_and_age(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    policy.decide(payment(ALICE))
    policy.decide(payment(BOB))
    policy.decide(payment(ALICE, merchant=OTHER_MERCHANT))

    assert len(memory.recent_decisions(merchant=MERCHANT)) == 2
    assert len(memory.recent_decisions(merchant=MERCHANT, owner=ALICE)) == 1
    assert len(memory.recent_decisions(owner=ALICE)) == 2
    assert memory.recent_decisions(within_seconds=0) == []


def test_the_journal_carries_what_the_query_needs(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """A filter is only as good as what was written for it to match on."""
    decision = policy.decide(payment(ALICE))
    entry = memory.recent_decisions(merchant=MERCHANT)[0]

    assert entry["extra"]["merchant"] == MERCHANT
    assert entry["extra"]["owner"] == ALICE
    assert entry["extra"]["action"] == "ESCALATE"
    assert entry["id"] == decision.journal_id


# ------------------------------------------------------------- the rule


def test_a_merchant_that_keeps_being_escalated_is_stopped(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(policy, times=3, amount="900")  # price spikes, over and over

    stopped = policy.decide(payment())
    assert stopped.action is Action.BLOCK
    assert stopped.rule == "repeated_escalations"
    assert stopped.evidence["escalations"] >= 3
    assert stopped.evidence["escalation_rules"] == ["price_spike"]


def test_two_escalations_are_not_yet_a_pattern(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(policy, times=2, amount="900")

    assert policy.decide(payment()).action is Action.PAY


def test_escalations_outside_the_window_do_not_count(
    memory: SpendingMemory,
) -> None:
    """An hour ago is history. The rule is about what is happening now."""
    wide = SpendingPolicy(memory, daily_cap_usd=Decimal("500"))
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(wide, times=3, amount="900")
    assert wide.decide(payment()).rule == "repeated_escalations"

    narrow = SpendingPolicy(
        memory, daily_cap_usd=Decimal("500"), escalation_window_seconds=0
    )
    assert narrow.decide(payment()).action is Action.PAY


def test_another_merchants_trouble_does_not_stop_this_one(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(policy, times=3, merchant=OTHER_MERCHANT)

    assert policy.decide(payment()).action is Action.PAY


def test_the_rule_counts_across_owners(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """How often a merchant is escalated is a fact about the merchant."""
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(policy, times=1, owner=ALICE, amount="900")
    escalate(policy, times=2, owner=BOB, amount="900")

    assert policy.decide(payment(BOB)).rule == "repeated_escalations"


def test_a_concrete_cause_still_fires_first(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Rule 6 is the weakest signal, so it must not shadow a real one."""
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(policy, times=3, amount="900")

    moved = Payment(
        MERCHANT, "0xdEf0000000000000000000000000000000000002", Decimal("25"), owner=ALICE
    )
    assert policy.decide(moved).rule == "payout_address_changed"


# ------------------------------------------- the journal is the only source


def test_deleting_the_journal_makes_the_rule_unreachable(
    policy: SpendingPolicy, memory: SpendingMemory, db: str
) -> None:
    """The gate, for this rule.

    Every other rule survives losing the journal, because its evidence lives on
    an entity or in state. This one does not: with the entries gone, the same
    merchant at the same address for the same price is paid without a word.
    """
    memory.remember_settlement(payment(), tx_id="0xtest")
    escalate(policy, times=3, amount="900")
    assert policy.decide(payment()).rule == "repeated_escalations"

    with sqlite3.connect(db) as connection:
        # The search index is a plain fts5 table fed by an insert trigger, so
        # the rows have to go from both or SQLite refuses the delete.
        connection.execute("DELETE FROM journal_events_fts")
        connection.execute("DELETE FROM journal_events")

    assert memory.recent_decisions(merchant=MERCHANT) == []
    assert policy.decide(payment()).action is Action.PAY
