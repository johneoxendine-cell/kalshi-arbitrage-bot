"""Token bucket rate limiter for API request throttling."""

import asyncio
import time
from typing import Optional

from config.logging_config import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """Token bucket rate limiter for controlling API request rates.

    Implements the token bucket algorithm:
    - Tokens are added at a fixed rate (tokens_per_second)
    - Each request consumes one token
    - Requests wait if no tokens are available
    - Maximum tokens capped at bucket capacity
    """

    def __init__(
        self,
        tokens_per_second: float,
        bucket_size: Optional[int] = None,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            tokens_per_second: Rate at which tokens are replenished
            bucket_size: Maximum tokens in bucket (defaults to tokens_per_second)
        """
        self.tokens_per_second = tokens_per_second
        self.bucket_size = bucket_size or int(tokens_per_second)
        self._tokens = float(self.bucket_size)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

        logger.info(
            "Rate limiter initialized",
            tokens_per_second=tokens_per_second,
            bucket_size=self.bucket_size,
        )

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(
            self.bucket_size,
            self._tokens + elapsed * self.tokens_per_second,
        )
        self._last_update = now

    async def acquire(self, tokens: int = 1) -> float:
        """Acquire tokens, waiting if necessary.

        Args:
            tokens: Number of tokens to acquire

        Returns:
            Time waited in seconds
        """
        async with self._lock:
            self._refill()

            wait_time = 0.0
            if self._tokens < tokens:
                # Calculate wait time
                deficit = tokens - self._tokens
                wait_time = deficit / self.tokens_per_second
                logger.debug("Rate limit wait", wait_seconds=wait_time)
                await asyncio.sleep(wait_time)
                self._refill()

            self._tokens -= tokens
            return wait_time

    @property
    def available_tokens(self) -> float:
        """Get current number of available tokens."""
        self._refill()
        return self._tokens


class DualRateLimiter:
    """Separate rate limiters for read and write operations.

    Kalshi API has different rate limits:
    - Basic tier: 20 read/sec, 10 write/sec
    - Higher tiers have increased limits
    """

    def __init__(
        self,
        read_rate: float = 20.0,
        write_rate: float = 10.0,
    ) -> None:
        """Initialize dual rate limiter.

        Args:
            read_rate: Read requests per second
            write_rate: Write requests per second
        """
        self.read_limiter = RateLimiter(read_rate)
        self.write_limiter = RateLimiter(write_rate)

    async def acquire_read(self) -> float:
        """Acquire a read token."""
        return await self.read_limiter.acquire()

    async def acquire_write(self) -> float:
        """Acquire a write token."""
        return await self.write_limiter.acquire()

    def get_limiter(self, method: str) -> RateLimiter:
        """Get the appropriate limiter based on HTTP method.

        Args:
            method: HTTP method

        Returns:
            Appropriate rate limiter
        """
        if method.upper() in ("GET", "HEAD", "OPTIONS"):
            return self.read_limiter
        return self.write_limiter
