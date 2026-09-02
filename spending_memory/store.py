"""The only module in this package that talks to Sibyl Memory.

Everything the agent knows about past spending is read and written here. If you
are looking for the load-bearing memory calls, they are all in this file:

    read   MemoryClient.get_entity   -> merchant identity, payout address, prices
    read   MemoryClient.get_state    -> today's running total, survives restarts
    read   MemoryClient.read_events  -> the decision journal, for "why did you buy that"
    write  MemoryClient.set_entity   -> a merchant becomes known after settlement
    write  MemoryClient.set_state    -> the daily total after every payment
    write  MemoryClient.write_event  -> one journal line per decision

One process serves several owners out of one database, so the records are split
by who they belong to:

    merchant       shared     what the merchant is: payout address, prices, count
    merchant_alert shared     an unresolved warning: this merchant's address moved
    merchant_pref  per owner  what one owner decided about them, and their own count
    spend:<owner>  per owner  what that owner has spent today

The split is the point. A payout address learned while serving one owner
protects all of them — the fleet notices a moved address faster than any public
directory updates. A refusal is the opposite: it is one person's opinion, and
letting it silence a merchant for everybody would be a bug that only shows up
once the second owner arrives.

There is deliberately no in-process fallback. `SpendingMemory` requires a live
`MemoryClient`; construct it without one and it raises. That is the design: an
agent that cannot read its history is not allowed to guess, because guessing
here means spending someone else's money.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sibyl_memory_client import MemoryClient, NotFoundError

from .types import DEFAULT_OWNER, Decision, MerchantMemory, Payment

MERCHANT_CATEGORY = "merchant"
"""Sibyl WARM tier: one record per merchant, shared by every owner.

Source of truth for the payout address, and the reason the fleet learns
together rather than one owner at a time.
"""

PREFERENCE_CATEGORY = "merchant_pref"
"""Sibyl WARM tier: `<owner>:<merchant>`, one owner's standing opinion."""

ALERT_CATEGORY = "merchant_alert"
"""Sibyl WARM tier: one open warning per merchant, shared by every owner.

Raised when someone is asked to pay a merchant at an address that is not the
one on file. It is shared because the danger is: the second owner to be asked
the same question should not have to work it out from scratch.
"""

SPEND_STATE_PREFIX = "spend"
"""Sibyl HOT tier: `spend:<owner>:<utc-date>`, rewritten in place all day."""

PRICE_HISTORY_LIMIT = 20
"""How many past prices to keep per merchant. Enough for a stable median."""

MERCHANT_STATUS_THRESHOLDS = ((10, "trusted"), (3, "established"), (0, "new"))
"""Settled payments needed for each status, strongest first.

Written to Sibyl's own `status` field on the entity, so the record carries the
agent's opinion of the merchant and not just the raw counter. A status is
cheap to read, survives a restart with everything else, and is what the price
band is chosen from.
"""


def merchant_status(payment_count: int) -> str:
    """`new` at 1-2 payments, `established` at 3-9, `trusted` at 10 and up."""
    for threshold, status in MERCHANT_STATUS_THRESHOLDS:
        if payment_count >= threshold:
            return status
    return "new"

DORMANT_AFTER_DAYS = 90
"""How long a merchant may go unpaid before its record is put away."""

LIST_LIMIT = 1000
"""How many merchants one archive sweep looks at."""

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


def preference_key(owner: str, merchant: str) -> str:
    """Where one owner's opinion of one merchant lives."""
    return f"{owner}:{merchant}"


