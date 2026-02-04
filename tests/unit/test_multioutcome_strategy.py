"""Unit tests for multi-outcome arbitrage strategy."""

import pytest
from datetime import datetime

from src.arbitrage.strategies.multioutcome import MultiOutcomeStrategy
from src.data.models import Market, Orderbook, OrderbookLevel, OrderSide


@pytest.fixture
def strategy():
    """Create strategy instance."""
    return MultiOutcomeStrategy(min_profit_cents=2)


@pytest.fixture
def three_outcome_markets():
    """Create three markets for a multi-outcome event."""
    return [
        Market(ticker="EVENT-A", event_ticker="EVENT", title="Outcome A"),
        Market(ticker="EVENT-B", event_ticker="EVENT", title="Outcome B"),
        Market(ticker="EVENT-C", event_ticker="EVENT", title="Outcome C"),
    ]


@pytest.fixture
def profitable_orderbooks():
    """Create orderbooks with arbitrage opportunity.

    YES ask prices: A=40, B=30, C=25 = 95 total (5 cent gross profit, ~4 net after fees)
    """
    return {
        "EVENT-A": Orderbook(
            ticker="EVENT-A",
            yes_bids=[OrderbookLevel(price=35, quantity=100)],
            no_bids=[OrderbookLevel(price=60, quantity=100)],  # YES ask = 40
        ),
        "EVENT-B": Orderbook(
            ticker="EVENT-B",
            yes_bids=[OrderbookLevel(price=25, quantity=100)],
            no_bids=[OrderbookLevel(price=70, quantity=100)],  # YES ask = 30
        ),
        "EVENT-C": Orderbook(
            ticker="EVENT-C",
            yes_bids=[OrderbookLevel(price=20, quantity=100)],
            no_bids=[OrderbookLevel(price=75, quantity=100)],  # YES ask = 25
        ),
    }


@pytest.fixture
def no_arbitrage_orderbooks():
    """Create orderbooks without arbitrage opportunity.

    YES ask prices: A=50, B=35, C=20 = 105 total (no profit)
    """
    return {
        "EVENT-A": Orderbook(
            ticker="EVENT-A",
            yes_bids=[OrderbookLevel(price=45, quantity=100)],
            no_bids=[OrderbookLevel(price=50, quantity=100)],  # YES ask = 50
        ),
        "EVENT-B": Orderbook(
            ticker="EVENT-B",
            yes_bids=[OrderbookLevel(price=30, quantity=100)],
            no_bids=[OrderbookLevel(price=65, quantity=100)],  # YES ask = 35
        ),
        "EVENT-C": Orderbook(
            ticker="EVENT-C",
            yes_bids=[OrderbookLevel(price=15, quantity=100)],
            no_bids=[OrderbookLevel(price=80, quantity=100)],  # YES ask = 20
        ),
    }


