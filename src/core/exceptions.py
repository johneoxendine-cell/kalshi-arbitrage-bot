"""Custom exception hierarchy for Kalshi trading bot."""

from typing import Any, Optional


class KalshiError(Exception):
    """Base exception for all Kalshi-related errors."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthenticationError(KalshiError):
    """Raised when authentication fails."""

    pass


class RateLimitError(KalshiError):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[float] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after


class OrderError(KalshiError):
    """Raised when order operations fail."""

    pass


class InsufficientFundsError(OrderError):
    """Raised when account has insufficient funds."""

    pass


class MarketClosedError(OrderError):
    """Raised when trying to trade on a closed market."""

    pass


class InvalidOrderError(OrderError):
    """Raised when order parameters are invalid."""

    pass


class WebSocketError(KalshiError):
    """Raised when WebSocket operations fail."""

    pass


class CircuitBreakerOpenError(KalshiError):
    """Raised when circuit breaker is open and trading is halted."""

    def __init__(
        self,
        message: str = "Circuit breaker is open",
        cooldown_remaining: Optional[float] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, details)
        self.cooldown_remaining = cooldown_remaining


class ConfigurationError(KalshiError):
    """Raised when configuration is invalid."""

    pass
