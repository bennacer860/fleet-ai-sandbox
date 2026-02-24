#!/usr/bin/env python3
"""
claim_winnings.py
─────────────────
Polls the Polymarket Data API every 5 minutes for redeemable winning positions
and automatically calls `redeemPositions` on the Conditional Token Framework (CTF)
contract to convert them back to USDC.

Run in a dedicated terminal:
    python3 claim_winnings.py

The script reads credentials from your existing .env file (PRIVATE_KEY, FUNDER).

Notes
─────
• Redemption is done directly via the CTF smart contract on Polygon.
• `redeemPositions` burns **all** of your balance for that conditionId — no amount arg.
• `indexSets` for a binary market are always [1, 2]  (both outcome slots).
• Already-redeemed conditionIds are tracked in memory to avoid duplicate txns.
"""

import os
import sys
import time
import json
import logging
import argparse
import requests
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
import eth_abi  # part of eth-abi, already installed

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
FUNDER: str = os.getenv("FUNDER", "")

# Ordered list of Polygon RPCs — tried in sequence, first success wins.
# Must support eth_sendRawTransaction for live redemptions.
# You can add your own (Alchemy/QuickNode/Infura) via POLYGON_RPC in .env.
POLYGON_RPCS: list[str] = [r for r in [
    os.getenv("POLYGON_RPC", ""),                                     # user .env override (highest priority)
    "https://polygon.drpc.org",                                       # ✅ free, no key, sendRawTx OK
    "https://polygon-mainnet.infura.io/v3/9aa3d95b3bc440fa88ea12eaa4456161",  # ✅ Infura demo key
    "https://1rpc.io/matic",                                          # read-only fallback
] if r]

# Contract addresses (from py_clob_client / Polymarket docs)
CTF_CONTRACT  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

DATA_API = "https://data-api.polymarket.com"

POLL_INTERVAL_SECONDS = 5 * 60  # 5 minutes
CHAIN_ID = 137  # Polygon mainnet

# redeemPositions(address,bytes32,bytes32,uint256[])  → 4-byte selector (keccak)
REDEEM_SELECTOR = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]

