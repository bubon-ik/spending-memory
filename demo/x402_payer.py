"""Settling a Graph query on Base mainnet, for real, with real USDC.

The adapter decides; this pays. Keeping them apart is deliberate: the package
must not import a wallet, a node, or anything that can move money, so the payer
lives in the demo and is handed in as `pay_and_fetch`. Anyone integrating this
supplies their own and the policy is unchanged.

The signing itself is not reimplemented here. `cdp-x402-service` in the SingIt
gateway already runs the full x402 cycle — 402, EIP-3009 authorisation, retry
with `PAYMENT-SIGNATURE` — against a CDP-held account, and it is the same code
path production uses to buy gift cards. It is called as a subprocess, which is
also what keeps a Node dependency out of a Python package.

Configure with:

    SPENDING_MEMORY_X402_SERVICE=/path/to/cdp-x402-service
    plus whatever that service needs for its account (CDP_API_KEY_ID etc.)

The caps passed on every call are not decoration. `--expected-receiver` is the
payout address the *policy* just approved, so if the gateway answered the 402
with one address and the signer is about to pay another, the payment is refused
before it is signed. The memory rule and the wallet guard have to agree.
"""

from __future__ import annotations

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

SERVICE_DIR_ENV = "SPENDING_MEMORY_X402_SERVICE"
TIMEOUT_SECONDS = 180
"""A mainnet settlement waits on a block, not on a function call."""


class PaymentFailed(RuntimeError):
    """The payment did not settle. No answer, and nothing is remembered."""


def _service_dir() -> Path:
    raw = os.environ.get(SERVICE_DIR_ENV, "").strip()
    if not raw:
        raise PaymentFailed(
            f"{SERVICE_DIR_ENV} is not set, so there is no x402 payer to settle "
            "with. Point it at the `cdp-x402-service` directory."
        )
    path = Path(raw).expanduser()
    if not (path / "src" / "index.mjs").exists():
        raise PaymentFailed(f"{path} does not look like cdp-x402-service")
    return path


def pay_and_fetch(
    payment: Any,
    requirements: Mapping[str, Any],
    *,
    resource_url: str,
    query: str,
) -> tuple[Any, str | None]:
    """Pay for one query and return `(answer, tx_id)`.

    Raises rather than returning an empty answer: a settlement that half
    worked must not be recorded as a merchant this agent has happily paid.
    """
    service = _service_dir()
    amount_atomic = str(requirements.get("amount") or requirements.get("amountAtomic") or "")
    pay_to = str(requirements.get("payTo") or "")
    if not amount_atomic or not pay_to:
        raise PaymentFailed(
            "the 402 did not carry both an amount and a payout address; "
            f"got amount={amount_atomic!r} payTo={pay_to!r}"
        )

    # `--key value`, space-separated. The service's parser puts the whole
    # `--key=value` string into the key and then reports the option missing,
    # which is a confusing five minutes if you assume the usual convention.
    command = [
        "node",
        str(service / "src" / "index.mjs"),
        "buy",
        "--url", resource_url,
        # The three caps travel together. The service treats a partial set as
        # unbounded, and an unbounded buy is exactly what a spending policy
        # exists to prevent.
        "--max-atomic", amount_atomic,
        "--expected-receiver", pay_to,
        "--expected-asset", BASE_USDC,
        "--method", "POST",
        "--body-json", json.dumps({"query": query}),
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=service,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PaymentFailed(
            f"the x402 payer did not finish within {TIMEOUT_SECONDS}s. The "
            "payment may or may not have settled — check the wallet before "
            "running this again."
        ) from exc

    stdout = completed.stdout.decode("utf-8", "replace").strip()
    stderr = completed.stderr.decode("utf-8", "replace").strip()

    if completed.returncode != 0:
        raise PaymentFailed(f"the x402 payer exited {completed.returncode}: {stderr[-500:]}")

    # The service pretty-prints, so the result spans many lines and the last
    # one is just `}`. Find where the JSON object starts and parse from there;
    # anything the CLI logged before it is left out.
    start = stdout.find("{")
    try:
        if start < 0:
            raise ValueError("no JSON object in the payer's output")
        result = json.loads(stdout[start:])
    except ValueError as exc:
        # This one is worth being loud about. The payment may well have
        # settled — the money is gone either way — and only the parse failed,
        # so the operator needs the raw output, not a summary of it.
        raise PaymentFailed(
            "the x402 payer's output could not be parsed, and the payment may "
            f"already have settled. Raw output follows:\n{stdout[-1500:]}"
        ) from exc

    if not result.get("ok"):
        raise PaymentFailed(
            f"the x402 payer refused or failed: {json.dumps(result)[:500]}"
        )

    tx_id = result.get("transactionHash")
    if not tx_id:
        # Worth failing over. Without a transaction hash there is nothing to
        # write next to the journal entry, and a receipt no one can check is
        # not a receipt.
        raise PaymentFailed(
            "the query was answered but no transaction hash came back, so the "
            "payment cannot be evidenced. Refusing to record it."
        )

    return result.get("body"), tx_id


def describe(payment: Any) -> str:
    """One line for the transcript, so the viewer sees what is about to move."""
    amount = payment.amount_usd if isinstance(payment.amount_usd, Decimal) else payment.amount_usd
    return f"paying {amount} USD to {payment.pay_to} on Base mainnet…"
