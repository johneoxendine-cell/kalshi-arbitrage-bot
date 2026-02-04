"""Unit tests for circuit breaker."""

import pytest
import asyncio
from datetime import datetime, timedelta

from src.risk.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
)
from src.core.exceptions import CircuitBreakerOpenError


@pytest.fixture
def config():
    """Create test configuration."""
    return CircuitBreakerConfig(
        max_daily_loss_cents=1000,  # $10
        max_consecutive_losses=3,
        max_exposure_cents=5000,  # $50
        cooldown_seconds=5,  # Short for testing
    )


@pytest.fixture
def breaker(config):
    """Create circuit breaker instance."""
    return CircuitBreaker(config=config)


@pytest.mark.asyncio
class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    async def test_initial_state_is_closed(self, breaker: CircuitBreaker):
        """Test breaker starts in closed state."""
        assert breaker.state == CircuitBreakerState.CLOSED
        assert breaker.is_closed
        assert not breaker.is_open

    async def test_allow_trading_when_closed(self, breaker: CircuitBreaker):
        """Test trading allowed when closed."""
        allowed = await breaker.check_and_allow()
        assert allowed

    async def test_trip_on_daily_loss_limit(self, breaker: CircuitBreaker):
        """Test breaker trips when daily loss exceeds limit."""
        # Record losses totaling $10 (1000 cents)
        await breaker.record_trade_result(profit_cents=-500)
        assert breaker.is_closed  # Still under limit

        await breaker.record_trade_result(profit_cents=-500)
        assert breaker.is_open
        assert "Daily loss limit" in breaker.trip_reason

    async def test_trip_on_consecutive_losses(self, breaker: CircuitBreaker):
        """Test breaker trips after consecutive losses."""
        for _ in range(3):
            await breaker.record_trade_result(profit_cents=-10)

        assert breaker.is_open
        assert "Consecutive losses" in breaker.trip_reason

    async def test_trip_on_exposure_limit(self, breaker: CircuitBreaker):
        """Test breaker trips when exposure exceeds limit."""
        await breaker.record_exposure(5000)
        assert breaker.is_open
        assert "Exposure limit" in breaker.trip_reason

    async def test_consecutive_losses_reset_on_win(self, breaker: CircuitBreaker):
        """Test consecutive loss counter resets on winning trade."""
        await breaker.record_trade_result(profit_cents=-10)
        await breaker.record_trade_result(profit_cents=-10)
        assert breaker.metrics.consecutive_losses == 2

        await breaker.record_trade_result(profit_cents=10)  # Win
        assert breaker.metrics.consecutive_losses == 0
        assert breaker.is_closed

    async def test_check_raises_when_open(self, breaker: CircuitBreaker):
        """Test check_and_allow raises error when open."""
        # Trip the breaker
        await breaker.record_trade_result(profit_cents=-1000)
        assert breaker.is_open

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await breaker.check_and_allow()

        assert exc_info.value.cooldown_remaining > 0

    async def test_half_open_after_cooldown(self, breaker: CircuitBreaker):
        """Test transition to half-open after cooldown."""
        # Trip the breaker
        await breaker.record_trade_result(profit_cents=-1000)
        assert breaker.is_open

        # Manually set trip time in past to simulate cooldown
        breaker._trip_time = datetime.utcnow() - timedelta(seconds=10)

        # Should transition to half-open
        allowed = await breaker.check_and_allow()
        assert allowed
        assert breaker.state == CircuitBreakerState.HALF_OPEN

    async def test_half_open_closes_on_success(self, breaker: CircuitBreaker):
        """Test half-open transitions to closed on successful trade."""
        # Get to half-open state
        await breaker.record_trade_result(profit_cents=-1000)
        breaker._trip_time = datetime.utcnow() - timedelta(seconds=10)
        await breaker.check_and_allow()
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        # Successful trade should close breaker
        await breaker.record_trade_result(profit_cents=10)
        assert breaker.is_closed

    async def test_half_open_trade_limit(self, breaker: CircuitBreaker):
        """Test half-open limits number of test trades."""
        # Get to half-open state
        await breaker.record_trade_result(profit_cents=-1000)
        breaker._trip_time = datetime.utcnow() - timedelta(seconds=10)

        # First check allowed
        await breaker.check_and_allow()
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        # Second check should fail (limit is 1)
        with pytest.raises(CircuitBreakerOpenError):
            await breaker.check_and_allow()

    async def test_force_close(self, breaker: CircuitBreaker):
        """Test force close functionality."""
        await breaker.record_trade_result(profit_cents=-1000)
        assert breaker.is_open

        await breaker.force_close()
        assert breaker.is_closed

    async def test_force_open(self, breaker: CircuitBreaker):
        """Test force open functionality."""
        assert breaker.is_closed

        await breaker.force_open("Manual test")
        assert breaker.is_open
        assert breaker.trip_reason == "Manual test"

    async def test_reset_daily_metrics(self, breaker: CircuitBreaker):
        """Test daily metrics reset."""
        await breaker.record_trade_result(profit_cents=-500)
        assert breaker.metrics.daily_loss_cents == 500

        await breaker.reset_daily_metrics()
        assert breaker.metrics.daily_loss_cents == 0

    async def test_get_status(self, breaker: CircuitBreaker):
        """Test status reporting."""
        status = breaker.get_status()

        assert "state" in status
        assert "is_open" in status
        assert "metrics" in status
        assert "limits" in status
        assert status["state"] == "closed"
        assert not status["is_open"]

    async def test_trip_callback(self, config: CircuitBreakerConfig):
        """Test trip callback is called."""
        callback_called = False
        callback_reason = None

        def on_trip(reason: str):
            nonlocal callback_called, callback_reason
            callback_called = True
            callback_reason = reason

        breaker = CircuitBreaker(config=config, on_trip=on_trip)
        await breaker.record_trade_result(profit_cents=-1000)

        assert callback_called
        assert "Daily loss" in callback_reason

    async def test_reset_callback(self, config: CircuitBreakerConfig):
        """Test reset callback is called."""
        reset_called = False

        def on_reset():
            nonlocal reset_called
            reset_called = True

        breaker = CircuitBreaker(config=config, on_reset=on_reset)

        # Trip and then recover
        await breaker.record_trade_result(profit_cents=-1000)
        breaker._trip_time = datetime.utcnow() - timedelta(seconds=10)
        await breaker.check_and_allow()  # Half-open
        await breaker.record_trade_result(profit_cents=10)  # Close

        assert reset_called

    async def test_trip_count_increments(self, breaker: CircuitBreaker):
        """Test trip count is tracked."""
        assert breaker.metrics.trip_count == 0

        await breaker.force_open("Test 1")
        assert breaker.metrics.trip_count == 1

        await breaker.force_close()
        await breaker.force_open("Test 2")
        assert breaker.metrics.trip_count == 2