# ANSI colors
C_GREEN  = "\033[92m"
C_YELLOW = "\033[93m"
C_RED    = "\033[91m"
C_BLUE   = "\033[94m"
C_RESET  = "\033[0m"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("claim_winnings")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _rpc(method: str, params: list) -> dict:
    """Make a Polygon JSON-RPC call, trying each endpoint in order."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = None
    for endpoint in POLYGON_RPCS:
        try:
            resp = requests.post(endpoint, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"RPC error from {endpoint}: {data['error']}")
            return data["result"]
        except Exception as e:
            logger.debug("RPC endpoint %s failed: %s — trying next...", endpoint, e)
            last_err = e
    raise RuntimeError(f"All Polygon RPC endpoints failed. Last error: {last_err}")


def get_nonce(address: str) -> int:
    result = _rpc("eth_getTransactionCount", [address, "latest"])
    return int(result, 16)


def get_gas_price() -> int:
    """Returns gas price in wei, with a 20% bump for reliability."""
    result = _rpc("eth_gasPrice", [])
    base = int(result, 16)
    return int(base * 1.20)


def estimate_gas(tx: dict) -> int:
    try:
        result = _rpc("eth_estimateGas", [tx])
        return int(int(result, 16) * 1.30)  # 30% buffer
    except Exception:
        return 300_000  # safe default for a redeem call


def encode_redeem_calldata(condition_id: str) -> bytes:
    """
    Encode calldata for:
        redeemPositions(address collateral, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)

    parentCollectionId is always bytes32(0) on Polymarket.
    indexSets = [1, 2] redeems both outcome slots (wins everything).
    """
    cid_bytes    = bytes.fromhex(condition_id.removeprefix("0x"))
    parent_bytes = b"\x00" * 32

    # ABI-encode the 4 arguments
    encoded = eth_abi.encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_CONTRACT, parent_bytes, cid_bytes, [1, 2]],
    )
    return REDEEM_SELECTOR + encoded


def send_raw_tx(account: LocalAccount, to: str, data: bytes, dry_run: bool = False) -> Optional[str]:
    """Sign and broadcast a raw transaction. Returns tx hash or None."""
    nonce = get_nonce(account.address)
    gas_price = get_gas_price()

    tx = {
        "to": to,
        "data": "0x" + data.hex(),
        "nonce": nonce,
        "chainId": CHAIN_ID,
        "gasPrice": gas_price,
    }
    gas = estimate_gas(tx)
    tx["gas"] = gas

    if dry_run:
        logger.info(
            "%s[DRY-RUN] Would send tx: to=%s nonce=%d gas=%d gasPrice=%.2f gwei data=%s...%s",
            C_YELLOW, to, nonce, gas, gas_price / 1e9, data.hex()[:20], C_RESET
        )
        return "DRY_RUN"

    signed = account.sign_transaction(tx)
    raw_hex = signed.raw_transaction.hex()
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + raw_hex])
    return tx_hash


# ── Core logic ────────────────────────────────────────────────────────────────

def fetch_redeemable_positions(user: str) -> list[dict]:
    """Query the Polymarket Data API for redeemable positions."""
    url = f"{DATA_API}/positions"
    params = {"user": user, "redeemable": "true"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        positions = resp.json()
        return positions if isinstance(positions, list) else []
    except Exception as e:
        logger.error("Failed to fetch redeemable positions: %s", e)
        return []


def claim_position(
    account: LocalAccount,
    position: dict,
    already_claimed: set,
    dry_run: bool = False,
) -> bool:
    """
    Attempt to redeem a single redeemable position.
    Returns True if a tx was sent (or dry-run triggered).
    """
    condition_id: str = position.get("conditionId", "")
    market_slug: str  = position.get("market", {}).get("slug", "") if isinstance(position.get("market"), dict) else ""
    title: str        = position.get("title", market_slug or condition_id[:16])
    size: float       = float(position.get("size", 0))
    outcome: str      = position.get("outcome", "?")
    pnl: float        = float(position.get("currentValue", size))

    if not condition_id:
        logger.warning("Position missing conditionId, skipping: %s", position)
        return False

    if condition_id in already_claimed:
        logger.debug("Already claimed conditionId=%s, skipping", condition_id[:10])
        return False

    logger.info(
        "%s[CLAIM] Redeeming: %s | outcome=%s | size=%.2f | value≈$%.2f%s",
        C_GREEN, title, outcome, size, pnl, C_RESET
    )

    try:
        calldata = encode_redeem_calldata(condition_id)
        tx_hash = send_raw_tx(account, CTF_CONTRACT, calldata, dry_run=dry_run)
        if tx_hash:
            if dry_run:
                logger.info("%s[DRY-RUN] Not submitted (dry run mode)%s", C_YELLOW, C_RESET)
            else:
                logger.info(
                    "%s[CLAIM] TX submitted: %s | hash=%s%s",
                    C_GREEN, title, tx_hash, C_RESET
                )
                logger.info("  View on Polygonscan: https://polygonscan.com/tx/%s", tx_hash)
            already_claimed.add(condition_id)
            return True
    except Exception as e:
        logger.error("%s[CLAIM] Failed to redeem %s: %s%s", C_RED, title[:40], e, C_RESET)

    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, interval: int = POLL_INTERVAL_SECONDS):
    if not PRIVATE_KEY:
        logger.error("PRIVATE_KEY is not set in .env — cannot sign transactions.")
        sys.exit(1)
    if not FUNDER:
        logger.error("FUNDER is not set in .env — cannot determine wallet address.")
        sys.exit(1)

    account: LocalAccount = Account.from_key(PRIVATE_KEY)
    wallet = FUNDER  # Use the funder/proxy address for position lookup

    logger.info("=" * 60)
    logger.info("%s  Polymarket Auto-Claimer  %s", C_BLUE, C_RESET)
    logger.info("  Wallet (proxy):  %s", wallet)
    logger.info("  Signer (EOA):    %s", account.address)
    logger.info("  Poll interval:   %ds (%dm)", interval, interval // 60)
    logger.info("  Dry-run:         %s", dry_run)
    logger.info("=" * 60)

    already_claimed: set[str] = set()

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("%s[POLL] Checking for redeemable positions at %s...%s", C_BLUE, now, C_RESET)

        positions = fetch_redeemable_positions(wallet)

        if not positions:
            logger.info("  No redeemable positions found.")
        else:
            logger.info("  Found %d redeemable position(s).", len(positions))
            claimed_count = 0
            for pos in positions:
                if claim_position(account, pos, already_claimed, dry_run=dry_run):
                    claimed_count += 1
                    time.sleep(2)  # Brief pause between txns to avoid nonce conflicts
            if claimed_count:
                logger.info("%s  Claimed %d position(s) this round.%s", C_GREEN, claimed_count, C_RESET)

        logger.info("  Next check in %d minutes...\n", interval // 60)
        time.sleep(interval)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Auto-claim Polymarket winning positions every N minutes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate redemptions without submitting transactions.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (useful for testing).",
    )
    args = parser.parse_args()

    if args.once:
        # Single-shot mode
        account = Account.from_key(PRIVATE_KEY)
        already_claimed: set = set()
        positions = fetch_redeemable_positions(FUNDER)
        if not positions:
            logger.info("No redeemable positions found.")
        else:
            logger.info("Found %d redeemable position(s):", len(positions))
            for p in positions:
                logger.info(
                    "  • %s | outcome=%s | size=%s",
                    p.get("title", p.get("conditionId", "?")[:16]),
                    p.get("outcome", "?"),
                    p.get("size", "?"),
                )
                claim_position(account, p, already_claimed, dry_run=args.dry_run)
    else:
        run(dry_run=args.dry_run, interval=args.interval)
