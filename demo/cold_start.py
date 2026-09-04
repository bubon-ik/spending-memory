"""The demo, as four separate commands.

Each command is its own process. That is the point: nothing is held in memory
between them, so when `buy` pays without asking, the only place that knowledge
could have come from is the database on disk.

    python demo/cold_start.py seed      # three approved purchases, the slow way
    python demo/cold_start.py buy       # fresh process — pays, no human
    python demo/cold_start.py attack    # same merchant, new payout address
    python demo/cold_start.py fleet     # one agent refuses, every agent inherits it
    python demo/cold_start.py forget    # delete the memory, watch it break

Run them in that order, in one unbroken take, with the clock visible.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy  # noqa: E402

DB = os.environ.get("SPENDING_MEMORY_DB", "./demo-memory.db")
CAP = Decimal(os.environ.get("SPENDING_MEMORY_CAP", "500"))

MERCHANT = "bitrefill-amazon-de"
ALICE = "telegram:1001"
BOB = "telegram:2002"
REAL_ADDRESS = "0x8f3a1c4e5b7d9028461fa0c3e5d7b91826af04c1"
ATTACKER_ADDRESS = "0x2b9e77d4c1a03f568e2b41d7c90fa3e5182bd0a7"
PRICE = Decimal("25")

BAR = "─" * 68


def banner(title: str) -> None:
    print(f"\n{BAR}\n  {title}\n  memory: {Path(DB).resolve()}\n{BAR}")


def build() -> tuple[SpendingMemory, SpendingPolicy]:
    memory = SpendingMemory.local(DB)
    return memory, SpendingPolicy(memory, daily_cap_usd=CAP)


def show(decision) -> None:
    mark = {Action.PAY: "PAY     ", Action.ESCALATE: "ASK YOU ", Action.BLOCK: "REFUSED "}
    print(f"\n  {mark[decision.action]}  [{decision.rule}]")
    print(f"  {decision.reason}\n")


def quote(
    pay_to: str = REAL_ADDRESS,
    amount: Decimal = PRICE,
    owner: str = "default",
) -> Payment:
    return Payment(MERCHANT, pay_to, amount, owner=owner, resource="amazon-de-25")


def cmd_seed() -> None:
    """Three purchases the way the product works today: a human approves each."""
    banner("SEED — three purchases, each one approved by a human")
    memory, policy = build()
    for i in range(1, 4):
        payment = quote()
        decision = policy.decide(payment)
        show(decision)
        if decision.needs_human:
            print("  [owner taps approve on their phone]")
        memory.remember_settlement(payment, tx_id=f"0xdemo{i:04d}")
        known = memory.recall_merchant(MERCHANT)
        assert known is not None
        print(f"  remembered: {known.payment_count} payments, "
              f"median {known.typical_usd} USD, address {known.pay_to[:10]}…")


def cmd_buy() -> None:
    """A brand new process. Nothing in RAM. The decision comes off the disk."""
    banner("BUY — fresh process, same purchase")
    _, policy = build()
    show(policy.decide(quote()))


def cmd_attack() -> None:
    """The merchant is known. The address is not."""
    banner("ATTACK — same merchant, different payout address")
    print(f"  known:     {REAL_ADDRESS}")
    print(f"  requested: {ATTACKER_ADDRESS}")
    _, policy = build()
    show(policy.decide(quote(pay_to=ATTACKER_ADDRESS)))


def cmd_fleet() -> None:
    """Two owners, one memory. What one agent learns, every agent knows.

    Bob is asked to pay the address that *is* on file, for the usual price, and
    nothing about his own purchase looks wrong. He is stopped because somebody
    else was asked the same question an hour ago and refused.
    """
    banner("FLEET — one agent refuses, every other agent inherits the doubt")
    memory, policy = build()

    print("\n  Alice has paid this merchant three times.")
    for i in range(1, 4):
        memory.remember_settlement(quote(owner=ALICE), tx_id=f"0xalice{i:04d}")

    print("  Her agent is now asked to pay them somewhere new.\n")
    print(f"  known:     {REAL_ADDRESS}")
    print(f"  requested: {ATTACKER_ADDRESS}")
    show(policy.decide(quote(pay_to=ATTACKER_ADDRESS, owner=ALICE)))

    print("  Bob has never paid this merchant. His agent asks about a normal")
    print(f"  purchase, at the address on file: {REAL_ADDRESS}\n")
    show(policy.decide(quote(owner=BOB)))
    print("  Nothing about Bob's payment is wrong. He is stopped because")
    print("  somebody else already saw this — and he is not told who.\n")

    memory.clear_alert(MERCHANT, cleared_by="operator")
    print("  A person looks at the address and clears the alert.\n")
    show(policy.decide(quote(owner=BOB)))


def cmd_forget() -> None:
    """Remove the memory. This is the judges' own test, run for them."""
    banner("FORGET — delete the memory and try the same purchase")
    path = Path(DB)
    if path.exists():
        path.unlink()
        print(f"  deleted {path.resolve()}")
    _, policy = build()
    show(policy.decide(quote()))
    print("  Same merchant. Same address. Same price. It has to ask again.")
    print("  Everything this project claims came out of that file.\n")


COMMANDS = {
    "seed": cmd_seed,
    "buy": cmd_buy,
    "attack": cmd_attack,
    "fleet": cmd_fleet,
    "forget": cmd_forget,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    COMMANDS[parser.parse_args().command]()


if __name__ == "__main__":
    main()
