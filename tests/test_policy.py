"""One test per rule, plus the one that matters most: surviving a restart."""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import pytest

from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy

KNOWN_ADDRESS = "0xAbC0000000000000000000000000000000000001"
OTHER_ADDRESS = "0xdEf0000000000000000000000000000000000002"


@pytest.fixture()
def memory(tmp_path) -> SpendingMemory:
    return SpendingMemory.local(str(tmp_path / "memory.db"))


@pytest.fixture()
def policy(memory: SpendingMemory) -> SpendingPolicy:
    """A cap high enough to stay out of the way of the other rules.

    Settling also spends, so a tight cap here would make every test a cap test.
    The cap gets its own policy, below.
    """
    return SpendingPolicy(memory, daily_cap_usd=Decimal("500"))


def payment(amount: str = "25", pay_to: str = KNOWN_ADDRESS) -> Payment:
    return Payment(
        merchant="bitrefill-amazon-de",
        pay_to=pay_to,
        amount_usd=Decimal(amount),
        resource="amazon-de-25",
    )


def settle(memory: SpendingMemory, times: int = 1, amount: str = "25") -> None:
    for _ in range(times):
        memory.remember_settlement(payment(amount=amount), tx_id="0xtest")


# --------------------------------------------------------------- rule 1


def test_unknown_merchant_escalates(policy: SpendingPolicy) -> None:
    decision = policy.decide(payment())
    assert decision.action is Action.ESCALATE
    assert decision.rule == "unknown_merchant"
    assert decision.needs_human


