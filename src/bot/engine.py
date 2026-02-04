"""Main arbitrage bot orchestrator."""

import asyncio
import signal
from datetime import datetime
from typing import Optional

from config.logging_config import get_logger
from config.settings import Settings
from src.arbitrage.detector import ArbitrageDetector
from src.core.authenticator import KalshiAuthenticator
from src.core.client import KalshiClient
from src.core.exceptions import CircuitBreakerOpenError
from src.core.rate_limiter import DualRateLimiter
from src.data.market_fetcher import MarketFetcher
from src.data.models import ArbitrageOpportunity
from src.data.orderbook_manager import OrderbookManager
from src.data.websocket_client import KalshiWebSocketClient
from src.execution.executor import ExecutionResult, Executor
from src.execution.order_manager import OrderManager
from src.execution.position_tracker import PositionTracker
from src.monitoring.alerting import AlertManager
from src.monitoring.metrics import MetricsCollector
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from src.risk.exposure_manager import ExposureManager, ExposureLimits

logger = get_logger(__name__)


class ArbitrageBotEngine:
    """Main orchestrator for the arbitrage trading bot.

    Coordinates:
    - Market data fetching and WebSocket updates
    - Arbitrage opportunity detection
    - Order execution
    - Risk management
    - Monitoring and alerting
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the bot engine.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Initialize components
        self._init_components()

        logger.info(
            "Bot engine initialized",
            environment=settings.environment.value,
            min_profit=settings.min_profit_cents,
        )

    def _init_components(self) -> None:
        """Initialize all bot components."""
        # Authentication
        self.authenticator = KalshiAuthenticator(
            api_key_id=self.settings.kalshi_api_key_id,
            private_key_path=self.settings.kalshi_private_key_path,
        )

        # Rate limiting
        self.rate_limiter = DualRateLimiter(
            read_rate=self.settings.read_rate_limit,
            write_rate=self.settings.write_rate_limit,
        )

        # API client
        self.client = KalshiClient(
            base_url=self.settings.base_url,
            authenticator=self.authenticator,
            rate_limiter=self.rate_limiter,
        )

        # Data layer
        self.market_fetcher = MarketFetcher(self.client)
        self.orderbook_manager = OrderbookManager()
        self.websocket_client = KalshiWebSocketClient(
            url=self.settings.websocket_url,
            authenticator=self.authenticator,
            orderbook_manager=self.orderbook_manager,
        )

        # Arbitrage detection
        self.detector = ArbitrageDetector(
            min_profit_cents=self.settings.min_profit_cents,
        )

        # Execution
        self.order_manager = OrderManager(self.client)
        self.position_tracker = PositionTracker(self.client)

        # Risk management
        self.circuit_breaker = CircuitBreaker(
            config=CircuitBreakerConfig(
                max_daily_loss_cents=self.settings.max_daily_loss_cents,
                max_consecutive_losses=self.settings.max_consecutive_losses,
                max_exposure_cents=self.settings.max_exposure_cents,
                cooldown_seconds=self.settings.cooldown_seconds,
            ),
            on_trip=self._on_circuit_breaker_trip,
        )
        self.exposure_manager = ExposureManager(
            position_tracker=self.position_tracker,
            limits=ExposureLimits(
                max_total_exposure_cents=self.settings.max_exposure_cents,
                max_position_per_market=self.settings.max_position_per_market,
            ),
        )

        # Executor
        self.executor = Executor(
            order_manager=self.order_manager,
            orderbook_manager=self.orderbook_manager,
            detector=self.detector,
            circuit_breaker=self.circuit_breaker,
        )
        self.executor.on_execution(self._on_execution_complete)

        # Monitoring
        self.metrics = MetricsCollector()
        self.alerts = AlertManager(
            slack_webhook=self.settings.slack_webhook_url,
            discord_webhook=self.settings.discord_webhook_url,
        )

        # Track watched events
        self._watched_events: set[str] = set()

    def _on_circuit_breaker_trip(self, reason: str) -> None:
        """Handle circuit breaker trip."""
        self.metrics.record_circuit_breaker_trip(reason)
        asyncio.create_task(
            self.alerts.alert_circuit_breaker(
                reason=reason,
                daily_loss=self.circuit_breaker.metrics.daily_loss_cents,
                exposure=self.circuit_breaker.metrics.total_exposure_cents,
            )
        )

    def _on_execution_complete(self, result: ExecutionResult) -> None:
        """Handle execution completion."""
        if result.success:
            self.metrics.record_order_filled("yes", "buy")
            asyncio.create_task(
                self.alerts.alert_trade_executed(
                    event_ticker="",
                    profit_cents=result.profit_cents,
                    legs=0,
                )
            )
        else:
            self.metrics.record_order_failed(result.error or "unknown")

    async def start(self) -> None:
        """Start the bot."""
        logger.info("Starting arbitrage bot...")
        self._running = True

        # Setup signal handlers
        self._setup_signal_handlers()

        # Start metrics server
        self.metrics.start_server(self.settings.prometheus_port)
        self.metrics.set_bot_info(
            version="0.1.0",
            environment=self.settings.environment.value,
            strategies=self.detector.enabled_strategies,
        )

        try:
            # Initial data sync
            await self._initial_sync()

            # Start main loops
            await asyncio.gather(
                self._websocket_loop(),
                self._scan_loop(),
                self._sync_loop(),
                self._wait_for_shutdown(),
            )

        except asyncio.CancelledError:
            logger.info("Bot tasks cancelled")

        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("Stopping arbitrage bot...")
        self._running = False
        self._shutdown_event.set()

        # Close connections
        await self.websocket_client.disconnect()
        await self.client.close()
        await self.alerts.close()

        logger.info("Bot stopped")

    async def _initial_sync(self) -> None:
        """Perform initial data sync."""
        logger.info("Performing initial sync...")

        # Sync balance and positions
        await self.position_tracker.sync_balance()
        await self.position_tracker.sync_positions()

        # Update metrics
        self.metrics.update_balance(self.position_tracker.balance_cents)
        self.metrics.update_exposure(self.position_tracker.get_total_exposure())

        logger.info(
            "Initial sync complete",
            balance=self.position_tracker.balance_cents,
            positions=len(self.position_tracker.positions),
        )

    async def _websocket_loop(self) -> None:
        """Run WebSocket connection loop."""
        while self._running:
            try:
                await self.websocket_client.run()
            except Exception as e:
                logger.error("WebSocket error", error=str(e))
                self.metrics.update_websocket_status(False)
                await asyncio.sleep(5)  # Wait before reconnect

    async def _scan_loop(self) -> None:
        """Main arbitrage scanning loop."""
        scan_interval = 1.0  # seconds

        while self._running:
            try:
                await self._scan_for_opportunities()
            except CircuitBreakerOpenError as e:
                logger.warning("Circuit breaker open", error=str(e))
            except Exception as e:
                logger.error("Scan error", error=str(e))

            await asyncio.sleep(scan_interval)

    async def _sync_loop(self) -> None:
        """Periodic data sync loop."""
        sync_interval = 30.0  # seconds

        while self._running:
            await asyncio.sleep(sync_interval)

            try:
                await self.position_tracker.sync_balance()
                await self.position_tracker.sync_positions()

                # Update metrics
                self.metrics.update_balance(self.position_tracker.balance_cents)
                self.metrics.update_exposure(self.position_tracker.get_total_exposure())
                self.metrics.update_positions_count(len(self.position_tracker.positions))

                # Update circuit breaker with current exposure
                await self.circuit_breaker.record_exposure(
                    self.position_tracker.get_total_exposure()
                )

            except Exception as e:
                logger.error("Sync error", error=str(e))

    async def _scan_for_opportunities(self) -> None:
        """Scan watched events for arbitrage opportunities."""
        for event_ticker in list(self._watched_events):
            try:
                # Get markets and orderbooks
                markets = self.market_fetcher.get_cached_markets_for_event(event_ticker)
                if not markets:
                    markets = await self.market_fetcher.get_markets_by_event(event_ticker)

                orderbooks = self.orderbook_manager.get_all_orderbooks()
                event_orderbooks = {
                    m.ticker: orderbooks[m.ticker]
                    for m in markets
                    if m.ticker in orderbooks
                }

                if not event_orderbooks:
                    continue

                # Detect opportunities
                opportunities = self.detector.scan_event(markets, event_orderbooks)

                for opp in opportunities:
                    await self._handle_opportunity(opp)

            except Exception as e:
                logger.error("Scan error for event", event=event_ticker, error=str(e))

    async def _handle_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Handle a detected arbitrage opportunity."""
        logger.info(
            "Opportunity detected",
            type=opportunity.type.value,
            event=opportunity.event_ticker,
            profit=opportunity.net_profit_cents,
        )

        # Record metric
        self.metrics.record_opportunity(
            opportunity.type.value,
            opportunity.net_profit_cents,
        )

        # Check circuit breaker
        try:
            await self.circuit_breaker.check_and_allow()
        except CircuitBreakerOpenError:
            return

        # Check exposure limits
        check = await self.exposure_manager.check_trade(opportunity)
        if not check.allowed:
            logger.info("Trade not allowed by exposure limits", reason=check.reason)
            return

        # Execute
        quantity = min(opportunity.max_quantity, check.max_allowed_quantity)
        result = await self.executor.execute(opportunity, quantity=quantity)

        # Update circuit breaker
        if result.success:
            await self.circuit_breaker.record_trade_result(
                profit_cents=result.profit_cents,
                exposure_cents=self.position_tracker.get_total_exposure(),
            )
        else:
            await self.circuit_breaker.record_trade_result(
                profit_cents=-opportunity.total_cost_cents,  # Assume worst case
                exposure_cents=self.position_tracker.get_total_exposure(),
            )

    async def watch_event(self, event_ticker: str) -> None:
        """Start watching an event for arbitrage.

        Args:
            event_ticker: Event ticker to watch
        """
        # Fetch markets
        markets = await self.market_fetcher.get_markets_by_event(event_ticker)

        if not markets:
            logger.warning("No markets found for event", event=event_ticker)
            return

        # Subscribe to orderbook updates
        tickers = [m.ticker for m in markets]
        await self.websocket_client.subscribe_orderbook(tickers)

        # Fetch initial orderbooks
        orderbooks = await self.market_fetcher.get_orderbooks_for_event(event_ticker)
        for ticker, ob in orderbooks.items():
            await self.orderbook_manager.update_snapshot(ticker, ob)

        self._watched_events.add(event_ticker)

        logger.info(
            "Watching event",
            event=event_ticker,
            markets=len(markets),
        )

    async def unwatch_event(self, event_ticker: str) -> None:
        """Stop watching an event.

        Args:
            event_ticker: Event ticker to stop watching
        """
        markets = self.market_fetcher.get_cached_markets_for_event(event_ticker)
        tickers = [m.ticker for m in markets]

        await self.websocket_client.unsubscribe_orderbook(tickers)
        self._watched_events.discard(event_ticker)

        logger.info("Stopped watching event", event=event_ticker)

    async def _wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(self._handle_signal(sig)),
                )
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        logger.info("Received shutdown signal", signal=sig.name)
        self._shutdown_event.set()

    # Status methods

    def get_status(self) -> dict:
        """Get current bot status."""
        return {
            "running": self._running,
            "environment": self.settings.environment.value,
            "watched_events": list(self._watched_events),
            "websocket_connected": self.websocket_client.is_connected,
            "balance_cents": self.position_tracker.balance_cents,
            "total_exposure_cents": self.position_tracker.get_total_exposure(),
            "circuit_breaker": self.circuit_breaker.get_status(),
            "execution_stats": {
                "total_executions": len(self.executor.execution_history),
                "successful": self.executor.successful_executions,
                "total_profit_cents": self.executor.total_profit_cents,
            },
        }
