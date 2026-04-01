import asyncio

from src.core.event_bus import EventBus
from src.core.events import OrderSubmitted
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
    assert submitted.size == 3.125  # $1.00 / 0.32
    assert submitted.price * submitted.size >= 1.0
    assert state.intent.size == submitted.size


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
