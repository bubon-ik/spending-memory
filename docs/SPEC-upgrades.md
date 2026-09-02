# Spec — coordination and dynamic storage

Work in **this** repository (`spending-memory`). The gateway integration is a
separate document in the SingItAI repo; do not edit gateway code from here.

## Why

The hackathon rubric puts 40 of 100 points on how the memory is used, and words
it precisely:

> Notepad-tier use is the floor and will not place. Cross-session work that
> steers behavior is competitive. Memory as a coordination or dynamic-storage
> layer (**shared state, work-claim primitives, a load-bearing journal**) tops
> the band.

Today this package does cross-session recall: read the merchant, read the daily
total, decide. That is "competitive", explicitly not the top.

Three patterns are named as topping the band, and this spec adds all three:

| Named pattern | Part | What it becomes here |
|---|---|---|
| Shared state | A | Merchant facts learned once protect every owner |
| Dynamic storage | B | Records promote, bands tighten, dormant merchants archive |
| Work-claim primitive | C | A payment is claimed before it is made, so it cannot be made twice |
| Load-bearing journal | D | The journal is **read** to decide, not only written |

None of them is decoration. C closes "Add durable replay protection", which is
already an open item on the product roadmap, and D is what makes the COLD tier
count for anything.

## What exists now (do not break it)

`spending_memory` v0.2.0, 27 tests passing.

- `types.py` — `Payment`, `MerchantMemory`, `Decision`, `Action`
- `store.py` — the only module importing Sibyl. Reads `get_entity`, `get_state`,
  `read_events`; writes `set_entity`, `set_state`, `write_event`
- `policy.py` — five rules, first match wins, severity ordered
- `adapters/x402.py` — `merchant_key`, `to_payment`, `build_policy`
- `demo/cold_start.py` — four commands, one process each

Verified facts about the Sibyl API (do not re-derive, do not guess):

- `MemoryClient.local(path, tenant_id=…)`
- `get_entity(category, name)` **raises `NotFoundError`** when absent
- `get_state(key)` returns `{"body": {...}, "updated_at": …}` or `None`
- `set_entity(category, name, body, status=…)` — `status` is a real parameter
- `archive_entity(category, name, reason=…)` moves a record to ARCHIVE
- `list_entities(category, status=…, limit=…)`
- `write_event(evaluated=…, acted=…, extra=…)` returns the entry id
- `search_entities(query, category=…)` returns a list-like `SearchResults`

---

# Part A — coordination: the fleet learns together

## The defect this fixes

One gateway process serves every user from one Sibyl database, so today all
users share one `merchant` record. That means **one user's rejection silences a
merchant for everybody**, which is wrong, and it is only invisible because
nobody has two users doing different things yet.

The fix is to split what is genuinely shared from what belongs to one owner.

| Shared across all owners | Private to one owner |
|---|---|
| The payout address a merchant is actually paid at | Whether *I* rejected them |
| How many payments the fleet has seen | What *I* have spent today |
| Observed prices | — |
| An open alert that this merchant's address moved | — |

That split is the coordination pattern: independent agent runs, acting for
different owners, cooperate through one memory. A payout address learned once
protects everyone, which is the right answer to stale directories — the fleet
notices faster than any catalog updates.

## API changes

`Payment` gains an owner. It is required, because a payment nobody owns cannot
be charged against anyone's budget.

```python
@dataclass(frozen=True)
class Payment:
    merchant: str
    pay_to: str
    amount_usd: Decimal
    owner: str = "default"      # keep a default so the demo stays one-liner
    resource: str | None = None
```

This is a breaking change for the gateway, so bump to **0.3.0** and say so in
the README.

## Storage layout

