"""The only module in this package that talks to Sibyl Memory.

Everything the agent knows about past spending is read and written here. If you
are looking for the load-bearing memory calls, they are all in this file:

    read   MemoryClient.get_entity   -> merchant identity, payout address, prices
    read   MemoryClient.get_state    -> today's running total, survives restarts
    read   MemoryClient.read_events  -> the decision journal, for "why did you buy that"
    write  MemoryClient.set_entity   -> a merchant becomes known after settlement
    write  MemoryClient.set_state    -> the daily total after every payment
    write  MemoryClient.write_event  -> one journal line per decision

There is deliberately no in-process fallback. `SpendingMemory` requires a live
`MemoryClient`; construct it without one and it raises. That is the design: an
agent that cannot read its history is not allowed to guess, because guessing
here means spending someone else's money.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sibyl_memory_client import MemoryClient, NotFoundError

from .types import Decision, MerchantMemory, Payment

MERCHANT_CATEGORY = "merchant"
"""Sibyl WARM tier: one record per merchant, source of truth for its payout address."""

SPEND_STATE_PREFIX = "spend"
"""Sibyl HOT tier: `spend:<utc-date>`, rewritten in place all day."""

PRICE_HISTORY_LIMIT = 20
"""How many past prices to keep per merchant. Enough for a stable median."""

CREDENTIALS_PATH = "~/.sibyl-memory/credentials.json"
"""Where `sibyl init` writes the activated account."""


def tenant_from_credentials(path: str = CREDENTIALS_PATH) -> str | None:
    """Read the activated account's tenant, the way the `sibyl` CLI does.

    One SQLite file holds several tenants, so opening it with the wrong one
    reads an empty database rather than failing. Without this the host
    application would write under the anonymous default tenant while
    `sibyl memory recall` looked under the account — same file, nothing found,
    and no error anywhere to explain it.
    """
    try:
        creds = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(creds, dict):
        return None
    tenant = creds.get("tenant_id") or creds.get("account_id")
    return str(tenant) if tenant else None


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _decimals(values: Any) -> tuple[Decimal, ...]:
    if not values:
        return ()
    return tuple(Decimal(str(v)) for v in values)


class SpendingMemory:
    """Sibyl-backed memory of what this agent has already paid for."""

    def __init__(self, client: MemoryClient) -> None:
        if client is None:
            raise ValueError(
                "SpendingMemory requires a live MemoryClient. There is no "
                "memoryless mode: without history there is nothing to decide on."
            )
        self._client = client

    @classmethod
    def local(
        cls,
        path: str = "~/.sibyl-memory/memory.db",
        *,
        tenant_id: str | None = None,
        credentials_path: str = CREDENTIALS_PATH,
    ) -> "SpendingMemory":
        """Open the local database as the activated account.

        Falls back to the client's anonymous default when `sibyl init` has not
        been run, so tests and the demo work without an account.
        """
        tenant = tenant_id or tenant_from_credentials(credentials_path)
        if tenant:
            return cls(MemoryClient.local(path, tenant_id=tenant))
        return cls(MemoryClient.local(path))

    # ---------------------------------------------------------------- reads

    def recall_merchant(self, merchant: str) -> MerchantMemory | None:
        """What we know about this merchant, or None if we have never paid them."""
        try:
            record = self._client.get_entity(MERCHANT_CATEGORY, merchant)
        except NotFoundError:
            return None
        body = record.get("body") or {}
        return MerchantMemory(
            merchant=merchant,
            pay_to=str(body.get("pay_to", "")).strip().lower(),
            payment_count=int(body.get("payment_count", 0)),
            prices_usd=_decimals(body.get("prices_usd")),
            rejected=bool(body.get("rejected", False)),
            rejected_reason=body.get("rejected_reason"),
        )

    def spent_today(self, *, day: str | None = None) -> Decimal:
        """Total settled so far in the current UTC day.

        This is the number a restart must not lose. Hold it in process memory
        instead and the daily cap silently resets every deploy.
        """
        state = self._client.get_state(f"{SPEND_STATE_PREFIX}:{day or utc_today()}")
        if not state:
            return Decimal("0")
        body = state.get("body") or {}
        return Decimal(str(body.get("total_usd", "0")))

    def journal(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Recent decisions, newest first. Answers "why did you buy that"."""
        return self._client.read_events(limit=limit)

    def search(self, query: str) -> list[dict[str, Any]]:
        return list(self._client.search_entities(query, category=MERCHANT_CATEGORY))

    # --------------------------------------------------------------- writes

    def remember_settlement(
        self,
        payment: Payment,
        *,
        tx_id: str | None = None,
        day: str | None = None,
    ) -> MerchantMemory:
        """Record a payment that actually settled.

        This is what turns an unknown merchant into a known one, and what makes
        the *next* purchase from them decidable without a human.
        """
        known = self.recall_merchant(payment.merchant)
        prices = list(known.prices_usd) if known else []
        prices.append(payment.amount_usd)
        prices = prices[-PRICE_HISTORY_LIMIT:]

        body = {
            "pay_to": payment.pay_to_normalised,
            "payment_count": (known.payment_count if known else 0) + 1,
            "prices_usd": [str(p) for p in prices],
            "rejected": False,
            "rejected_reason": None,
            "last_resource": payment.resource,
            "last_settled_at": datetime.now(timezone.utc).isoformat(),
        }
        self._client.set_entity(MERCHANT_CATEGORY, payment.merchant, body)

        today = day or utc_today()
        total = self.spent_today(day=today) + payment.amount_usd
        self._client.set_state(
            f"{SPEND_STATE_PREFIX}:{today}", {"total_usd": str(total)}
        )

        self._client.write_event(
            acted=[
                f"settled {payment.amount_usd} USD to {payment.merchant} "
                f"at {payment.pay_to_normalised}"
                + (f" tx={tx_id}" if tx_id else "")
            ],
            extra={"merchant": payment.merchant, "tx_id": tx_id},
        )
        recalled = self.recall_merchant(payment.merchant)
        assert recalled is not None  # just written
        return recalled

    def remember_rejection(self, payment: Payment, *, reason: str) -> None:
        """Record that the owner said no. A refusal is training, not an incident."""
        known = self.recall_merchant(payment.merchant)
        body = {
            "pay_to": known.pay_to if known else payment.pay_to_normalised,
            "payment_count": known.payment_count if known else 0,
            "prices_usd": [str(p) for p in (known.prices_usd if known else ())],
            "rejected": True,
            "rejected_reason": reason,
            "rejected_at": datetime.now(timezone.utc).isoformat(),
        }
        self._client.set_entity(MERCHANT_CATEGORY, payment.merchant, body)
        self._client.write_event(
            acted=[f"owner rejected {payment.amount_usd} USD to {payment.merchant}: {reason}"],
            extra={"merchant": payment.merchant, "rejected": True},
        )

    def record_decision(self, payment: Payment, decision: Decision) -> str:
        """One journal line per decision, whatever the outcome.

        Returns the journal entry id so the caller can carry it into its own
        ledger and keep the two records joinable.
        """
        return self._client.write_event(
            evaluated=[
                f"{payment.merchant} {payment.amount_usd} USD -> {payment.pay_to_normalised}"
            ],
            acted=[f"{decision.action.value}: {decision.reason}"],
            extra={"rule": decision.rule, **decision.evidence},
        )
