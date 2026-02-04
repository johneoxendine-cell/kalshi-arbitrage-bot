#!/usr/bin/env python3
"""Find interesting events on Kalshi with actual liquidity."""

import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import Settings
from src.core.authenticator import KalshiAuthenticator
from src.core.client import KalshiClient


async def find_events():
    """Search for events with liquidity."""
    settings = Settings()

    auth = KalshiAuthenticator(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    async with KalshiClient(
        base_url=settings.base_url,
        authenticator=auth,
    ) as client:
        # Search for various event types
        searches = [
            "president",
            "fed",
            "inflation",
            "gdp",
            "unemployment",
            "trump",
            "election",
            "congress",
            "senate",
            "bitcoin",
            "stock",
            "weather",
        ]

        all_events = {}

        for query in searches:
            await asyncio.sleep(1.5)  # Rate limit
            print(f"Searching: {query}...")

            try:
                response = await client.get("/events", params={
                    "status": "open",
                    "limit": 20,
                })

                for event in response.get("events", []):
                    ticker = event.get("event_ticker", "")
                    if ticker and ticker not in all_events:
                        all_events[ticker] = event
            except Exception as e:
                print(f"  Error: {e}")

        print(f"\nFound {len(all_events)} unique events")
        print("\n" + "="*70)

        # Get details on each event
        for ticker, event in list(all_events.items())[:15]:
            title = event.get("title", "")[:60]
            category = event.get("category", "")
            market_count = event.get("mutually_exclusive", False)

            print(f"\n{ticker}")
            print(f"  {title}")
            print(f"  Category: {category}, Mutually Exclusive: {market_count}")

            # Get markets for this event
            await asyncio.sleep(1.5)
            try:
                markets_resp = await client.get_markets(
                    event_ticker=ticker,
                    status="open",
                    limit=20,
                )
                markets = markets_resp.get("markets", [])

                if markets:
                    print(f"  Markets: {len(markets)}")
                    total_ask = 0
                    has_all_prices = True

                    for m in markets[:8]:
                        ya = m.get("yes_ask")
                        yb = m.get("yes_bid")
                        vol = m.get("volume", 0)
                        subtitle = m.get("subtitle", m.get("title", ""))[:35]

                        if ya:
                            total_ask += ya
                            print(f"    {ya:2d}¢/{yb or 0:2d}¢ vol:{vol:5d}  {subtitle}")
                        else:
                            has_all_prices = False
                            print(f"    --¢/--¢ vol:{vol:5d}  {subtitle}")

                    if len(markets) > 8:
                        print(f"    ... +{len(markets)-8} more markets")

                    if has_all_prices and len(markets) >= 2:
                        profit = 100 - total_ask
                        status = f"ARBITRAGE {profit}¢!" if profit > 0 else f"no arb ({-profit}¢ over)"
                        print(f"  TOTAL: {total_ask}¢ - {status}")

            except Exception as e:
                print(f"  Error fetching markets: {e}")

        print("\n" + "="*70)


if __name__ == "__main__":
    asyncio.run(find_events())
