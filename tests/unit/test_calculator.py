"""Unit tests for profit calculator."""

import pytest
from decimal import Decimal

from src.arbitrage.calculator import ProfitCalculator
from src.data.models import ArbitrageLeg, OrderAction, OrderSide


@pytest.fixture
def calculator():
    """Create calculator instance."""
    return ProfitCalculator()


@pytest.fixture
def three_leg_arb():
    """Create three-leg arbitrage with known costs."""
    return [
        ArbitrageLeg(
            ticker="EVENT-A",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            price=45,
            quantity=1,
        ),
        ArbitrageLeg(
            ticker="EVENT-B",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            price=35,
            quantity=1,
        ),
        ArbitrageLeg(
            ticker="EVENT-C",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            price=15,
            quantity=1,
        ),
    ]


class TestProfitCalculator:
    """Tests for ProfitCalculator."""

    def test_calculate_leg_cost_buy(self, calculator: ProfitCalculator):
        """Test leg cost calculation for buy orders."""
        leg = ArbitrageLeg(
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            price=45,
            quantity=10,
        )
        cost = calculator.calculate_leg_cost(leg)
        assert cost == 450  # 45 cents * 10 contracts

    def test_calculate_leg_cost_sell(self, calculator: ProfitCalculator):
        """Test leg cost calculation for sell orders (receives premium)."""
        leg = ArbitrageLeg(
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.SELL,
            price=45,
            quantity=10,
        )
        cost = calculator.calculate_leg_cost(leg)
        assert cost == -450  # Negative = receives money

    def test_calculate_total_cost(
        self,
        calculator: ProfitCalculator,
        three_leg_arb: list[ArbitrageLeg],
    ):
        """Test total cost calculation."""
        total = calculator.calculate_total_cost(three_leg_arb)
        assert total == 95  # 45 + 35 + 15

    def test_calculate_gross_profit(self, calculator: ProfitCalculator):
        """Test gross profit calculation."""
        gross = calculator.calculate_gross_profit(total_cost=95)
        assert gross == 5  # 100 - 95

    def test_calculate_fee_per_contract(self, calculator: ProfitCalculator):
        """Test fee calculation per contract."""
        # Fee = 0.7% of potential profit
        # At price 45, potential profit = 100 - 45 = 55 cents
        # Fee = 55 * 0.007 = 0.385, rounded up = 1 cent
        fee = calculator.calculate_fee_per_contract(price=45)
        assert fee == 1

        # At price 10, potential profit = 90 cents
        # Fee = 90 * 0.007 = 0.63, rounded up = 1 cent
        fee = calculator.calculate_fee_per_contract(price=10)
        assert fee == 1

        # At price 1, potential profit = 99 cents
        # Fee = 99 * 0.007 = 0.693, rounded up = 1 cent
        fee = calculator.calculate_fee_per_contract(price=1)
        assert fee == 1

    def test_calculate_total_fees(
        self,
        calculator: ProfitCalculator,
        three_leg_arb: list[ArbitrageLeg],
    ):
        """Test total fees calculation."""
        # For multi-outcome, one leg will win
        # Max fee is from the leg with highest potential profit
        # Leg A: price 45, profit 55, fee = 0.385 -> 1 cent
        # Leg B: price 35, profit 65, fee = 0.455 -> 1 cent
        # Leg C: price 15, profit 85, fee = 0.595 -> 1 cent
        # Max fee = 1 cent (from leg C)
        fees = calculator.calculate_total_fees(three_leg_arb)
        assert fees == 1

    def test_calculate_net_profit(
        self,
        calculator: ProfitCalculator,
        three_leg_arb: list[ArbitrageLeg],
    ):
        """Test net profit calculation."""
        # Total cost = 95, gross profit = 5, fees = 1
        # Net profit = 5 - 1 = 4
        net = calculator.calculate_net_profit(three_leg_arb)
        assert net == 4

    def test_is_profitable(
        self,
        calculator: ProfitCalculator,
        three_leg_arb: list[ArbitrageLeg],
    ):
        """Test profitability check."""
        assert calculator.is_profitable(three_leg_arb, min_profit_cents=1)
        assert calculator.is_profitable(three_leg_arb, min_profit_cents=4)
        assert not calculator.is_profitable(three_leg_arb, min_profit_cents=5)

    def test_profit_summary(
        self,
        calculator: ProfitCalculator,
        three_leg_arb: list[ArbitrageLeg],
    ):
        """Test profit summary generation."""
        summary = calculator.profit_summary(three_leg_arb)

        assert summary["total_cost_cents"] == 95
        assert summary["guaranteed_return_cents"] == 100
        assert summary["gross_profit_cents"] == 5
        assert summary["estimated_fees_cents"] == 1
        assert summary["net_profit_cents"] == 4
        assert summary["is_profitable"]
        assert summary["profit_margin_pct"] == pytest.approx(4.21, rel=0.01)

    def test_no_profit_scenario(self, calculator: ProfitCalculator):
        """Test when there's no profit."""
        legs = [
            ArbitrageLeg(
                ticker="A",
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=60,
                quantity=1,
            ),
            ArbitrageLeg(
                ticker="B",
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=50,
                quantity=1,
            ),
        ]
        # Total cost = 110, exceeds payout of 100
        net = calculator.calculate_net_profit(legs)
        assert net < 0
        assert not calculator.is_profitable(legs)

    def test_quantity_affects_fees(self, calculator: ProfitCalculator):
        """Test that quantity affects fee calculation."""
        single_leg = [
            ArbitrageLeg(
                ticker="A",
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=20,  # 80 cent potential profit
                quantity=1,
            ),
        ]
        multi_leg = [
            ArbitrageLeg(
                ticker="A",
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=20,
                quantity=10,  # 10 contracts
            ),
        ]

        single_fee = calculator.calculate_total_fees(single_leg)
        multi_fee = calculator.calculate_total_fees(multi_leg)

        # Fee for 10 contracts should be ~10x single
        # 80 * 0.007 = 0.56 per contract, 5.6 for 10 -> 6 cents
        assert multi_fee > single_fee

    def test_custom_fee_rate(self):
        """Test calculator with custom fee rate."""
        calculator = ProfitCalculator(fee_rate=Decimal("0.01"))  # 1% fee

        leg = [
            ArbitrageLeg(
                ticker="A",
                side=OrderSide.YES,
                action=OrderAction.BUY,
                price=50,  # 50 cent potential profit
                quantity=1,
            ),
        ]

        # Fee = 50 * 0.01 = 0.5, rounded up = 1 cent
        fee = calculator.calculate_total_fees(leg)
        assert fee == 1

    def test_break_even_cost(self, calculator: ProfitCalculator):
        """Test break-even cost calculation."""
        break_even = calculator.calculate_break_even_cost(num_legs=3)

        # Should be less than 100 to account for fees
        assert break_even < 100
        assert break_even > 90  # Should still be close to 100
