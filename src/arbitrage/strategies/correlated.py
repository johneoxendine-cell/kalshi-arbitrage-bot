"""Correlated events arbitrage strategy.

Detects opportunities when related events violate logical constraints.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
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


class CorrelationType(str, Enum):
    """Type of correlation between events."""

    IMPLIES = "implies"  # A implies B (if A then B)
    EXCLUDES = "excludes"  # A excludes B (if A then not B)
    EQUIVALENT = "equivalent"  # A iff B


@dataclass
class CorrelationRule:
    """Defines a correlation between two markets."""

    market_a_pattern: str  # Ticker pattern for market A
    market_b_pattern: str  # Ticker pattern for market B
    correlation: CorrelationType
    description: str


class CorrelatedStrategy:
    """Correlated events arbitrage strategy.

    Exploits logical constraints between related markets:

    1. IMPLIES (A -> B): P(A) <= P(B)
       Example: "Team wins championship" implies "Team makes playoffs"
       Arb if: championship_ask > playoffs_bid

    2. EXCLUDES (A -> !B): P(A) + P(B) <= 1
       Example: "Candidate A wins" excludes "Candidate B wins"
       Arb if: A_ask + B_ask < 100

    3. EQUIVALENT (A <-> B): P(A) = P(B)
       Example: Same event on different platforms
       Arb if: abs(A_price - B_price) > threshold
    """

    # Predefined correlation rules
    DEFAULT_RULES: list[CorrelationRule] = [
        # Sports examples
        CorrelationRule(
            market_a_pattern="*-CHAMPIONSHIP-*",
            market_b_pattern="*-PLAYOFFS-*",
            correlation=CorrelationType.IMPLIES,
            description="Championship winner must make playoffs",
        ),
        # Political examples
        CorrelationRule(
            market_a_pattern="*-PRIMARY-WIN-*",
            market_b_pattern="*-NOMINATION-*",
            correlation=CorrelationType.IMPLIES,
            description="Primary winner likely gets nomination",
        ),
    ]

    def __init__(
        self,
        min_profit_cents: int = 2,
        rules: Optional[list[CorrelationRule]] = None,
    ) -> None:
        """Initialize correlated strategy.

        Args:
            min_profit_cents: Minimum net profit to report
            rules: Correlation rules (uses defaults if not provided)
        """
        self.min_profit_cents = min_profit_cents
        self.rules = rules or self.DEFAULT_RULES
        self.calculator = ProfitCalculator()

    def detect_implies(
        self,
        antecedent: Market,
        consequent: Market,
        antecedent_ob: Orderbook,
        consequent_ob: Orderbook,
    ) -> Optional[ArbitrageOpportunity]:
        """Detect arbitrage from implication violation.

        If A implies B, then P(A) <= P(B).
        Arbitrage when: A_bid > B_ask

        Strategy: Sell YES on A, Buy YES on B
        - If A happens: Both win, net profit = A_bid - B_ask
        - If A doesn't happen: Keep A premium, B outcome uncertain

        Args:
            antecedent: Market A (the "if" condition)
            consequent: Market B (the "then" condition)
            antecedent_ob: Orderbook for A
            consequent_ob: Orderbook for B

        Returns:
            ArbitrageOpportunity if found
        """
        a_bid = antecedent_ob.best_yes_bid
        b_ask = consequent_ob.best_yes_ask

        if a_bid is None or b_ask is None:
            return None

        # Check for violation: A_bid > B_ask
        price_diff = a_bid - b_ask
        if price_diff <= 0:
            return None

        legs = [
            ArbitrageLeg(
                ticker=antecedent.ticker,
                side=OrderSide.YES,
                action=OrderAction.SELL,
                price=a_bid,
                quantity=1,
            ),
            ArbitrageLeg(
                ticker=consequent.ticker,
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=b_ask,
                quantity=1,
            ),
        ]

        # Calculate profits
        gross_profit = price_diff
        fees = self.calculator.calculate_total_fees(legs)
        net_profit = gross_profit - fees

        if net_profit < self.min_profit_cents:
            return None

        max_qty = min(
            self._get_bid_quantity(antecedent_ob),
            consequent_ob.yes_ask_quantity,
        )

        if max_qty <= 0:
            return None

        opportunity = ArbitrageOpportunity(
            id=str(uuid.uuid4()),
            type=ArbitrageType.CORRELATED,
            event_ticker=f"{antecedent.event_ticker}+{consequent.event_ticker}",
            legs=legs,
            total_cost_cents=b_ask,
            guaranteed_return_cents=a_bid,
            gross_profit_cents=gross_profit,
            estimated_fees_cents=fees,
            net_profit_cents=net_profit,
            max_quantity=max_qty,
            detected_at=datetime.utcnow(),
            confidence=0.8,
        )

        logger.info(
            "Implies arbitrage detected",
            antecedent=antecedent.ticker,
            consequent=consequent.ticker,
            a_bid=a_bid,
            b_ask=b_ask,
            net_profit=net_profit,
        )

        return opportunity

    def detect_excludes(
        self,
        market_a: Market,
        market_b: Market,
        orderbook_a: Orderbook,
        orderbook_b: Orderbook,
    ) -> Optional[ArbitrageOpportunity]:
        """Detect arbitrage from exclusion violation.

        If A excludes B, then P(A) + P(B) <= 1.
        Arbitrage when: A_ask + B_ask < 100

        Strategy: Buy YES on both A and B
        - If A happens: Win A (100), lose B cost
        - If B happens: Win B (100), lose A cost
        - If neither: Lose both costs (but this violates exclusion)

        This is similar to multi-outcome arb for mutually exclusive events.

        Args:
            market_a: First market
            market_b: Second market
            orderbook_a: Orderbook for A
            orderbook_b: Orderbook for B

        Returns:
            ArbitrageOpportunity if found
        """
        a_ask = orderbook_a.best_yes_ask
        b_ask = orderbook_b.best_yes_ask

        if a_ask is None or b_ask is None:
            return None

        total_cost = a_ask + b_ask

        # Arbitrage exists if total cost < 100
        if total_cost >= 100:
            return None

        legs = [
            ArbitrageLeg(
                ticker=market_a.ticker,
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=a_ask,
                quantity=1,
            ),
            ArbitrageLeg(
                ticker=market_b.ticker,
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=b_ask,
                quantity=1,
            ),
        ]

        gross_profit = 100 - total_cost
        fees = self.calculator.calculate_total_fees(legs)
        net_profit = gross_profit - fees

        if net_profit < self.min_profit_cents:
            return None

        max_qty = min(
            orderbook_a.yes_ask_quantity,
            orderbook_b.yes_ask_quantity,
        )

        if max_qty <= 0:
            return None

        opportunity = ArbitrageOpportunity(
            id=str(uuid.uuid4()),
            type=ArbitrageType.CORRELATED,
            event_ticker=f"{market_a.event_ticker}+{market_b.event_ticker}",
            legs=legs,
            total_cost_cents=total_cost,
            guaranteed_return_cents=100,
            gross_profit_cents=gross_profit,
            estimated_fees_cents=fees,
            net_profit_cents=net_profit,
            max_quantity=max_qty,
            detected_at=datetime.utcnow(),
            confidence=0.85,
        )

        logger.info(
            "Excludes arbitrage detected",
            market_a=market_a.ticker,
            market_b=market_b.ticker,
            total_cost=total_cost,
            net_profit=net_profit,
        )

        return opportunity

    def detect_equivalent(
        self,
        market_a: Market,
        market_b: Market,
        orderbook_a: Orderbook,
        orderbook_b: Orderbook,
        price_threshold: int = 5,
    ) -> Optional[ArbitrageOpportunity]:
        """Detect arbitrage from equivalent market mispricing.

        For equivalent markets, prices should be equal.
        Arbitrage when: A_bid > B_ask or B_bid > A_ask

        Args:
            market_a: First market
            market_b: Second market (equivalent to A)
            orderbook_a: Orderbook for A
            orderbook_b: Orderbook for B
            price_threshold: Minimum price difference

        Returns:
            ArbitrageOpportunity if found
        """
        a_bid = orderbook_a.best_yes_bid
        a_ask = orderbook_a.best_yes_ask
        b_bid = orderbook_b.best_yes_bid
        b_ask = orderbook_b.best_yes_ask

        if None in (a_bid, a_ask, b_bid, b_ask):
            return None

        # Check A_bid > B_ask (sell A, buy B)
        if a_bid - b_ask >= price_threshold:
            return self._create_equivalent_opportunity(
                sell_market=market_a,
                buy_market=market_b,
                sell_price=a_bid,
                buy_price=b_ask,
                sell_ob=orderbook_a,
                buy_ob=orderbook_b,
            )

        # Check B_bid > A_ask (sell B, buy A)
        if b_bid - a_ask >= price_threshold:
            return self._create_equivalent_opportunity(
                sell_market=market_b,
                buy_market=market_a,
                sell_price=b_bid,
                buy_price=a_ask,
                sell_ob=orderbook_b,
                buy_ob=orderbook_a,
            )

        return None

    def _create_equivalent_opportunity(
        self,
        sell_market: Market,
        buy_market: Market,
        sell_price: int,
        buy_price: int,
        sell_ob: Orderbook,
        buy_ob: Orderbook,
    ) -> Optional[ArbitrageOpportunity]:
        """Create opportunity for equivalent market mispricing."""
        legs = [
            ArbitrageLeg(
                ticker=sell_market.ticker,
                side=OrderSide.YES,
                action=OrderAction.SELL,
                price=sell_price,
                quantity=1,
            ),
            ArbitrageLeg(
                ticker=buy_market.ticker,
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=buy_price,
                quantity=1,
            ),
        ]

        gross_profit = sell_price - buy_price
        fees = self.calculator.calculate_total_fees(legs)
        net_profit = gross_profit - fees

        if net_profit < self.min_profit_cents:
            return None

        max_qty = min(
            self._get_bid_quantity(sell_ob),
            buy_ob.yes_ask_quantity,
        )

        if max_qty <= 0:
            return None

        return ArbitrageOpportunity(
            id=str(uuid.uuid4()),
            type=ArbitrageType.CORRELATED,
            event_ticker=f"{sell_market.event_ticker}={buy_market.event_ticker}",
            legs=legs,
            total_cost_cents=buy_price,
            guaranteed_return_cents=sell_price,
            gross_profit_cents=gross_profit,
            estimated_fees_cents=fees,
            net_profit_cents=net_profit,
            max_quantity=max_qty,
            detected_at=datetime.utcnow(),
            confidence=0.9,
        )

    def _get_bid_quantity(self, orderbook: Orderbook) -> int:
        """Get quantity at best YES bid."""
        if not orderbook.yes_bids:
            return 0
        best_price = orderbook.best_yes_bid
        return sum(
            level.quantity
            for level in orderbook.yes_bids
            if level.price == best_price
        )

    def match_rule(
        self,
        market_a: Market,
        market_b: Market,
    ) -> Optional[CorrelationRule]:
        """Find matching correlation rule for market pair.

        Args:
            market_a: First market
            market_b: Second market

        Returns:
            Matching rule or None
        """
        import fnmatch

        for rule in self.rules:
            # Check A matches pattern A and B matches pattern B
            if fnmatch.fnmatch(market_a.ticker, rule.market_a_pattern) and fnmatch.fnmatch(
                market_b.ticker, rule.market_b_pattern
            ):
                return rule

            # Check reverse (B matches A pattern, A matches B pattern)
            if fnmatch.fnmatch(market_b.ticker, rule.market_a_pattern) and fnmatch.fnmatch(
                market_a.ticker, rule.market_b_pattern
            ):
                return rule

        return None

    def validate_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        orderbooks: dict[str, Orderbook],
    ) -> bool:
        """Validate that opportunity still exists."""
        if len(opportunity.legs) != 2:
            return False

        for leg in opportunity.legs:
            ob = orderbooks.get(leg.ticker)
            if not ob:
                return False

            if leg.action == OrderAction.SELL:
                current_bid = ob.best_yes_bid
                if current_bid is None or current_bid < leg.price:
                    return False
                if self._get_bid_quantity(ob) < leg.quantity:
                    return False
            else:  # BUY
                current_ask = ob.best_yes_ask
                if current_ask is None or current_ask > leg.price:
                    return False
                if ob.yes_ask_quantity < leg.quantity:
                    return False

        return True
