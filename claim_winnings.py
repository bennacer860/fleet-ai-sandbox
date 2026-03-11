#!/usr/bin/env python3
"""
claim_winnings.py
─────────────────
Polls the Polymarket Data API for redeemable winning positions
and automatically calls `redeemPositions` on the Conditional Token Framework (CTF)
contract to convert them back to USDC.

Now supports GASLESS redemptions via the Polymarket Builder Relayer.
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
import eth_abi

# Polymarket Builder SDKs
try:
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import SafeTransaction, OperationType
    HAS_RELAYER = True
except ImportError:
    HAS_RELAYER = False

# ── Config ────────────────────────────────────────────────────────────────────

# Intercept --profile early to set env overrides before loading config
for i, arg in enumerate(sys.argv):
    if arg == "--profile" and i + 1 < len(sys.argv):
        os.environ["ACTIVE_PROFILE"] = sys.argv[i + 1]

load_dotenv()

# Profile-aware config loading
_profile = os.environ.get("ACTIVE_PROFILE", "")
if _profile:
    _prefix = f"P{_profile}_"
    print(f"[PROFILE] Loading config overrides from prefix {_prefix}")
    PRIVATE_KEY = os.getenv(f"{_prefix}PRIVATE_KEY", os.getenv("PRIVATE_KEY", ""))
    FUNDER = os.getenv(f"{_prefix}FUNDER", os.getenv("FUNDER", ""))
    POLY_API_KEY = os.getenv(f"{_prefix}POLY_API_KEY", os.getenv("POLY_API_KEY", ""))
    POLY_SECRET = os.getenv(f"{_prefix}POLY_SECRET", os.getenv("POLY_SECRET", ""))
    POLY_PASSPHRASE = os.getenv(f"{_prefix}POLY_PARAPHRASE") or os.getenv(f"{_prefix}POLY_PASSPHRASE") or os.getenv("POLY_PASSPHRASE") or os.getenv("POLY_PARAPHRASE", "")
else:
    PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
    FUNDER = os.getenv("FUNDER", "")
    POLY_API_KEY = os.getenv("POLY_API_KEY", "")
    POLY_SECRET = os.getenv("POLY_SECRET", "")
    POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE") or os.getenv("POLY_PARAPHRASE", "")

# Ordered list of Polygon RPCs
POLYGON_RPCS: list[str] = [r for r in [
    os.getenv("POLYGON_RPC", ""),
    "https://polygon.drpc.org",
    "https://polygon-mainnet.infura.io/v3/9aa3d95b3bc440fa88ea12eaa4456161",
    "https://1rpc.io/matic",
] if r]

# Contract addresses
CTF_CONTRACT  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

DATA_API = "https://data-api.polymarket.com"
RELAYER_HOST = "https://relayer-v2.polymarket.com"

POLL_INTERVAL_SECONDS = 5 * 60  # 5 minutes
MIN_CLAIM_VALUE_USD = 20.0       # Only claim if win is >= $20
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
    """Make a Polygon JSON-RPC call."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = None
    for endpoint in POLYGON_RPCS:
        try:
            resp = requests.post(endpoint, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                # Some RPCs return error as a string, some as a dict
                err_msg = data['error'].get('message', data['error']) if isinstance(data['error'], dict) else data['error']
                raise RuntimeError(f"RPC error from {endpoint}: {err_msg}")
            return data["result"]
        except Exception as e:
            logger.debug("RPC endpoint %s failed: %s", endpoint, e)
            last_err = e
    raise RuntimeError(f"All RPCs failed. Last error: {last_err}")


def get_balance(address: str) -> int:
    return int(_rpc("eth_getBalance", [address, "latest"]), 16)


def get_gas_price() -> int:
    try:
        base = int(_rpc("eth_gasPrice", []), 16)
        return int(base * 1.20)
    except Exception:
        return 50_000_000_000  # 50 gwei


def encode_redeem_calldata(condition_id: str) -> bytes:
    cid_bytes    = bytes.fromhex(condition_id.removeprefix("0x"))
    parent_bytes = b"\x00" * 32
    encoded = eth_abi.encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_CONTRACT, parent_bytes, cid_bytes, [1, 2]],
    )
    return REDEEM_SELECTOR + encoded


def claim_position_gasless(relayer: 'RelayClient', condition_id: str, title: str) -> bool:
    """Redeem a position for free using the sponsored relayer."""
    try:
        calldata = "0x" + encode_redeem_calldata(condition_id).hex()
        
        # Build the transaction for the Safe (FUNDER)
        tx = SafeTransaction(
            to=CTF_CONTRACT,
            data=calldata,
            value="0",
            operation=OperationType.Call
        )
        
        logger.info(f"  [GASLESS] Submitting redemption for {title}...")
        resp = relayer.execute([tx])
        
        if resp and resp.transaction_id:
            logger.info(f"{C_GREEN}  [GASLESS] Success! Transaction ID: {resp.transaction_id}{C_RESET}")
            if resp.transaction_hash:
                logger.info(f"  View on Polygonscan: https://polygonscan.com/tx/{resp.transaction_hash}")
            return True
        return False
    except Exception as e:
        logger.error(f"{C_RED}  [GASLESS] Relayer failed: {e}{C_RESET}")
        return False


