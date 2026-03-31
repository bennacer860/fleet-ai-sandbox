"""Auto-claimer — periodically redeems winning positions via the gasless relayer.

Runs as a supervised async task inside the Bot.  Every ``interval`` seconds
it polls the Polymarket Data API for redeemable positions whose value
meets the configured threshold, then calls ``redeemPositions`` on the
CTF contract through the Builder Relayer (gasless).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from ..clob_client import get_usdc_balance

import eth_abi
import requests
from eth_utils import keccak

logger = logging.getLogger(__name__)

# Contract addresses (Polygon mainnet)
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
DATA_API = "https://data-api.polymarket.com"
RELAYER_HOST = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137

REDEEM_SELECTOR = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]


def _encode_redeem_calldata(condition_id: str) -> bytes:
    cid_bytes = bytes.fromhex(condition_id.removeprefix("0x"))
    parent_bytes = b"\x00" * 32
    encoded = eth_abi.encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_CONTRACT, parent_bytes, cid_bytes, [1, 2]],
    )
    return REDEEM_SELECTOR + encoded


class AutoClaimer:
    """Periodically checks for and redeems winning positions gaslessly."""

    def __init__(
        self,
        min_value: float,
        interval: float = 60,  # 1 minute
        funder: str = "",
        private_key: str = "",
    ) -> None:
        self.min_value = min_value
        self.interval = interval
        self.funder = funder or os.getenv("FUNDER", "")
        self.private_key = private_key or os.getenv("PRIVATE_KEY", "")
        self._claimed: set[str] = set()
        self._relayer = None
        self._initialized = False

        # Stats for dashboard
        self.last_check_time: float | None = None
        self.total_claimed: int = 0
        self.last_claim_time: float | None = None
        self.last_balance: float | None = None
        self.on_claim: callable | None = None  # callback(title, balance, tx_hash)

    def _init_relayer(self) -> bool:
        """Lazy-init the Builder Relayer client."""
        if self._initialized:
            return self._relayer is not None
        self._initialized = True

        api_key = os.getenv("POLY_API_KEY", "")
        secret = os.getenv("POLY_SECRET", "")
        passphrase = os.getenv("POLY_PASSPHRASE") or os.getenv("POLY_PARAPHRASE", "")

        if not all([api_key, secret, passphrase, self.private_key]):
            logger.warning(
                "[AutoClaimer] Missing Builder API credentials — "
                "gasless claims disabled.  Set POLY_API_KEY, POLY_SECRET, "
                "and POLY_PASSPHRASE in .env."
            )
            return False

        try:
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            from py_builder_relayer_client.client import RelayClient
            from py_builder_relayer_client.models import SafeTransaction, OperationType  # noqa: F401

            creds = BuilderApiKeyCreds(
                key=api_key, secret=secret, passphrase=passphrase
            )
            cfg = BuilderConfig(local_builder_creds=creds)
            self._relayer = RelayClient(
                RELAYER_HOST, CHAIN_ID, self.private_key, cfg
            )
            logger.info("[AutoClaimer] Relayer initialized — gasless claims enabled (min $%.2f).", self.min_value)
            return True
        except ImportError:
            logger.warning(
                "[AutoClaimer] Builder SDK not installed — gasless claims disabled."
            )
            return False
        except Exception as e:
            logger.warning("[AutoClaimer] Failed to init relayer: %s", e)
            return False

    def _fetch_redeemable(self) -> list[dict]:
        """Poll Data API for redeemable positions."""
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": self.funder, "redeemable": "true"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("[AutoClaimer] Failed to fetch positions: %s", e)
            return []

    def _claim_batch(self, claims: list[dict]) -> bool:
        """Submit a batch of redemptions in a single gasless transaction."""
        from py_builder_relayer_client.models import SafeTransaction, OperationType

        if not claims:
            return False

        try:
            txs = []
            titles = []
            for c in claims:
                cid = c["conditionId"]
                title = c.get("title", cid[:16])
                calldata = "0x" + _encode_redeem_calldata(cid).hex()
                txs.append(SafeTransaction(
                    to=CTF_CONTRACT,
                    data=calldata,
                    value="0",
                    operation=OperationType.Call,
                ))
                titles.append(title)

            resp = self._relayer.execute(txs)
            if resp and resp.transaction_id:
                tx_hash = resp.transaction_hash or "pending"
                summary = f"{len(claims)} position(s): " + ", ".join(titles)
                # Refresh balance right after claim so callbacks/notifications
                # receive the current post-claim value.
                try:
                    self.last_balance = get_usdc_balance()
                except Exception:
                    pass
                logger.info(
                    "[AutoClaimer] ✅ Batch Claimed %s — tx_id=%s hash=%s",
                    summary, resp.transaction_id, tx_hash,
                )
                if self.on_claim:
                    # Notify for each one so they appear in dashboard/telegram
                    for c in claims:
                        self.on_claim(c.get("title", c["conditionId"][:16]), self.last_balance, tx_hash)
                return True
            return False
        except Exception as e:
            logger.error("[AutoClaimer] ❌ Batch Claim failed: %s", e)
            return False

    async def _tick(self) -> int:
        """Run one claim cycle using aggregate threshold."""
        if not self._init_relayer():
            return 0

        positions = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_redeemable
        )

        if not positions:
            logger.debug("[AutoClaimer] No redeemable positions found.")
            return 0

        # Filter only eligible (unclaimed and valid)
        to_claim = []
        total_value = 0.0
        
        for pos in positions:
            cid = pos.get("conditionId")
            if not cid or cid in self._claimed:
                continue
            
            val = float(pos.get("currentValue", 0))
            to_claim.append(pos)
            total_value += val

        if not to_claim:
            return 0

        if total_value < self.min_value:
            logger.info(
                "[AutoClaimer] Aggregate winnings $%.2f < $%.2f threshold. Waiting for more...",
                total_value, self.min_value,
            )
            return 0

        logger.info(
            "[AutoClaimer] Triggering claim for %d positions! Aggregate value $%.2f >= $%.2f",
            len(to_claim), total_value, self.min_value,
        )

        success = await asyncio.get_event_loop().run_in_executor(
            None, self._claim_batch, to_claim
        )
        
        if success:
            for c in to_claim:
                self._claimed.add(c["conditionId"])
            return len(to_claim)

        return 0

    async def run(self) -> None:
        """Long-running loop — check every ``self.interval`` seconds."""
        logger.info(
            "[AutoClaimer] Started — checking every %.0f min, min claim $%.2f",
            self.interval / 60, self.min_value,
        )
        while True:
            try:
                import time as _time
                self.last_check_time = _time.time()
                n = await self._tick()
                if n:
                    # Update balance after successful claim
                    try:
                        self.last_balance = await asyncio.get_event_loop().run_in_executor(
                            None, get_usdc_balance
                        )
                    except Exception:
                        pass
                    
                    self.total_claimed += n
                    self.last_claim_time = _time.time()
                    logger.info("[AutoClaimer] Claimed %d position(s) this cycle. New balance: $%.2f", n, self.last_balance or 0)
            except Exception as e:
                logger.error("[AutoClaimer] Error in claim cycle: %s", e)
            await asyncio.sleep(self.interval)
