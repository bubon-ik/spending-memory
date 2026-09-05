---
name: paying-for-data
description: This skill should be used whenever the agent is about to spend money on behalf of someone else — an HTTP 402 Payment Required, an x402 endpoint, a paid API, a per-query data provider such as The Graph's x402 gateway, or any purchase charged to an owner's budget. It covers asking a spending policy for a verdict before paying, and what each verdict obliges the agent to do.
version: 1.0.0
---

# Paying for data without spending someone else's money badly

An agent with a wallet and no account is a new kind of thing. On an x402
endpoint there is no API key, so **the payment is the authentication**: no
signup, no rate limit attached to a person, nothing that says "you have already
asked this". The wallet is the account.

That is a good property. It is also why a retry loop is a bill, why a forgotten
answer is a second charge, and why a merchant that changes where it is paid is
invisible to everything else in the stack.

This skill is the discipline that makes an autonomous payer safe: **ask before
paying, and obey the answer.**

## When to use this

Any time money is about to move on someone else's behalf:

- an HTTP **402 Payment Required** arrives
- an x402 or paid API is about to be called
- a per-query data provider is about to be queried — The Graph's x402 gateway
  charges $0.01 in USDC on Base per subgraph query, with no key
- any purchase charged to an owner's budget

Do not use it for free endpoints. Asking about a payment that is not happening
fills the journal with noise and teaches nobody anything.

## First: a 402 is a price, not an error

This is the single most common mistake and it goes in both directions.

**402 is not a failure.** It is the seller quoting a price. An agent that
reports "the API returned an error" has misread a price tag as a locked door.

**402 is not permission either.** It is not a reason to pay. It is the input to
the decision below.

So: read the price, ask whether to pay it, and if the answer is yes, pay and
**retry the request once**. Once. A 402 that arrives again after a settled
payment is a real failure, and retrying past it is how one query becomes twenty
charges.

## The call

`POST /v1/decide`

```json
{
  "merchant": "gateway.thegraph.com",
  "payTo": "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB",
  "amountUsd": "0.01",
  "owner": "agent-7",
  "resource": "https://gateway.thegraph.com/api/x402/subgraphs/id/5zvR82…"
}
```

Four fields decide everything, and each has a way of being quietly wrong:

**`merchant` is a bare host, never a URL.** `gateway.thegraph.com`, not
`https://gateway.thegraph.com/api/x402/...`. This is the identity the payout
address is remembered against. Send a URL and you get a `400 invalid-merchant`
— which is the good outcome. The bad outcome, if it were accepted, is being
told a seller you have paid twenty times is a stranger.

**`payTo` is copied from the 402 exactly as it arrived.** Do not substitute an
address you have on file, do not correct it, do not normalise it. The whole
value of the check is that it compares what the merchant *just asked for*
against what is remembered. Repair it and there is nothing left to notice.

**`amountUsd` is a decimal string.** `"25.00"`, not `25.0`. A JSON number is a
`400 invalid-amount`. The amount is compared against a limit, and a float
cannot hold `0.1`, so `25.0` arriving as `25.000000000000004` decides an edge
case differently from the same payment sent as a string.

**`owner` is required and has no default.** Budgets, refusals and daily totals
belong to a person. Two owners sharing an identifier share a budget.

## Then: what the answer obliges you to do

**This is the part no schema can tell you.** An OpenAPI document can describe
the response perfectly and leave you with no idea what is being asked of you.
`ESCALATE` on its own means nothing until someone says what an agent must do
about it.

| Verdict | What you must do |
| --- | --- |
| **`PAY`** | Go ahead. Record `journalId` alongside the transaction, so the payment points at the facts that authorised it. Release or settle the `claimId` when the attempt finishes, either way. |
| **`ESCALATE`** | **Stop.** Show `reason` to the human **verbatim** and wait for them. Do not retry. Do not rephrase the request. Do not try a smaller amount. Do not ask a second time hoping for a different answer. |
| **`BLOCK`** | **Stop completely.** Do not look for another route to the same payment. Show `rule` and `evidence` to the human. This is not a rate limit and waiting will not clear it. |

Three things that follow from the table and are worth saying plainly:

**`ESCALATE` is a question, not a rate limit.** The only thing that resolves it
is a person answering. An agent that retries an escalation is an agent asking
the same question faster.

**Do not reshape a payment to get a different verdict.** Splitting $25 into five
payments of $5 to get under a cap, or trying a different amount after a price
spike, defeats the control deliberately. If the limit is wrong, the human
changes the limit.

**`reason` is written to be read.** It is a sentence for a person, with the
merchant and the numbers in it. Show it as it is. Do not summarise it, do not
turn it into "payment failed", and do not translate it into an error code — the
detail you drop is exactly what the human needs to decide.

## What the HTTP status means, and what it does not

**A verdict is always `200`, including `BLOCK`.** The status code says whether
the question was understood. The `action` field says what the answer was.

This trips agents up because every HTTP client has a reflex about status codes,
and both reflexes are wrong here: a `BLOCK` is not an outage to retry and not a
bug to report. It is a considered refusal, and it arrived successfully.

| Status | Meaning |
| --- | --- |
| `200` | Answered. **Read `action`.** |
| `400` | The request was malformed — see `error`. Fix the field and ask again. This is the one case where asking again is correct, because nothing was decided. |
| `503` | Spending memory is switched off. **There is no verdict.** Treat this as the absence of an answer, not as permission. Escalate to a human. |

## Reading the journal

`GET /v1/journal?owner=agent-7&limit=50`

Every decision leaves a line, whatever the verdict. This is what answers "why
did you buy that" and "what did we spend on data this week". One owner per
call.

## Not paying twice

Before paying for a query, check whether the answer was already bought. In this
package that is `spending_memory.adapters.thegraph`, and the check reads the
journal — the same journal above — so *reading the ledger is what prevents the
spend*.

```python
from spending_memory import SpendingMemory, SpendingPolicy
from spending_memory.adapters.thegraph import PaidGraphQueries

policy = SpendingPolicy(SpendingMemory.local(), daily_cap_usd=Decimal("5"))
graph = PaidGraphQueries(policy, owner="agent-7")

answer = graph.query(
    resource_url=RESOURCE,
    deployment=DEPLOYMENT,
    query="{ _meta { block { number } } }",
    fetch_402=fetch_402,        # yours: returns (headers, body) of the 402
    pay_and_fetch=pay_and_fetch,  # yours: settles and returns (answer, tx_id)
)

if answer.needs_human:
    show(answer.decision.reason)   # verbatim, then stop
else:
    use(answer.answer)             # answer.paid says whether it cost anything
```

`fetch_402` and `pay_and_fetch` are supplied by the caller on purpose: this
package decides and remembers, and never moves money itself.

## Worked example

An agent is asked to buy a $25 gift card from a merchant it has never paid.

1. It calls the merchant and gets a `402` naming `0x8f3a…` and $25.
2. It reads that as a price, not an error, and asks `/v1/decide` with
   `merchant: "giftcards.example.com"` — the host, not the URL — and
   `amountUsd: "25.00"` as a string.
3. The answer is `200`, `action: "ESCALATE"`, `rule: "unknown_merchant"`,
   `claimId: null`, and a reason that says so in a sentence.
4. **The agent stops.** It shows the reason to its owner, word for word, and
   waits. It does not pay, does not try $24, does not ask again, and does not
   report an API error.

That is the whole skill. The first purchase from a stranger costs a human's
attention, and every purchase after it does not.