def test_known_merchant_pays_without_asking(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    decision = policy.decide(payment())
    assert decision.action is Action.PAY
    assert not decision.needs_human
    assert "3 times" in decision.reason


def test_a_single_past_payment_reads_as_english(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=1)
    assert "once" in policy.decide(payment()).reason


# --------------------------------------------------------------- rule 2


def test_changed_payout_address_blocks(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    decision = policy.decide(payment(pay_to=OTHER_ADDRESS))
    assert decision.action is Action.BLOCK
    assert decision.rule == "payout_address_changed"
    assert decision.evidence["remembered_pay_to"] == KNOWN_ADDRESS.lower()
    assert decision.evidence["requested_pay_to"] == OTHER_ADDRESS.lower()


def test_address_comparison_ignores_case(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory)
    decision = policy.decide(payment(pay_to=KNOWN_ADDRESS.upper()))
    assert decision.action is Action.PAY


# --------------------------------------------------------------- rule 3


def test_previously_rejected_merchant_escalates(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=2)
    memory.remember_rejection(payment(), reason="not this shop again")
    decision = policy.decide(payment())
    assert decision.action is Action.ESCALATE
    assert decision.rule == "previously_rejected"
    assert "not this shop again" in decision.reason


# --------------------------------------------------------------- rule 4


def test_price_spike_escalates(policy: SpendingPolicy, memory: SpendingMemory) -> None:
    settle(memory, times=3, amount="10")
    decision = policy.decide(payment(amount="31"))
    assert decision.action is Action.ESCALATE
    assert decision.rule == "price_spike"


def test_price_at_the_ceiling_still_pays(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """Exactly at the ceiling is inside it, not outside.

    Two settlements, because the ceiling now depends on how much evidence
    there is: at `new` the band is 3x, so 10 USD of history allows 30.
    """
    settle(memory, times=2, amount="10")
    decision = policy.decide(payment(amount="30"))
    assert decision.action is Action.PAY


def test_median_resists_one_outlier(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """A single expensive purchase must not raise the baseline for the next one."""
    settle(memory, times=4, amount="10")
    memory.remember_settlement(payment(amount="200"), tx_id="0xoutlier")
    decision = policy.decide(payment(amount="90"))
    assert decision.action is Action.ESCALATE
    assert decision.rule == "price_spike"


# --------------------------------------------------------------- rule 5


def test_daily_cap_escalates(memory: SpendingMemory) -> None:
    policy = SpendingPolicy(memory, daily_cap_usd=Decimal("50"))
    settle(memory, times=2, amount="20")  # 40 spent
    decision = policy.decide(payment(amount="15"))
    assert decision.action is Action.ESCALATE
    assert decision.rule == "daily_cap"
    assert decision.evidence["remaining_usd"] == "10"


def test_rules_fire_in_severity_order(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """A changed address on an over-cap payment is still an address problem."""
    settle(memory, times=3, amount="20")  # 60 spent, cap 50
    decision = policy.decide(payment(amount="500", pay_to=OTHER_ADDRESS))
    assert decision.action is Action.BLOCK
    assert decision.rule == "payout_address_changed"


# --------------------------------- the gate: memory outlives the process


def test_a_fresh_client_on_the_same_db_still_knows(tmp_path) -> None:
    db = str(tmp_path / "memory.db")
    settle(SpendingMemory.local(db), times=3)

    reopened = SpendingMemory.local(db)
    known = reopened.recall_merchant("bitrefill-amazon-de")
    assert known is not None
    assert known.payment_count == 3
    assert reopened.spent_today() == Decimal("75")


COLD_START = """
import sys
from decimal import Decimal
from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy

memory = SpendingMemory.local(sys.argv[1])
policy = SpendingPolicy(memory, daily_cap_usd=Decimal("500"))
decision = policy.decide(
    Payment("bitrefill-amazon-de", "%s", Decimal("25"))
)
print(decision.action.value)
""" % KNOWN_ADDRESS


def test_a_brand_new_process_pays_without_asking(tmp_path) -> None:
    """The demo, as a test.

    Settle three times, kill everything, start a fresh interpreter, and the new
    process pays without a human. Delete the database between the two halves and
    this test fails — which is the whole claim of the project.
    """
    db = str(tmp_path / "memory.db")
    settle(SpendingMemory.local(db), times=3)

    result = subprocess.run(
        [sys.executable, "-c", COLD_START, db],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "PAY"


def test_the_same_process_without_memory_cannot_decide(tmp_path) -> None:
    """Removing the memory does not degrade the product, it stops it."""
    empty = SpendingMemory.local(str(tmp_path / "empty.db"))
    policy = SpendingPolicy(empty, daily_cap_usd=Decimal("50"))
    assert policy.decide(payment()).needs_human

    with pytest.raises(ValueError):
        SpendingMemory(None)  # type: ignore[arg-type]


# ------------------------------------------- the audit trail joins up


def test_a_decision_points_at_its_journal_entry(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The id the host application carries into its own ledger.

    Without it a memory-approved purchase settles with an empty approval id,
    and the one record that could explain it is unreachable from the other.
    """
    settle(memory, times=2)
    decision = policy.decide(payment())

    assert decision.journal_id
    entry = next(
        e for e in memory.journal(limit=10) if e["id"] == decision.journal_id
    )
    assert decision.reason in entry["acted"][0]
    assert entry["extra"]["rule"] == decision.rule


def test_not_recording_leaves_no_journal_id(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=2)
    assert policy.decide(payment(), record=False).journal_id is None


# ------------------------------------------- the account the CLI reads


def test_the_activated_account_is_used(tmp_path) -> None:
    """Written by the gateway, readable by `sibyl memory recall`.

    One SQLite file holds several tenants. Open it as the wrong one and the
    read is empty rather than an error, so this mismatch is silent — the demo
    would show a merchant the CLI cannot find.
    """
    creds = tmp_path / "credentials.json"
    creds.write_text('{"account_id": "4294ae4b-f8fb", "tier": "free"}')
    db = str(tmp_path / "memory.db")

    as_account = SpendingMemory.local(db, credentials_path=str(creds))
    as_account.remember_settlement(payment())

    same = SpendingMemory.local(db, credentials_path=str(creds))
    assert same.recall_merchant("bitrefill-amazon-de") is not None

    anonymous = SpendingMemory.local(db, credentials_path=str(tmp_path / "none.json"))
    assert anonymous.recall_merchant("bitrefill-amazon-de") is None


def test_tenant_id_is_preferred_over_account_id(tmp_path) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text('{"tenant_id": "tenant-1", "account_id": "account-1"}')
    from spending_memory.store import tenant_from_credentials

    assert tenant_from_credentials(str(creds)) == "tenant-1"


def test_missing_credentials_fall_back_to_anonymous(tmp_path) -> None:
    from spending_memory.store import tenant_from_credentials

    assert tenant_from_credentials(str(tmp_path / "absent.json")) is None


# --------------------------------- the sentences a person actually reads


def test_one_past_payment_reads_as_english_in_the_block(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    """The single most-read line in the product. "the last 1 payments" is a seam."""
    settle(memory, times=1)
    reason = policy.decide(payment(pay_to=OTHER_ADDRESS)).reason
    assert "the last payment went to" in reason
    assert "1 payments" not in reason


def test_several_past_payments_stay_plural_in_the_block(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=3)
    assert "the last 3 payments went to" in policy.decide(payment(pay_to=OTHER_ADDRESS)).reason


def test_the_price_rule_counts_in_english_too(
    policy: SpendingPolicy, memory: SpendingMemory
) -> None:
    settle(memory, times=1, amount="10")
    reason = policy.decide(payment(amount="90")).reason
    assert "After payment I hold" in reason or "After 1 payments" not in reason
