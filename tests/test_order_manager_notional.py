import asyncio

from src.core.event_bus import EventBus
from src.core.events import OrderStatus, OrderSubmitted, OrderTerminal
from src.core.models import OrderIntent, Side
from src.execution.order_manager import OrderManager
from src.execution.risk_manager import RiskConfig, RiskManager


class _CapturingRestClient:
    def __init__(self) -> None:
        self.intents: list[OrderIntent] = []

    async def place_order(self, intent: OrderIntent, dry_run: bool = False) -> OrderSubmitted:
        self.intents.append(intent)
        return OrderSubmitted(
            order_id="oid-1",
            token_id=intent.token_id,
            slug=intent.slug,
            strategy=intent.strategy,
            price=intent.price,
            size=intent.size,
            side=intent.side.value,
            dry_run=dry_run,
        )


class _RejectThenSubmitRestClient:
    def __init__(self) -> None:
        self.calls = 0
        self.intents: list[OrderIntent] = []

    async def place_order(self, intent: OrderIntent, dry_run: bool = False) -> OrderSubmitted | OrderTerminal:
        self.calls += 1
        self.intents.append(intent)
        if self.calls == 1:
            return OrderTerminal(
                order_id="",
                status=OrderStatus.REJECTED,
                reason="simulated reject",
            )
        return OrderSubmitted(
            order_id=f"oid-{self.calls}",
            token_id=intent.token_id,
            slug=intent.slug,
            strategy=intent.strategy,
            price=intent.price,
            size=intent.size,
            side=intent.side.value,
            dry_run=dry_run,
        )


def _build_manager(rest_client: _CapturingRestClient) -> OrderManager:
    return OrderManager(
        event_bus=EventBus(),
        rest_client=rest_client,  # type: ignore[arg-type]
        risk_manager=RiskManager(RiskConfig(max_position_per_market=10_000, max_total_exposure=100_000)),
        persistence=None,
        dry_run=False,
    )


def test_submit_resizes_gabagool_buy_under_min_notional() -> None:
    rest = _CapturingRestClient()
    manager = _build_manager(rest)
    intent = OrderIntent(
        token_id="tok-yes",
        price=0.32,
        size=2.5,
        side=Side.BUY,
        strategy="gabagool",
        slug="btc-updown-15m-abc",
    )

    state = asyncio.run(manager.submit(intent))

    assert state is not None
    assert rest.intents, "Expected place_order to be called"
    submitted = rest.intents[0]
    assert submitted.size == 3.15625  # $1.01 / 0.32
    assert submitted.price * submitted.size >= 1.01
    assert state.intent.size == submitted.size


def test_submit_resizes_with_buffer_for_boundary_case() -> None:
    rest = _CapturingRestClient()
    manager = _build_manager(rest)
    # This order would be rejected at the venue as ~$0.9996 marketable notional.
    intent = OrderIntent(
        token_id="tok-yes",
        price=0.4165,
        size=2.4,
        side=Side.BUY,
        strategy="gabagool",
        slug="btc-updown-15m-boundary",
    )

    state = asyncio.run(manager.submit(intent))

    assert state is not None
    submitted = rest.intents[0]
    assert submitted.size > 2.4
    assert submitted.price * submitted.size >= 1.01


def test_submit_does_not_resize_non_gabagool_strategy() -> None:
    rest = _CapturingRestClient()
    manager = _build_manager(rest)
    intent = OrderIntent(
        token_id="tok-yes",
        price=0.32,
        size=2.5,
        side=Side.BUY,
        strategy="sweep",
        slug="btc-updown-15m-abc",
    )

    state = asyncio.run(manager.submit(intent))

    assert state is not None
    submitted = rest.intents[0]
    assert submitted.size == 2.5
    assert state.intent.size == 2.5


def test_submit_allows_gabagool_retry_after_rejection() -> None:
    rest = _RejectThenSubmitRestClient()
    manager = _build_manager(rest)  # type: ignore[arg-type]
    intent = OrderIntent(
        token_id="tok-yes",
        price=0.32,
        size=2.5,
        side=Side.BUY,
        strategy="gabagool",
        slug="btc-updown-15m-retry",
    )

    first = asyncio.run(manager.submit(intent))
    second = asyncio.run(manager.submit(intent))

    assert first is not None
    assert first.status == OrderStatus.REJECTED
    assert second is not None
    assert second.status == OrderStatus.SUBMITTED
    assert rest.calls == 2


def test_submit_keeps_non_gabagool_dedup_after_rejection() -> None:
    rest = _RejectThenSubmitRestClient()
    manager = _build_manager(rest)  # type: ignore[arg-type]
    intent = OrderIntent(
        token_id="tok-yes",
        price=0.32,
        size=2.5,
        side=Side.BUY,
        strategy="sweep",
        slug="btc-updown-15m-no-retry",
    )

    first = asyncio.run(manager.submit(intent))
    second = asyncio.run(manager.submit(intent))

    assert first is not None
    assert first.status == OrderStatus.REJECTED
    # dedup still blocks immediate resubmit for non-gabagool
    assert second is None
    assert rest.calls == 1


def test_submit_does_not_resize_gabagool_sell() -> None:
    rest = _CapturingRestClient()
    manager = _build_manager(rest)
    intent = OrderIntent(
        token_id="tok-yes",
        price=0.32,
        size=2.5,
        side=Side.SELL,
        strategy="gabagool",
        slug="btc-updown-15m-abc",
    )

    state = asyncio.run(manager.submit(intent))

    assert state is not None
    submitted = rest.intents[0]
    assert submitted.size == 2.5
    assert state.intent.size == 2.5
