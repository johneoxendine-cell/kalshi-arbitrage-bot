"""Circuit breaker for trading halt mechanism."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

from config.logging_config import get_logger
from src.core.exceptions import CircuitBreakerOpenError

logger = get_logger(__name__)


class CircuitBreakerState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Trading halted
    HALF_OPEN = "half_open"  # Testing if safe to resume


@dataclass
class CircuitBreakerMetrics:
    """Metrics tracked by circuit breaker."""

    daily_loss_cents: int = 0
    consecutive_losses: int = 0
    total_exposure_cents: int = 0
    last_loss_time: Optional[datetime] = None
    trip_count: int = 0
    last_trip_time: Optional[datetime] = None


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    max_daily_loss_cents: int = 10000  # $100
    max_consecutive_losses: int = 5
    max_exposure_cents: int = 50000  # $500
    cooldown_seconds: int = 300  # 5 minutes
    half_open_test_limit: int = 1  # Trades allowed in half-open


class CircuitBreaker:
    """Trading halt mechanism for risk control.

    Trips (halts trading) when:
    - Daily loss exceeds limit
    - Consecutive losses exceed limit
    - Total exposure exceeds limit

    After cooldown, enters half-open state to test market conditions.
    """

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        on_trip: Optional[Callable[[str], None]] = None,
        on_reset: Optional[Callable[[], None]] = None,
    ) -> None:
        """Initialize circuit breaker.

        Args:
            config: Configuration settings
            on_trip: Callback when breaker trips (receives reason)
            on_reset: Callback when breaker resets
        """
        self.config = config or CircuitBreakerConfig()
        self._on_trip = on_trip
        self._on_reset = on_reset

        self._state = CircuitBreakerState.CLOSED
        self._metrics = CircuitBreakerMetrics()
        self._trip_reason: Optional[str] = None
        self._trip_time: Optional[datetime] = None
        self._half_open_trades: int = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        """Current circuit breaker state."""
        return self._state

    @property
    def is_open(self) -> bool:
        """Whether trading is halted."""
        return self._state == CircuitBreakerState.OPEN

    @property
    def is_closed(self) -> bool:
        """Whether trading is allowed."""
        return self._state == CircuitBreakerState.CLOSED

    @property
    def metrics(self) -> CircuitBreakerMetrics:
        """Current metrics."""
        return self._metrics

    @property
    def trip_reason(self) -> Optional[str]:
        """Reason for last trip."""
        return self._trip_reason

    async def check_and_allow(self) -> bool:
        """Check if trading is allowed and update state if needed.

        Returns:
            True if trading is allowed

        Raises:
            CircuitBreakerOpenError: If breaker is open
        """
        async with self._lock:
            # Check if cooldown has passed
            if self._state == CircuitBreakerState.OPEN:
                if self._should_transition_to_half_open():
                    self._transition_to_half_open()
                else:
                    remaining = self._cooldown_remaining()
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker open: {self._trip_reason}",
                        cooldown_remaining=remaining,
                    )

            # In half-open, allow limited trades
            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._half_open_trades >= self.config.half_open_test_limit:
                    raise CircuitBreakerOpenError(
                        "Half-open trade limit reached",
                        cooldown_remaining=0,
                    )
                self._half_open_trades += 1

            return True

    async def record_trade_result(
        self,
        profit_cents: int,
        exposure_cents: int = 0,
    ) -> None:
        """Record the result of a trade.

        Args:
            profit_cents: Profit (positive) or loss (negative)
            exposure_cents: Current total exposure
        """
        async with self._lock:
            self._metrics.total_exposure_cents = exposure_cents

            if profit_cents < 0:
                # Loss
                self._metrics.daily_loss_cents += abs(profit_cents)
                self._metrics.consecutive_losses += 1
                self._metrics.last_loss_time = datetime.utcnow()

                # Check trip conditions
                await self._check_trip_conditions()
            else:
                # Win - reset consecutive losses
                self._metrics.consecutive_losses = 0

                # If in half-open and successful, close the breaker
                if self._state == CircuitBreakerState.HALF_OPEN:
                    self._transition_to_closed()

    async def record_exposure(self, exposure_cents: int) -> None:
        """Record current exposure.

        Args:
            exposure_cents: Current total exposure
        """
        async with self._lock:
            self._metrics.total_exposure_cents = exposure_cents
            await self._check_trip_conditions()

    async def _check_trip_conditions(self) -> None:
        """Check if any trip condition is met."""
        if self._state == CircuitBreakerState.OPEN:
            return

        reason = None

        if self._metrics.daily_loss_cents >= self.config.max_daily_loss_cents:
            reason = f"Daily loss limit: ${self._metrics.daily_loss_cents / 100:.2f}"

        elif self._metrics.consecutive_losses >= self.config.max_consecutive_losses:
            reason = f"Consecutive losses: {self._metrics.consecutive_losses}"

        elif self._metrics.total_exposure_cents >= self.config.max_exposure_cents:
            reason = f"Exposure limit: ${self._metrics.total_exposure_cents / 100:.2f}"

        if reason:
            self._trip(reason)

    def _trip(self, reason: str) -> None:
        """Trip the circuit breaker."""
        self._state = CircuitBreakerState.OPEN
        self._trip_reason = reason
        self._trip_time = datetime.utcnow()
        self._metrics.trip_count += 1
        self._metrics.last_trip_time = self._trip_time

        logger.warning(
            "Circuit breaker tripped",
            reason=reason,
            daily_loss=self._metrics.daily_loss_cents,
            consecutive_losses=self._metrics.consecutive_losses,
            exposure=self._metrics.total_exposure_cents,
        )

        if self._on_trip:
            try:
                self._on_trip(reason)
            except Exception as e:
                logger.error("Trip callback error", error=str(e))

    def _should_transition_to_half_open(self) -> bool:
        """Check if cooldown has passed."""
        if not self._trip_time:
            return True
        elapsed = datetime.utcnow() - self._trip_time
        return elapsed >= timedelta(seconds=self.config.cooldown_seconds)

    def _transition_to_half_open(self) -> None:
        """Transition to half-open state."""
        self._state = CircuitBreakerState.HALF_OPEN
        self._half_open_trades = 0
        logger.info("Circuit breaker half-open, testing conditions")

    def _transition_to_closed(self) -> None:
        """Transition to closed (normal) state."""
        self._state = CircuitBreakerState.CLOSED
        self._trip_reason = None
        self._trip_time = None
        self._half_open_trades = 0

        logger.info("Circuit breaker closed, normal operation resumed")

        if self._on_reset:
            try:
                self._on_reset()
            except Exception as e:
                logger.error("Reset callback error", error=str(e))

    def _cooldown_remaining(self) -> float:
        """Get remaining cooldown in seconds."""
        if not self._trip_time:
            return 0.0
        elapsed = datetime.utcnow() - self._trip_time
        remaining = self.config.cooldown_seconds - elapsed.total_seconds()
        return max(0.0, remaining)

    async def reset_daily_metrics(self) -> None:
        """Reset daily metrics (call at start of trading day)."""
        async with self._lock:
            self._metrics.daily_loss_cents = 0
            logger.info("Daily metrics reset")

    async def force_close(self) -> None:
        """Force close the circuit breaker."""
        async with self._lock:
            self._transition_to_closed()
            logger.info("Circuit breaker force closed")

    async def force_open(self, reason: str = "Manual trigger") -> None:
        """Force open the circuit breaker.

        Args:
            reason: Reason for manual trip
        """
        async with self._lock:
            self._trip(reason)

    def get_status(self) -> dict:
        """Get current status.

        Returns:
            Status dictionary
        """
        return {
            "state": self._state.value,
            "is_open": self.is_open,
            "trip_reason": self._trip_reason,
            "cooldown_remaining": self._cooldown_remaining() if self.is_open else 0,
            "metrics": {
                "daily_loss_cents": self._metrics.daily_loss_cents,
                "consecutive_losses": self._metrics.consecutive_losses,
                "total_exposure_cents": self._metrics.total_exposure_cents,
                "trip_count": self._metrics.trip_count,
            },
            "limits": {
                "max_daily_loss_cents": self.config.max_daily_loss_cents,
                "max_consecutive_losses": self.config.max_consecutive_losses,
                "max_exposure_cents": self.config.max_exposure_cents,
            },
        }
