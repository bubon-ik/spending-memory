"""x402 payment requirements -> a decision.

An x402 resource answers `402 Payment Required` with a block that says what to
pay, in which asset, on which network, and — the field this package cares about
most — **to whom**:

    {
      "scheme": "exact",
      "network": "eip155:8453",
      "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
      "maxAmountRequired": "3000",
      "payTo": "0x8f3a…",
      "resource": "https://api.example.com/search"
    }

`payTo` comes from the merchant on every single call, which is exactly why it
has to be checked against something. Public x402 directories go stale: a
meaningful share of live services pay to an address the catalogs do not carry.
An agent that trusts the block it was just handed has nothing to notice with.

This module is the whole integration surface. A host application maps its 402
response through `to_payment` and asks the policy; nothing else needs to know
that memory exists.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlparse

from ..policy import SpendingPolicy
from ..store import SpendingMemory
from ..types import DEFAULT_OWNER, Payment

USDC_DECIMALS = 6
"""Base USDC. Pass `decimals=` explicitly for anything else."""


def merchant_key(resource_url: str) -> str:
    """Stable identity for a seller: the host, not the path.

    One merchant usually sells several endpoints, and the payout address belongs
    to the merchant rather than to the endpoint. Keying on the full URL would
    make `/search` and `/prices` two strangers, and the agent would ask about
    every new path from a seller it has paid twenty times.
    """
    if not resource_url:
        raise ValueError("resource_url is required")
    host = urlparse(resource_url).netloc
    return (host or resource_url).strip().lower()


def to_payment(
    payment_requirements: Mapping[str, Any],
    resource_url: str,
    *,
    decimals: int = USDC_DECIMALS,
    owner: str = DEFAULT_OWNER,
) -> Payment:
    """Map one x402 requirements block onto a `Payment`.

    `maxAmountRequired` is atomic, as the protocol specifies. It is the ceiling
    the merchant is allowed to take, so it is the number worth deciding on — a
    scheme that settles for less cannot surprise you, one that asks for more
    cannot get past the check.

    `owner` says whose budget this is charged against. It has a default so a
    host with one user can adopt this without changing any call site, and a
    host with many can pass its user id and get separate budgets, separate
    rejections, and one shared view of the merchant.
    """
    for field in ("payTo", "maxAmountRequired"):
        if not payment_requirements.get(field):
            raise ValueError(f"payment requirements are missing {field}")

    atomic = Decimal(str(payment_requirements["maxAmountRequired"]))
    return Payment(
        merchant=merchant_key(resource_url),
        pay_to=str(payment_requirements["payTo"]),
        amount_usd=atomic / (Decimal(10) ** decimals),
        owner=owner,
        resource=resource_url,
    )


def build_policy(
    *,
    db_path: str | None = None,
    daily_cap_usd: Decimal | None = None,
) -> SpendingPolicy:
    """Build the policy once, at start-up, and hold on to it.

    Constructing it per request opens a SQLite handle per request.

    Environment:
      SPENDING_MEMORY_DB              path to the Sibyl database
      SPENDING_MEMORY_AUTONOMY_CAP    what may be spent per UTC day without asking
    """
    memory = SpendingMemory.local(
        db_path or os.getenv("SPENDING_MEMORY_DB", "~/.sibyl-memory/memory.db")
    )
    cap = daily_cap_usd or Decimal(os.getenv("SPENDING_MEMORY_AUTONOMY_CAP", "5"))
    return SpendingPolicy(memory, daily_cap_usd=cap)
