#!/usr/bin/env python3
"""Test Kalshi API connection and fetch sample market data."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import Settings
from config.logging_config import configure_logging, get_logger
from src.core.authenticator import KalshiAuthenticator
from src.core.client import KalshiClient


async def test_connection():
    """Test API connection and fetch markets."""
    configure_logging(log_level="INFO", log_format="console")
    logger = get_logger(__name__)

    try:
        settings = Settings()
        logger.info(f"Environment: {settings.environment.value}")
        logger.info(f"Base URL: {settings.base_url}")

        # Create authenticator and client
        auth = KalshiAuthenticator(
            api_key_id=settings.kalshi_api_key_id,
            private_key_path=settings.kalshi_private_key_path,
        )

        async with KalshiClient(
            base_url=settings.base_url,
            authenticator=auth,
        ) as client:
            # Test 1: Get markets
            logger.info("Fetching open markets...")
            response = await client.get_markets(status="open", limit=10)
            markets = response.get("markets", [])

            print(f"\n{'='*60}")
            print(f"Found {len(markets)} markets (showing first 10)")
            print('='*60)

            for market in markets[:10]:
                ticker = market.get("ticker", "")
                title = market.get("title", "")[:50]
                yes_bid = market.get("yes_bid", "-")
                yes_ask = market.get("yes_ask", "-")
                print(f"\n{ticker}")
                print(f"  {title}...")
                print(f"  YES: {yes_bid}¢ / {yes_ask}¢")

            # Test 2: Get a specific orderbook
            if markets:
                test_ticker = markets[0].get("ticker")
                logger.info(f"\nFetching orderbook for {test_ticker}...")
                orderbook = await client.get_orderbook(test_ticker, depth=5)
                ob_data = orderbook.get("orderbook", orderbook)

                print(f"\n{'='*60}")
                print(f"Orderbook: {test_ticker}")
                print('='*60)
                print(f"YES bids: {ob_data.get('yes', [])}")
                print(f"NO bids: {ob_data.get('no', [])}")

            # Test 3: Find events with multiple markets (arbitrage candidates)
            logger.info("\nLooking for multi-outcome events...")

            # Group markets by event
            events = {}
            all_markets = await client.get_markets(status="open", limit=100)
            for m in all_markets.get("markets", []):
                event_ticker = m.get("event_ticker", "")
                if event_ticker:
                    events.setdefault(event_ticker, []).append(m)

            # Find events with 3+ markets
            multi_outcome = {k: v for k, v in events.items() if len(v) >= 3}

            print(f"\n{'='*60}")
            print(f"Multi-outcome events (3+ markets) - Arbitrage candidates")
            print('='*60)

            for event_ticker, event_markets in list(multi_outcome.items())[:5]:
                print(f"\n{event_ticker} ({len(event_markets)} markets)")
                total_yes_ask = 0
                for m in event_markets[:6]:
                    yes_ask = m.get("yes_ask")
                    title = m.get("title", "")[:40]
                    if yes_ask:
                        total_yes_ask += yes_ask
                        print(f"  {yes_ask:2d}¢ - {title}")
                    else:
                        print(f"  --¢ - {title}")
                if len(event_markets) > 6:
                    print(f"  ... and {len(event_markets) - 6} more")
                if total_yes_ask > 0:
                    print(f"  TOTAL: {total_yes_ask}¢ (arb if < 100¢)")

            print(f"\n{'='*60}")
            print("Connection test successful!")
            print('='*60)

    except Exception as e:
        logger.error(f"Connection test failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(test_connection())
