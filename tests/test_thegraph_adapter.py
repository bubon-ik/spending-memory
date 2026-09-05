"""Paying for subgraph queries: the 402 The Graph actually sends, and the four
behaviours that make an autonomous payer for queries safe.

The 402 block used throughout is the one captured from the live gateway during
Phase 0, byte for byte, including the base64 header it arrives in. It is a
fixture only in the sense that a test cannot make a mainnet payment; the shape
was measured, not invented, which is the difference between this and a mock
that agrees with the code.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest

from spending_memory import Action, SpendingMemory, SpendingPolicy
from spending_memory.adapters.thegraph import (
    DEFAULT_CACHE_TTL_SECONDS,
    MAX_REMEMBERED_ANSWER_BYTES,
    PaidGraphQueries,
    payment_requirements,
    query_fingerprint,
    to_payment,
)

DEPLOYMENT = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
RESOURCE = f"https://gateway.thegraph.com/api/x402/subgraphs/id/{DEPLOYMENT}"
GRAPH_PAY_TO = "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB"
ATTACKER = "0x2b9e77d4c1a03f568e2b41d7c90fa3e5182bd0a7"
QUERY = "{ pairs(first: 5) { id } }"


def live_402(pay_to: str = GRAPH_PAY_TO, amount: str = "10000") -> dict:
    """Exactly what gateway.thegraph.com returned on 2026-09-05."""
    return {
        "x402Version": 2,
        "error": "Payment-Signature header is required",
        "resource": {
            "url": (
                "http://mainnet-thegraph-arbitrum-02-eu-west3.thegraph.com"
                f"/subgraphs/id/{DEPLOYMENT}"
            )
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "extra": {
                    "assetTransferMethod": "eip3009",
                    "name": "USD Coin",
                    "version": "2",
                },
            }
        ],
    }


def live_headers(**kwargs) -> dict:
    encoded = base64.b64encode(json.dumps(live_402(**kwargs)).encode()).decode()
    return {
        "content-length": "0",
        "payment-required": encoded,
        "allow": "POST",
    }


def build(tmp_path, *, cap: str = "5", ttl: int | None = None) -> PaidGraphQueries:
    policy = SpendingPolicy(
        SpendingMemory.local(str(tmp_path / "m.db")), daily_cap_usd=Decimal(cap)
    )
    return PaidGraphQueries(policy, cache_ttl_seconds=ttl, owner="agent-7")


def make_known(graph: PaidGraphQueries, pay_to: str = GRAPH_PAY_TO) -> None:
    """Settle one payment, so The Graph stops being a stranger."""
    graph.policy.memory.remember_settlement(
        to_payment(live_402(pay_to)["accepts"][0], RESOURCE, owner="agent-7"),
        tx_id="0xseed",
    )


# --------------------------------------------------------------- the 402 shape


def test_the_requirements_come_out_of_the_header_not_the_body() -> None:
    """The body is empty. A client that parses the body finds nothing at all."""
    block = payment_requirements(live_headers(), body=None)

    assert block["payTo"] == GRAPH_PAY_TO
    assert block["amount"] == "10000"


def test_the_header_is_found_whatever_its_case() -> None:
    encoded = base64.b64encode(json.dumps(live_402()).encode()).decode()

    assert payment_requirements({"Payment-Required": encoded})["amount"] == "10000"


def test_base64_padding_is_restored_rather_than_required() -> None:
    encoded = base64.b64encode(json.dumps(live_402()).encode()).decode()

    assert payment_requirements({"payment-required": encoded.rstrip("=")})[
        "payTo"
    ] == GRAPH_PAY_TO


def test_a_top_level_block_in_the_body_still_works() -> None:
    """So one call site handles this gateway and the more common shape."""
    block = payment_requirements(
        {}, {"payTo": GRAPH_PAY_TO, "maxAmountRequired": "3000"}
    )

    assert block["maxAmountRequired"] == "3000"


def test_a_402_with_no_requirements_anywhere_is_an_error() -> None:
    with pytest.raises(ValueError, match="no x402 payment requirements"):
        payment_requirements({"content-length": "0"}, body=None)

    with pytest.raises(ValueError, match="no x402 payment requirements"):
        payment_requirements({"payment-required": "not base64 json"}, body=None)


def test_the_third_spelling_of_amount_is_read() -> None:
    """`amount`, where the protocol says `maxAmountRequired`. This is the
    field that made the existing x402 adapter raise in Phase 0."""
    payment = to_payment(live_402()["accepts"][0], RESOURCE, owner="agent-7")

    assert payment.amount_usd == Decimal("0.01")
    assert payment.pay_to_normalised == GRAPH_PAY_TO.lower()


def test_the_other_two_spellings_still_work() -> None:
    for field in ("maxAmountRequired", "amountAtomic"):
        payment = to_payment(
            {"payTo": GRAPH_PAY_TO, field: "10000"}, RESOURCE, owner="o"
        )
        assert payment.amount_usd == Decimal("0.01")


def test_the_merchant_is_the_gateway_not_the_indexer_in_the_block() -> None:
    """`resource.url` names an internal indexer that varies by region.

    Keying on it would make every region a different seller, and the payout
    address belongs to The Graph rather than to the machine serving the query.
    """
    payment = to_payment(live_402()["accepts"][0], RESOURCE, owner="agent-7")

    assert payment.merchant == "gateway.thegraph.com"
    assert "eu-west3" not in payment.merchant


def test_requirements_without_a_payout_address_are_refused() -> None:
    with pytest.raises(ValueError, match="payTo"):
        to_payment({"amount": "10000"}, RESOURCE)


def test_requirements_without_an_amount_are_refused() -> None:
    with pytest.raises(ValueError, match="amount"):
        to_payment({"payTo": GRAPH_PAY_TO}, RESOURCE)


# ------------------------------------------------------------- the fingerprint


def test_the_same_question_fingerprints_the_same() -> None:
    assert query_fingerprint(DEPLOYMENT, QUERY) == query_fingerprint(
        DEPLOYMENT, QUERY
    )


def test_reformatting_a_query_does_not_make_it_a_new_question() -> None:
    """A formatter must not cost a cent."""
    assert query_fingerprint(
        DEPLOYMENT, "{ pairs(first: 5) { id } }"
    ) == query_fingerprint(DEPLOYMENT, "{\n  pairs(first: 5) {\n    id\n  }\n}")


def test_variable_order_does_not_change_the_fingerprint() -> None:
    assert query_fingerprint(DEPLOYMENT, QUERY, {"a": 1, "b": 2}) == (
        query_fingerprint(DEPLOYMENT, QUERY, {"b": 2, "a": 1})
    )


def test_a_different_deployment_query_or_variable_is_a_different_question() -> None:
    base = query_fingerprint(DEPLOYMENT, QUERY, {"first": 5})

    assert query_fingerprint("other", QUERY, {"first": 5}) != base
    assert query_fingerprint(DEPLOYMENT, "{ tokens { id } }", {"first": 5}) != base
    assert query_fingerprint(DEPLOYMENT, QUERY, {"first": 6}) != base


def test_a_deployment_is_required() -> None:
    with pytest.raises(ValueError, match="deployment"):
        query_fingerprint("", QUERY)


# ------------------------------------------------------- behaviour 1: the limit


def test_a_query_over_the_daily_cap_is_escalated_like_anything_else(tmp_path) -> None:
    """The limit already running in production is what makes this safe.

    A cent is not a special case. An agent in a retry loop spends real money a
    cent at a time, and the cap is the only thing that has ever stopped it.
    """
    graph = build(tmp_path, cap="0.05")
    make_known(graph)
    for _ in range(5):
        graph.policy.memory.remember_settlement(
            to_payment(live_402()["accepts"][0], RESOURCE, owner="agent-7")
        )

    answer = fetch(graph, query=QUERY)

    assert answer.paid is False
    assert answer.decision.action is Action.ESCALATE
    assert answer.decision.rule == "daily_cap"


# --------------------------------------------- behaviour 2: provider memory


def test_the_first_payment_to_the_graph_escalates_like_any_stranger(tmp_path) -> None:
    graph = build(tmp_path)

    answer = fetch(graph, query=QUERY)

    assert answer.paid is False
    assert answer.answer is None
    assert answer.decision.action is Action.ESCALATE
    assert answer.decision.rule == "unknown_merchant"
    assert answer.needs_human


def test_once_known_the_graph_is_paid_without_asking(tmp_path) -> None:
    graph = build(tmp_path)
    make_known(graph)

    answer = fetch(graph, query=QUERY)

    assert answer.paid is True
    assert answer.decision.action is Action.PAY
    assert answer.answer == {"data": {"pairs": []}}


def test_a_changed_payout_address_blocks_and_warns_the_whole_fleet(tmp_path) -> None:
    """The reason this endpoint in particular needs memory.

    There is no API key here — the payment *is* the authentication — so a
    redirected payout address is invisible to everything else in the stack.
    Nothing would notice except a record of where this seller has been paid
    before.
    """
    graph = build(tmp_path)
    make_known(graph)

    answer = fetch(graph, query=QUERY, pay_to=ATTACKER)

    assert answer.paid is False
    assert answer.decision.action is Action.BLOCK
    assert answer.decision.rule == "payout_address_changed"
    assert answer.decision.evidence["remembered_pay_to"] == GRAPH_PAY_TO.lower()
    assert answer.decision.evidence["requested_pay_to"] == ATTACKER.lower()

    alert = graph.policy.memory.open_alert("gateway.thegraph.com")
    assert alert is not None
    assert alert["requested_pay_to"] == ATTACKER.lower()


def test_the_alert_stops_a_second_agent_paying_the_moved_address(tmp_path) -> None:
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query=QUERY, pay_to=ATTACKER)

    second = PaidGraphQueries(graph.policy, owner="agent-9")
    answer = fetch(second, query=QUERY, owner="agent-9")

    assert answer.decision.action is Action.BLOCK
    assert answer.decision.rule == "merchant_alert"


# --------------------------------------------- behaviour 3: do not pay twice


def test_a_repeated_query_is_answered_from_the_journal_without_paying(
    tmp_path,
) -> None:
    """The journal is load-bearing in the most literal sense available:
    *reading* it is what prevents the spend."""
    graph = build(tmp_path)
    make_known(graph)

    first = fetch(graph, query=QUERY)
    calls: list[str] = []
    second = fetch(graph, query=QUERY, record=calls)

    assert first.paid is True
    assert second.paid is False
    assert second.answer == first.answer
    assert second.decision is None, "no payment was even considered"
    assert calls == [], "the network was not touched"


def test_a_reformatted_repeat_is_also_answered_from_the_journal(tmp_path) -> None:
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query="{ pairs(first: 5) { id } }")

    again = fetch(graph, query="{\n  pairs(first: 5) {\n    id\n  }\n}")

    assert again.paid is False


def test_a_different_query_is_paid_for(tmp_path) -> None:
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query=QUERY)

    other = fetch(graph, query="{ tokens(first: 5) { id } }")

    assert other.paid is True


def test_an_expired_answer_is_paid_for_again(tmp_path) -> None:
    """The window is a property of the data, not of the budget.

    An agent that gets an answer from four minutes ago has been served
    correctly. One that gets an answer from an hour ago has been served stale
    data to save a cent.
    """
    graph = build(tmp_path, ttl=0)
    make_known(graph)
    fetch(graph, query=QUERY)

    again = fetch(graph, query=QUERY)

    assert again.paid is True


def test_another_owners_answer_is_not_served_to_this_one(tmp_path) -> None:
    """A shared cache would let one owner's budget answer another's question,
    and the journal would say the second one paid for nothing."""
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query=QUERY)

    other = PaidGraphQueries(graph.policy, owner="agent-9")
    answer = fetch(other, query=QUERY, owner="agent-9")

    assert answer.paid is True


def test_an_oversized_answer_is_recorded_but_not_kept(tmp_path) -> None:
    """An unbounded cache inside an append-only journal is a disk-filling bug.

    The ledger stays complete; only the saved cent is lost.
    """
    graph = build(tmp_path)
    make_known(graph)
    huge = {"data": {"blob": "x" * (MAX_REMEMBERED_ANSWER_BYTES + 1)}}

    first = fetch(graph, query=QUERY, answer=huge)

    assert first.paid is True
    line = next(
        e
        for e in graph.policy.memory.journal(limit=50)
        if (e.get("extra") or {}).get("query_fingerprint")
        and (e.get("extra") or {}).get("paid")
    )
    assert "answer" not in line["extra"]
    assert line["extra"]["answer_omitted"]


def test_an_uncacheable_query_is_still_not_paid_for_twice(tmp_path) -> None:
    """The two mechanisms do different jobs and this is where you can see it.

    The journal cache has nothing to give back — the answer was too large to
    keep — so the second attempt goes on to ask the policy. The *claim* is what
    stops it: the payment for this question in this window already settled, and
    a settled claim is permanent.

    Refusing is the right answer rather than an awkward one. The agent was
    handed the answer a moment ago and should still have it; paying a second
    time for a document it already holds is the failure this exists to prevent,
    and the refusal says exactly that.
    """
    graph = build(tmp_path)
    make_known(graph)
    huge = {"data": {"blob": "x" * (MAX_REMEMBERED_ANSWER_BYTES + 1)}}
    fetch(graph, query=QUERY, answer=huge)

    again = fetch(graph, query=QUERY, answer=huge)

    assert again.paid is False
    assert again.decision.action is Action.BLOCK
    assert again.decision.rule == "already_in_flight"
    assert "already went through" in again.decision.reason


# ------------------------------------------------------- behaviour 4: journal


def test_every_query_leaves_one_line_saying_what_it_cost(tmp_path) -> None:
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query=QUERY)
    fetch(graph, query=QUERY)

    lines = [
        e["extra"]
        for e in graph.policy.memory.journal(limit=50)
        if (e.get("extra") or {}).get("kind") == "graph_query"
    ]

    assert len(lines) == 2
    served, paid = lines  # newest first
    assert paid["paid"] is True
    assert paid["amount_usd"] == "0.01"
    assert paid["deployment"] == DEPLOYMENT
    assert served["paid"] is False
    assert served["amount_usd"] == "0"
    assert served["served_from"] == paid_journal_id(graph)


def test_the_week_of_data_spending_is_a_question_with_an_answer(tmp_path) -> None:
    """What nobody with a bare x402 client can say about their own agent."""
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query=QUERY)
    fetch(graph, query=QUERY)
    fetch(graph, query="{ tokens { id } }")

    report = graph.spent_on_data()

    assert report == {
        "owner": "agent-7",
        "queries_paid": 2,
        "queries_from_memory": 1,
        "spent_usd": "0.02",
    }


def test_a_query_note_may_not_carry_an_action(tmp_path) -> None:
    """Rule 6 counts escalations by reading `action` out of the journal, so a
    note that set it would change what the policy decides."""
    graph = build(tmp_path)

    with pytest.raises(ValueError, match="may not set `action`"):
        graph.policy.memory.record_note(
            evaluated=["x"], acted=["y"], extra={"action": "ESCALATE"}
        )


# ------------------------------------------------------------------- claims


def test_a_failed_fetch_gives_the_claim_back(tmp_path) -> None:
    """Otherwise the retry is refused as a duplicate of a payment that never
    happened, and the agent is stuck until the claim expires."""
    graph = build(tmp_path)
    make_known(graph)

    def explode(payment, requirements):
        raise RuntimeError("indexer fell over")

    with pytest.raises(RuntimeError):
        graph.query(
            resource_url=RESOURCE,
            deployment=DEPLOYMENT,
            query=QUERY,
            fetch_402=lambda: (live_headers(), None),
            pay_and_fetch=explode,
        )

    retried = fetch(graph, query=QUERY)
    assert retried.paid is True


def test_the_default_window_is_five_minutes() -> None:
    assert DEFAULT_CACHE_TTL_SECONDS == 300


# ------------------------------------------------------------------- helpers


def paid_journal_id(graph: PaidGraphQueries) -> str:
    for entry in graph.policy.memory.journal(limit=50):
        extra = entry.get("extra") or {}
        if extra.get("kind") == "graph_query" and extra.get("paid"):
            return str(entry["id"])
    raise AssertionError("no paid query line in the journal")


def fetch(
    graph: PaidGraphQueries,
    *,
    query: str,
    pay_to: str = GRAPH_PAY_TO,
    owner: str | None = None,
    answer=None,
    record: list | None = None,
):
    """Drive `query()` with the network stubbed out at the seam it provides."""
    answer = answer if answer is not None else {"data": {"pairs": []}}

    def fetch_402():
        if record is not None:
            record.append("fetch_402")
        return live_headers(pay_to=pay_to), None

    def pay_and_fetch(payment, requirements):
        if record is not None:
            record.append("pay")
        return answer, "0xtx"

    return graph.query(
        resource_url=RESOURCE,
        deployment=DEPLOYMENT,
        query=query,
        fetch_402=fetch_402,
        pay_and_fetch=pay_and_fetch,
        owner=owner,
    )


def test_a_cached_answer_is_served_without_looking_at_the_payout_address(
    tmp_path,
) -> None:
    """The journal is read before the network is touched, so a still-cached
    question never sees a payout address at all.

    That is right rather than a gap. The address check exists to stop money
    going somewhere it should not, and here no money moves: the answer was
    bought and paid for before the merchant moved. The very next question that
    is *not* cached reaches the check and blocks, and the alert it raises stops
    every other agent on this memory.
    """
    graph = build(tmp_path)
    make_known(graph)
    fetch(graph, query=QUERY)

    cached = fetch(graph, query=QUERY, pay_to=ATTACKER)

    assert cached.paid is False
    assert cached.decision is None, "no payment was considered, so none was judged"
    assert graph.policy.memory.open_alert("gateway.thegraph.com") is None

    fresh = fetch(graph, query="{ tokens { id } }", pay_to=ATTACKER)

    assert fresh.decision.action is Action.BLOCK
    assert graph.policy.memory.open_alert("gateway.thegraph.com") is not None