class TestMultiOutcomeStrategy:
    """Tests for MultiOutcomeStrategy."""

    def test_detect_opportunity(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
        profitable_orderbooks: dict[str, Orderbook],
    ):
        """Test detection of profitable arbitrage."""
        opportunity = strategy.detect(three_outcome_markets, profitable_orderbooks)

        assert opportunity is not None
        assert opportunity.type.value == "multi_outcome"
        assert len(opportunity.legs) == 3
        assert opportunity.total_cost_cents == 95  # 40 + 30 + 25
        assert opportunity.gross_profit_cents == 5  # 100 - 95

    def test_no_opportunity_when_not_profitable(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
        no_arbitrage_orderbooks: dict[str, Orderbook],
    ):
        """Test no detection when sum >= 100."""
        opportunity = strategy.detect(three_outcome_markets, no_arbitrage_orderbooks)
        assert opportunity is None

    def test_no_opportunity_with_missing_orderbook(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
        profitable_orderbooks: dict[str, Orderbook],
    ):
        """Test no detection when orderbook is missing."""
        del profitable_orderbooks["EVENT-C"]
        opportunity = strategy.detect(three_outcome_markets, profitable_orderbooks)
        assert opportunity is None

    def test_no_opportunity_with_no_liquidity(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
    ):
        """Test no detection when no liquidity (no NO bids = no YES asks)."""
        orderbooks = {
            "EVENT-A": Orderbook(
                ticker="EVENT-A",
                yes_bids=[OrderbookLevel(price=40, quantity=100)],
                no_bids=[],  # No NO bids = no YES ask
            ),
            "EVENT-B": Orderbook(
                ticker="EVENT-B",
                yes_bids=[OrderbookLevel(price=30, quantity=100)],
                no_bids=[OrderbookLevel(price=65, quantity=100)],
            ),
            "EVENT-C": Orderbook(
                ticker="EVENT-C",
                yes_bids=[OrderbookLevel(price=15, quantity=100)],
                no_bids=[OrderbookLevel(price=82, quantity=100)],
            ),
        }
        opportunity = strategy.detect(three_outcome_markets, orderbooks)
        assert opportunity is None

    def test_min_profit_threshold(
        self,
        three_outcome_markets: list[Market],
    ):
        """Test that min profit threshold is respected."""
        # Create opportunity with exactly 1 cent gross profit
        orderbooks = {
            "EVENT-A": Orderbook(
                ticker="EVENT-A",
                no_bids=[OrderbookLevel(price=55, quantity=100)],  # YES ask = 45
            ),
            "EVENT-B": Orderbook(
                ticker="EVENT-B",
                no_bids=[OrderbookLevel(price=65, quantity=100)],  # YES ask = 35
            ),
            "EVENT-C": Orderbook(
                ticker="EVENT-C",
                no_bids=[OrderbookLevel(price=81, quantity=100)],  # YES ask = 19
            ),
        }
        # Total = 45 + 35 + 19 = 99 (1 cent gross profit)

        strategy_strict = MultiOutcomeStrategy(min_profit_cents=5)
        opportunity = strategy_strict.detect(three_outcome_markets, orderbooks)
        assert opportunity is None  # 1 cent profit < 5 cent threshold

        strategy_lenient = MultiOutcomeStrategy(min_profit_cents=1)
        # Note: After fees, this might still not be profitable
        # Let's use a more profitable scenario
        orderbooks["EVENT-C"] = Orderbook(
            ticker="EVENT-C",
            no_bids=[OrderbookLevel(price=85, quantity=100)],  # YES ask = 15
        )
        # Total = 45 + 35 + 15 = 95 (5 cent gross profit)
        opportunity = strategy_lenient.detect(three_outcome_markets, orderbooks)
        # Should find opportunity (5 cents gross, ~4-5 after fees)
        assert opportunity is not None

    def test_max_quantity_limited_by_liquidity(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
    ):
        """Test max quantity is limited by smallest available liquidity."""
        # YES ask prices: 40 + 30 + 25 = 95 (5 cent gross profit)
        orderbooks = {
            "EVENT-A": Orderbook(
                ticker="EVENT-A",
                no_bids=[OrderbookLevel(price=60, quantity=100)],  # YES ask = 40
            ),
            "EVENT-B": Orderbook(
                ticker="EVENT-B",
                no_bids=[OrderbookLevel(price=70, quantity=50)],  # YES ask = 30, only 50 available
            ),
            "EVENT-C": Orderbook(
                ticker="EVENT-C",
                no_bids=[OrderbookLevel(price=75, quantity=200)],  # YES ask = 25
            ),
        }
        opportunity = strategy.detect(three_outcome_markets, orderbooks)

        assert opportunity is not None
        assert opportunity.max_quantity == 50  # Limited by EVENT-B

    def test_validate_opportunity_still_exists(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
        profitable_orderbooks: dict[str, Orderbook],
    ):
        """Test validation of existing opportunity."""
        opportunity = strategy.detect(three_outcome_markets, profitable_orderbooks)
        assert opportunity is not None

        # Validate with same orderbooks
        assert strategy.validate_opportunity(opportunity, profitable_orderbooks)

    def test_validate_opportunity_price_moved(
        self,
        strategy: MultiOutcomeStrategy,
        three_outcome_markets: list[Market],
        profitable_orderbooks: dict[str, Orderbook],
    ):
        """Test validation fails when prices move adversely."""
        opportunity = strategy.detect(three_outcome_markets, profitable_orderbooks)
        assert opportunity is not None

        # Price moved up (worse for us) - YES ask now 50, was 40
        profitable_orderbooks["EVENT-A"] = Orderbook(
            ticker="EVENT-A",
            no_bids=[OrderbookLevel(price=50, quantity=100)],  # YES ask now 50
        )

        assert not strategy.validate_opportunity(opportunity, profitable_orderbooks)

    def test_two_market_event(self, strategy: MultiOutcomeStrategy):
        """Test with only two outcomes."""
        markets = [
            Market(ticker="BINARY-YES", event_ticker="BINARY", title="Yes"),
            Market(ticker="BINARY-NO", event_ticker="BINARY", title="No"),
        ]
        orderbooks = {
            "BINARY-YES": Orderbook(
                ticker="BINARY-YES",
                no_bids=[OrderbookLevel(price=55, quantity=100)],  # YES ask = 45
            ),
            "BINARY-NO": Orderbook(
                ticker="BINARY-NO",
                no_bids=[OrderbookLevel(price=52, quantity=100)],  # YES ask = 48
            ),
        }
        # Total = 45 + 48 = 93 (7 cent gross profit)

        opportunity = strategy.detect(markets, orderbooks)
        assert opportunity is not None
        assert opportunity.total_cost_cents == 93
        assert opportunity.gross_profit_cents == 7
