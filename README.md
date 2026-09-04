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

Where it runs is checkable rather than assertable:
[`docs/INTEGRATION.md`](docs/INTEGRATION.md) quotes the thirty lines that
changed in the payment system and links each one at the commit it landed in —
including a plain statement of which payment paths this covers and which it
does not.

**It decides real payments today.** Memory was seeded from that agent's own
history — 22 delivered gift-card orders, 301.33 USD, buyers in the Czech
Republic, Germany and Argentina — which makes the merchant `trusted` with a
median order of 2.43 USD. Then, on production, on Base mainnet: the first
purchase from an x402 API nobody had paid before was escalated to a human and
approved
([`0xafc64a…`](https://basescan.org/tx/0xafc64a25dad22f5249cf74554562071dbcb44d4c3a331af4af823fb8b77a0035)),
and the second identical one settled **with nobody asked**
([`0x24e945…`](https://basescan.org/tx/0x24e9454a35cdf3020bb0af80e8adc116d81eab2110bcaf0d40326fe9aa2bc0a7)),
because by then the agent remembered the merchant, the payout address and the
price. Two transactions, one minute apart, and the only difference between them
is what was in memory.

---

## Where the memory is load-bearing

Everything in this section is one file: [`spending_memory/store.py`](spending_memory/store.py).
It is the only module in the package that imports Sibyl, on purpose.

| Sibyl call | Tier | What the agent cannot decide without it |
|---|---|---|
| `get_entity("merchant", …)` | WARM | Whether this merchant is known, and the address it was actually paid at |
| `get_entity("merchant_alert", …)` | WARM | Whether another agent already refused this merchant |
| `get_entity("merchant_pref", …)` | WARM | Whether *this* owner said no to them before |
| `get_state("spend:<owner>:<date>")` | HOT | How much of this owner's limit is already gone |
| `read_events()` | COLD | Whether this merchant keeps being escalated — read back to decide, not only to explain |
| `get_state("claim:<owner>:<digest>")` | HOT | Whether this exact payment is already on its way |
| `set_entity("merchant", …, status=…)` | WARM | — writes a merchant into existence, and promotes it as it earns evidence |
| `set_entity("merchant_alert", …)` | WARM | — warns every other agent that an address moved |
| `set_state("spend:<owner>:<date>")` | HOT | — the running daily total, after every payment |
| `set_state("claim:<owner>:<digest>")` | HOT | — claims a payment before it is made, so it is made once |
| `write_event(…)` | COLD | — one journal line per decision, with what the rules query on |
| `list_entities` / `archive_entity` | WARM | — puts dormant merchants away so they are asked about again |

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

**The rule means two different things on the two paths it runs on, and the
difference is worth stating plainly.** For a paid x402 API, the address in the
402 block is the *merchant's* — so the rule catches the case above: a seller
whose payout address is not the one they were paid at last time. For a gift
card bought through SingIt, the address the user's funds actually move to is
*our own settlement wallet*, not the shop's; there the same rule catches drift
in our wallet rather than a merchant rotating, and if a deployment has no
settlement wallet configured there is no counterparty to compare and the rule
does not fire at all. One mechanism, two honestly different things caught.

---

## Partner stacks, and where each one does the work

**Sibyl Memory** — every decision this package makes. It is the only dependency
the engine has, and [`spending_memory/store.py`](spending_memory/store.py) is
the only module that imports it: `get_entity` and `set_entity` for the merchant,
the per-owner preference and the shared alert (WARM), `get_state` and `set_state`
for the daily total and the in-flight payment claim (HOT), `write_event` and
`read_events` for the journal that rule 6 reads back (COLD). The table at the
top of this README lists each call and what cannot be decided without it. Delete
the layer and the product does not degrade, it stops — `python demo/cold_start.py
forget` runs that test for you.

**Base** — where the money actually moves. Purchases settle in USDC on Base
mainnet over x402, and the decision this package returns is what authorises the
transfer or stops it. Two transactions from production, one minute apart, are
the shortest proof that the memory is on the payment path rather than beside it:

| Transaction | What decided it |
|---|---|
| [`0xafc64a…`](https://basescan.org/tx/0xafc64a25dad22f5249cf74554562071dbcb44d4c3a331af4af823fb8b77a0035) | a human, because the merchant was unknown |
| [`0x24e945…`](https://basescan.org/tx/0x24e9454a35cdf3020bb0af80e8adc116d81eab2110bcaf0d40326fe9aa2bc0a7) | memory, with nobody asked |

The x402 side of it is [`adapters/x402.py`](spending_memory/adapters/x402.py),
which maps a `402 Payment Required` block — `payTo`, `maxAmountRequired`, Base
USDC in atomic units — onto the payment that gets decided. The gateway lines
that call it, and the wallet that signs, are quoted and linked in
[`docs/INTEGRATION.md`](docs/INTEGRATION.md).

No other partner stack is claimed. Nothing here touches Virtuals.

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
decision, claim = policy.authorise(payment)           # add owner="…" for many users

if decision.needs_human:
    ask_the_owner(decision.reason)                    # iMessage, WhatsApp, hardware
else:
    try:
        tx = settle(payment)                          # `claim` is not None here
    except PaymentFailed:
        memory.release_claim(claim)                   # someone may try again
        raise
    memory.settle_claim(claim, tx_id=tx)              # never claimable again
    memory.remember_settlement(payment, tx_id=tx)     # the next one decides itself
```

Only a PAY takes a claim, so a decision that needs a human never holds one.
`policy.decide(payment)` answers the same question without claiming anything,
for a caller that only wants to know.

`SpendingMemory` will not construct without a live `MemoryClient`. There is no
memoryless mode by design: an agent that cannot read its history is not allowed
to guess, because guessing here means spending someone else's money.

## The rules

They run in order, first match wins, and the ordering is the severity ordering.

| # | When | Then | Because |
|---|---|---|---|
| 0 | An identical payment is already in flight | **refuse** | It is one purchase, retried |
| 1 | Never paid this merchant | **ask** | We know nothing |
| 1b | Another agent raised an alert on them | **refuse** | Someone already saw this |
| 2 | Payout address differs from the remembered one | **refuse** | We know something is wrong |
| 3 | This owner rejected them before | **ask** | We were told no, once |
| 4 | Quote is over the band for their status | **ask** | We know what it costs |
| 5 | Over this owner's daily cap | **ask** | We know what today looks like |
| 6 | Escalated three times in the last hour | **refuse** | The journal says something changed |
| — | Otherwise | **pay** | Everything matches memory |

Prices use the **median** of the last twenty, not the mean: one mispriced
purchase should not raise the baseline enough to wave the next one through.

The band in rule 4 comes from how well the merchant is known, and it tightens
as evidence accumulates — 3× when `new`, 2× when `established`, 1.5× when
`trusted`. That is the opposite of the usual instinct, and it is the right way
round: with two payments the median is barely a number and the slack absorbs
how little is known; with twenty, the price is genuinely known, and a spike
against a well-measured baseline is more suspicious than one against a guess.

---

## How memory made this possible

Two things this layer does could not be done by a rule engine with a database
behind it. Both of them are memory patterns, and both exist because they fix a
real defect rather than to demonstrate a pattern.

### The fleet learns together

One process serves every user out of one memory, so the records are split by
what they actually are. **What the merchant is** — the address they are paid
at, how often they have been paid, what they charge — is shared by everybody.
**What one owner decided** — a refusal, a budget, a day's spending — is theirs
alone.

That split is what makes independent agent runs cooperate. A payout address
learned while serving one user protects every other user immediately, which is
the right answer to stale x402 directories: the fleet notices a moved address
faster than any catalog updates. And when one agent refuses a merchant that
asked to be paid somewhere new, it writes a shared alert, so the next agent
asked the same question starts from that refusal instead of working it out
again — without ever learning who raised it.

Run the opposite arrangement and the defect is obvious: with one shared record,
one user saying "not this shop again" silences that shop for everybody.

### A payment is claimed before it is made

Approval takes minutes, and in those minutes the same purchase can arrive again
— a double-tapped button, a client that resent after a timeout, a queue that
redelivered. So `authorise` takes a claim in memory, keyed by owner, merchant,
payout address and amount, and a second identical attempt is refused rather
than paid.

Held in memory rather than in the process, the claim survives the deploy that
happens mid-approval; `tests/test_work_claim.py` claims in one interpreter and
fails to re-claim in another. And a **settled** claim is never re-claimable,
expiry or not — that asymmetry is the replay protection, because a redelivered
request an hour later is exactly when an expiry-only check would pay twice.

### The record changes with use

A merchant is not a fixed row. Every settlement recomputes its status on the
Sibyl record — `new`, then `established`, then `trusted` — and the price band
is chosen from that status, tightening as evidence accumulates. The same quote
that is waved through when a merchant is new is stopped once it is trusted,
because by then the agent knows what they charge well enough for a spike to
mean something.

And a merchant nobody has paid in ninety days is archived, which makes it
unknown again, so the next purchase asks. Storage that only grows would keep
treating a year-old address as current. This one forgets on purpose, and
recoverably — the record moves to ARCHIVE rather than being deleted.

### The journal is read, not only written

A journal nobody reads is ten thousand rows of nothing. Rule 6 reads it: three
escalations for one merchant inside an hour and the agent stops, because a
merchant that has started producing escalations is behaving differently from
how it used to.

That rule cannot be answered from any entity record — not the merchant, not the
preference, not today's total. It exists only because the decisions themselves
are queryable, which is why `record_decision` writes the merchant, the owner and
the action into every entry. `test_deleting_the_journal_makes_the_rule_unreachable`
is the proof: delete the entries and the same merchant, at the same address, for
the same price, is paid without a word.

---

## Prior Work

Read this before judging originality.

**SingIt / Sign402 existed before this hackathon.** It is a live Telegram agent
with managed CDP wallets on Base, Bitrefill fulfilment, out-of-band approval over
iMessage and WhatsApp, and a working x402 client. It has delivered real orders to
real people. None of that was built here, and none of it is claimed as new.

**What is new in this repository** is the memory layer: `store.py`, `policy.py`,
`types.py`, the coordination and dynamic-storage patterns above, the tests, and
the demo — the part that lets the agent decide for
itself whether a purchase needs a human. Before this, SingIt asked about every
single payment. The commit history in this repository starts at the opening of
the build window and is the honest record of what was written during it.

## Licence

MIT. See [LICENSE](LICENSE).
