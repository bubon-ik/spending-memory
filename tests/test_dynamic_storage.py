"""The record changes with use.

A merchant is not a fixed row that a rule reads. It is promoted as it earns
evidence, judged more tightly the better it is known, and eventually put away
if it stops being used at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy
from spending_memory.store import merchant_status

MERCHANT = "bitrefill-amazon-de"
KNOWN_ADDRESS = "0xAbC0000000000000000000000000000000000001"


@pytest.fixture()
def memory(tmp_path) -> SpendingMemory:
    return SpendingMemory.local(str(tmp_path / "memory.db"))


@pytest.fixture()
def policy(memory: SpendingMemory) -> SpendingPolicy:
    return SpendingPolicy(memory, daily_cap_usd=Decimal("5000"))


def payment(amount: str = "25", pay_to: str = KNOWN_ADDRESS) -> Payment:
    return Payment(MERCHANT, pay_to, Decimal(amount), resource="amazon-de-25")


def settle(memory: SpendingMemory, times: int = 1, amount: str = "25") -> None:
    for _ in range(times):
        memory.remember_settlement(payment(amount=amount), tx_id="0xtest")


def status_of(memory: SpendingMemory) -> str:
    known = memory.recall_merchant(MERCHANT)
    assert known is not None
    return known.status


# ------------------------------------------------- promotion by evidence


def test_a_merchant_is_promoted_as_it_earns_evidence(memory: SpendingMemory) -> None:
    settle(memory, times=1)
    assert status_of(memory) == "new"

    settle(memory, times=2)  # 3
    assert status_of(memory) == "established"

    settle(memory, times=7)  # 10
    assert status_of(memory) == "trusted"


def test_the_status_boundaries_are_where_they_say_they_are() -> None:
    assert merchant_status(0) == "new"
    assert merchant_status(2) == "new"
    assert merchant_status(3) == "established"
    assert merchant_status(9) == "established"
    assert merchant_status(10) == "trusted"


def test_the_status_is_stored_on_the_sibyl_record(memory: SpendingMemory) -> None:
    """Not derived at read time: the record itself carries the opinion."""
    settle(memory, times=3)
    record = memory._client.get_entity("merchant", MERCHANT)
    assert record["status"] == "established"


# ------------------------------------------------- the band tightens


def test_a_price_allowed_when_new_is_refused_once_trusted(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Same merchant, same address, same price — different amount of evidence.

    At two payments the median is barely a number, and 25 USD against a 10 USD
    baseline is inside the slack. At ten payments the baseline is real, and the
    same quote is a spike worth stopping for.
    """
    settle(memory, times=2, amount="10")
    assert policy.decide(payment(amount="25")).action is Action.PAY

    settle(memory, times=8, amount="10")
    assert status_of(memory) == "trusted"

    refused = policy.decide(payment(amount="25"))
    assert refused.action is Action.ESCALATE
    assert refused.rule == "price_spike"
    assert refused.evidence["ceiling_usd"] == "15.0"


def test_an_explicit_factor_still_pins_one_band(memory: SpendingMemory) -> None:
    """A host that wants one number for every merchant keeps getting it."""
    policy = SpendingPolicy(
        memory, daily_cap_usd=Decimal("5000"), price_spike_factor=Decimal("3")
    )
    settle(memory, times=10, amount="10")
    assert policy.decide(payment(amount="25")).action is Action.PAY


# ------------------------------------------------- evidence carries it


def test_the_applied_band_is_on_the_decision(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The dashboard and the journal show which band was used, and why."""
    settle(memory, times=3, amount="10")

    paid = policy.decide(payment(amount="15"))
    assert paid.action is Action.PAY
    assert paid.evidence["merchant_status"] == "established"

    spiked = policy.decide(payment(amount="90"))
    assert spiked.rule == "price_spike"
    assert spiked.evidence["merchant_status"] == "established"
    assert spiked.evidence["price_spike_factor"] == "2"


def test_the_journal_entry_carries_the_status(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    decision = policy.decide(payment())
    entry = next(e for e in memory.journal(limit=10) if e["id"] == decision.journal_id)
    assert entry["extra"]["merchant_status"] == "established"


# ------------------------------------------------- dormancy


OTHER_MERCHANT = "api.example.com"


def backdate(memory: SpendingMemory, merchant: str, days: int) -> None:
    """Age a merchant's last settlement, the way the calendar would."""
    record = memory._client.get_entity("merchant", merchant)
    body = dict(record["body"])
    body["last_settled_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    memory._client.set_entity("merchant", merchant, body, status=record["status"])


def test_archiving_puts_away_the_dormant_and_leaves_the_active_alone(
    memory: SpendingMemory,
) -> None:
    settle(memory, times=3)
    memory.remember_settlement(
        Payment(OTHER_MERCHANT, KNOWN_ADDRESS, Decimal("1")), tx_id="0xtest"
    )
    backdate(memory, MERCHANT, days=200)

    assert memory.archive_dormant(older_than_days=90) == [MERCHANT]
    assert memory.recall_merchant(OTHER_MERCHANT) is not None


def test_a_merchant_just_inside_the_cutoff_survives(memory: SpendingMemory) -> None:
    settle(memory, times=3)
    backdate(memory, MERCHANT, days=89)
    assert memory.archive_dormant(older_than_days=90) == []


def test_an_archived_merchant_is_unknown_again_and_asks(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The point of archiving rather than deleting, and of asking rather than paying.

    A shop last paid a year ago is not a shop the agent knows. The address on
    file has had a year to go stale, so the next purchase gets a human — and
    the record it used to have is in ARCHIVE, not gone.
    """
    settle(memory, times=10)
    assert policy.decide(payment()).action is Action.PAY

    backdate(memory, MERCHANT, days=400)
    memory.archive_dormant()

    assert memory.recall_merchant(MERCHANT) is None
    assert policy.decide(payment()).rule == "unknown_merchant"


def test_settling_again_brings_the_merchant_back(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=10)
    backdate(memory, MERCHANT, days=400)
    memory.archive_dormant()

    settle(memory, times=1)
    known = memory.recall_merchant(MERCHANT)
    assert known is not None
    assert known.status == "new"  # it starts earning its way back


def test_the_sweep_is_never_run_from_the_decision_path(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """`decide` must not quietly rewrite storage, or no decision is reproducible."""
    settle(memory, times=3)
    backdate(memory, MERCHANT, days=400)

    assert policy.decide(payment()).action is Action.PAY
    assert memory.recall_merchant(MERCHANT) is not None