def claim_position_standard(account: LocalAccount, condition_id: str, title: str) -> bool:
    """Redeem a position by paying gas from the EOA (old fallback)."""
    try:
        calldata = encode_redeem_calldata(condition_id)
        nonce = int(_rpc("eth_getTransactionCount", [account.address, "latest"]), 16)
        gas_price = get_gas_price()
        
        tx = {
            "to": CTF_CONTRACT,
            "data": "0x" + calldata.hex(),
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "gasPrice": gas_price,
            "gas": 300_000
        }
        
        # Final balance check
        bal = get_balance(account.address)
        if bal < (tx['gas'] * tx['gasPrice']):
            logger.error(f"{C_RED}  [GAS-FAIL] Insufficient POL. Need ~{300000 * gas_price / 1e18:.4f} POL.{C_RESET}")
            return False

        signed = account.sign_transaction(tx)
        tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        logger.info(f"{C_GREEN}  [GAS-PAID] Transaction sent: {tx_hash}{C_RESET}")
        return True
    except Exception as e:
        logger.error(f"{C_RED}  [GAS-PAID] Failed: {e}{C_RESET}")
        return False

# ── Core logic ────────────────────────────────────────────────────────────────

def fetch_redeemable_positions(user: str) -> list[dict]:
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


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, interval: int = POLL_INTERVAL_SECONDS):
    if not PRIVATE_KEY or not FUNDER:
        logger.error("PRIVATE_KEY and FUNDER must be set in .env")
        sys.exit(1)

    account: LocalAccount = Account.from_key(PRIVATE_KEY)
    
    # Initialize Relayer if keys are available
    relayer = None
    if HAS_RELAYER and POLY_API_KEY and POLY_SECRET and POLY_PASSPHRASE:
        try:
            creds = BuilderApiKeyCreds(
                key=POLY_API_KEY,
                secret=POLY_SECRET,
                passphrase=POLY_PASSPHRASE
            )
            cfg = BuilderConfig(local_builder_creds=creds)
            relayer = RelayClient(RELAYER_HOST, CHAIN_ID, PRIVATE_KEY, cfg)
            
            # Check if expected safe is deployed, if not, try to deploy it
            expected_safe = relayer.get_expected_safe()
            if not relayer.get_deployed(expected_safe):
                logger.info(f"Safe {expected_safe} not deployed. Attempting deployment...")
                relayer.deploy()
                logger.info(f"Safe deployment transaction submitted.")
            
            logger.info(f"{C_BLUE}Relayer initialized. Using GASLESS redemptions.{C_RESET}")
        except Exception as e:
            logger.warning(f"Failed to init relayer: {e}. Falling back to gas-paying mode.")

    already_claimed = set()

    logger.info("=" * 60)
    logger.info(f"{C_BLUE}  Polymarket Auto-Claimer (Gasless Mode)  {C_RESET}")
    logger.info(f"  Wallet (proxy):  {FUNDER}")
    logger.info(f"  Dry-run:         {dry_run}")
    logger.info("=" * 60)

    while True:
        positions = fetch_redeemable_positions(FUNDER)
        if not positions:
            logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] No winning positions to claim.")
        else:
            logger.info(f"Found {len(positions)} winning position(s).")
            for pos in positions:
                cid = pos.get("conditionId")
                title = pos.get("title", cid[:16])
                value = float(pos.get("currentValue", 0))
                
                if not cid or cid in already_claimed:
                    continue

                if value < MIN_CLAIM_VALUE_USD:
                    logger.info(f"  Skipping {title} (value ${value:.2f} < ${MIN_CLAIM_VALUE_USD})")
                    continue

                if dry_run:
                    logger.info(f"{C_YELLOW}[DRY-RUN] Would redeem: {title}{C_RESET}")
                    already_claimed.add(cid)
                    continue

                success = False
                if relayer:
                    success = claim_position_gasless(relayer, cid, title)
                else:
                    success = claim_position_standard(account, cid, title)
                
                if success:
                    already_claimed.add(cid)
                    time.sleep(2)

        if os.getenv("RUN_ONCE"): break
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--profile", type=int, default=None, help="Profile number to use")
    parser.add_argument("--min-value", type=float, default=None, help="Override minimum claim value in USD")
    args = parser.parse_args()

    if args.once: os.environ["RUN_ONCE"] = "1"
    if args.min_value is not None:
        MIN_CLAIM_VALUE_USD = args.min_value
    run(dry_run=args.dry_run)
