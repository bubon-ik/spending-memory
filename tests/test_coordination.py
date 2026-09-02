"""Two owners, one memory.

One gateway process serves everybody out of one Sibyl database. These tests pin
down what that is allowed to mean: the fleet learns about a merchant together,
and nobody spends or refuses on anyone else's behalf.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy

MERCHANT = "bitrefill-amazon-de"
KNOWN_ADDRESS = "0xAbC0000000000000000000000000000000000001"

ALICE = "telegram:1001"
BOB = "telegram:2002"


@pytest.fixture()
def memory(tmp_path) -> SpendingMemory:
    return SpendingMemory.local(str(tmp_path / "memory.db"))


@pytest.fixture()
def policy(memory: SpendingMemory) -> SpendingPolicy:
    return SpendingPolicy(memory, daily_cap_usd=Decimal("500"))


def payment(owner: str, amount: str = "25", pay_to: str = KNOWN_ADDRESS) -> Payment:
    return Payment(
        merchant=MERCHANT,
        pay_to=pay_to,
        amount_usd=Decimal(amount),
        owner=owner,
        resource="amazon-de-25",
    )


def settle(memory: SpendingMemory, owner: str, times: int = 1, amount: str = "25") -> None:
    for _ in range(times):
        memory.remember_settlement(payment(owner, amount=amount), tx_id="0xtest")


# ------------------------------------------------- opinions stay personal


def test_one_owners_rejection_does_not_silence_the_merchant_for_another(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The defect this split exists to fix.

    With one shared record, Alice saying no would have made Bob's next purchase
    from the same shop ask him about a refusal he never made.
    """
    settle(memory, ALICE, times=2)
    settle(memory, BOB, times=2)

    memory.remember_rejection(payment(ALICE), reason="not this shop again")

    assert policy.decide(payment(ALICE)).rule == "previously_rejected"
    assert policy.decide(payment(BOB)).action is Action.PAY


def test_a_rejection_is_not_written_to_the_shared_merchant_record(
    memory: SpendingMemory,
) -> None:
    settle(memory, ALICE, times=2)
    memory.remember_rejection(payment(ALICE), reason="too expensive")

    known = memory.recall_merchant(MERCHANT)
    assert known is not None
    assert known.payment_count == 2  # untouched by an opinion
    assert memory.recall_preference(ALICE, MERCHANT)["rejected"] is True
    assert memory.recall_preference(BOB, MERCHANT)["rejected"] is False


# ------------------------------------------------- facts are shared


def test_the_fleet_learns_a_payout_address_once_for_everybody(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The coordination test.

    Bob has never paid this merchant. Alice has, three times, and what she
    learned is the address they are actually paid at. That is a fact about the
    merchant rather than an opinion about them, so Bob's first purchase decides
    itself — and, more to the point, a *changed* address would be caught on
    Bob's first purchase too, before he has any history of his own.
    """
    settle(memory, ALICE, times=3)

    first_time_for_bob = policy.decide(payment(BOB))
    assert first_time_for_bob.action is Action.PAY
    assert first_time_for_bob.evidence["pay_to"] == KNOWN_ADDRESS.lower()


def test_a_moved_address_is_caught_for_an_owner_who_never_paid_before(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, ALICE, times=3)
    decision = policy.decide(payment(BOB, pay_to="0xdEf0000000000000000000000000000000000002"))
    assert decision.action is Action.BLOCK
    assert decision.rule == "payout_address_changed"


# ------------------------------------------------- budgets stay personal


def test_two_owners_have_separate_daily_budgets(memory: SpendingMemory) -> None:
    """Without the owner in the state key, Alice would spend Bob's allowance."""
    policy = SpendingPolicy(memory, daily_cap_usd=Decimal("50"))
    settle(memory, ALICE, times=2, amount="20")  # Alice: 40 of 50 gone
    settle(memory, BOB, times=2, amount="20")  # Bob: 40 of 50 gone, his own

    assert memory.spent_today(ALICE) == Decimal("40")
    assert memory.spent_today(BOB) == Decimal("40")

    over_for_alice = policy.decide(payment(ALICE, amount="15"))
    assert over_for_alice.rule == "daily_cap"
    assert over_for_alice.evidence["spent_today_usd"] == "40"

    assert policy.decide(payment(BOB, amount="10")).action is Action.PAY


def test_a_third_owner_starts_the_day_at_zero(memory: SpendingMemory) -> None:
    settle(memory, ALICE, times=3, amount="20")
    assert memory.spent_today("telegram:3003") == Decimal("0")


# ------------------------------------------------- a warning travels


ATTACKER_ADDRESS = "0x2b9e77d4c1a03f568e2b41d7c90fa3e5182bd0a7"


def test_one_agents_refusal_warns_every_other_agent(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Alice's agent refuses a moved address. Bob's agent inherits the doubt.

    Bob is asked to pay the address that *is* on file, so nothing about his own
    payment looks wrong. The only reason to stop is that somebody else already
    saw this merchant ask for money somewhere else.
    """
    settle(memory, ALICE, times=3)

    blocked = policy.decide(payment(ALICE, pay_to=ATTACKER_ADDRESS))
    assert blocked.rule == "payout_address_changed"

    for_bob = policy.decide(payment(BOB))
    assert for_bob.action is Action.BLOCK
    assert for_bob.rule == "merchant_alert"
    assert for_bob.evidence["alert_requested_pay_to"] == ATTACKER_ADDRESS


def test_the_warning_does_not_say_who_raised_it(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Bob learns that a merchant is disputed, not who else shops there."""
    settle(memory, ALICE, times=3)
    policy.decide(payment(ALICE, pay_to=ATTACKER_ADDRESS))

    for_bob = policy.decide(payment(BOB))
    assert ALICE not in for_bob.reason
    assert ALICE not in str(for_bob.evidence)
    assert "Another agent" in for_bob.reason


def test_the_alert_outranks_everything_below_it(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """An open alert is the strongest thing we can know about a merchant."""
    settle(memory, ALICE, times=3)
    policy.decide(payment(ALICE, pay_to=ATTACKER_ADDRESS))
    memory.remember_rejection(payment(BOB), reason="unrelated opinion")

    assert policy.decide(payment(BOB)).rule == "merchant_alert"


def test_clearing_the_alert_lets_the_fleet_pay_again(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Deliberate and manual: a person looked at the address and said it is fine."""
    settle(memory, ALICE, times=3)
    policy.decide(payment(ALICE, pay_to=ATTACKER_ADDRESS))
    assert policy.decide(payment(BOB)).action is Action.BLOCK

    memory.clear_alert(MERCHANT, cleared_by="operator")

    assert memory.open_alert(MERCHANT) is None
    assert policy.decide(payment(BOB)).action is Action.PAY


def test_an_unknown_merchant_is_still_asked_about_first(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """No alert can exist before somebody has an address to compare against."""
    assert policy.decide(payment(ALICE)).rule == "unknown_merchant"
    assert memory.open_alert(MERCHANT) is None
