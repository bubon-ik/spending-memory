"""Paying The Graph for subgraph queries, as four separate commands.

The 402 is fetched live from `gateway.thegraph.com` every time. Nothing here is
a fixture: if the gateway changes its payout address, this demo shows the block
rather than the answer, which is the whole point.

    python demo/graph_queries.py stranger   # first query -> ESCALATE, nothing paid
    python demo/graph_queries.py buy        # approved once, then it pays itself
    python demo/graph_queries.py again      # same query -> answered, NOTHING PAID
    python demo/graph_queries.py moved      # payout address changed -> BLOCK
    python demo/graph_queries.py bill       # what a week of data cost

Run them in that order, in one unbroken take, with the clock visible.

Without `--live-pay` the payment is decided for real and the fetch is simulated,
and the transcript says so on every line. With `--live-pay` it settles on Base
mainnet through `cdp-x402-service` — real USDC, $0.01 a query — and prints the
transaction hash and its Basescan link. Nothing pretends in either mode.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import x402_payer  # noqa: E402
from spending_memory import SpendingMemory, SpendingPolicy  # noqa: E402
from spending_memory.adapters.thegraph import (  # noqa: E402
    PaidGraphQueries,
    payment_requirements,
    to_payment,
)

DEPLOYMENT = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
RESOURCE = f"https://gateway.thegraph.com/api/x402/subgraphs/id/{DEPLOYMENT}"
QUERY = "{ _meta { block { number } } }"
MOVED_QUERY = "{ _meta { block { hash } } }"
"""A different question for the `moved` scene, and the reason is worth knowing.

The journal is read before the network is touched, so a question that is still
cached never reaches a payout address at all. That is correct — no payment is
made, so there is nothing to check — but it means the scene has to ask something
new to have any money at stake.
"""
DB = Path.home() / ".spending-memory-graph-demo" / "memory.db"
OWNER = "demo-agent"
MOVED_ADDRESS = "0x2b9e77d4c1a03f568e2b41d7c90fa3e5182bd0a7"


USER_AGENT = "spending-memory-demo/0.6 (+https://github.com/bubon-ik/spending-memory)"
"""Cloudflare in front of the gateway answers 403 to the default urllib agent.

Named rather than disguised: an agent paying for data should say who it is even
where nothing makes it.
"""


def fetch_402(
    pay_to: str | None = None, query: str = QUERY
) -> tuple[dict, None]:
    """The real 402, from the real gateway, on every run."""
    request = urllib.request.Request(
        RESOURCE,
        data=json.dumps({"query": query}).encode(),
        headers={"content-type": "application/json", "user-agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers)

    if pay_to is not None:
        # The one thing the demo fakes, and it fakes it loudly: a merchant
        # quietly changing where they are paid. There is no way to make The
        # Graph do that on request, and it is the case the memory exists for.
        block = payment_requirements(headers, None)
        block["payTo"] = pay_to
        return {}, {"accepts": [block]}
    return headers, None


def build(cap: str) -> PaidGraphQueries:
    DB.parent.mkdir(parents=True, exist_ok=True)
    policy = SpendingPolicy(
        SpendingMemory.local(str(DB)), daily_cap_usd=Decimal(cap)
    )
    return PaidGraphQueries(policy, owner=OWNER)


def run(
    graph: PaidGraphQueries,
    *,
    pay_to: str | None = None,
    live: bool = False,
    query: str = QUERY,
):
    def pay_and_fetch(payment, requirements):
        if not live:
            print(
                f"   [simulated fetch — decided for real, not settled on mainnet]"
            )
            return {"data": {"_meta": {"block": {"number": "simulated"}}}}, None
        print(f"   {x402_payer.describe(payment)}")
        answer, tx_id = x402_payer.pay_and_fetch(
            payment,
            requirements,
            resource_url=RESOURCE,
            query=query,
        )
        print(f"   settled       : {tx_id}")
        print(f"   basescan      : https://basescan.org/tx/{tx_id}")
        return answer, tx_id

    return graph.query(
        resource_url=RESOURCE,
        deployment=DEPLOYMENT,
        query=query,
        fetch_402=lambda: fetch_402(pay_to, query),
        pay_and_fetch=pay_and_fetch,
    )


def show(answer) -> None:
    print(f"   paid          : {answer.paid}")
    if answer.decision is not None:
        print(f"   action        : {answer.decision.action.value}")
        print(f"   rule          : {answer.decision.rule}")
        print(f"   reason        : {answer.decision.reason}")
        for key, value in (answer.decision.evidence or {}).items():
            print(f"     {key}: {value}")
    else:
        print("   action        : none — no payment was considered")
    print(f"   journal       : {answer.journal_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["stranger", "buy", "again", "moved", "bill", "reset"],
    )
    parser.add_argument("--cap", default="5")
    parser.add_argument("--live-pay", action="store_true")
    args = parser.parse_args()

    if args.command == "reset":
        if DB.exists():
            DB.unlink()
        print(f"forgot everything in {DB}")
        return

    graph = build(args.cap)

    if args.command == "stranger":
        print("A subgraph query, from an agent that has never paid The Graph.")
        show(run(graph, live=args.live_pay))
        print("\nNothing was paid. The address it was asked to pay is new.")
        return

    if args.command == "buy":
        print("The owner approved them once. Recording that, then querying.")
        headers, _ = fetch_402()
        graph.policy.memory.remember_settlement(
            to_payment(payment_requirements(headers, None), RESOURCE, owner=OWNER),
            tx_id="approved-by-owner",
        )
        show(run(graph, live=args.live_pay))
        return

    if args.command == "again":
        print("The same question, a moment later.")
        answer = run(graph, live=args.live_pay)
        show(answer)
        print(
            "\nNo 402 was fetched and no cent was spent. The journal was read "
            "and it already held the answer."
        )
        return

    if args.command == "moved":
        print(f"The same gateway, now asking to be paid at {MOVED_ADDRESS}.")
        show(
            run(
                graph,
                pay_to=MOVED_ADDRESS,
                live=args.live_pay,
                query=MOVED_QUERY,
            )
        )
        alert = graph.policy.memory.open_alert("gateway.thegraph.com")
        print("\nAn alert is now open for every agent on this memory:")
        print("   " + json.dumps(alert, indent=2).replace("\n", "\n   "))
        return

    if args.command == "bill":
        print(json.dumps(graph.spent_on_data(), indent=2))


if __name__ == "__main__":
    main()
