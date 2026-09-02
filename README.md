# Spending Memory

An agent that spends your money should remember what it already spent it on.
Otherwise every purchase is the first one, and it has to ask you every time.

This is the layer that decides whether a payment needs its owner. It reads what
the agent remembers — who it paid, at which address, what things normally cost,
what you said no to, what today already looks like — and returns one of three
answers: **pay**, **ask the owner**, or **refuse**.

Built for the Sibyl Labs hackathon, on [Sibyl Memory](https://docs.sibyllabs.org).
It runs in production behind [SingIt](https://singitai.app), a Telegram agent
that buys real goods and paid APIs, settling in USDC on Base.

---

## Where the memory is load-bearing

Everything in this section is one file: [`spending_memory/store.py`](spending_memory/store.py).
It is the only module in the package that imports Sibyl, on purpose.

| Sibyl call | Tier | What the agent cannot decide without it |
|---|---|---|
| `get_entity("merchant", …)` | WARM | Whether this merchant is known, and the address it was actually paid at |
| `get_state("spend:<date>")` | HOT | How much of today's limit is already gone |
| `read_events()` | COLD | Why a past purchase was made, when the owner asks |
| `set_entity("merchant", …)` | WARM | — writes a merchant into existence after settlement |
| `set_state("spend:<date>")` | HOT | — the running daily total, after every payment |
| `write_event(…)` | COLD | — one journal line per decision |

The decision itself is [`spending_memory/policy.py`](spending_memory/policy.py),
about eighty lines. Every branch is decided by a value that came out of the table
above.

### Remove the memory and the product stops

Not "gets worse" — stops. Three ways, all of them demonstrable:

1. **Every purchase goes back to asking.** With no merchant history there is no
   basis to trust anyone, so the agent escalates on all of them. That is exactly
   the product as it existed before this layer: a wallet with extra steps.
2. **The daily limit becomes fiction.** The running total lives in Sibyl HOT
   state. Hold it in process memory instead and a restart silently resets the
   cap to zero.
3. **A changed payout address goes through unnoticed.** There is nothing to
   compare the requested address against.

Run the third one yourself:

```bash
python demo/cold_start.py seed      # three approved purchases
python demo/cold_start.py buy       # fresh process — pays, no human asked
python demo/cold_start.py attack    # same merchant, new payout address — refused
python demo/cold_start.py forget    # delete the database, watch it ask again
```

`tests/test_policy.py::test_a_brand_new_process_pays_without_asking` is the same
thing as a test: it settles three payments, then launches a **separate Python
interpreter** that pays without a human. Delete the database between the halves
and the test fails.

---

## Why the payout address rule exists

This one is not hypothetical, and it is the reason a plain spending limit is not
enough.

We read every live x402 service we could find and checked where each actually
pays. A meaningful share of them pay to an address the public catalogs do not
have. Directories go stale; merchants rotate payout addresses and never update
their listing. An agent with a generous limit and a stale catalog does not make
a small mistake — it pays a stranger, on schedule, with your permission.

A limit is a number. Deciding takes something to compare against, and that is
what memory is for here.

---

## Install

```bash
pip install -e ".[dev]"
python -m pytest
```

Requires Python 3.10+ and `sibyl-memory-client`. Sibyl stores everything in one
local SQLite file — no vector database, no embeddings, no network call in the
decision path.

## Use

```python
from decimal import Decimal
from spending_memory import Payment, SpendingMemory, SpendingPolicy

memory = SpendingMemory.local()                       # ~/.sibyl-memory/memory.db
policy = SpendingPolicy(memory, daily_cap_usd=Decimal("50"))

payment = Payment("bitrefill-amazon-de", "0x8f3a…", Decimal("25"))
decision = policy.decide(payment)

if decision.needs_human:
    ask_the_owner(decision.reason)                    # iMessage, WhatsApp, hardware
else:
    tx = settle(payment)
    memory.remember_settlement(payment, tx_id=tx)     # the next one decides itself
```

`SpendingMemory` will not construct without a live `MemoryClient`. There is no
memoryless mode by design: an agent that cannot read its history is not allowed
to guess, because guessing here means spending someone else's money.

## The rules

They run in order, first match wins, and the ordering is the severity ordering.

| # | When | Then | Because |
|---|---|---|---|
| 1 | Never paid this merchant | **ask** | We know nothing |
| 2 | Payout address differs from the remembered one | **refuse** | We know something is wrong |
| 3 | Owner rejected them before | **ask** | We were told no, once |
| 4 | Quote is over 3× the remembered median | **ask** | We know what it costs |
| 5 | Over the daily cap | **ask** | We know what today looks like |
| — | Otherwise | **pay** | Everything matches memory |

Prices use the **median** of the last twenty, not the mean: one mispriced
purchase should not raise the baseline enough to wave the next one through.

---

## Prior work

Read this before judging originality.

**SingIt / Sign402 existed before this hackathon.** It is a live Telegram agent
with managed CDP wallets on Base, Bitrefill fulfilment, out-of-band approval over
iMessage and WhatsApp, and a working x402 client. It has delivered real orders to
real people. None of that was built here, and none of it is claimed as new.

**What is new in this repository** is the memory layer: `store.py`, `policy.py`,
`types.py`, the tests, and the demo — the part that lets the agent decide for
itself whether a purchase needs a human. Before this, SingIt asked about every
single payment. The commit history in this repository starts at the opening of
the build window and is the honest record of what was written during it.

## Licence

MIT. See [LICENSE](LICENSE).
