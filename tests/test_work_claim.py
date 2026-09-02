"""A payment is claimed before it is made, so it cannot be made twice.

The claim lives in memory rather than in the process that took it. That is the
whole difference: a hold a deploy forgets is a hold that lets the same purchase
happen again.
"""

from __future__ import annotations

import subprocess
import sys
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


def payment(owner: str = ALICE, amount: str = "25") -> Payment:
    return Payment(MERCHANT, KNOWN_ADDRESS, Decimal(amount), owner=owner)


def settle(memory: SpendingMemory, owner: str = ALICE, times: int = 1) -> None:
    for _ in range(times):
        memory.remember_settlement(payment(owner), tx_id="0xtest")


# ------------------------------------------------------------- the claim


def test_an_identical_payment_cannot_be_claimed_twice(memory: SpendingMemory) -> None:
    """A retry inside the window is one intention to spend, not two."""
    first = memory.claim_payment(payment())
    assert first is not None
    assert memory.claim_payment(payment()) is None


def test_a_different_payment_is_a_different_claim(memory: SpendingMemory) -> None:
    assert memory.claim_payment(payment()) is not None
    assert memory.claim_payment(payment(amount="26")) is not None
    assert memory.claim_payment(payment(owner=BOB)) is not None


def test_an_abandoned_claim_can_be_retaken_after_its_ttl(memory: SpendingMemory) -> None:
    """The process that took it died. The merchant is not locked out for ever."""
    assert memory.claim_payment(payment(), ttl_seconds=0) is not None
    assert memory.claim_payment(payment()) is not None


def test_a_settled_payment_can_never_be_replayed(memory: SpendingMemory) -> None:
    """Replay protection, and the reason `settled` ignores the TTL.

    A redelivered request an hour later finds an expired claim. If expiry were
    the only test, it would be allowed to pay a second time for the one thing
    that already went through.
    """
    claim_id = memory.claim_payment(payment(), ttl_seconds=0)
    assert claim_id is not None
    memory.settle_claim(claim_id, tx_id="0xdone")

    assert memory.claim_payment(payment()) is None
    assert memory.claim_payment(payment()) is None  # still no, and always no


def test_a_released_claim_can_be_retaken_immediately(memory: SpendingMemory) -> None:
    """The owner said no, or the transfer failed. Trying again is legitimate."""
    claim_id = memory.claim_payment(payment())
    assert claim_id is not None
    memory.release_claim(claim_id)

    assert memory.claim_payment(payment()) is not None


def test_a_superseded_claim_id_cannot_settle_the_claim_that_replaced_it(
    memory: SpendingMemory,
) -> None:
    stale = memory.claim_payment(payment())
    assert stale is not None
    memory.release_claim(stale)
    current = memory.claim_payment(payment())

    memory.settle_claim(stale, tx_id="0xstale")

    assert current is not None
    assert memory.existing_claim(payment())["claim_id"] == current
    assert memory.existing_claim(payment())["status"] == "held"


# ------------------------------------------------------------- authorise


def test_authorise_hands_out_the_claim_with_the_pay(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    decision, claim_id = policy.authorise(payment())
    assert decision.action is Action.PAY
    assert claim_id is not None


def test_a_second_authorise_blocks_instead_of_paying_twice(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The double-tapped button. The first attempt is still in flight."""
    settle(memory, times=3)
    _, first_claim = policy.authorise(payment())

    decision, claim_id = policy.authorise(payment())

    assert decision.action is Action.BLOCK
    assert decision.rule == "already_in_flight"
    assert claim_id is None
    assert first_claim is not None


def test_authorise_never_returns_a_pay_without_a_claim(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    for _ in range(4):
        decision, claim_id = policy.authorise(payment())
        assert (decision.action is Action.PAY) == (claim_id is not None)


def test_a_refused_payment_takes_no_claim(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Deciding is not spending: an escalation must not hold anything."""
    decision, claim_id = policy.authorise(payment())
    assert decision.rule == "unknown_merchant"
    assert claim_id is None
    assert memory.existing_claim(payment()) is None


def test_releasing_lets_the_owner_try_again_after_saying_no(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    _, claim_id = policy.authorise(payment())
    assert claim_id is not None
    memory.release_claim(claim_id)

    decision, second = policy.authorise(payment())
    assert decision.action is Action.PAY
    assert second is not None


def test_the_block_is_journalled_and_the_pay_that_never_happened_is_not(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    policy.authorise(payment())
    blocked, _ = policy.authorise(payment())

    entry = next(e for e in memory.journal(limit=10) if e["id"] == blocked.journal_id)
    assert entry["extra"]["rule"] == "already_in_flight"
    assert entry["extra"]["action"] == "BLOCK"


# ------------------------------------------------- across processes


CLAIM_AGAIN = """
import sys
from decimal import Decimal
from spending_memory import Payment, SpendingMemory

memory = SpendingMemory.local(sys.argv[1])
claim = memory.claim_payment(
    Payment("%s", "%s", Decimal("25"), owner="%s")
)
print("TAKEN" if claim else "REFUSED")
""" % (MERCHANT, KNOWN_ADDRESS, ALICE)


def test_a_claim_survives_the_process_that_took_it(tmp_path) -> None:
    """The defect this fixes, as a test.

    The gateway holds its in-flight spend in its own process. Restart it
    mid-approval and the hold is gone, so the retry measures itself against a
    total that ignores the payment already on its way. Held in memory, the
    second interpreter finds the claim and refuses.
    """
    db = str(tmp_path / "memory.db")
    assert SpendingMemory.local(db).claim_payment(payment()) is not None

    result = subprocess.run(
        [sys.executable, "-c", CLAIM_AGAIN, db],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "REFUSED"
