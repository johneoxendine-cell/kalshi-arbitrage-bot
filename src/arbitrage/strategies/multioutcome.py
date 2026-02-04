"""Multi-outcome arbitrage strategy.

Detects opportunities when the sum of YES acquisition costs across
all outcomes in an event is less than 100 cents (guaranteed payout).
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


class MultiOutcomeStrategy:
    """Multi-outcome arbitrage strategy.

    For mutually exclusive events (e.g., "Who will win the election?"),
    buying YES on ALL outcomes guarantees exactly one wins.

    Arbitrage exists when:
        sum(YES_ask[i]) < 100 cents

    Example:
        - Candidate A YES ask: 45 cents
        - Candidate B YES ask: 35 cents
        - Candidate C YES ask: 18 cents
        - Total: 98 cents
        - Guaranteed return: 100 cents
        - Gross profit: 2 cents
    """

    GUARANTEED_PAYOUT = 100  # cents

    def __init__(
        self,
        min_profit_cents: int = 2,
        min_markets: int = 2,
        max_markets: int = 10,
    ) -> None:
        """Initialize multi-outcome strategy.

        Args:
            min_profit_cents: Minimum net profit to report opportunity
            min_markets: Minimum markets required for valid event
            max_markets: Maximum markets to consider
        """
        self.min_profit_cents = min_profit_cents
        self.min_markets = min_markets
        self.max_markets = max_markets
        self.calculator = ProfitCalculator()

    def detect(
        self,
        markets: list[Market],
        orderbooks: dict[str, Orderbook],
    ) -> Optional[ArbitrageOpportunity]:
        """Detect multi-outcome arbitrage opportunity.

        Args:
            markets: List of markets in the event
            orderbooks: Dict mapping ticker to orderbook

        Returns:
            ArbitrageOpportunity if found, None otherwise
        """
        if len(markets) < self.min_markets:
            return None

        if len(markets) > self.max_markets:
            logger.debug(
                "Too many markets for multi-outcome",
                count=len(markets),
                max=self.max_markets,
            )
            return None

        # Build legs for each market
        legs: list[ArbitrageLeg] = []
        max_quantities: dict[str, int] = {}
        total_cost = 0

        for market in markets:
            orderbook = orderbooks.get(market.ticker)
            if not orderbook:
                logger.debug("Missing orderbook", ticker=market.ticker)
                return None

            # Get best YES ask price (implied from NO bids)
            yes_ask = orderbook.best_yes_ask
            if yes_ask is None:
                logger.debug("No YES ask available", ticker=market.ticker)
                return None

            # Get available quantity at best ask
            quantity = orderbook.yes_ask_quantity
            if quantity <= 0:
                logger.debug("No quantity available", ticker=market.ticker)
                return None

            legs.append(
                ArbitrageLeg(
                    ticker=market.ticker,
                    side=OrderSide.YES,
                    action=OrderAction.BUY,
                    price=yes_ask,
                    quantity=1,  # Will be adjusted based on max available
                )
            )
            max_quantities[market.ticker] = quantity
            total_cost += yes_ask

        # Check if arbitrage exists (total cost < guaranteed payout)
        if total_cost >= self.GUARANTEED_PAYOUT:
            return None

        # Calculate profits
        gross_profit = self.GUARANTEED_PAYOUT - total_cost
        fees = self.calculator.calculate_total_fees(legs)
        net_profit = gross_profit - fees

        if net_profit < self.min_profit_cents:
            logger.debug(
                "Insufficient profit",
                gross=gross_profit,
                fees=fees,
                net=net_profit,
                min_required=self.min_profit_cents,
            )
            return None

        # Determine max quantity across all legs
        max_qty = min(max_quantities.values())

        # Get event ticker from first market
        event_ticker = markets[0].event_ticker if markets else "unknown"

        opportunity = ArbitrageOpportunity(
            id=str(uuid.uuid4()),
            type=ArbitrageType.MULTI_OUTCOME,
            event_ticker=event_ticker,
            legs=legs,
            total_cost_cents=total_cost,
            guaranteed_return_cents=self.GUARANTEED_PAYOUT,
            gross_profit_cents=gross_profit,
            estimated_fees_cents=fees,
            net_profit_cents=net_profit,
            max_quantity=max_qty,
            detected_at=datetime.utcnow(),
            confidence=self._calculate_confidence(markets, orderbooks),
        )

        logger.info(
            "Multi-outcome arbitrage detected",
            event_ticker=event_ticker,
            total_cost=total_cost,
            net_profit=net_profit,
            max_quantity=max_qty,
            num_legs=len(legs),
        )

        return opportunity

    def _calculate_confidence(
        self,
        markets: list[Market],
        orderbooks: dict[str, Orderbook],
    ) -> float:
        """Calculate confidence score for opportunity.

        Higher confidence when:
        - Larger quantities available
        - Tighter spreads
        - More liquid markets

        Args:
            markets: List of markets
            orderbooks: Dict of orderbooks

        Returns:
            Confidence score 0-1
        """
        if not markets:
            return 0.0

        # Factor 1: Average quantity available
        quantities = []
        for market in markets:
            ob = orderbooks.get(market.ticker)
            if ob:
                quantities.append(ob.yes_ask_quantity)

        avg_qty = sum(quantities) / len(quantities) if quantities else 0
        qty_score = min(avg_qty / 100, 1.0)  # Max out at 100 contracts

        # Factor 2: All markets have orderbooks
        coverage = len(orderbooks) / len(markets)

        # Combined score
        return (qty_score * 0.5 + coverage * 0.5)

    def validate_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        orderbooks: dict[str, Orderbook],
    ) -> bool:
        """Validate that opportunity still exists.

        Args:
            opportunity: Previously detected opportunity
            orderbooks: Current orderbook state

        Returns:
            True if opportunity is still valid
        """
        total_cost = 0

        for leg in opportunity.legs:
            orderbook = orderbooks.get(leg.ticker)
            if not orderbook:
                return False

            yes_ask = orderbook.best_yes_ask
            if yes_ask is None:
                return False

            # Check price hasn't moved adversely
            if yes_ask > leg.price:
                return False

            # Check quantity still available
            if orderbook.yes_ask_quantity < leg.quantity:
                return False

            total_cost += yes_ask

        # Recalculate profitability
        gross_profit = self.GUARANTEED_PAYOUT - total_cost
        fees = self.calculator.calculate_total_fees(opportunity.legs)
        net_profit = gross_profit - fees

        return net_profit >= self.min_profit_cents
