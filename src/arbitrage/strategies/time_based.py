"""Time-based arbitrage strategy.

Detects opportunities when markets with the same underlying but
different expirations violate temporal pricing constraints.
"""

import uuid
from datetime import datetime
from typing import Optional

from config.logging_config import get_logger
from src.arbitrage.calculator import ProfitCalculator
from src.data.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ArbitrageType,
    Market,
    Orderbook,
    OrderAction,
    OrderSide,
)

logger = get_logger(__name__)


class TimeBasedStrategy:
    """Time-based arbitrage strategy.

    For markets on the same underlying with different expirations,
    earlier expirations should be priced <= later expirations.

    Example constraint violations:
        - "Bitcoin > $50k by end of Q1" @ 60 cents
        - "Bitcoin > $50k by end of Q2" @ 55 cents
        This violates: if Q1 happens, Q2 must also happen.

    Arbitrage: Buy YES on Q2 (55c), Sell YES on Q1 (60c)
    If Q1 hits: Both win, net +5c
    If Q1 misses but Q2 hits: Win Q2 (45c), pay out Q1 short (-40c), net +5c
    If both miss: Keep Q1 premium (60c), lose Q2 (55c), net +5c
    """

    def __init__(
        self,
        min_profit_cents: int = 2,
        min_price_diff: int = 3,
    ) -> None:
        """Initialize time-based strategy.

        Args:
            min_profit_cents: Minimum net profit to report
            min_price_diff: Minimum price difference to consider
        """
        self.min_profit_cents = min_profit_cents
        self.min_price_diff = min_price_diff
        self.calculator = ProfitCalculator()

    def detect(
        self,
        earlier_market: Market,
        later_market: Market,
        earlier_orderbook: Orderbook,
        later_orderbook: Orderbook,
    ) -> Optional[ArbitrageOpportunity]:
        """Detect time-based arbitrage opportunity.

        Args:
            earlier_market: Market with earlier expiration
            later_market: Market with later expiration
            earlier_orderbook: Orderbook for earlier market
            later_orderbook: Orderbook for later market

        Returns:
            ArbitrageOpportunity if found, None otherwise
        """
        # Validate expiration ordering
        if not self._validate_expiration_order(earlier_market, later_market):
            return None

        # Get prices
        earlier_yes_bid = earlier_orderbook.best_yes_bid
        later_yes_ask = later_orderbook.best_yes_ask

        if earlier_yes_bid is None or later_yes_ask is None:
            return None

        # Check for mispricing: earlier bid > later ask
        # This means we can sell earlier YES and buy later YES
        price_diff = earlier_yes_bid - later_yes_ask

        if price_diff < self.min_price_diff:
            return None

        # Build legs
        legs = [
            ArbitrageLeg(
                ticker=earlier_market.ticker,
                side=OrderSide.YES,
                action=OrderAction.SELL,  # Sell earlier expiration
                price=earlier_yes_bid,
                quantity=1,
            ),
            ArbitrageLeg(
                ticker=later_market.ticker,
                side=OrderSide.YES,
                action=OrderAction.BUY,  # Buy later expiration
                price=later_yes_ask,
                quantity=1,
            ),
        ]

        # Calculate profits
        # Net cost = later_ask - earlier_bid (negative if profitable)
        total_cost = later_yes_ask - earlier_yes_bid

        # For time arb, guaranteed return depends on outcome scenarios
        # Worst case is still profitable by the price diff
        gross_profit = price_diff
        fees = self.calculator.calculate_total_fees(legs)
        net_profit = gross_profit - fees

        if net_profit < self.min_profit_cents:
            return None

        # Max quantity limited by both orderbooks
        max_qty = min(
            self._get_bid_quantity(earlier_orderbook),
            later_orderbook.yes_ask_quantity,
        )

        if max_qty <= 0:
            return None

        opportunity = ArbitrageOpportunity(
            id=str(uuid.uuid4()),
            type=ArbitrageType.TIME_BASED,
            event_ticker=earlier_market.event_ticker,
            legs=legs,
            total_cost_cents=max(total_cost, 0),  # Cost is the later position
            guaranteed_return_cents=price_diff,
            gross_profit_cents=gross_profit,
            estimated_fees_cents=fees,
            net_profit_cents=net_profit,
            max_quantity=max_qty,
            detected_at=datetime.utcnow(),
            confidence=0.9,  # Time arb is generally reliable
        )

        logger.info(
            "Time-based arbitrage detected",
            earlier_ticker=earlier_market.ticker,
            later_ticker=later_market.ticker,
            price_diff=price_diff,
            net_profit=net_profit,
        )

        return opportunity

    def find_temporal_pairs(
        self,
        markets: list[Market],
    ) -> list[tuple[Market, Market]]:
        """Find pairs of markets that could have temporal arbitrage.

        Groups markets by underlying and finds pairs with different expirations.

        Args:
            markets: List of markets to analyze

        Returns:
            List of (earlier, later) market pairs
        """
        # Group by underlying (using event_ticker as proxy)
        by_event: dict[str, list[Market]] = {}
        for market in markets:
            if market.expiration_time:
                by_event.setdefault(market.event_ticker, []).append(market)

        pairs: list[tuple[Market, Market]] = []

        for event_ticker, event_markets in by_event.items():
            if len(event_markets) < 2:
                continue

            # Sort by expiration
            sorted_markets = sorted(
                event_markets,
                key=lambda m: m.expiration_time or datetime.max,
            )

            # Create pairs of consecutive expirations
            for i in range(len(sorted_markets) - 1):
                pairs.append((sorted_markets[i], sorted_markets[i + 1]))

        return pairs

    def _validate_expiration_order(
        self,
        earlier: Market,
        later: Market,
    ) -> bool:
        """Validate that earlier market expires before later market."""
        if not earlier.expiration_time or not later.expiration_time:
            return False
        return earlier.expiration_time < later.expiration_time

    def _get_bid_quantity(self, orderbook: Orderbook) -> int:
        """Get quantity available at best YES bid."""
        if not orderbook.yes_bids:
            return 0
        best_price = orderbook.best_yes_bid
        return sum(
            level.quantity
            for level in orderbook.yes_bids
            if level.price == best_price
        )

    def validate_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        orderbooks: dict[str, Orderbook],
    ) -> bool:
        """Validate that opportunity still exists."""
        if len(opportunity.legs) != 2:
            return False

        sell_leg = next(
            (l for l in opportunity.legs if l.action == OrderAction.SELL),
            None,
        )
        buy_leg = next(
            (l for l in opportunity.legs if l.action == OrderAction.BUY),
            None,
        )

        if not sell_leg or not buy_leg:
            return False

        sell_ob = orderbooks.get(sell_leg.ticker)
        buy_ob = orderbooks.get(buy_leg.ticker)

        if not sell_ob or not buy_ob:
            return False

        # Check prices still valid
        current_bid = sell_ob.best_yes_bid
        current_ask = buy_ob.best_yes_ask

        if current_bid is None or current_ask is None:
            return False

        if current_bid < sell_leg.price or current_ask > buy_leg.price:
            return False

        # Check quantities
        if self._get_bid_quantity(sell_ob) < sell_leg.quantity:
            return False
        if buy_ob.yes_ask_quantity < buy_leg.quantity:
            return False

        return True