| Sibyl | Category / key | Holds |
|---|---|---|
| WARM | `merchant` / `<merchant>` | `pay_to`, `payment_count`, `prices_usd`, `status`, `last_settled_at` |
| WARM | `merchant_pref` / `<owner>:<merchant>` | `rejected`, `rejected_reason`, `own_count` |
| WARM | `merchant_alert` / `<merchant>` | `previous_pay_to`, `requested_pay_to`, `raised_by`, `raised_at`, `cleared` |
| HOT | `spend:<owner>:<utc-date>` | `total_usd` |
| COLD | journal | one entry per decision, as now |

Note `spend:` gains the owner segment. Without it two users share one budget.

## New store methods

```python
def recall_merchant(self, merchant: str) -> MerchantMemory | None      # shared, unchanged shape
def recall_preference(self, owner: str, merchant: str) -> dict         # {"rejected": bool, "reason": str|None}
def open_alert(self, merchant: str) -> dict | None                     # None when absent or cleared
def raise_alert(self, merchant, *, previous_pay_to, requested_pay_to, raised_by) -> None
def clear_alert(self, merchant: str, *, cleared_by: str) -> None
def spent_today(self, owner: str, *, day: str | None = None) -> Decimal
```

`remember_settlement`, `remember_rejection` and `record_decision` all take the
owner from `payment.owner`. `remember_rejection` writes to `merchant_pref`
**only** — a rejection is a personal preference and must not touch the shared
record.

## New rule, and where it goes

`raise_alert` is called by the policy itself whenever rule 2 fires. A new rule
sits **between rules 1 and 2**, because an alert raised by someone else is the
strongest thing we can know about a merchant:

```
1. never paid this merchant          -> ESCALATE
1b. an open alert on this merchant   -> BLOCK      <- new
2. payout address differs            -> BLOCK  (and raise the alert)
3. this owner rejected them before   -> ESCALATE  (reads merchant_pref)
4. price outside the band            -> ESCALATE
5. over this owner's daily cap       -> ESCALATE
```

Rule 1b's reason names that someone else hit this, without naming who:

> "Another agent was asked to pay bitrefill at a different address two hours
> ago and refused. Until that is resolved I am not paying them either."

Clearing an alert is deliberate and manual — `clear_alert` is exposed but never
called automatically. An alert that expires on a timer protects nobody.

---

# Part B — dynamic storage: the record changes with use

## Merchant status, stored not computed

Use Sibyl's `status` field on `set_entity`. Recompute it on every settlement:

| Status | Condition | Price band |
|---|---|---|
| `new` | 1–2 payments | 3× the median |
| `established` | 3–9 payments | 2× |
| `trusted` | 10+ payments | 1.5× |

The band **tightens** as evidence accumulates, which is the opposite of what
people expect, so say why in the code: with two data points the median is
barely a number and the slack absorbs that; with twenty the price is genuinely
known and a spike is more suspicious, not less.

`PRICE_SPIKE_FACTOR` stays as the `new` value so existing behaviour is the
default, and the constant becomes a mapping.

## Archive on dormancy

```python
def archive_dormant(self, *, older_than_days: int = 90) -> list[str]
```

Walk `list_entities("merchant")`, and for each whose `last_settled_at` is older
than the cutoff, call `archive_entity("merchant", name, reason="dormant")`.
Return the names archived.

`recall_merchant` must then return `None` for an archived merchant, so it is
treated as unknown and asks again. That is the intended behaviour: a merchant
you last paid a year ago deserves a fresh look, and the record is recoverable
rather than deleted.

Do **not** run this on a timer inside the policy. Expose it as a method and let
the host call it; a decision path that silently mutates storage is a decision
path nobody can reason about.

## Decision evidence

`Decision.evidence` gains `merchant_status` on every rule that has a merchant,
so the dashboard and the journal show the band that was applied.

---

# Part C — work-claim: a payment is claimed before it is made

## The defect this fixes

The gateway already reasons about this in `_reserve_user_wallet_spend`:

> "The hold is what closes the window between the cap check and the recorded
> spend: approval can take minutes, and a second purchase started in that window
> would otherwise measure itself against a total that ignores the first."

