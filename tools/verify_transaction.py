"""CLI for the independent transaction verifier.

Examples:
    python tools/verify_transaction.py 0x<hash> --chain ethereum --expected-to 0x...
    python tools/verify_transaction.py <solana-signature> --chain solana
    python tools/verify_transaction.py --self-test

Exit code 0 means verified=true; 2 means the transaction was checked but not
verified (pending, reverted, mismatch, unconfirmed, not found or RPC error).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.core.blockchain import TransactionVerifier  # noqa: E402

RPC = "https://ethereum-rpc.publicnode.com"


def _rpc(method: str, params: list[Any]) -> Any:
    response = httpx.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=40,
    )
    body = response.json()
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


async def _self_test(verifier: TransactionVerifier) -> int:
    """Verify one real native transfer and reject one fabricated hash."""
    latest = int(_rpc("eth_blockNumber", []), 16)
    block = _rpc("eth_getBlockByNumber", [hex(latest - 20), True])
    candidates = [
        tx for tx in block["transactions"] if tx.get("to") and int(str(tx.get("value") or "0x0"), 16) > 0
    ]
    if not candidates:
        print("self-test: no value transfer in sampled block")
        return 2
    tx = candidates[0]
    value = int(str(tx["value"]), 16) / 10**18
    real = await verifier.verify(
        tx_hash=tx["hash"],
        chain="ethereum",
        expected_from=tx["from"],
        expected_to=tx["to"],
        expected_value=str(value),
        min_confirmations=12,
    )
    fake = await verifier.verify(tx_hash="0x" + "00" * 32, chain="ethereum")
    print("real  :", real.status, "| verified", real.verified, "| confirmations", real.confirmations)
    print("       ", real.explorer_url)
    print("fake  :", fake.status, "| verified", fake.verified)
    return 0 if real.verified and not fake.verified else 2


async def main() -> int:
    parser = argparse.ArgumentParser(description="Independently verify a blockchain transaction.")
    parser.add_argument("tx_hash", nargs="?", default="")
    parser.add_argument("--chain", default="ethereum")
    parser.add_argument("--rpc-url", default="")
    parser.add_argument("--explorer-base", default="")
    parser.add_argument("--expected-from", default="")
    parser.add_argument("--expected-to", default="")
    parser.add_argument("--expected-value", default="")
    parser.add_argument("--expected-chain-id", type=int, default=None)
    parser.add_argument("--min-confirmations", type=int, default=0)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--contract-address", default="")
    parser.add_argument("--event-topic", default="")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    verifier = TransactionVerifier(get_settings())
    if args.self_test:
        return await _self_test(verifier)
    if not args.tx_hash:
        parser.error("tx_hash is required unless --self-test is used")

    result = await verifier.verify(
        tx_hash=args.tx_hash,
        chain=args.chain,
        rpc_url=args.rpc_url,
        explorer_base=args.explorer_base,
        expected_from=args.expected_from,
        expected_to=args.expected_to,
        expected_value=args.expected_value,
        expected_chain_id=args.expected_chain_id,
        min_confirmations=args.min_confirmations,
        wait_seconds=args.wait_seconds,
        contract_address=args.contract_address,
        event_topic=args.event_topic,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(result.for_model())
        print(f"\nverified = {result.verified} | status = {result.status}")
    return 0 if result.verified else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
