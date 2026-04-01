"""Dry-run e2e checks for gabagool_dual strategy wiring."""

import asyncio
import math
import os
import tempfile

from src.core.event_bus import EventBus
from src.core.events import BookUpdate, OrderFill, OrderSubmitted
from src.execution.fill_simulator import FillSimulator
from src.execution.order_manager import OrderManager
from src.execution.position_tracker import PositionTracker
from src.execution.risk_manager import RiskConfig, RiskManager
from src.gateway.rest_client import AsyncRestClient
from src.monitoring.metrics import Metrics
from src.storage.database import init_db
from src.storage.persistence import AsyncPersistence
from src.strategy.base import StrategyContext
from src.strategy.gabagool_dual_adapter import GabagoolDualConfig, GabagoolDualStrategy

TOKEN_YES = "token_yes_gabagool_dual_e2e"
TOKEN_NO = "token_no_gabagool_dual_e2e"
SLUG = "btc-updown-15m-gabagool-dual-e2e"
CONDITION_ID = "cond_gabagool_dual_e2e"


class DualHarness:
    def __init__(self) -> None:
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test_gabagool_dual_e2e.db")
        self.conn = init_db(db_path)
        self.event_bus = EventBus()
        self.persistence = AsyncPersistence(self.conn, flush_interval=0.05)
        self.order_manager = OrderManager(
            event_bus=self.event_bus,
            rest_client=AsyncRestClient(),
            risk_manager=RiskManager(RiskConfig(
                max_position_per_market=10_000,
                max_total_exposure=50_000,
                max_orders_per_minute=400,
                max_daily_loss=10_000,
            )),
            persistence=self.persistence,
            dry_run=True,
        )
        self.position_tracker = PositionTracker(persistence=self.persistence)
        self.fill_simulator = FillSimulator(event_bus=self.event_bus, mode="instant")
        self.strategy = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=1.01,
                cooldown_pair_cost=1.02,
                resume_pair_cost=1.00,
                base_order_size=2.5,
                min_order_notional_usd=0.0,
                max_notional_per_slug=100.0,
            )
        )
        self.submitted: list[OrderSubmitted] = []
        self.fills: list[OrderFill] = []

        async def _capture_sub(e: OrderSubmitted) -> None:
            self.submitted.append(e)

        async def _capture_fill(e: OrderFill) -> None:
            self.fills.append(e)
            state = self.order_manager.active_orders.get(e.order_id)
            if state:
                self.strategy.on_fill_event(
                    token_id=state.intent.token_id,
                    fill_size=e.fill_size,
                    fill_price=e.fill_price,
                )

        bus = self.event_bus
        bus.subscribe(OrderSubmitted, self.fill_simulator.on_order_submitted)
        bus.subscribe(OrderSubmitted, _capture_sub)
        bus.subscribe(OrderFill, self.order_manager.on_order_fill)
        bus.subscribe(OrderFill, self.position_tracker.on_fill)
        bus.subscribe(OrderFill, _capture_fill)
        bus.subscribe(BookUpdate, self.fill_simulator.on_book_update)

    async def start(self) -> None:
        self._bus = asyncio.create_task(self.event_bus.run())
        self._persist = asyncio.create_task(self.persistence.drain_loop())

    async def stop(self) -> None:
        await self.event_bus.stop()
        await self.persistence.stop()
        self._bus.cancel()
        self._persist.cancel()
        await asyncio.gather(self._bus, self._persist, return_exceptions=True)
        self.conn.close()

    def _ctx(self, yes_ask: float, no_ask: float) -> StrategyContext:
        return StrategyContext(
            market_meta={
                SLUG: {
                    "token_ids": (TOKEN_YES, TOKEN_NO),
                    "outcomes": ("Up", "Down"),
                    "condition_id": CONDITION_ID,
                }
            },
            best_prices={
                TOKEN_YES: {"ask": yes_ask, "bid": yes_ask - 0.01},
                TOKEN_NO: {"ask": no_ask, "bid": no_ask - 0.01},
            },
            tick_sizes={TOKEN_YES: 0.01, TOKEN_NO: 0.01},
            dry_run=True,
        )

    async def tick(self, yes_ask: float) -> None:
        no_ask = max(0.01, min(0.99, 1.0 - yes_ask))
        event_yes = BookUpdate(
            token_id=TOKEN_YES,
            condition_id=CONDITION_ID,
            slug=SLUG,
            bids=((yes_ask - 0.01, 100.0),),
            asks=((yes_ask, 100.0),),
            best_bid=yes_ask - 0.01,
            best_ask=yes_ask,
        )
        event_no = BookUpdate(
            token_id=TOKEN_NO,
            condition_id=CONDITION_ID,
            slug=SLUG,
            bids=((no_ask - 0.01, 100.0),),
            asks=((no_ask, 100.0),),
            best_bid=no_ask - 0.01,
            best_ask=no_ask,
        )
        await self.event_bus.publish(event_yes)
        await self.event_bus.publish(event_no)
        intents = await self.strategy.on_book_update(event_yes, self._ctx(yes_ask, no_ask))
        if not intents:
            return
        for intent in intents:
            await self.order_manager.submit(intent)


def test_dual_strategy_submits_both_sides() -> None:
    async def _run() -> None:
        h = DualHarness()
        await h.start()
        try:
            prices = [
                max(0.05, min(0.95, 0.5 + 0.2 * math.sin(2 * math.pi * i / 10)))
                for i in range(30)
            ]
            for p in prices:
                await h.tick(p)
                await asyncio.sleep(0.05)
            assert len(h.submitted) > 0
            strategies = {s.strategy for s in h.submitted}
            assert strategies == {"gabagool_dual"}
            token_ids = {s.token_id for s in h.submitted}
            assert TOKEN_YES in token_ids and TOKEN_NO in token_ids
            state = h.strategy.get_slug_state(SLUG)
            assert state is not None
            assert state.pair.qty_yes + state.pair.qty_no > 0
        finally:
            await h.stop()

    Metrics.reset()
    asyncio.run(_run())
