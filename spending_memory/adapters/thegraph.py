"""Paying for subgraph queries on The Graph, out of an agent's budget.

The Graph's x402 gateway takes $0.01 in USDC on Base per query and asks for no
API key at all: the payment *is* the authentication. That is a genuinely good
property and a loaded gun. There is no account, so there is no rate limit
attached to a person — the wallet is the account. An agent in a retry loop, or
one that has forgotten it already has the answer, spends real money, and there
is nothing in the protocol to stop it.

What an autonomous payer for queries needs is a limit, a memory of who it has
paid, a record of what it has already bought, and a journal. That is this
package, unchanged, reached through this adapter:

    1. every query goes through `authorise()`, so the daily cap applies
    2. the first payment to The Graph escalates like any unknown merchant, and
       a changed payout address blocks and warns the whole fleet
    3. a query already bought inside the cache window is answered out of the
       journal without paying
    4. one journal line per query, so "what did we spend on data this week" is
       a question with an answer

## The 402 this has to read, and why `x402.py` cannot

Measured against the live gateway rather than assumed (see `docs/checks.md` in
the gateway repo). The Graph answers with an **empty body** and the payment
requirements base64-encoded in a **`payment-required` header**:

    {
      "x402Version": 2,
      "error": "Payment-Signature header is required",
      "resource": {"url": "http://mainnet-thegraph-arbitrum-02-eu-west3…"},
      "accepts": [{
        "scheme": "exact",
        "network": "eip155:8453",
        "amount": "10000",
        "payTo": "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "extra": {"assetTransferMethod": "eip3009", …}
      }]
    }

Three things break the existing adapter. The block is not in the body, so a
client that parses the body finds nothing. The requirements are nested under
`accepts[]` rather than sitting at the top level. And the amount is spelled
`amount` — a third vocabulary alongside the protocol's `maxAmountRequired` and
the normalised `amountAtomic`, which is exactly where `x402.to_payment` raises.

`resource.url` names an internal indexer host, not the gateway URL that was
called. It is **not** the merchant: keying on it would make each region and
each indexer a different seller, and the payout address belongs to The Graph.
The merchant is the host the agent actually requested.

## No network here

This module parses, decides and records. Fetching is the caller's, passed in as
a function. That keeps the package free of an HTTP dependency and keeps the
part worth testing — what gets paid for and what does not — testable without a
socket.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from ..policy import SpendingPolicy
from ..types import DEFAULT_OWNER, Action, Decision, Payment
from .x402 import USDC_DECIMALS, merchant_key

PAYMENT_REQUIRED_HEADER = "payment-required"

CACHE_TTL_ENV = "GRAPH_CACHE_TTL_SECONDS"
DEFAULT_CACHE_TTL_SECONDS = 300
"""Five minutes. Long enough that a retry loop cannot bill twice, short enough
that a subgraph which has since indexed another block is not hidden.

The window is a property of the *data*, not of the budget, which is why it is
short and configurable rather than clever. An agent that wants today's answer
and gets one from four minutes ago has been served correctly; one that gets an
answer from an hour ago has been served stale data to save a cent.
"""

MAX_REMEMBERED_ANSWER_BYTES = 64 * 1024
"""Above this, the journal records that the query was paid for but not what it
returned, and the next identical query pays again.