That is a work-claim, and today it lives in the gateway's own SQLite, which
means it does not survive a restart and does not span processes. A payment
system that forgets its in-flight claims on deploy can pay twice.

## The primitive

```python
def claim_payment(self, payment: Payment, *, ttl_seconds: int = 120) -> str | None:
    """Claim the right to make this payment. None means someone already has it.

    The key is derived from owner, merchant, payout address and amount. Inside
    the TTL an identical request is a retry — a double-tapped button, a client
    that resent after a timeout, a queue that redelivered — not a second
    intention to spend. Returning None is how the caller learns to stop.
    """

def settle_claim(self, claim_id: str, *, tx_id: str | None = None) -> None:
    """Mark the claim spent. It is never reusable again, TTL or not."""

def release_claim(self, claim_id: str) -> None:
    """Give the claim back after a rejection, timeout or error."""
```

Storage: Sibyl HOT state, key `claim:<owner>:<digest>`, body
`{"claim_id", "status": "held"|"settled"|"released", "claimed_at", "expires_at",
"merchant", "amount_usd"}`. `digest` is a sha256 of owner, merchant,
`pay_to_normalised` and the amount — stable, and it leaks nothing in a key name.

Rules for the implementation:

- A `held` claim that has not expired → return `None`.
- A `settled` claim → return `None` **regardless of expiry**. Settled is
  permanent; that is the replay protection.
- A `released` or expired `held` claim → may be re-claimed, and the new claim
  overwrites it.
- Expiry is compared against stored `expires_at`, never against process uptime.

## Where the policy uses it

`SpendingPolicy.decide` does not claim — deciding is not spending. Add a
separate method so the caller's order is explicit and testable:

```python
def authorise(self, payment: Payment) -> tuple[Decision, str | None]:
    """Decide, and if the answer is PAY, take the claim.

    Returns the decision and the claim id. A PAY with a None claim id means
    another attempt already holds it — the caller must not settle.
    """
```

When the claim is refused, return a `BLOCK` decision with rule
`already_in_flight` and a reason a person can read:

> "An identical payment to bitrefill for 25 USD was already started a moment
> ago and has not finished. I am not sending a second one."

---

# Part D — the journal is read, not just written

## The defect this fixes

Today `record_decision` writes to the COLD tier and nothing ever reads it back
to make a decision. A journal nobody reads is exactly the "ten thousand rows you
never read" the gate warns about.

## New read

```python
def recent_decisions(
    self,
    *,
    merchant: str | None = None,
    owner: str | None = None,
    within_seconds: int = 3600,
) -> list[dict[str, Any]]:
    """Journal entries matching the filters, newest first.

    Reads `read_events` and filters on the `extra` payload, so `record_decision`
    must put `merchant` and `owner` in `extra` — check that it does before
    relying on it here.
    """
```

## New rule, from the journal

A merchant that keeps producing escalations is behaving differently from how it
used to, and the evidence for that lives only in the journal:

```
6. three or more escalations for this merchant in the last hour -> BLOCK
```

Reason:

> "bitrefill has been asked about four times in the last hour and something is
> off. Stopping until you look at it."

This rule cannot be evaluated from the entity record. It exists only because the
journal is read — which is the point, and which is what to say in the README.

## The full rule order after this spec

```
0.  an identical payment is already in flight   -> BLOCK   (Part C)
1.  never paid this merchant                    -> ESCALATE
1b. an open alert raised by another agent       -> BLOCK   (Part A)
2.  payout address differs                      -> BLOCK   (raises the alert)
3.  this owner rejected them before             -> ESCALATE
4.  price outside the status band               -> ESCALATE (Part B)
5.  over this owner's daily cap                 -> ESCALATE
6.  repeated escalations in the journal         -> BLOCK   (Part D)
                                                -> PAY
```