def _decimals(values: Any) -> tuple[Decimal, ...]:
    if not values:
        return ()
    return tuple(Decimal(str(v)) for v in values)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    """Read a timestamp this module wrote, treating anything unreadable as absent."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
        """What the fleet knows about this merchant, or None if nobody has paid them."""
        try:
            record = self._client.get_entity(MERCHANT_CATEGORY, merchant)
        except NotFoundError:
            return None
        body = record.get("body") or {}
        payment_count = int(body.get("payment_count", 0))
        return MerchantMemory(
            merchant=merchant,
            pay_to=str(body.get("pay_to", "")).strip().lower(),
            payment_count=payment_count,
            prices_usd=_decimals(body.get("prices_usd")),
            # Recomputed when the record predates the status field, so an
            # existing database promotes itself on the next read instead of
            # treating twenty payments as a stranger.
            status=str(record.get("status") or merchant_status(payment_count)),
            last_settled_at=body.get("last_settled_at"),
        )

    def recall_preference(self, owner: str, merchant: str) -> dict[str, Any]:
        """What this one owner decided about this merchant.

        Absent is not the same as empty everywhere else in this file, but here
        it is: an owner who has never met a merchant has no opinion of them,
        and the shared record already says what the merchant is.
        """
        try:
            record = self._client.get_entity(
                PREFERENCE_CATEGORY, preference_key(owner, merchant)
            )
        except NotFoundError:
            return {"rejected": False, "reason": None, "own_count": 0}
        body = record.get("body") or {}
        return {
            "rejected": bool(body.get("rejected", False)),
            "reason": body.get("rejected_reason"),
            "own_count": int(body.get("own_count", 0)),
        }

    def open_alert(self, merchant: str) -> dict[str, Any] | None:
        """An unresolved warning about this merchant, or None.

        A cleared alert reads the same as no alert. The record stays for the
        journal rather than being deleted, because "this was raised and then
        resolved by a person" is a different history from "this never happened".
        """
        try:
            record = self._client.get_entity(ALERT_CATEGORY, merchant)
        except NotFoundError:
            return None
        body = record.get("body") or {}
        if body.get("cleared"):
            return None
        return dict(body)

    def raise_alert(
        self,
        merchant: str,
        *,
        previous_pay_to: str,
        requested_pay_to: str,
        raised_by: str,
    ) -> None:
        """Warn every owner that this merchant asked to be paid somewhere new.

        Overwrites whatever was there, including a previously cleared alert: if
        the same bad address comes back after a human resolved it, that is news
        again, not history.
        """
        self._client.set_entity(
            ALERT_CATEGORY,
            merchant,
            {
                "previous_pay_to": previous_pay_to,
                "requested_pay_to": requested_pay_to,
                "raised_by": raised_by,
                "raised_at": _now(),
                "cleared": False,
            },
        )
        self._client.write_event(
            acted=[
                f"raised an alert on {merchant}: asked to be paid at "
                f"{requested_pay_to} instead of {previous_pay_to}"
            ],
            extra={"merchant": merchant, "alert": "payout_address_changed"},
        )

    def clear_alert(self, merchant: str, *, cleared_by: str) -> None:
        """Resolve an alert. Deliberate, manual, and never on a timer.

        An alert that expires by itself protects nobody: the merchant has only
        to wait. Someone has to look at the address and say it is fine.
        """
        try:
            body = dict(self._client.get_entity(ALERT_CATEGORY, merchant).get("body") or {})
        except NotFoundError:
            return
        body.update({"cleared": True, "cleared_by": cleared_by, "cleared_at": _now()})
        self._client.set_entity(ALERT_CATEGORY, merchant, body)
        self._client.write_event(
            acted=[f"cleared the alert on {merchant}"],
            extra={"merchant": merchant, "alert": "cleared", "cleared_by": cleared_by},
        )

    def spent_today(
        self, owner: str = DEFAULT_OWNER, *, day: str | None = None
    ) -> Decimal:
        """Total this owner has settled so far in the current UTC day.

        This is the number a restart must not lose. Hold it in process memory
        instead and the daily cap silently resets every deploy. It is keyed by
        owner because a shared bucket would let one owner spend another's
        allowance without either of them doing anything wrong.
        """
        state = self._client.get_state(self._spend_key(owner, day))
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
        the *next* purchase from them decidable without a human — including the
        next one for a different owner, who has never paid them before.
        """
        known = self.recall_merchant(payment.merchant)
        prices = list(known.prices_usd) if known else []
        prices.append(payment.amount_usd)
        prices = prices[-PRICE_HISTORY_LIMIT:]

        payment_count = (known.payment_count if known else 0) + 1
        body = {
            "pay_to": payment.pay_to_normalised,
            "payment_count": payment_count,
            "prices_usd": [str(p) for p in prices],
            "last_resource": payment.resource,
            "last_settled_at": _now(),
        }
        self._client.set_entity(
            MERCHANT_CATEGORY,
            payment.merchant,
            body,
            status=merchant_status(payment_count),
        )

        # Settling is this owner saying yes, so it also retires whatever they
        # said no to before. Their count is kept apart from the fleet's: it is
        # what "you have paid them twice" means when the sentence is shown to
        # one person.
        preference = self.recall_preference(payment.owner, payment.merchant)
        self._write_preference(
            payment.owner,
            payment.merchant,
            rejected=False,
            rejected_reason=None,
            own_count=preference["own_count"] + 1,
            last_settled_at=_now(),
        )

        today = day or utc_today()
        total = self.spent_today(payment.owner, day=today) + payment.amount_usd
        self._client.set_state(
            self._spend_key(payment.owner, today), {"total_usd": str(total)}
        )

        self._client.write_event(
            acted=[
                f"settled {payment.amount_usd} USD to {payment.merchant} "
                f"at {payment.pay_to_normalised}"
                + (f" tx={tx_id}" if tx_id else "")
            ],
            extra={
                "merchant": payment.merchant,
                "owner": payment.owner,
                "tx_id": tx_id,
            },
        )
        recalled = self.recall_merchant(payment.merchant)
        assert recalled is not None  # just written
        return recalled

    def remember_rejection(self, payment: Payment, *, reason: str) -> None:
        """Record that this owner said no. A refusal is training, not an incident.

        Written to the owner's preference record and nowhere else. A rejection
        is an opinion, and the shared merchant record holds facts — one user
        turning down a shop must not stop everybody else from buying there.
        """
        self._write_preference(
            payment.owner,
            payment.merchant,
            rejected=True,
            rejected_reason=reason,
            rejected_at=_now(),
        )
        self._client.write_event(
            acted=[
                f"owner rejected {payment.amount_usd} USD to {payment.merchant}: {reason}"
            ],
            extra={
                "merchant": payment.merchant,
                "owner": payment.owner,
                "rejected": True,
            },
        )

    def archive_dormant(self, *, older_than_days: int = DORMANT_AFTER_DAYS) -> list[str]:
        """Put away merchants nobody has paid in a long time. Returns their names.

        `recall_merchant` then returns None for them, so the next payment is
        treated as a first payment and asks. That is intended: a shop you last
        used a year ago deserves a fresh look, and the address on file has had a
        year to go stale. Archiving is not deleting — the record moves to
        ARCHIVE and everything it knew is recoverable.

        Never called from the decision path, and deliberately not on a timer
        inside it. A `decide()` that quietly rewrote storage would be a decision
        nobody could reproduce afterwards; the host calls this when it wants a
        sweep, and the journal says when it happened.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        archived: list[str] = []

        for record in self._client.list_entities(MERCHANT_CATEGORY, limit=LIST_LIMIT):
            name = str(record.get("name") or "")
            body = record.get("body") or {}
            last_settled = _parse_timestamp(body.get("last_settled_at"))
            # No timestamp means nothing has ever settled against this record,
            # so there is no dormancy to measure and nothing to put away.
            if name and last_settled is not None and last_settled < cutoff:
                self._client.archive_entity(MERCHANT_CATEGORY, name, reason="dormant")
                archived.append(name)

        if archived:
            self._client.write_event(
                acted=[
                    f"archived {len(archived)} merchants unpaid for "
                    f"{older_than_days} days: {', '.join(sorted(archived))}"
                ],
                extra={"archived": sorted(archived), "older_than_days": older_than_days},
            )
        return archived

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
            extra={
                "rule": decision.rule,
                "owner": payment.owner,
                **decision.evidence,
            },
        )

    # -------------------------------------------------------------- internals

    def _spend_key(self, owner: str, day: str | None = None) -> str:
        return f"{SPEND_STATE_PREFIX}:{owner}:{day or utc_today()}"

    def _write_preference(self, owner: str, merchant: str, **updates: Any) -> None:
        """Merge into one owner's record, leaving the fields it does not mention.

        A settlement should not erase why they rejected the merchant last month,
        and a rejection should not reset their count.
        """
        key = preference_key(owner, merchant)
        try:
            existing = self._client.get_entity(PREFERENCE_CATEGORY, key).get("body") or {}
        except NotFoundError:
            existing = {}
        body = {**existing, **updates, "owner": owner, "merchant": merchant}
        self._client.set_entity(PREFERENCE_CATEGORY, key, body)