An unbounded cache inside an append-only journal is a disk-filling bug waiting
for one agent to query something large in a loop. Recording the decision and
dropping the payload is the honest degradation: the ledger stays complete, and
the only thing lost is a saved cent.
"""

QUERY_NOTE_KIND = "graph_query"
"""Marks the journal lines this adapter writes, so they are findable and so it
is clear they are notes rather than decisions."""


def payment_requirements(
    headers: Mapping[str, str], body: Any = None
) -> dict[str, Any]:
    """The `accepts[0]` block out of a 402, wherever this gateway put it.

    Reads the `payment-required` header first, because that is where The Graph
    puts it and where nothing else looks. Falls back to the body so that a
    gateway which follows the more common shape still works through the same
    call — one function that handles both is one fewer decision at the call
    site, and the caller cannot tell the difference anyway.
    """
    block = _decode_header(headers)
    if block is None:
        block = body if isinstance(body, Mapping) else None
    if block is None:
        raise ValueError(
            "no x402 payment requirements found: the `payment-required` header "
            "is absent or unreadable and the body is not a JSON object"
        )

    accepts = block.get("accepts")
    if isinstance(accepts, list) and accepts:
        first = accepts[0]
        if not isinstance(first, Mapping):
            raise ValueError("x402 `accepts[0]` is not an object")
        return dict(first)

    # A top-level block, the shape `x402.py` already knows.
    return dict(block)


def _decode_header(headers: Mapping[str, str]) -> dict[str, Any] | None:
    raw = None
    for name, value in headers.items():
        if str(name).lower() == PAYMENT_REQUIRED_HEADER:
            raw = value
            break
    if not raw:
        return None
    try:
        # Padding is restored rather than required: the value is standard
        # base64 today, and a gateway that trims `=` should not become an
        # unpayable 402.
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def to_payment(
    requirements: Mapping[str, Any],
    resource_url: str,
    *,
    decimals: int = USDC_DECIMALS,
    owner: str = DEFAULT_OWNER,
) -> Payment:
    """One Graph `accepts[]` entry as a `Payment`.

    Reads `amount`, and `maxAmountRequired`/`amountAtomic` as well, so a
    gateway that changes its spelling — or a mixed fleet where some hosts
    normalise the block — does not need a second adapter. Reading three
    spellings is one line here and a debugging afternoon anywhere else.
    """
    pay_to = requirements.get("payTo") or requirements.get("receiver")
    if not pay_to:
        raise ValueError("payment requirements are missing payTo")

    amount = None
    for field in ("amount", "maxAmountRequired", "amountAtomic"):
        if requirements.get(field) is not None:
            amount = requirements[field]
            break
    if amount is None or str(amount) == "":
        raise ValueError(
            "payment requirements are missing amount (or maxAmountRequired, "
            "or amountAtomic)"
        )

    atomic = Decimal(str(amount))
    return Payment(
        # Deliberately not `requirements["resource"]["url"]`: that names the
        # indexer that will serve the query, which changes by region and by
        # deployment. The seller is the gateway the agent called.
        merchant=merchant_key(resource_url),
        pay_to=str(pay_to),
        amount_usd=atomic / (Decimal(10) ** decimals),
        owner=owner,
        resource=resource_url,
    )


def query_fingerprint(
    deployment: str, query: str, variables: Mapping[str, Any] | None = None
) -> str:
    """A stable identity for "this exact question of this exact subgraph".

    Whitespace in the query text is normalised, because a query re-indented by
    a formatter is the same query and should not be bought twice. Variables are
    serialised with sorted keys, because dict ordering is not part of what was
    asked.

    Nothing beyond that is normalised. Field order, aliases and fragment
    structure are left alone: two queries that differ there may well return
    different documents, and a fingerprint that collided would serve the wrong
    answer to save a cent — much worse than paying the cent.
    """
    if not deployment:
        raise ValueError("deployment is required")
    normalised = " ".join(str(query).split())
    payload = json.dumps(
        {
            "deployment": str(deployment),
            "query": normalised,
            "variables": variables or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GraphAnswer:
    """What came of asking for one query.

    `paid` is the field that matters: it is the difference between the two
    outcomes that both look like success, and it is what makes the weekly
    data-spend number real rather than an estimate.
    """

    answer: Any
    paid: bool
    journal_id: str
    fingerprint: str
    decision: Decision | None = None
    """The spending verdict, when one was needed. `None` when the journal
    answered the question and no payment was ever considered."""

    payment: Payment | None = None
    claim_id: str | None = None

    @property
    def needs_human(self) -> bool:
        return self.decision is not None and self.decision.needs_human


class PaidGraphQueries:
    """Buy subgraph queries with a budget, a memory and a receipt.

    Holds a `SpendingPolicy` and adds nothing to it. Every rule that decides
    anything — unknown merchant, moved payout address, price spike, daily cap —
    is the policy's, and applies to a one-cent query exactly as it applies to a
    twenty-five-dollar gift card. That is the point: the limit already running
    in production is what makes paying per query safe to automate.
    """

    def __init__(
        self,
        policy: SpendingPolicy,
        *,
        cache_ttl_seconds: int | None = None,
        owner: str = DEFAULT_OWNER,
    ) -> None:
        self.policy = policy
        self.owner = owner
        if cache_ttl_seconds is None:
            raw = os.getenv(CACHE_TTL_ENV, "").strip()
            cache_ttl_seconds = (
                int(raw) if raw.isdigit() else DEFAULT_CACHE_TTL_SECONDS
            )
        self.cache_ttl_seconds = cache_ttl_seconds

    # ----------------------------------------------------------------- reads

    def remembered_answer(
        self, fingerprint: str, *, owner: str | None = None
    ) -> tuple[Any, str] | None:
        """The answer to this exact query, if it was bought recently enough.

        This is the journal being load-bearing in the most literal way
        available: *reading* it is what prevents the spend. Not a cache beside
        the ledger that the ledger does not know about — the ledger itself.
        """
        owner = owner or self.owner
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.cache_ttl_seconds
        )
        for entry in self.policy.memory.journal(limit=200):
            extra = entry.get("extra") or {}
            if extra.get("kind") != QUERY_NOTE_KIND:
                continue
            if extra.get("query_fingerprint") != fingerprint:
                continue
            if extra.get("owner") != owner:
                continue
            if "answer" not in extra:
                # Paid for, but the payload was too large to keep. The record
                # is complete; the saving is not available.
                continue
            stamped = _parse_timestamp(entry.get("ts"))
            if stamped is None or stamped < cutoff:
                # Older entries are older still — the journal is newest first —
                # so there is nothing left to find for this fingerprint.
                return None
            return extra["answer"], str(entry.get("id"))
        return None

    # ---------------------------------------------------------------- writes

    def note_served_from_memory(
        self,
        *,
        fingerprint: str,
        deployment: str,
        source_journal_id: str,
        owner: str | None = None,
    ) -> str:
        owner = owner or self.owner
        return self.policy.memory.record_note(
            evaluated=[f"{deployment} query {fingerprint[:12]}"],
            acted=["served from the journal, nothing paid"],
            extra={
                "kind": QUERY_NOTE_KIND,
                "owner": owner,
                "deployment": deployment,
                "query_fingerprint": fingerprint,
                "paid": False,
                "amount_usd": "0",
                "served_from": source_journal_id,
            },
        )

    def note_paid(
        self,
        payment: Payment,
        *,
        fingerprint: str,
        deployment: str,
        answer: Any,
        tx_id: str | None = None,
    ) -> str:
        """Record the settlement, then the query line that points at it.

        The settlement is what makes The Graph a known merchant, so the *next*
        query decides without a human. The query line is what makes the spend
        attributable to a question rather than to a counterparty.
        """
        self.policy.memory.remember_settlement(payment, tx_id=tx_id)

        extra: dict[str, Any] = {
            "kind": QUERY_NOTE_KIND,
            "owner": payment.owner,
            "deployment": deployment,
            "query_fingerprint": fingerprint,
            "paid": True,
            "amount_usd": str(payment.amount_usd),
            "merchant": payment.merchant,
            "tx_id": tx_id,
        }
        if _fits_in_the_journal(answer):
            extra["answer"] = answer
        else:
            extra["answer_omitted"] = "larger than MAX_REMEMBERED_ANSWER_BYTES"

        return self.policy.memory.record_note(
            evaluated=[
                f"{deployment} query {fingerprint[:12]} "
                f"{payment.amount_usd} USD -> {payment.pay_to_normalised}"
            ],
            acted=[f"paid {payment.amount_usd} USD" + (f" tx={tx_id}" if tx_id else "")],
            extra=extra,
        )

    # ------------------------------------------------------------ the flow

    def query(
        self,
        *,
        resource_url: str,
        deployment: str,
        query: str,
        variables: Mapping[str, Any] | None = None,
        fetch_402: Callable[[], tuple[Mapping[str, str], Any]],
        pay_and_fetch: Callable[[Payment, Mapping[str, Any]], tuple[Any, str | None]],
        owner: str | None = None,
    ) -> GraphAnswer:
        """Ask for one query, paying only if the journal says we have to.

        `fetch_402` returns `(headers, body)` of the unpaid 402.
        `pay_and_fetch` settles and returns `(answer, tx_id)`. Both are the
        caller's, because this package does not open sockets.

        The order is the whole design: **the journal is read before the network
        is touched.** A cache consulted after paying would save nothing.
        """
        owner = owner or self.owner
        fingerprint = query_fingerprint(deployment, query, variables)

        remembered = self.remembered_answer(fingerprint, owner=owner)
        if remembered is not None:
            answer, source = remembered
            return GraphAnswer(
                answer=answer,
                paid=False,
                fingerprint=fingerprint,
                journal_id=self.note_served_from_memory(
                    fingerprint=fingerprint,
                    deployment=deployment,
                    source_journal_id=source,
                    owner=owner,
                ),
            )

        headers, body = fetch_402()
        requirements = payment_requirements(headers, body)
        payment = to_payment(requirements, resource_url, owner=owner)

        decision, claim_id = self.policy.authorise(
            payment, claim_scope=self._claim_scope(fingerprint)
        )
        if decision.action is not Action.PAY:
            # Escalated or blocked. Nothing is fetched and nothing is paid; the
            # caller shows `decision.reason` to a person. A one-cent query is
            # not a special case worth waving through: the first payment to a
            # new merchant is exactly when the payout address is worth looking
            # at, and this endpoint has no API key to notice a redirect with.
            return GraphAnswer(
                answer=None,
                paid=False,
                fingerprint=fingerprint,
                journal_id=str(decision.journal_id or ""),
                decision=decision,
                payment=payment,
            )

        try:
            answer, tx_id = pay_and_fetch(payment, requirements)
        except Exception:
            # The claim is the thing that stops a second attempt being sent
            # while this one is in flight. A failed attempt has to give it
            # back, or the retry is refused as a duplicate of a payment that
            # never happened.
            if claim_id:
                self.policy.memory.release_claim(claim_id)
            raise

        if claim_id:
            self.policy.memory.settle_claim(claim_id, tx_id=tx_id)

        return GraphAnswer(
            answer=answer,
            paid=True,
            fingerprint=fingerprint,
            journal_id=self.note_paid(
                payment,
                fingerprint=fingerprint,
                deployment=deployment,
                answer=answer,
                tx_id=tx_id,
            ),
            decision=decision,
            payment=payment,
            claim_id=claim_id,
        )

    def _claim_scope(self, fingerprint: str) -> str:
        """What makes two payments for queries different intentions to spend.

        Without this the claim key is owner, merchant, payout address and
        amount — and every subgraph query costs exactly one cent, so every
        distinct question would be the same payment. Because a settled claim is
        permanent, the *second* query an agent ever made would be refused
        forever as a duplicate of the first.

        The scope is the question plus which cache window we are in, which
        makes the claim say exactly what the cache already promises: one
        payment per query per window. Two concurrent attempts at the same
        question are still deduplicated, which the journal cache alone cannot
        do — both would read "not bought yet" before either wrote. And by the
        time the cached answer has expired the window has always turned over,
        because they are the same length, so a legitimate repurchase is never
        blocked by the claim that paid for the last one.
        """
        if self.cache_ttl_seconds <= 0:
            # No caching means no window to share, so every attempt is its own
            # intention. A nonce is the honest expression of that.
            return f"{fingerprint}:{secrets.token_hex(8)}"
        return f"{fingerprint}:{int(time.time() // self.cache_ttl_seconds)}"

    # ------------------------------------------------------------- reporting

    def spent_on_data(
        self, *, within_seconds: int = 7 * 86400, owner: str | None = None
    ) -> dict[str, Any]:
        """What this owner spent on queries, and how much was saved by not paying.

        The question nobody with a bare x402 client can answer about their own
        agent.
        """
        owner = owner or self.owner
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        total = Decimal("0")
        paid = 0
        from_memory = 0

        for entry in self.policy.memory.journal(limit=10_000):
            extra = entry.get("extra") or {}
            if extra.get("kind") != QUERY_NOTE_KIND or extra.get("owner") != owner:
                continue
            stamped = _parse_timestamp(entry.get("ts"))
            if stamped is None or stamped < cutoff:
                break
            if extra.get("paid"):
                paid += 1
                total += Decimal(str(extra.get("amount_usd", "0")))
            else:
                from_memory += 1

        return {
            "owner": owner,
            "queries_paid": paid,
            "queries_from_memory": from_memory,
            "spent_usd": str(total),
        }


def _fits_in_the_journal(answer: Any) -> bool:
    try:
        return (
            len(json.dumps(answer, default=str).encode("utf-8"))
            <= MAX_REMEMBERED_ANSWER_BYTES
        )
    except (TypeError, ValueError):
        return False


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamped = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return stamped
