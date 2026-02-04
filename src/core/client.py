"""Async HTTP client for Kalshi API with retry logic and rate limiting."""

import asyncio
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp

from config.logging_config import get_logger
from .authenticator import KalshiAuthenticator
from .exceptions import (
    AuthenticationError,
    InsufficientFundsError,
    KalshiError,
    OrderError,
    RateLimitError,
)
from .rate_limiter import DualRateLimiter

logger = get_logger(__name__)


class KalshiClient:
    """Async HTTP client for Kalshi API.

    Features:
    - RSA-PSS authentication
    - Rate limiting (read/write)
    - Exponential backoff retry
    - Automatic error handling
    """

    DEFAULT_TIMEOUT = 30.0
    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0
    BACKOFF_MAX = 60.0

    def __init__(
        self,
        base_url: str,
        authenticator: KalshiAuthenticator,
        rate_limiter: Optional[DualRateLimiter] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the client.

        Args:
            base_url: Kalshi API base URL
            authenticator: Authentication handler
            rate_limiter: Optional rate limiter (creates default if not provided)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.authenticator = authenticator
        self.rate_limiter = rate_limiter or DualRateLimiter()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

        logger.info("Kalshi client initialized", base_url=base_url)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.info("HTTP session closed")

    async def __aenter__(self) -> "KalshiClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def _build_url(self, path: str) -> str:
        """Build full URL from path."""
        if path.startswith("/"):
            path = path[1:]
        return urljoin(self.base_url + "/", path)

    async def _handle_response(
        self,
        response: aiohttp.ClientResponse,
        method: str,
        path: str,
    ) -> dict[str, Any]:
        """Handle API response and raise appropriate errors.

        Args:
            response: HTTP response
            method: HTTP method used
            path: Request path

        Returns:
            Response JSON data

        Raises:
            Various KalshiError subclasses based on response
        """
        # Try to parse JSON response
        try:
            data = await response.json()
        except Exception:
            data = {"raw": await response.text()}

        if response.status == 200:
            return data

        error_msg = data.get("error", {}).get("message", str(data))

        if response.status == 401:
            raise AuthenticationError(f"Authentication failed: {error_msg}", data)

        if response.status == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                f"Rate limit exceeded: {error_msg}",
                retry_after=float(retry_after) if retry_after else None,
                details=data,
            )

        if response.status == 400:
            if "insufficient" in error_msg.lower():
                raise InsufficientFundsError(error_msg, data)
            raise OrderError(f"Bad request: {error_msg}", data)

        if response.status == 403:
            raise AuthenticationError(f"Forbidden: {error_msg}", data)

        if response.status == 404:
            raise KalshiError(f"Not found: {error_msg}", data)

        raise KalshiError(
            f"API error ({response.status}): {error_msg}",
            {"status": response.status, **data},
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        """Make an authenticated API request.

        Args:
            method: HTTP method
            path: API path
            params: Query parameters
            json_data: JSON body data
            retry: Whether to retry on failure

        Returns:
            Response JSON data
        """
        url = self._build_url(path)
        limiter = self.rate_limiter.get_limiter(method)
        session = await self._get_session()

        last_error: Optional[Exception] = None
        retries = self.MAX_RETRIES if retry else 1

        for attempt in range(retries):
            try:
                # Wait for rate limit
                await limiter.acquire()

                # Generate auth headers (path without query params)
                auth_headers = self.authenticator.get_auth_headers(method, path)
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    **auth_headers,
                }

                logger.debug(
                    "Making API request",
                    method=method,
                    url=url,
                    attempt=attempt + 1,
                )

                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_data,
                ) as response:
                    return await self._handle_response(response, method, path)

            except RateLimitError as e:
                # Use server-provided retry-after or exponential backoff
                wait_time = e.retry_after or min(
                    self.BACKOFF_BASE ** attempt,
                    self.BACKOFF_MAX,
                )
                logger.warning(
                    "Rate limited, waiting",
                    wait_seconds=wait_time,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(wait_time)
                last_error = e

            except (AuthenticationError, InsufficientFundsError):
                # Don't retry auth or funds errors
                raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Network errors - retry with backoff
                wait_time = min(self.BACKOFF_BASE ** attempt, self.BACKOFF_MAX)
                logger.warning(
                    "Request failed, retrying",
                    error=str(e),
                    wait_seconds=wait_time,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(wait_time)
                last_error = e

            except KalshiError:
                # API errors - don't retry by default
                raise

        raise KalshiError(f"Request failed after {retries} attempts: {last_error}")

    # Convenience methods

    async def get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Make GET request."""
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Make POST request."""
        return await self._request("POST", path, json_data=data)

    async def delete(
        self,
        path: str,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Make DELETE request."""
        return await self._request("DELETE", path, json_data=data)

    # API Endpoints

    async def get_balance(self) -> dict[str, Any]:
        """Get account balance."""
        return await self.get("/portfolio/balance")

    async def get_markets(
        self,
        event_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get markets list.

        Args:
            event_ticker: Filter by event ticker
            status: Market status filter (open, closed, settled)
            limit: Max results per page
            cursor: Pagination cursor

        Returns:
            Markets response with 'markets' list and 'cursor'
        """
        params: dict[str, Any] = {"status": status, "limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        return await self.get("/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """Get single market details."""
        return await self.get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """Get market orderbook.

        Args:
            ticker: Market ticker
            depth: Number of price levels

        Returns:
            Orderbook with 'yes' and 'no' bids
        """
        return await self.get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_event(self, event_ticker: str) -> dict[str, Any]:
        """Get event details."""
        return await self.get(f"/events/{event_ticker}")

    async def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        expiration_ts: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new order.

        Args:
            ticker: Market ticker
            side: 'yes' or 'no'
            action: 'buy' or 'sell'
            count: Number of contracts
            type: Order type ('limit', 'market')
            yes_price: Price in cents (1-99) for yes side
            no_price: Price in cents (1-99) for no side
            expiration_ts: Order expiration timestamp
            client_order_id: Client-provided order ID

        Returns:
            Order response
        """
        data: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": type,
        }
        if yes_price is not None:
            data["yes_price"] = yes_price
        if no_price is not None:
            data["no_price"] = no_price
        if expiration_ts is not None:
            data["expiration_ts"] = expiration_ts
        if client_order_id:
            data["client_order_id"] = client_order_id

        return await self.post("/portfolio/orders", data=data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order."""
        return await self.delete(f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        ticker: Optional[str] = None,
        status: str = "resting",
    ) -> dict[str, Any]:
        """Get orders list."""
        params: dict[str, Any] = {"status": status}
        if ticker:
            params["ticker"] = ticker
        return await self.get("/portfolio/orders", params=params)

    async def get_positions(self) -> dict[str, Any]:
        """Get current positions."""
        return await self.get("/portfolio/positions")

    async def get_fills(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Get trade fills."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self.get("/portfolio/fills", params=params)
