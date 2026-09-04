# Where this runs in production

This package is not a demo with a wrapper around it. It decides real purchases
in [SingIt](https://singitai.app), a Telegram agent with managed CDP wallets
that settles in USDC on Base and has delivered real gift cards to real people.

Everything linked below is a commit you can open, not a claim you have to take
on trust. The links are pinned to the commit the code landed in
([`b92f904`](https://github.com/bubon-ik/SingItAI/commit/b92f904)), so they keep
pointing at the same lines after the branch moves on.

## The integration is about thirty lines

Every user spend in the gateway already passed through one pair of functions —
one that holds budget before a purchase, one that records it after. The decision
went there rather than onto a product path, so nothing that spends money can
forget to ask.

**Reserving now also decides** —
[`_reserve_user_wallet_spend`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L7033):

```python
    if reservation_id is not None:
        if server.spending_policy is None:
            # Memory is off. No decision means "ask the owner", which is what
            # every caller already does when the answer is not a clean PAY.
            return reservation_id, None, None
        payment = _payment_from_requirements(
            payment_requirements,
            owner=telegram_user_id,
            resource_url=resource_url,
        )
        decision, claim_id = server.spending_policy.authorise(payment)
        if decision.action.value == "BLOCK":
            # Nothing was spent, so nothing may stay held — including on the
            # paths whose own error handling never learns a decision was taken.
            _release_user_wallet_spend(server, reservation_id)
            raise SpendingBlocked(decision)
        return reservation_id, decision, claim_id
```

`authorise` decides and, only on a PAY, takes the claim, so there is no case
where the gateway settles something it does not hold.
[`SpendingBlocked`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L6981)
is a `ValueError` subclass carrying the decision: every existing handler still
turns it into a 400, and the two call sites that care render the reason a person
reads.

**The caller branches on whether a human is needed** —
[the x402 tool path](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L1715):

```python
            if decision is None or decision.needs_human:
                approval = self.server.imessage_approval_service.request_purchase_approval(
                    ...
                )
                ...
            else:
                approval = {
                    "ok": True,
                    "status": "approved",
                    "source": "spending_memory",
                    # Carried into the event by the buyer and from there into
                    # the spend ledger, so the row points at the journal entry
                    # holding the rule and the evidence.
                    "approvalId": f"sm-{decision.journal_id}",
                    "reason": decision.reason,
                    "rule": decision.rule,
                }
```

That `sm-<journal_id>` is the whole audit story in one field. It travels into
the gateway's own spend ledger, so a purchase nobody approved still points at
the exact journal entry — rule, evidence, remembered facts — that authorised it.
A memory-approved payment is *more* auditable than a person tapping yes, not
less.

**Settling remembers** —
[`_settle_user_wallet_spend`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L7155):

```python
    if payment is None or server.spending_policy is None:
        return
    if claim_id:
        # Settled claims are never re-claimable, whatever their TTL says: this
        # is what stops a redelivered request paying for the same thing twice.
        server.spending_policy.memory.settle_claim(claim_id, tx_id=tx_id or None)
    server.spending_policy.memory.remember_settlement(payment, tx_id=tx_id or None)
```

The payment is passed in by the callers rather than rebuilt here, because the
requirement does not say whose money it was, and a settlement charged to the
wrong owner is worse than one nobody remembers.

**Gift cards needed a counterparty before they could be decided on at all** —
[`_bitrefill_spend_requirement`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L7240)
now names an address. See the honest caveat below about *whose* it is.

**The rescue lever** —
[`build_spending_policy_from_env`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L6958)
returns `None` when `SIGN402_SPENDING_MEMORY_ENABLED=0`, and reads the switch
*before* building the policy, so a box with memory off boots even with no
autonomy cap configured. A rescue lever that itself needs configuration is not a
rescue lever.

## What memory covers, and what it does not

**Covered:** x402 paid tools and Bitrefill gift-card purchases. Both settle
through the reserve/settle pair, so both are decided and both are remembered.

**Not covered:** Bankr LLM credit top-ups, the Venice chat prefund, and web
search. All three move a user's money by other routes — the LLM top-up enforces
its caps through a separate function and records the spend directly, and the
chat and search lanes pay from the user's wallet without touching the spend
limit store at all. They ask nothing and remember nothing today.

This is stated because the alternative is worse. A reader who works out on their
own that "every payment" meant "two of five payment paths" has found an
overclaim; the same reader told plainly has found a system that knows its own
boundaries. Extending memory to the other three is mechanical — the same two
calls at their own chokepoints — and deliberately not claimed as done.

**One more boundary, on the payout-address rule.** For an x402 API the address
in the 402 block belongs to the merchant, so the rule catches a seller whose
payout address is not the one they were paid at last time. For a gift card, the
address the user's funds actually move to is SingIt's own settlement wallet, not
Bitrefill's, so there the same rule catches drift in *our* wallet. Where a
deployment has no settlement wallet configured, there is no counterparty and the
rule does not fire —
[`_bitrefill_settlement_address`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/sign402_gateway/server.py#L7225)
returns a constant that cannot drift rather than inventing an address that would
make the check look like it was checking something.

## Prior art inside the same codebase

The idea did not arrive from nowhere, and pretending otherwise would be easy to
check.

`venice_chat.py` in the same gateway already binds a chat policy to a single
`pay_to` and pauses every affected user with `MERCHANT_CHANGED` when the address
it was bound to stops matching. That is one merchant, one address, decided in
the moment and thrown away afterwards.

What this package adds is the memory underneath that instinct: not one bound
address but a record per merchant, accumulated across sessions and across
owners, with a payment count, a price history, a status that tightens as
evidence grows, and a journal that later decisions read. The Venice check
answers "is this the address I was told about". This answers "is this the
address I have been paying, is that price normal for them, did another agent
just refuse them, and have I already sent this exact payment".

## Reproducing the claim

The gateway suite covers the branch that spends money without a human:
a known merchant pays with the approval service **not called** and an
`approvalId` of `sm-<journal_id>` that resolves to a real journal entry; an
unknown merchant still asks; a `SpendingBlocked` returns 400 with its rule,
reason and evidence, and gives the hold back; and with the kill switch off,
every purchase asks again. Those tests are in
[`tests/test_gateway_server.py`](https://github.com/bubon-ik/SingItAI/blob/b92f904/sign402-gateway/tests/test_gateway_server.py).