Rule 6 sits last on purpose: it is the weakest signal and the most likely to
misfire, so anything with a concrete cause should fire before it.


---

# Tests

Add to `tests/`, keep every existing test passing.

**Coordination**

1. Two owners, one merchant: owner A's rejection does not stop owner B.
2. Two owners, one merchant: the payout address learned from A's settlement
   lets B pay without asking. This is the coordination test — name it clearly.
3. A's blocked address change raises an alert; B's next payment is BLOCKED by
   rule 1b, with a reason that does not leak A's identity.
4. `clear_alert` lets B pay again.
5. Two owners have separate daily budgets; A spending does not consume B's.

**Dynamic storage**

6. Status is `new` at 1 payment, `established` at 3, `trusted` at 10.
7. A price allowed at `new` is refused at `trusted` — same price, same merchant,
   different amount of evidence.
8. `archive_dormant` archives a merchant whose last settlement is older than the
   cutoff, and leaves a recent one alone.
9. After archiving, `recall_merchant` returns `None` and the next payment asks.
10. `evidence["merchant_status"]` is present on PAY and on the price rule.

**Work-claim (Part C)**

11. A second identical claim inside the TTL returns `None`.
12. After the TTL passes, an unsettled claim can be re-taken.
13. A **settled** claim can never be re-taken, even long after the TTL. Name this
    test for replay protection — it is the one a judge should find.
14. A released claim can be re-taken immediately.
15. `authorise` returns a BLOCK with rule `already_in_flight` when the claim is
    refused, and never returns a PAY with a claim id someone else holds.
16. Claims survive a process restart: claim in one interpreter, fail to re-claim
    in a second, same database.

**Load-bearing journal (Part D)**

17. `recent_decisions` filters by merchant, by owner and by age.
18. Three escalations inside the window produce rule 6; two do not.
19. Escalations older than the window do not count.
20. Deleting the journal entries makes rule 6 unreachable — the rule has no
    other source of truth.

**Still true**

21. The cold-start subprocess test still passes.
22. `python -m pytest` is green: 27 existing plus the new ones.

---

# What must not change

- `store.py` stays the only module that imports Sibyl.
- No in-process fallback: `SpendingMemory(None)` still raises.
- `Decision.journal_id` still carries the journal entry id.
- `adapters/x402.to_payment` keeps its signature; add `owner` as a keyword with
  a default so the gateway can adopt it in a second step.
- The demo keeps working with a single owner and no arguments.

---

# Commits

This repository's history is read by a judge as evidence the work happened
inside the build window. Rules below are not style preferences.

**One commit per thing that works** — a point where the tests pass and something
new is possible. Not per file, not per day. Expect roughly four here:

```
store: split shared merchant facts from per-owner preferences
policy: block on an alert another agent raised
store: promote merchants by evidence and tighten the price band
store: archive dormant merchants so they are asked about again
store: claim a payment before making it, so it cannot be made twice
policy: read the journal to stop a merchant that keeps escalating
```

Subjects say what the code can now do, not what was touched. Bodies explain
*why* when the reasoning is not obvious from the diff — the tightening band and
the alert-never-auto-clears decision both deserve one.

**Never rewrite history.** No squash, no `--amend` on anything pushed, no
force-push. A history flattened near the deadline is the pattern the rules
describe as assumed unqualified.

**Never commit** `memory.db`, `*.sqlite3`, `.env`, or any key. Run `git status`
before every commit.

**Version.** Bump to `0.3.0` in `pyproject.toml` and `__init__.py` in the first
commit that breaks the `Payment` signature, and tag `v0.3.0` when the suite is
green. The gateway pins a commit SHA, so nothing there breaks until it is
re-pinned deliberately.

**README.** Add two sections named exactly as the rules name them:
`How memory made this possible` and `Prior Work`. The first is where the
coordination and dynamic-storage patterns get explained in plain words — it is
the paragraph a judge reads while scoring the 40-point band.
