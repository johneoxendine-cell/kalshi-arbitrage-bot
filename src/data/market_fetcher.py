"""REST API market data fetching."""

from collections import defaultdict
from datetime import datetime
from typing import Optional

from config.logging_config import get_logger
from src.core.client import KalshiClient
from .models import Market, Orderbook, OrderbookLevel

logger = get_logger(__name__)


class MarketFetcher:
    """Fetches and manages market data from Kalshi REST API."""

    def __init__(self, client: KalshiClient) -> None:
        """Initialize market fetcher.

        Args:
            client: Kalshi API client
        """
        self.client = client
        self._market_cache: dict[str, Market] = {}
        self._event_markets: dict[str, list[str]] = defaultdict(list)

    async def get_market(self, ticker: str, use_cache: bool = True) -> Market:
        """Get market by ticker.

        Args:
            ticker: Market ticker
            use_cache: Whether to use cached data

        Returns:
            Market object
        """
        if use_cache and ticker in self._market_cache:
            return self._market_cache[ticker]

        response = await self.client.get_market(ticker)
        market = self._parse_market(response.get("market", response))
        self._market_cache[ticker] = market
        return market

    async def get_markets_by_event(
        self,
        event_ticker: str,
        status: str = "open",
    ) -> list[Market]:
        """Get all markets for an event.

        Args:
            event_ticker: Event ticker
            status: Market status filter

        Returns:
            List of markets for the event
        """
        markets: list[Market] = []
        cursor: Optional[str] = None

        while True:
            response = await self.client.get_markets(
                event_ticker=event_ticker,
                status=status,
                cursor=cursor,
            )

            for market_data in response.get("markets", []):
                market = self._parse_market(market_data)
                markets.append(market)
                self._market_cache[market.ticker] = market

            cursor = response.get("cursor")
            if not cursor:
                break

        # Update event-to-markets mapping
        self._event_markets[event_ticker] = [m.ticker for m in markets]

        logger.info(
            "Fetched markets for event",
            event_ticker=event_ticker,
            market_count=len(markets),
        )

        return markets

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        """Get orderbook for a market.

        Args:
            ticker: Market ticker
            depth: Number of price levels

        Returns:
            Orderbook object
        """
        response = await self.client.get_orderbook(ticker, depth)
        return self._parse_orderbook(ticker, response.get("orderbook", response))

    async def get_orderbooks_for_event(
        self,
        event_ticker: str,
        depth: int = 10,
    ) -> dict[str, Orderbook]:
        """Get orderbooks for all markets in an event.

        Args:
            event_ticker: Event ticker
            depth: Number of price levels

        Returns:
            Dict mapping ticker to orderbook
        """
        # Ensure we have markets for this event
        if event_ticker not in self._event_markets:
            await self.get_markets_by_event(event_ticker)

        orderbooks: dict[str, Orderbook] = {}
        for ticker in self._event_markets.get(event_ticker, []):
            try:
                orderbook = await self.get_orderbook(ticker, depth)
                orderbooks[ticker] = orderbook
            except Exception as e:
                logger.warning(
                    "Failed to fetch orderbook",
                    ticker=ticker,
                    error=str(e),
                )

        return orderbooks

    async def search_events(
        self,
        query: Optional[str] = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[dict]:
        """Search for events.

        Args:
            query: Search query
            status: Event status filter
            limit: Max results

        Returns:
            List of event data dicts
        """
        params = {"status": status, "limit": limit}
        if query:
            params["query"] = query

        response = await self.client.get("/events", params=params)
        return response.get("events", [])

    def get_cached_markets_for_event(self, event_ticker: str) -> list[Market]:
        """Get cached markets for an event.

        Args:
            event_ticker: Event ticker

        Returns:
            List of cached markets
        """
        tickers = self._event_markets.get(event_ticker, [])
        return [self._market_cache[t] for t in tickers if t in self._market_cache]

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._market_cache.clear()
        self._event_markets.clear()
        logger.info("Market cache cleared")

    def _parse_market(self, data: dict) -> Market:
        """Parse market data from API response."""
        return Market(
            ticker=data["ticker"],
            event_ticker=data.get("event_ticker", ""),
            title=data.get("title", ""),
            subtitle=data.get("subtitle", ""),
            status=data.get("status", "open"),
            yes_bid=data.get("yes_bid"),
            yes_ask=data.get("yes_ask"),
            no_bid=data.get("no_bid"),
            no_ask=data.get("no_ask"),
            volume=data.get("volume", 0),
            open_interest=data.get("open_interest", 0),
            close_time=self._parse_datetime(data.get("close_time")),
            expiration_time=self._parse_datetime(data.get("expiration_time")),
        )

    def _parse_orderbook(self, ticker: str, data: dict) -> Orderbook:
        """Parse orderbook data from API response."""
        yes_bids = [
            OrderbookLevel(price=level[0], quantity=level[1])
            for level in data.get("yes", [])
        ]
        no_bids = [
            OrderbookLevel(price=level[0], quantity=level[1])
            for level in data.get("no", [])
        ]

        return Orderbook(
            ticker=ticker,
            yes_bids=yes_bids,
            no_bids=no_bids,
            timestamp=datetime.utcnow(),
        )

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Parse datetime string from API."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
