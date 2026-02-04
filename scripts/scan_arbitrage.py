#!/usr/bin/env python3
"""Scan Kalshi markets for arbitrage opportunities."""

import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import Settings
from config.logging_config import configure_logging, get_logger
from src.core.authenticator import KalshiAuthenticator
from src.core.client import KalshiClient
from src.data.market_fetcher import MarketFetcher
from src.data.orderbook_manager import OrderbookManager
from src.arbitrage.detector import ArbitrageDetector


async def scan_for_arbitrage():
    """Scan markets for arbitrage opportunities."""
    configure_logging(log_level="INFO", log_format="console")
    logger = get_logger(__name__)

    settings = Settings()

    auth = KalshiAuthenticator(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    async with KalshiClient(
        base_url=settings.base_url,
        authenticator=auth,
    ) as client:
        fetcher = MarketFetcher(client)
        orderbook_mgr = OrderbookManager()
        detector = ArbitrageDetector(min_profit_cents=1)  # Low threshold to find more

        # Fetch all open markets
        print("Fetching all open markets...")
        all_markets = []
        cursor = None

        while True:
            response = await client.get_markets(status="open", limit=100, cursor=cursor)
            markets = response.get("markets", [])
            all_markets.extend(markets)
            cursor = response.get("cursor")
            if not cursor or len(markets) < 100:
                break

        print(f"Found {len(all_markets)} total open markets")

        # Group by event
        events = {}
        for m in all_markets:
            event_ticker = m.get("event_ticker", "")
            if event_ticker:
                events.setdefault(event_ticker, []).append(m)

        # Filter to multi-outcome events with prices
        candidates = {}
        for event_ticker, markets in events.items():
            if len(markets) >= 2:
                # Check if any have actual prices
                has_prices = any(m.get("yes_ask") for m in markets)
                if has_prices:
                    candidates[event_ticker] = markets

        print(f"Found {len(candidates)} events with 2+ markets and liquidity")
        print("\n" + "="*70)

        # Analyze each candidate event
        opportunities_found = []

        for event_ticker, markets in sorted(candidates.items(), key=lambda x: -len(x[1]))[:20]:
            # Calculate sum of YES asks
            total_yes_ask = 0
            market_details = []
            all_have_prices = True

            for m in markets:
                yes_ask = m.get("yes_ask")
                if yes_ask and yes_ask > 0:
                    total_yes_ask += yes_ask
                    market_details.append({
                        "ticker": m.get("ticker"),
                        "title": m.get("title", "")[:45],
                        "yes_ask": yes_ask,
                        "yes_bid": m.get("yes_bid", 0),
                    })
                else:
                    all_have_prices = False

            if not all_have_prices or not market_details:
                continue

            # Check for arbitrage
            profit = 100 - total_yes_ask
            is_arb = profit > 0

            print(f"\nEvent: {event_ticker}")
            print(f"Markets: {len(markets)}, With prices: {len(market_details)}")

            for md in market_details[:8]:
                prefix = "  "
                print(f"{prefix}{md['yes_ask']:2d}¢ ask | {md['yes_bid']:2d}¢ bid | {md['title']}")

            if len(market_details) > 8:
                print(f"  ... and {len(market_details) - 8} more markets")

            if len(market_details) == len(markets):
                print(f"  TOTAL YES ASK: {total_yes_ask}¢", end="")
                if is_arb:
                    print(f" *** ARBITRAGE: {profit}¢ profit ***")
                    opportunities_found.append({
                        "event": event_ticker,
                        "profit": profit,
                        "markets": len(markets),
                        "total_cost": total_yes_ask,
                    })
                else:
                    print(f" (no arb, {-profit}¢ over)")
            else:
                print(f"  Partial prices: {total_yes_ask}¢ for {len(market_details)}/{len(markets)} markets")

        print("\n" + "="*70)
        print("ARBITRAGE SUMMARY")
        print("="*70)

        if opportunities_found:
            print(f"\nFound {len(opportunities_found)} arbitrage opportunities:\n")
            for opp in sorted(opportunities_found, key=lambda x: -x["profit"]):
                print(f"  {opp['event']}")
                print(f"    Profit: {opp['profit']}¢ | Cost: {opp['total_cost']}¢ | Markets: {opp['markets']}")
        else:
            print("\nNo arbitrage opportunities found at this time.")
            print("This is expected - markets are generally efficient.")
            print("The bot monitors continuously to catch brief mispricings.")


if __name__ == "__main__":
    asyncio.run(scan_for_arbitrage())
