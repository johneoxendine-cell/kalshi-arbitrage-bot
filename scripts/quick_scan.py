#!/usr/bin/env python3
"""Quick scan of Kalshi markets - respects strict rate limits."""

import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import Settings
from src.core.authenticator import KalshiAuthenticator
from src.core.client import KalshiClient


async def quick_scan():
    """Quick scan with strict rate limiting."""
    settings = Settings()

    auth = KalshiAuthenticator(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    async with KalshiClient(
        base_url=settings.base_url,
        authenticator=auth,
    ) as client:
        print("Fetching markets (limited to 200)...")

        # Just get first 200 markets with a delay
        all_markets = []
        cursor = None

        for i in range(2):  # Only 2 pages
            await asyncio.sleep(1.5)  # Respect rate limits
            response = await client.get_markets(status="open", limit=100, cursor=cursor)
            markets = response.get("markets", [])
            all_markets.extend(markets)
            cursor = response.get("cursor")
            print(f"  Fetched {len(all_markets)} markets...")
            if not cursor:
                break

        print(f"\nAnalyzing {len(all_markets)} markets...")

        # Group by event
        events = {}
        for m in all_markets:
            event_ticker = m.get("event_ticker", "")
            if event_ticker:
                events.setdefault(event_ticker, []).append(m)

        # Find multi-outcome events with prices
        print("\n" + "="*70)
        print("MULTI-OUTCOME EVENTS (potential arbitrage)")
        print("="*70)

        arb_found = False
        for event_ticker, markets in sorted(events.items(), key=lambda x: -len(x[1])):
            if len(markets) < 2:
                continue

            # Check prices
            priced_markets = [m for m in markets if m.get("yes_ask")]
            if len(priced_markets) < 2:
                continue

            total = sum(m.get("yes_ask", 0) for m in priced_markets)

            print(f"\n{event_ticker} ({len(priced_markets)} priced markets)")
            for m in priced_markets[:5]:
                title = m.get("title", "")[:40]
                ya = m.get("yes_ask", 0)
                yb = m.get("yes_bid", 0)
                print(f"  {ya:2d}¢/{yb:2d}¢  {title}")

            if len(priced_markets) > 5:
                print(f"  ... +{len(priced_markets)-5} more")

            if len(priced_markets) == len(markets):
                profit = 100 - total
                if profit > 0:
                    print(f"  >>> TOTAL: {total}¢ - ARBITRAGE: {profit}¢ profit! <<<")
                    arb_found = True
                else:
                    print(f"  TOTAL: {total}¢ (no arb)")

        if not arb_found:
            print("\n" + "="*70)
            print("No immediate arbitrage found - markets are efficient.")
            print("Real arbitrage requires continuous monitoring for brief mispricings.")
            print("="*70)


if __name__ == "__main__":
    asyncio.run(quick_scan())
