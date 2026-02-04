#!/usr/bin/env python3
"""Main entrypoint for the Kalshi Arbitrage Bot."""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.logging_config import configure_logging, get_logger
from config.settings import Settings, Environment


async def run_bot(
    event_tickers: list[str],
    paper_trading: bool = False,
) -> None:
    """Run the arbitrage bot.

    Args:
        event_tickers: List of event tickers to watch
        paper_trading: If True, log trades but don't execute
    """
    # Load settings
    settings = Settings()

    # Configure logging
    configure_logging(
        log_level=settings.log_level,
        log_format=settings.log_format,
    )

    logger = get_logger(__name__)
    logger.info(
        "Starting Kalshi Arbitrage Bot",
        environment=settings.environment.value,
        paper_trading=paper_trading,
        events=event_tickers,
    )

    # Import here to avoid circular imports
    from src.bot.engine import ArbitrageBotEngine

    # Create and start engine
    engine = ArbitrageBotEngine(settings)

    try:
        # Start the engine in background
        engine_task = asyncio.create_task(engine.start())

        # Wait for initial connection
        await asyncio.sleep(2)

        # Watch specified events
        for event_ticker in event_tickers:
            await engine.watch_event(event_ticker)

        # Wait for engine to complete (shutdown)
        await engine_task

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.exception("Fatal error", error=str(e))
        raise
    finally:
        await engine.stop()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Watch a specific event
  python run_bot.py --events PRES-2024

  # Watch multiple events
  python run_bot.py --events PRES-2024 FED-RATE-2024

  # Paper trading mode (no real trades)
  python run_bot.py --events PRES-2024 --paper

  # Use demo environment
  python run_bot.py --events PRES-2024 --demo
        """,
    )

    parser.add_argument(
        "--events",
        "-e",
        nargs="+",
        required=True,
        help="Event tickers to watch for arbitrage",
    )

    parser.add_argument(
        "--paper",
        "-p",
        action="store_true",
        help="Paper trading mode (log but don't execute)",
    )

    parser.add_argument(
        "--demo",
        "-d",
        action="store_true",
        help="Use demo environment",
    )

    parser.add_argument(
        "--log-level",
        "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level",
    )

    args = parser.parse_args()

    # Override environment if demo flag set
    if args.demo:
        import os
        os.environ["ENVIRONMENT"] = "development"

    # Override log level
    import os
    os.environ["LOG_LEVEL"] = args.log_level

    # Run the bot
    asyncio.run(run_bot(args.events, args.paper))


if __name__ == "__main__":
    main()
