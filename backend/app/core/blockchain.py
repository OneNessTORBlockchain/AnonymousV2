"""Independent on-chain transaction verification.

The workbench never trusts a model's statement that a transaction succeeded.
:class:`TransactionVerifier` talks to a JSON-RPC endpoint directly, re-reads
the transaction and its receipt, and only reports ``verified=True`` when the
receipt status is successful, the chain id matches, the expected parties,
value and contract events match, and the confirmation count reaches the
configured finality threshold.

Nothing here signs or sends a transaction.  Submission stays with the
operator's tooling (cast, ethers, web3.py, wallet CLI); this module is the
independent second opinion that turns a hash into evidence.

Supported EVM chains ship with public RPC fallbacks and explorer URLs.  They
can be overridden per deployment with ``AV2_RPC_OVERRIDES`` and
``AV2_EXPLORER_OVERRIDES`` (JSON objects).  Solana mainnet is supported through
``getTransaction``/``getSignatureStatuses``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..config import Settings


class VerificationError(RuntimeError):
    """Raised when a verification request cannot be executed at all."""


class RpcError(RuntimeError):
    """Raised when every RPC endpoint for a chain fails."""


@dataclass(frozen=True, slots=True)
class ChainSpec:
    key: str
    name: str
    kind: str
    chain_id: int
    rpc: tuple[str, ...]
    explorer: str
    native_symbol: str
    decimals: int
    finality: int


EVM_CHAINS: dict[str, ChainSpec] = {
    "ethereum": ChainSpec(
        "ethereum", "Ethereum", "evm", 1,
        ("https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"),
        "https://etherscan.io/tx/", "ETH", 18, 12,
    ),
    "sepolia": ChainSpec(
        "sepolia", "Ethereum Sepolia", "evm", 11155111,
        ("https://ethereum-sepolia-rpc.publicnode.com",),
        "https://sepolia.etherscan.io/tx/", "ETH", 18, 12,
    ),
    "bsc": ChainSpec(
        "bsc", "BNB Smart Chain", "evm", 56,
        ("https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"),
        "https://bscscan.com/tx/", "BNB", 18, 15,
    ),
    "polygon": ChainSpec(
        "polygon", "Polygon PoS", "evm", 137,
        ("https://polygon-bor-rpc.publicnode.com",),
        "https://polygonscan.com/tx/", "POL", 18, 128,
    ),
    "base": ChainSpec(
        "base", "Base", "evm", 8453,
        ("https://base-rpc.publicnode.com",),
        "https://basescan.org/tx/", "ETH", 18, 12,
    ),
    "arbitrum": ChainSpec(
        "arbitrum", "Arbitrum One", "evm", 42161,
        ("https://arbitrum-one-rpc.publicnode.com",),
        "https://arbiscan.io/tx/", "ETH", 18, 12,
    ),
    "optimism": ChainSpec(
        "optimism", "OP Mainnet", "evm", 10,
        ("https://optimism-rpc.publicnode.com",),
        "https://optimistic.etherscan.io/tx/", "ETH", 18, 12,
    ),
    "avalanche": ChainSpec(
        "avalanche", "Avalanche C-Chain", "evm", 43114,
        ("https://avalanche-c-chain-rpc.publicnode.com",),
        "https://snowtrace.io/tx/", "AVAX", 18, 12,
    ),
}

SOLANA_CHAINS: dict[str, ChainSpec] = {
    "solana": ChainSpec(
        "solana", "Solana Mainnet", "solana", 0,
        ("https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"),
        "https://solscan.io/tx/", "SOL", 9, 1,
    ),
}

CHAIN_ALIASES = {
    "eth": "ethereum",
    "mainnet": "ethereum",
    "bnb": "bsc",
    "binance": "bsc",
    "matic": "polygon",
    "arb": "arbitrum",
    "arbitrum-one": "arbitrum",
    "op": "optimism",
    "opt": "optimism",
    "avax": "avalanche",
    "sol": "solana",
    "svm": "solana",
}

ALL_CHAINS: dict[str, ChainSpec] = {**EVM_CHAINS, **SOLANA_CHAINS}

EVM_HASH_RE = re.compile(r"\b0x[0-9a-fA-F]{64}\b")
SOLANA_SIG_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{87,88}\b")
_TX_CONTEXT_RE = re.compile(
    r"\b(tx|txn|transaction|hash|digest|signature|sent|submitted|broadcast|confirmed|confirmation|"
    r"success|succeeded|receipt|explorer|mined|included|block)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    expected: str = ""
    actual: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "expected": self.expected, "actual": self.actual}


@dataclass(slots=True)
class VerificationResult:
    tx_hash: str
    chain: str
    verified: bool
    status: str
    summary: str
    chain_id: int | None = None
    block_number: int | None = None
    confirmations: int = 0
    required_confirmations: int = 0
    receipt_status: str = ""
    from_address: str = ""
    to_address: str = ""
    value: str = ""
    value_wei: int | None = None
    fee: str = ""
    explorer_url: str = ""
    checks: list[Check] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    queried_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_hash": self.tx_hash,
            "chain": self.chain,
            "verified": self.verified,
            "status": self.status,
            "summary": self.summary,
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "confirmations": self.confirmations,
            "required_confirmations": self.required_confirmations,
            "receipt_status": self.receipt_status,
            "from": self.from_address,
            "to": self.to_address,
            "value": self.value,
            "value_wei": self.value_wei,
            "fee": self.fee,
            "explorer_url": self.explorer_url,
            "checks": [check.to_dict() for check in self.checks],
            "events": self.events,
            "evidence": self.evidence,
            "error": self.error,
            "queried_at": self.queried_at,
        }

    def for_model(self) -> str:
        """Operator/model-facing report.  Never claims more than was proven."""
        head = (
            f"VERIFIED — {self.chain} transaction confirmed on-chain."
            if self.verified
            else f"NOT VERIFIED — {self.chain} transaction status: {self.status}."
        )
        lines = [
            head,
            f"hash: {self.tx_hash}",
            f"chain_id: {self.chain_id if self.chain_id is not None else 'unknown'}",
            f"receipt: {self.receipt_status or 'unavailable'}",
            f"confirmations: {self.confirmations} (required {self.required_confirmations})",
        ]
        if self.block_number is not None:
            lines.append(f"block: {self.block_number}")
        if self.from_address:
            lines.append(f"from: {self.from_address}")
        if self.to_address:
            lines.append(f"to: {self.to_address}")
        if self.value:
            lines.append(f"value: {self.value}")
        if self.fee:
            lines.append(f"fee: {self.fee}")
        if self.explorer_url:
            lines.append(f"explorer: {self.explorer_url}")
        if self.error:
            lines.append(f"error: {self.error}")
        failed = [check for check in self.checks if not check.ok]
        if failed:
            lines.append("failed checks:")
            lines.extend(
                f"- {check.name}: expected {check.expected or '-'} got {check.actual or '-'}"
                for check in failed
            )
        if self.events:
            lines.append(f"matching events: {len(self.events)}")
        return "\n".join(lines)


def normalise_chain(value: str) -> str:
    key = str(value or "ethereum").strip().lower()
    return CHAIN_ALIASES.get(key, key)


def format_units(amount: int, decimals: int) -> str:
    try:
        scaled = Decimal(amount) / (Decimal(10) ** decimals)
    except (InvalidOperation, ValueError):
        return str(amount)
    text = f"{scaled:.9f}".rstrip("0").rstrip(".")
    return text or "0"


def to_base_units(value: str, decimals: int) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(Decimal(text) * (Decimal(10) ** decimals))
    except (InvalidOperation, ValueError):
        return None


def find_transaction_references(text: str) -> list[dict[str, str]]:
    """Hashes/signatures in prose that are likely transaction claims.

    EVM hashes are unambiguous (0x + 64 hex).  Solana signatures are matched
    only when the surrounding text talks about Solana or a transaction, which
    keeps ordinary base58 strings out of the gate.
    """
    body = text or ""
    found: dict[str, dict[str, str]] = {}
    for match in EVM_HASH_RE.finditer(body):
        start, end = match.span()
        window = body[max(0, start - 220): min(len(body), end + 220)]
        if not _TX_CONTEXT_RE.search(window):
            continue
        hint = _chain_hint(window)
        found.setdefault(match.group(0).lower(), {"hash": match.group(0), "chain": hint or "ethereum", "kind": "evm"})
    for match in SOLANA_SIG_RE.finditer(body):
        start, end = match.span()
        window = body[max(0, start - 220): min(len(body), end + 220)]
        if not re.search(r"\b(solana|solscan|phantom|signature|tx|transaction)\b", window, re.IGNORECASE):
            continue
        found.setdefault(match.group(0), {"hash": match.group(0), "chain": "solana", "kind": "solana"})
    return list(found.values())


def _chain_hint(window: str) -> str:
    lowered = window.lower()
    for key in sorted(ALL_CHAINS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            return key
    for alias, key in CHAIN_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return key
    return ""



class TransactionVerifier:
    """Read-only JSON-RPC verifier.  Never signs or submits anything."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- plumbing --------------------------------------------------------
    def chain_spec(self, chain: str) -> ChainSpec | None:
        return ALL_CHAINS.get(normalise_chain(chain))

    def endpoints(self, chain: str, rpc_url: str = "") -> tuple[ChainSpec | None, list[str]]:
        key = normalise_chain(chain)
        spec = ALL_CHAINS.get(key)
        if rpc_url.strip():
            return spec, [rpc_url.strip()]
        override = str(self.settings.rpc_overrides.get(key, "")).strip()
        if override:
            return spec, [override]
        return spec, list(spec.rpc) if spec else []

    def explorer_url(self, chain: str, tx_hash: str, base: str = "") -> str:
        key = normalise_chain(chain)
        root = base.strip() or str(self.settings.explorer_overrides.get(key, "")).strip()
        if not root:
            spec = ALL_CHAINS.get(key)
            root = spec.explorer if spec else ""
        if not root:
            return ""
        return root.rstrip("/") + "/" + tx_hash

    async def _resolve_endpoint(
        self, client: httpx.AsyncClient, endpoints: list[str], probe: str, params: list[Any]
    ) -> tuple[str, Any]:
        errors: list[str] = []
        for url in endpoints:
            try:
                result = await self._call(client, url, probe, params)
                return url, result
            except (httpx.HTTPError, RpcError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
        raise RpcError("; ".join(errors) or "no RPC endpoint configured")

    async def _call(
        self, client: httpx.AsyncClient, url: str, method: str, params: list[Any]
    ) -> Any:
        response = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RpcError(f"{method}: non-object response")
        if payload.get("error"):
            raise RpcError(f"{method}: {payload['error']}")
        return payload.get("result")

    # -- public entry point ---------------------------------------------
    async def verify(
        self,
        *,
        tx_hash: str,
        chain: str = "ethereum",
        rpc_url: str = "",
        explorer_base: str = "",
        expected_from: str = "",
        expected_to: str = "",
        expected_value: str = "",
        expected_chain_id: int | None = None,
        min_confirmations: int | None = None,
        wait_seconds: int = 0,
        contract_address: str = "",
        event_topic: str = "",
    ) -> VerificationResult:
        tx_hash = str(tx_hash or "").strip()
        if not tx_hash:
            raise VerificationError("tx_hash is required")
        is_solana = bool(SOLANA_SIG_RE.fullmatch(tx_hash)) and not tx_hash.startswith("0x")
        key = "solana" if is_solana else normalise_chain(chain)
        if key not in ALL_CHAINS:
            raise VerificationError(
                "unsupported chain '" + str(chain) + "'; known chains: " + ", ".join(sorted(ALL_CHAINS))
            )
        timeout = httpx.Timeout(self.settings.tx_rpc_timeout_s, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                if key == "solana":
                    return await self._verify_solana(
                        client,
                        tx_hash=tx_hash,
                        rpc_url=rpc_url,
                        explorer_base=explorer_base,
                        expected_from=expected_from,
                        expected_to=expected_to,
                        expected_value=expected_value,
                        min_confirmations=min_confirmations,
                        wait_seconds=wait_seconds,
                    )
                return await self._verify_evm(
                    client,
                    tx_hash=tx_hash,
                    chain=key,
                    rpc_url=rpc_url,
                    explorer_base=explorer_base,
                    expected_from=expected_from,
                    expected_to=expected_to,
                    expected_value=expected_value,
                    expected_chain_id=expected_chain_id,
                    min_confirmations=min_confirmations,
                    wait_seconds=wait_seconds,
                    contract_address=contract_address,
                    event_topic=event_topic,
                )
            except RpcError as exc:
                spec = ALL_CHAINS.get(key)
                return VerificationResult(
                    tx_hash=tx_hash,
                    chain=spec.name if spec else key,
                    verified=False,
                    status="rpc_error",
                    summary="Could not reach an RPC endpoint; transaction is unverified.",
                    required_confirmations=min_confirmations or (spec.finality if spec else 0),
                    explorer_url=self.explorer_url(key, tx_hash, explorer_base),
                    error=str(exc),
                )


    # -- EVM -------------------------------------------------------------
    async def _verify_evm(
        self,
        client: httpx.AsyncClient,
        *,
        tx_hash: str,
        chain: str,
        rpc_url: str,
        explorer_base: str,
        expected_from: str,
        expected_to: str,
        expected_value: str,
        expected_chain_id: int | None,
        min_confirmations: int | None,
        wait_seconds: int,
        contract_address: str,
        event_topic: str,
    ) -> VerificationResult:
        import asyncio
        import time

        spec = EVM_CHAINS[chain]
        _spec, endpoints = self.endpoints(chain, rpc_url)
        url, chain_id_hex = await self._resolve_endpoint(client, endpoints, "eth_chainId", [])
        actual_chain_id = int(str(chain_id_hex), 16)
        required_id = int(expected_chain_id) if expected_chain_id else spec.chain_id
        required_conf = max(1, int(min_confirmations or spec.finality))

        tx = await self._call(client, url, "eth_getTransactionByHash", [tx_hash])
        receipt = await self._call(client, url, "eth_getTransactionReceipt", [tx_hash])
        deadline = time.monotonic() + max(0, int(wait_seconds or 0))
        while receipt is None and time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            receipt = await self._call(client, url, "eth_getTransactionReceipt", [tx_hash])
        latest_hex = await self._call(client, url, "eth_blockNumber", [])

        result = VerificationResult(
            tx_hash=tx_hash,
            chain=spec.name,
            verified=False,
            status="not_found",
            summary="Transaction hash not found on the queried node.",
            chain_id=actual_chain_id,
            required_confirmations=required_conf,
            explorer_url=self.explorer_url(chain, tx_hash, explorer_base),
            evidence={"rpc": url, "latest_block": int(str(latest_hex or "0x0"), 16)},
        )
        if tx is None:
            result.error = "eth_getTransactionByHash returned null"
            result.summary = (
                "The node does not know this hash. It may be pending, sent to another chain, "
                "or fabricated. It is not verified."
            )
            return result

        result.from_address = str(tx.get("from") or "")
        result.to_address = str(tx.get("to") or "")
        result.value_wei = int(str(tx.get("value") or "0x0"), 16)
        result.value = f"{format_units(result.value_wei, spec.decimals)} {spec.native_symbol}"
        result.evidence.update(
            {
                "block_number": tx.get("blockNumber"),
                "nonce": tx.get("nonce"),
                "input_selector": str(tx.get("input") or "")[:10],
            }
        )

        checks: list[Check] = [
            Check("chain_id", actual_chain_id == required_id, str(required_id), str(actual_chain_id)),
        ]
        if expected_from.strip():
            checks.append(
                Check(
                    "sender",
                    result.from_address.lower() == expected_from.strip().lower(),
                    expected_from.strip(),
                    result.from_address,
                )
            )
        if expected_to.strip():
            checks.append(
                Check(
                    "recipient",
                    result.to_address.lower() == expected_to.strip().lower(),
                    expected_to.strip(),
                    result.to_address,
                )
            )
        expected_wei = to_base_units(expected_value, spec.decimals)
        if expected_wei is not None:
            checks.append(
                Check("value", result.value_wei == expected_wei, f"{expected_value} {spec.native_symbol}", result.value)
            )


        if receipt is None:
            result.checks = checks
            result.status = "pending" if actual_chain_id == required_id else "chain_mismatch"
            result.summary = (
                "Transaction is known to the node but has no receipt yet: it is pending, not confirmed."
            )
            return result

        result.block_number = int(str(receipt.get("blockNumber") or "0x0"), 16)
        result.confirmations = max(0, int(str(latest_hex or "0x0"), 16) - result.block_number + 1)
        status_hex = str(receipt.get("status") or "")
        result.receipt_status = (
            "success" if status_hex == "0x1" else "reverted" if status_hex == "0x0" else "unknown"
        )
        checks.append(Check("receipt_status", result.receipt_status == "success", "success", result.receipt_status))

        try:
            gas_used = int(str(receipt.get("gasUsed") or "0x0"), 16)
            gas_price = int(str(receipt.get("effectiveGasPrice") or tx.get("gasPrice") or "0x0"), 16)
            result.fee = f"{format_units(gas_used * gas_price, spec.decimals)} {spec.native_symbol}"
        except ValueError:
            result.fee = ""

        logs = receipt.get("logs") or []
        selected = logs
        if contract_address.strip():
            selected = [
                log
                for log in logs
                if str(log.get("address") or "").lower() == contract_address.strip().lower()
            ]
            checks.append(
                Check(
                    "contract_address",
                    bool(selected),
                    contract_address.strip(),
                    f"{len(selected)} matching log(s)",
                )
            )
        if event_topic.strip():
            selected = [
                log
                for log in selected
                if event_topic.strip().lower()
                in [str(topic).lower() for topic in (log.get("topics") or [])]
            ]
            checks.append(
                Check("event_topic", bool(selected), event_topic.strip(), f"{len(selected)} matching log(s)")
            )
        result.events = [
            {
                "address": log.get("address"),
                "topics": log.get("topics"),
                "data": str(log.get("data") or "")[:200],
                "log_index": log.get("logIndex"),
            }
            for log in selected[:20]
        ]
        checks.append(
            Check(
                "confirmations",
                result.confirmations >= required_conf,
                f">={required_conf}",
                str(result.confirmations),
            )
        )
        result.checks = checks
        result.evidence["raw_status"] = status_hex or "missing"
        result.evidence["log_count"] = len(logs)

        if actual_chain_id != required_id:
            result.status = "chain_mismatch"
            result.summary = (
                f"RPC reports chain id {actual_chain_id}, expected {required_id}. "
                "The hash is real but it is not the chain that was asked for."
            )
        elif result.receipt_status == "reverted":
            result.status = "reverted"
            result.summary = "Receipt status is 0x0: the transaction reverted. It did not succeed."
        elif result.receipt_status != "success":
            result.status = "unknown_receipt"
            result.summary = "Receipt has no usable status field; success cannot be proven."
        elif any(not check.ok for check in checks if check.name not in {"confirmations"}):
            result.status = "mismatch"
            result.summary = "On-chain data does not match the expected sender, recipient, value or events."
        elif result.confirmations < required_conf:
            result.status = "insufficient_confirmations"
            result.summary = (
                f"Execution succeeded but only {result.confirmations} confirmation(s) are visible; "
                f"{required_conf} required. Treat it as confirmed-but-not-final."
            )
        else:
            result.verified = True
            result.status = "verified"
            result.summary = (
                f"Execution succeeded with {result.confirmations} confirmations and every expected field matched."
            )
        return result


    # -- Solana ----------------------------------------------------------
    async def _verify_solana(
        self,
        client: httpx.AsyncClient,
        *,
        tx_hash: str,
        rpc_url: str,
        explorer_base: str,
        expected_from: str,
        expected_to: str,
        expected_value: str,
        min_confirmations: int | None,
        wait_seconds: int,
    ) -> VerificationResult:
        import asyncio
        import time

        spec = SOLANA_CHAINS["solana"]
        _spec, endpoints = self.endpoints("solana", rpc_url)
        url, _health = await self._resolve_endpoint(client, endpoints, "getHealth", [])
        required_conf = max(1, int(min_confirmations or spec.finality))
        params = {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1,
            "commitment": "confirmed",
        }
        tx = await self._call(client, url, "getTransaction", [tx_hash, params])
        deadline = time.monotonic() + max(0, int(wait_seconds or 0))
        while tx is None and time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            tx = await self._call(client, url, "getTransaction", [tx_hash, params])

        status: dict[str, Any] = {}
        try:
            statuses = await self._call(
                client,
                url,
                "getSignatureStatuses",
                [[tx_hash], {"searchTransactionHistory": True}],
            )
            values = (statuses or {}).get("value") if isinstance(statuses, dict) else None
            if isinstance(values, list) and values and isinstance(values[0], dict):
                status = values[0]
        except RpcError:
            status = {}

        finalized_slot = 0
        try:
            finalized_slot = int(await self._call(client, url, "getSlot", [{"commitment": "finalized"}]) or 0)
        except (RpcError, ValueError, TypeError):
            finalized_slot = 0

        result = VerificationResult(
            tx_hash=tx_hash,
            chain=spec.name,
            verified=False,
            status="not_found",
            summary="Signature not found on the queried Solana RPC.",
            required_confirmations=required_conf,
            explorer_url=self.explorer_url("solana", tx_hash, explorer_base),
            evidence={"rpc": url, "finalized_slot": finalized_slot},
        )
        if tx is None:
            confirmation = str(status.get("confirmationStatus") or "")
            result.status = "pending" if confirmation in {"processed", "confirmed"} else "not_found"
            result.error = "getTransaction returned null"
            result.summary = (
                "The signature is known but not yet finalized."
                if result.status == "pending"
                else "The node does not know this signature. It is not verified."
            )
            if status:
                result.confirmations = int(status.get("confirmations") or 0)
            return result

        meta = tx.get("meta") or {}
        error = meta.get("err")
        slot = int(tx.get("slot") or 0)
        result.block_number = slot
        result.confirmations = (
            max(0, finalized_slot - slot + 1) if finalized_slot and slot else 0
        )
        result.receipt_status = "failed" if error else "success"
        result.evidence.update(
            {
                "slot": slot,
                "fee_lamports": meta.get("fee"),
                "err": error,
                "confirmation_status": status.get("confirmationStatus") or "",
            }
        )
        result.chain_id = None


        message = (tx.get("transaction") or {}).get("message") or {}
        keys: list[str] = []
        for item in message.get("accountKeys") or []:
            keys.append(str(item.get("pubkey") if isinstance(item, dict) else item))
        transfers: list[dict[str, Any]] = []

        def collect(instructions: Any) -> None:
            for instruction in instructions or []:
                if not isinstance(instruction, dict):
                    continue
                parsed = instruction.get("parsed")
                if not isinstance(parsed, dict):
                    continue
                info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
                if str(instruction.get("program")) == "system" and parsed.get("type") == "transfer":
                    try:
                        lamports = int(info.get("lamports") or 0)
                    except (TypeError, ValueError):
                        lamports = 0
                    transfers.append(
                        {
                            "source": info.get("source"),
                            "destination": info.get("destination"),
                            "lamports": lamports,
                        }
                    )

        collect(message.get("instructions"))
        for inner in meta.get("innerInstructions") or []:
            if isinstance(inner, dict):
                collect(inner.get("instructions"))

        result.from_address = keys[0] if keys else ""
        result.to_address = transfers[0]["destination"] if transfers else ""
        if transfers:
            result.value_wei = int(transfers[0]["lamports"])
            result.value = f"{format_units(result.value_wei, spec.decimals)} {spec.native_symbol}"
        if meta.get("fee") is not None:
            result.fee = f"{format_units(int(meta.get('fee') or 0), spec.decimals)} {spec.native_symbol}"
        result.events = transfers[:20]

        checks: list[Check] = [
            Check("execution", error is None, "no error", str(error) if error else "none"),
        ]
        if expected_from.strip():
            checks.append(
                Check(
                    "sender",
                    expected_from.strip() in keys,
                    expected_from.strip(),
                    "present as account key" if expected_from.strip() in keys else "absent",
                )
            )
        if expected_to.strip():
            recipients = [transfer for transfer in transfers if str(transfer.get("destination")) == expected_to.strip()]
            checks.append(
                Check(
                    "recipient",
                    bool(recipients),
                    expected_to.strip(),
                    f"{len(recipients)} matching transfer(s)",
                )
            )
        expected_lamports = to_base_units(expected_value, spec.decimals)
        if expected_lamports is not None:
            matches = [transfer for transfer in transfers if int(transfer.get("lamports") or 0) == expected_lamports]
            if expected_to.strip():
                matches = [
                    transfer
                    for transfer in matches
                    if str(transfer.get("destination")) == expected_to.strip()
                ]
            checks.append(
                Check(
                    "value",
                    bool(matches),
                    f"{expected_value} {spec.native_symbol}",
                    f"{len(matches)} matching transfer(s)",
                )
            )
        confirmation = str(status.get("confirmationStatus") or "")
        finalized = confirmation == "finalized"
        checks.append(
            Check(
                "finality",
                finalized and result.confirmations >= required_conf,
                f"finalized with >={required_conf} confirmation(s)",
                f"{confirmation or 'unknown'}, {result.confirmations} confirmation(s)",
            )
        )
        result.checks = checks

        if error:
            result.status = "failed"
            result.summary = "The Solana transaction executed but its meta.err is set: it failed."
        elif any(not check.ok for check in checks if check.name not in {"finality"}):
            result.status = "mismatch"
            result.summary = "On-chain data does not match the expected sender, recipient or value."
        elif not finalized or result.confirmations < required_conf:
            result.status = "insufficient_confirmations"
            result.summary = (
                "Execution succeeded but the signature is not finalized with the required confirmations."
            )
        else:
            result.verified = True
            result.status = "verified"
            result.summary = (
                f"Solana transaction finalized at slot {slot} with {result.confirmations} confirmation(s)."
            )
        return result


__all__ = [
    "ALL_CHAINS",
    "CHAIN_ALIASES",
    "EVM_CHAINS",
    "SOLANA_CHAINS",
    "ChainSpec",
    "Check",
    "RpcError",
    "TransactionVerifier",
    "VerificationError",
    "VerificationResult",
    "find_transaction_references",
    "format_units",
    "normalise_chain",
    "to_base_units",
]

