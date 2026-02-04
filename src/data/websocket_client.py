"""WebSocket client for real-time Kalshi market data."""

import asyncio
import json
from typing import Any, Callable, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from config.logging_config import get_logger
from src.core.authenticator import KalshiAuthenticator
from src.core.exceptions import WebSocketError
from .models import OrderSide
from .orderbook_manager import OrderbookManager

logger = get_logger(__name__)


class KalshiWebSocketClient:
    """WebSocket client for Kalshi real-time data.

    Connects to Kalshi WebSocket API for:
    - Orderbook delta updates
    - Trade notifications
    - Market status changes
    """

    RECONNECT_DELAY_BASE = 1.0
    RECONNECT_DELAY_MAX = 60.0
    PING_INTERVAL = 30.0
    PING_TIMEOUT = 10.0

    def __init__(
        self,
        url: str,
        authenticator: KalshiAuthenticator,
        orderbook_manager: OrderbookManager,
    ) -> None:
        """Initialize WebSocket client.

        Args:
            url: WebSocket URL
            authenticator: Authentication handler
            orderbook_manager: Orderbook state manager
        """
        self.url = url
        self.authenticator = authenticator
        self.orderbook_manager = orderbook_manager

        self._ws: Optional[WebSocketClientProtocol] = None
        self._subscribed_tickers: set[str] = set()
        self._running = False
        self._reconnect_count = 0
        self._message_handlers: dict[str, Callable[[dict], None]] = {}

        # Register default handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register message type handlers."""
        self._message_handlers = {
            "orderbook_snapshot": self._handle_orderbook_snapshot,
            "orderbook_delta": self._handle_orderbook_delta,
            "trade": self._handle_trade,
            "subscribed": self._handle_subscribed,
            "unsubscribed": self._handle_unsubscribed,
            "error": self._handle_error,
        }

    async def connect(self) -> None:
        """Connect to WebSocket with authentication."""
        if self._ws and not self._ws.closed:
            return

        # Generate auth headers for WebSocket connection
        auth_headers = self.authenticator.get_auth_headers("GET", "/ws")

        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers=auth_headers,
                ping_interval=self.PING_INTERVAL,
                ping_timeout=self.PING_TIMEOUT,
            )
            self._reconnect_count = 0
            logger.info("WebSocket connected", url=self.url)

            # Resubscribe to previous tickers
            if self._subscribed_tickers:
                await self._resubscribe()

        except Exception as e:
            logger.error("WebSocket connection failed", error=str(e))
            raise WebSocketError(f"Connection failed: {e}")

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("WebSocket disconnected")

    async def subscribe_orderbook(self, tickers: list[str]) -> None:
        """Subscribe to orderbook updates for markets.

        Args:
            tickers: List of market tickers
        """
        if not self._ws or self._ws.closed:
            raise WebSocketError("WebSocket not connected")

        message = {
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }

        await self._ws.send(json.dumps(message))
        self._subscribed_tickers.update(tickers)
        logger.info("Subscribed to orderbooks", tickers=tickers)

    async def unsubscribe_orderbook(self, tickers: list[str]) -> None:
        """Unsubscribe from orderbook updates.

        Args:
            tickers: List of market tickers
        """
        if not self._ws or self._ws.closed:
            return

        message = {
            "id": 2,
            "cmd": "unsubscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }

        await self._ws.send(json.dumps(message))
        self._subscribed_tickers.difference_update(tickers)
        logger.info("Unsubscribed from orderbooks", tickers=tickers)

    async def run(self) -> None:
        """Run WebSocket message loop with auto-reconnect."""
        self._running = True

        while self._running:
            try:
                await self.connect()
                await self._message_loop()

            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket connection closed", code=e.code)
                if self._running:
                    await self._reconnect()

            except Exception as e:
                logger.error("WebSocket error", error=str(e))
                if self._running:
                    await self._reconnect()

    async def _message_loop(self) -> None:
        """Process incoming WebSocket messages."""
        if not self._ws:
            return

        async for message in self._ws:
            try:
                data = json.loads(message)
                await self._process_message(data)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON message", message=message[:100])
            except Exception as e:
                logger.error("Message processing error", error=str(e))

    async def _process_message(self, data: dict[str, Any]) -> None:
        """Process a parsed WebSocket message.

        Args:
            data: Parsed message data
        """
        msg_type = data.get("type")
        if not msg_type:
            logger.debug("Message without type", data=data)
            return

        handler = self._message_handlers.get(msg_type)
        if handler:
            handler(data)
        else:
            logger.debug("Unhandled message type", type=msg_type)

    def _handle_orderbook_snapshot(self, data: dict) -> None:
        """Handle orderbook snapshot message."""
        ticker = data.get("market_ticker")
        if not ticker:
            return

        from .models import Orderbook, OrderbookLevel

        yes_bids = [
            OrderbookLevel(price=level[0], quantity=level[1])
            for level in data.get("yes", [])
        ]
        no_bids = [
            OrderbookLevel(price=level[0], quantity=level[1])
            for level in data.get("no", [])
        ]

        orderbook = Orderbook(ticker=ticker, yes_bids=yes_bids, no_bids=no_bids)

        # Update synchronously (manager handles async internally if needed)
        asyncio.create_task(self.orderbook_manager.update_snapshot(ticker, orderbook))

    def _handle_orderbook_delta(self, data: dict) -> None:
        """Handle orderbook delta message."""
        ticker = data.get("market_ticker")
        if not ticker:
            return

        # Process delta updates
        for delta in data.get("deltas", []):
            side = OrderSide.YES if delta.get("side") == "yes" else OrderSide.NO
            price = delta.get("price", 0)
            quantity = delta.get("delta", 0)

            asyncio.create_task(
                self.orderbook_manager.apply_delta(ticker, side, price, quantity)
            )

    def _handle_trade(self, data: dict) -> None:
        """Handle trade notification."""
        logger.debug(
            "Trade received",
            ticker=data.get("market_ticker"),
            price=data.get("price"),
            count=data.get("count"),
        )

    def _handle_subscribed(self, data: dict) -> None:
        """Handle subscription confirmation."""
        logger.info("Subscription confirmed", data=data)

    def _handle_unsubscribed(self, data: dict) -> None:
        """Handle unsubscription confirmation."""
        logger.info("Unsubscription confirmed", data=data)

    def _handle_error(self, data: dict) -> None:
        """Handle error message."""
        logger.error("WebSocket error message", error=data.get("error"))

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        self._reconnect_count += 1
        delay = min(
            self.RECONNECT_DELAY_BASE * (2 ** self._reconnect_count),
            self.RECONNECT_DELAY_MAX,
        )
        logger.info(
            "Reconnecting",
            attempt=self._reconnect_count,
            delay_seconds=delay,
        )
        await asyncio.sleep(delay)

    async def _resubscribe(self) -> None:
        """Resubscribe to previously subscribed tickers."""
        if self._subscribed_tickers:
            tickers = list(self._subscribed_tickers)
            self._subscribed_tickers.clear()  # Clear to let subscribe_orderbook re-add
            await self.subscribe_orderbook(tickers)

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._ws is not None and not self._ws.closed

    @property
    def subscribed_tickers(self) -> set[str]:
        """Get set of subscribed tickers."""
        return self._subscribed_tickers.copy()
