"""Orderbook state management with snapshot and delta support."""

import asyncio
from datetime import datetime
from typing import Callable, Optional

from config.logging_config import get_logger
from .models import Orderbook, OrderbookLevel, OrderSide

logger = get_logger(__name__)


class OrderbookManager:
    """Manages live orderbook state for multiple markets.

    Handles:
    - Orderbook snapshots from REST API
    - Incremental deltas from WebSocket
    - Computing implied asks from bids
    """

    def __init__(self) -> None:
        """Initialize orderbook manager."""
        self._orderbooks: dict[str, Orderbook] = {}
        self._subscribers: list[Callable[[str, Orderbook], None]] = []
        self._lock = asyncio.Lock()

    def subscribe(self, callback: Callable[[str, Orderbook], None]) -> None:
        """Subscribe to orderbook updates.

        Args:
            callback: Function called with (ticker, orderbook) on updates
        """
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, Orderbook], None]) -> None:
        """Unsubscribe from orderbook updates."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def update_snapshot(self, ticker: str, orderbook: Orderbook) -> None:
        """Update orderbook with full snapshot.

        Args:
            ticker: Market ticker
            orderbook: Full orderbook snapshot
        """
        async with self._lock:
            self._orderbooks[ticker] = orderbook
            logger.debug("Orderbook snapshot updated", ticker=ticker)
            self._notify_subscribers(ticker, orderbook)

    async def apply_delta(
        self,
        ticker: str,
        side: OrderSide,
        price: int,
        quantity: int,
    ) -> None:
        """Apply incremental orderbook delta.

        Args:
            ticker: Market ticker
            side: YES or NO side
            price: Price level in cents
            quantity: New quantity (0 = remove level)
        """
        async with self._lock:
            if ticker not in self._orderbooks:
                logger.warning("Delta for unknown orderbook", ticker=ticker)
                return

            orderbook = self._orderbooks[ticker]

            if side == OrderSide.YES:
                self._update_levels(orderbook.yes_bids, price, quantity)
            else:
                self._update_levels(orderbook.no_bids, price, quantity)

            orderbook.timestamp = datetime.utcnow()
            self._notify_subscribers(ticker, orderbook)

    def _update_levels(
        self,
        levels: list[OrderbookLevel],
        price: int,
        quantity: int,
    ) -> None:
        """Update a specific price level in bid list.

        Args:
            levels: List of orderbook levels
            price: Price to update
            quantity: New quantity (0 removes level)
        """
        # Find existing level
        for i, level in enumerate(levels):
            if level.price == price:
                if quantity == 0:
                    levels.pop(i)
                else:
                    levels[i] = OrderbookLevel(price=price, quantity=quantity)
                return

        # Add new level if quantity > 0
        if quantity > 0:
            levels.append(OrderbookLevel(price=price, quantity=quantity))
            # Keep sorted by price descending (best bid first)
            levels.sort(key=lambda x: x.price, reverse=True)

    def _notify_subscribers(self, ticker: str, orderbook: Orderbook) -> None:
        """Notify all subscribers of orderbook update."""
        for callback in self._subscribers:
            try:
                callback(ticker, orderbook)
            except Exception as e:
                logger.error("Subscriber callback error", error=str(e))

    def get_orderbook(self, ticker: str) -> Optional[Orderbook]:
        """Get current orderbook for ticker.

        Args:
            ticker: Market ticker

        Returns:
            Current orderbook or None
        """
        return self._orderbooks.get(ticker)

    def get_all_orderbooks(self) -> dict[str, Orderbook]:
        """Get all current orderbooks."""
        return dict(self._orderbooks)

    def get_best_prices(self, ticker: str) -> dict[str, Optional[int]]:
        """Get best bid/ask prices for a market.

        Args:
            ticker: Market ticker

        Returns:
            Dict with yes_bid, yes_ask, no_bid, no_ask (cents)
        """
        orderbook = self._orderbooks.get(ticker)
        if not orderbook:
            return {
                "yes_bid": None,
                "yes_ask": None,
                "no_bid": None,
                "no_ask": None,
            }

        return {
            "yes_bid": orderbook.best_yes_bid,
            "yes_ask": orderbook.best_yes_ask,
            "no_bid": orderbook.best_no_bid,
            "no_ask": orderbook.best_no_ask,
        }

    def get_acquisition_costs(
        self,
        tickers: list[str],
        side: OrderSide = OrderSide.YES,
        quantity: int = 1,
    ) -> dict[str, Optional[int]]:
        """Get acquisition costs for multiple markets.

        For multi-outcome arbitrage, this computes the cost to
        acquire YES positions across all markets in an event.

        Args:
            tickers: List of market tickers
            side: Side to acquire (YES or NO)
            quantity: Number of contracts

        Returns:
            Dict mapping ticker to cost in cents (None if no liquidity)
        """
        costs: dict[str, Optional[int]] = {}
        for ticker in tickers:
            orderbook = self._orderbooks.get(ticker)
            if orderbook:
                costs[ticker] = orderbook.get_acquisition_cost(side, quantity)
            else:
                costs[ticker] = None
        return costs

    def calculate_total_acquisition_cost(
        self,
        tickers: list[str],
        side: OrderSide = OrderSide.YES,
        quantity: int = 1,
    ) -> Optional[int]:
        """Calculate total cost to acquire positions in all markets.

        Args:
            tickers: List of market tickers
            side: Side to acquire
            quantity: Number of contracts per market

        Returns:
            Total cost in cents, or None if any market lacks liquidity
        """
        costs = self.get_acquisition_costs(tickers, side, quantity)
        if None in costs.values():
            return None
        return sum(c for c in costs.values() if c is not None)

    def clear(self, ticker: Optional[str] = None) -> None:
        """Clear orderbook data.

        Args:
            ticker: Specific ticker to clear, or None for all
        """
        if ticker:
            self._orderbooks.pop(ticker, None)
        else:
            self._orderbooks.clear()

    @property
    def tracked_tickers(self) -> list[str]:
        """Get list of tracked market tickers."""
        return list(self._orderbooks.keys())
