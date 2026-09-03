"""The x402 mapping, and the one attack it is there to catch."""

from __future__ import annotations

from decimal import Decimal

import pytest

from spending_memory import Action, SpendingMemory, SpendingPolicy
from spending_memory.adapters.x402 import build_policy, merchant_key, to_payment

REAL = "0x8f3a1c4e5b7d9028461fa0c3e5d7b91826af04c1"
ATTACKER = "0x2b9e77d4c1a03f568e2b41d7c90fa3e5182bd0a7"
RESOURCE = "https://api.example.com/search"


def requirements(pay_to: str = REAL, atomic: str = "3000") -> dict:
    return {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "maxAmountRequired": atomic,
        "payTo": pay_to,
        "resource": RESOURCE,
    }


def test_atomic_usdc_becomes_dollars() -> None:
    payment = to_payment(requirements(), RESOURCE)
    assert payment.amount_usd == Decimal("0.003")
    assert payment.pay_to_normalised == REAL.lower()
    assert payment.resource == RESOURCE


def test_merchant_is_the_host_not_the_path() -> None:
    assert merchant_key("https://api.example.com/search") == "api.example.com"
    assert merchant_key("https://API.Example.com/prices") == "api.example.com"


def test_two_endpoints_from_one_seller_are_one_merchant(tmp_path) -> None:
    """Otherwise every new path from a known seller would ask again."""
    memory = SpendingMemory.local(str(tmp_path / "m.db"))
    policy = SpendingPolicy(memory, daily_cap_usd=Decimal("5"))

    search = to_payment(requirements(), "https://api.example.com/search")
    memory.remember_settlement(search)

    prices = to_payment(requirements(), "https://api.example.com/prices")
    assert policy.decide(prices).action is Action.PAY


def test_a_changed_payout_address_is_refused(tmp_path) -> None:
    memory = SpendingMemory.local(str(tmp_path / "m.db"))
    policy = SpendingPolicy(memory, daily_cap_usd=Decimal("5"))
    for _ in range(3):
        memory.remember_settlement(to_payment(requirements(), RESOURCE))

    decision = policy.decide(to_payment(requirements(pay_to=ATTACKER), RESOURCE))
    assert decision.action is Action.BLOCK
    assert decision.evidence["requested_pay_to"] == ATTACKER.lower()


def test_non_usdc_decimals_are_explicit() -> None:
    payment = to_payment(requirements(atomic="10000000000000000000"), RESOURCE, decimals=18)
    assert payment.amount_usd == Decimal("10")


@pytest.mark.parametrize("missing", ["payTo", "maxAmountRequired"])
def test_incomplete_requirements_are_refused_loudly(missing: str) -> None:
    body = requirements()
    del body[missing]
    with pytest.raises(ValueError, match=missing):
        to_payment(body, RESOURCE)


def test_build_policy_reads_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPENDING_MEMORY_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("SPENDING_MEMORY_AUTONOMY_CAP", "1.25")
    policy = build_policy()
    assert policy.daily_cap_usd == Decimal("1.25")


# ------------------------------- the other vocabulary hosts actually use


def test_normalised_requirements_are_accepted_too() -> None:
    """`receiver` / `amountAtomic`, as a gateway hands them on.

    SingIt normalises every 402 block into these names before the decision
    ever sees it. Reading only the protocol spelling raised on every single
    purchase, and the error blamed the merchant for a mismatch that was ours.
    """
    payment = to_payment(
        {
            "network": "base-mainnet",
            "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "amountAtomic": "3000",
            "receiver": REAL,
        },
        RESOURCE,
    )
    assert payment.amount_usd == Decimal("0.003")
    assert payment.pay_to_normalised == REAL.lower()


def test_the_protocol_spelling_still_wins_when_both_are_present() -> None:
    payment = to_payment(
        {**requirements(), "receiver": ATTACKER, "amountAtomic": "999"}, RESOURCE
    )
    assert payment.pay_to_normalised == REAL.lower()
    assert payment.amount_usd == Decimal("0.003")


@pytest.mark.parametrize("missing", ["payTo", "maxAmountRequired"])
def test_the_error_names_both_spellings(missing: str) -> None:
    body = requirements()
    del body[missing]
    with pytest.raises(ValueError, match="or "):
        to_payment(body, RESOURCE)
