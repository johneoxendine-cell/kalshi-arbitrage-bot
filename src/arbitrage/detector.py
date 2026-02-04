"""Main arbitrage detection coordinator."""

from typing import Optional

from config.logging_config import get_logger
from src.data.models import ArbitrageOpportunity, Market, Orderbook
from .strategies.correlated import CorrelatedStrategy
from .strategies.multioutcome import MultiOutcomeStrategy
from .strategies.time_based import TimeBasedStrategy

logger = get_logger(__name__)


class ArbitrageDetector:
    """Coordinates multiple arbitrage detection strategies.

    Scans markets using all available strategies:
    - Multi-outcome: Sum of YES costs < 100
    - Time-based: Earlier expiration priced higher
    - Correlated: Related events violate constraints
    """

    def __init__(
        self,
        min_profit_cents: int = 2,
        enable_multioutcome: bool = True,
        enable_time_based: bool = True,
        enable_correlated: bool = True,
    ) -> None:
        """Initialize arbitrage detector.

        Args:
            min_profit_cents: Minimum profit threshold for all strategies
            enable_multioutcome: Enable multi-outcome strategy
            enable_time_based: Enable time-based strategy
            enable_correlated: Enable correlated events strategy
        """
        self.min_profit_cents = min_profit_cents

        self.strategies: dict = {}

        if enable_multioutcome:
            self.strategies["multioutcome"] = MultiOutcomeStrategy(
                min_profit_cents=min_profit_cents
            )

        if enable_time_based:
            self.strategies["time_based"] = TimeBasedStrategy(
                min_profit_cents=min_profit_cents
            )

        if enable_correlated:
            self.strategies["correlated"] = CorrelatedStrategy(
                min_profit_cents=min_profit_cents
            )

        logger.info(
            "Arbitrage detector initialized",
            strategies=list(self.strategies.keys()),
            min_profit_cents=min_profit_cents,
        )

    def scan_event(
        self,
        markets: list[Market],
        orderbooks: dict[str, Orderbook],
    ) -> list[ArbitrageOpportunity]:
        """Scan an event for arbitrage opportunities.

        Applies all enabled strategies to find opportunities.

        Args:
            markets: Markets in the event
            orderbooks: Current orderbook state for each market

        Returns:
            List of detected opportunities
        """
        opportunities: list[ArbitrageOpportunity] = []

        if not markets or not orderbooks:
            return opportunities

        # Multi-outcome strategy
        if "multioutcome" in self.strategies:
            opp = self._scan_multioutcome(markets, orderbooks)
            if opp:
                opportunities.append(opp)

        # Time-based strategy
        if "time_based" in self.strategies:
            opps = self._scan_time_based(markets, orderbooks)
            opportunities.extend(opps)

        # Log results
        if opportunities:
            logger.info(
                "Arbitrage opportunities found",
                event_ticker=markets[0].event_ticker if markets else "unknown",
                count=len(opportunities),
                types=[o.type.value for o in opportunities],
            )

        return opportunities

    def scan_market_pair(
        self,
        market_a: Market,
        market_b: Market,
        orderbook_a: Orderbook,
        orderbook_b: Orderbook,
    ) -> list[ArbitrageOpportunity]:
        """Scan a pair of markets for correlated arbitrage.

        Args:
            market_a: First market
            market_b: Second market
            orderbook_a: Orderbook for first market
            orderbook_b: Orderbook for second market

        Returns:
            List of detected opportunities
        """
        opportunities: list[ArbitrageOpportunity] = []

        if "correlated" not in self.strategies:
            return opportunities

        strategy: CorrelatedStrategy = self.strategies["correlated"]

        # Check for correlation rule match
        rule = strategy.match_rule(market_a, market_b)
        if rule:
            from .strategies.correlated import CorrelationType

            if rule.correlation == CorrelationType.IMPLIES:
                opp = strategy.detect_implies(
                    market_a, market_b, orderbook_a, orderbook_b
                )
                if opp:
                    opportunities.append(opp)

            elif rule.correlation == CorrelationType.EXCLUDES:
                opp = strategy.detect_excludes(
                    market_a, market_b, orderbook_a, orderbook_b
                )
                if opp:
                    opportunities.append(opp)

            elif rule.correlation == CorrelationType.EQUIVALENT:
                opp = strategy.detect_equivalent(
                    market_a, market_b, orderbook_a, orderbook_b
                )
                if opp:
                    opportunities.append(opp)

        return opportunities

    def _scan_multioutcome(
        self,
        markets: list[Market],
        orderbooks: dict[str, Orderbook],
    ) -> Optional[ArbitrageOpportunity]:
        """Scan for multi-outcome arbitrage."""
        strategy: MultiOutcomeStrategy = self.strategies["multioutcome"]
        return strategy.detect(markets, orderbooks)

    def _scan_time_based(
        self,
        markets: list[Market],
        orderbooks: dict[str, Orderbook],
    ) -> list[ArbitrageOpportunity]:
        """Scan for time-based arbitrage."""
        opportunities: list[ArbitrageOpportunity] = []
        strategy: TimeBasedStrategy = self.strategies["time_based"]

        # Find temporal pairs
        pairs = strategy.find_temporal_pairs(markets)

        for earlier, later in pairs:
            earlier_ob = orderbooks.get(earlier.ticker)
            later_ob = orderbooks.get(later.ticker)

            if earlier_ob and later_ob:
                opp = strategy.detect(earlier, later, earlier_ob, later_ob)
                if opp:
                    opportunities.append(opp)

        return opportunities

    def validate_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        orderbooks: dict[str, Orderbook],
    ) -> bool:
        """Validate that an opportunity still exists.

        Should be called before execution to confirm prices haven't moved.

        Args:
            opportunity: Previously detected opportunity
            orderbooks: Current orderbook state

        Returns:
            True if opportunity is still valid
        """
        strategy_name = {
            "multi_outcome": "multioutcome",
            "time_based": "time_based",
            "correlated": "correlated",
        }.get(opportunity.type.value)

        if strategy_name not in self.strategies:
            return False

        strategy = self.strategies[strategy_name]
        return strategy.validate_opportunity(opportunity, orderbooks)

    def get_best_opportunity(
        self,
        opportunities: list[ArbitrageOpportunity],
    ) -> Optional[ArbitrageOpportunity]:
        """Get the best opportunity from a list.

        Ranks by:
        1. Net profit (higher is better)
        2. Confidence (higher is better)
        3. Max quantity (higher is better)

        Args:
            opportunities: List of opportunities

        Returns:
            Best opportunity or None
        """
        if not opportunities:
            return None

        # Filter to profitable only
        profitable = [o for o in opportunities if o.is_profitable]
        if not profitable:
            return None

        # Sort by composite score
        def score(opp: ArbitrageOpportunity) -> float:
            return (
                opp.net_profit_cents * 100
                + opp.confidence * 10
                + opp.max_quantity
            )

        return max(profitable, key=score)

    @property
    def enabled_strategies(self) -> list[str]:
        """Get list of enabled strategy names."""
        return list(self.strategies.keys())
