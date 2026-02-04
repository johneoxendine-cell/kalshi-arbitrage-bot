"""Prometheus metrics collection."""

from typing import Optional

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

from config.logging_config import get_logger

logger = get_logger(__name__)


class MetricsCollector:
    """Collects and exposes Prometheus metrics.

    Metrics:
    - opportunities_detected_total: Counter of detected opportunities
    - orders_placed_total: Counter of orders placed
    - orders_filled_total: Counter of orders filled
    - current_balance_cents: Gauge of account balance
    - current_exposure_cents: Gauge of total exposure
    - execution_latency_ms: Histogram of execution times
    - circuit_breaker_trips_total: Counter of breaker trips
    - pnl_cents: Gauge of profit/loss
    """

    def __init__(self, prefix: str = "kalshi_arb") -> None:
        """Initialize metrics collector.

        Args:
            prefix: Metric name prefix
        """
        self.prefix = prefix

        # Info metric
        self.bot_info = Info(
            f"{prefix}_bot",
            "Bot information",
        )

        # Counters
        self.opportunities_detected = Counter(
            f"{prefix}_opportunities_detected_total",
            "Total arbitrage opportunities detected",
            ["type"],  # multi_outcome, time_based, correlated
        )

        self.orders_placed = Counter(
            f"{prefix}_orders_placed_total",
            "Total orders placed",
            ["side", "action"],  # yes/no, buy/sell
        )

        self.orders_filled = Counter(
            f"{prefix}_orders_filled_total",
            "Total orders filled",
            ["side", "action"],
        )

        self.orders_failed = Counter(
            f"{prefix}_orders_failed_total",
            "Total orders failed",
            ["reason"],
        )

        self.circuit_breaker_trips = Counter(
            f"{prefix}_circuit_breaker_trips_total",
            "Total circuit breaker trips",
            ["reason"],
        )

        self.api_requests = Counter(
            f"{prefix}_api_requests_total",
            "Total API requests",
            ["method", "endpoint", "status"],
        )

        # Gauges
        self.balance_cents = Gauge(
            f"{prefix}_balance_cents",
            "Current account balance in cents",
        )

        self.exposure_cents = Gauge(
            f"{prefix}_exposure_cents",
            "Current total exposure in cents",
        )

        self.positions_count = Gauge(
            f"{prefix}_positions_count",
            "Number of open positions",
        )

        self.daily_pnl_cents = Gauge(
            f"{prefix}_daily_pnl_cents",
            "Daily profit/loss in cents",
        )

        self.total_pnl_cents = Gauge(
            f"{prefix}_total_pnl_cents",
            "Total profit/loss in cents",
        )

        self.websocket_connected = Gauge(
            f"{prefix}_websocket_connected",
            "WebSocket connection status (1=connected, 0=disconnected)",
        )

        self.subscribed_markets = Gauge(
            f"{prefix}_subscribed_markets",
            "Number of markets subscribed to",
        )

        # Histograms
        self.execution_latency = Histogram(
            f"{prefix}_execution_latency_ms",
            "Order execution latency in milliseconds",
            buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
        )

        self.opportunity_profit_cents = Histogram(
            f"{prefix}_opportunity_profit_cents",
            "Profit per opportunity in cents",
            buckets=[1, 2, 5, 10, 25, 50, 100, 250],
        )

        self.api_latency = Histogram(
            f"{prefix}_api_latency_ms",
            "API request latency in milliseconds",
            ["method"],
            buckets=[10, 25, 50, 100, 250, 500, 1000, 2500],
        )

        self._server_started = False
        logger.info("Metrics collector initialized", prefix=prefix)

    def start_server(self, port: int = 8000) -> None:
        """Start Prometheus HTTP server.

        Args:
            port: Port to serve metrics on
        """
        if not self._server_started:
            start_http_server(port)
            self._server_started = True
            logger.info("Prometheus metrics server started", port=port)

    def set_bot_info(
        self,
        version: str,
        environment: str,
        strategies: list[str],
    ) -> None:
        """Set bot information.

        Args:
            version: Bot version
            environment: Running environment
            strategies: Enabled strategies
        """
        self.bot_info.info({
            "version": version,
            "environment": environment,
            "strategies": ",".join(strategies),
        })

    # Opportunity metrics

    def record_opportunity(self, arb_type: str, profit_cents: int) -> None:
        """Record a detected opportunity.

        Args:
            arb_type: Type of arbitrage
            profit_cents: Expected profit
        """
        self.opportunities_detected.labels(type=arb_type).inc()
        self.opportunity_profit_cents.observe(profit_cents)

    # Order metrics

    def record_order_placed(self, side: str, action: str) -> None:
        """Record an order placement."""
        self.orders_placed.labels(side=side, action=action).inc()

    def record_order_filled(self, side: str, action: str) -> None:
        """Record an order fill."""
        self.orders_filled.labels(side=side, action=action).inc()

    def record_order_failed(self, reason: str) -> None:
        """Record an order failure."""
        self.orders_failed.labels(reason=reason).inc()

    def record_execution_latency(self, latency_ms: float) -> None:
        """Record execution latency."""
        self.execution_latency.observe(latency_ms)

    # Balance and position metrics

    def update_balance(self, balance_cents: int) -> None:
        """Update balance gauge."""
        self.balance_cents.set(balance_cents)

    def update_exposure(self, exposure_cents: int) -> None:
        """Update exposure gauge."""
        self.exposure_cents.set(exposure_cents)

    def update_positions_count(self, count: int) -> None:
        """Update positions count gauge."""
        self.positions_count.set(count)

    def update_pnl(self, daily_cents: int, total_cents: int) -> None:
        """Update P&L gauges."""
        self.daily_pnl_cents.set(daily_cents)
        self.total_pnl_cents.set(total_cents)

    # Circuit breaker metrics

    def record_circuit_breaker_trip(self, reason: str) -> None:
        """Record a circuit breaker trip."""
        self.circuit_breaker_trips.labels(reason=reason).inc()

    # Connection metrics

    def update_websocket_status(self, connected: bool) -> None:
        """Update WebSocket connection status."""
        self.websocket_connected.set(1 if connected else 0)

    def update_subscribed_markets(self, count: int) -> None:
        """Update subscribed markets count."""
        self.subscribed_markets.set(count)

    # API metrics

    def record_api_request(
        self,
        method: str,
        endpoint: str,
        status: int,
        latency_ms: float,
    ) -> None:
        """Record an API request."""
        self.api_requests.labels(
            method=method,
            endpoint=endpoint,
            status=str(status),
        ).inc()
        self.api_latency.labels(method=method).observe(latency_ms)
