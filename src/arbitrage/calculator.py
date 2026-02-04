"""Profit and fee calculations for arbitrage opportunities."""

from decimal import Decimal, ROUND_UP
from typing import Optional

from config.logging_config import get_logger
from src.data.models import ArbitrageLeg, OrderAction

logger = get_logger(__name__)


class ProfitCalculator:
    """Calculates profits, fees, and costs for arbitrage trades.

    Kalshi fee structure:
    - 0.7% fee on profits (not on loss)
    - Fee applies to each leg independently
    - Winning legs pay fees, losing legs don't
    """

    # Kalshi fee rate (0.7%)
    FEE_RATE = Decimal("0.007")

    # Guaranteed payout for complete outcome coverage (100 cents = $1)
    PAYOUT_CENTS = 100

    def __init__(self, fee_rate: Optional[Decimal] = None) -> None:
        """Initialize calculator.

        Args:
            fee_rate: Override fee rate (default 0.7%)
        """
        self.fee_rate = fee_rate or self.FEE_RATE

    def calculate_leg_cost(self, leg: ArbitrageLeg) -> int:
        """Calculate cost for a single leg in cents.

        Args:
            leg: Arbitrage leg

        Returns:
            Cost in cents (positive for buys)
        """
        if leg.action == OrderAction.BUY:
            return leg.price * leg.quantity
        else:
            # Selling receives premium
            return -leg.price * leg.quantity

    def calculate_total_cost(self, legs: list[ArbitrageLeg]) -> int:
        """Calculate total cost for all legs in cents.

        Args:
            legs: List of arbitrage legs

        Returns:
            Total cost in cents
        """
        return sum(self.calculate_leg_cost(leg) for leg in legs)

    def calculate_fee_per_contract(self, price: int) -> int:
        """Calculate fee for a single contract at given price.

        Fee is 0.7% of potential profit. For a YES position bought at X cents,
        potential profit is (100 - X) cents if YES wins.

        Args:
            price: Acquisition price in cents

        Returns:
            Fee in cents (rounded up)
        """
        potential_profit = self.PAYOUT_CENTS - price
        fee = Decimal(potential_profit) * self.fee_rate
        return int(fee.quantize(Decimal("1"), rounding=ROUND_UP))

    def calculate_total_fees(self, legs: list[ArbitrageLeg]) -> int:
        """Calculate total fees for arbitrage trade.

        For multi-outcome arbitrage, exactly one leg will win.
        Fee is charged on the winning leg's profit.

        Args:
            legs: List of arbitrage legs

        Returns:
            Total estimated fees in cents
        """
        # For each leg, calculate fee if that leg wins
        # Use max fee as conservative estimate
        max_fee = 0
        for leg in legs:
            if leg.action == OrderAction.BUY:
                potential_profit = self.PAYOUT_CENTS - leg.price
                fee = int(
                    (Decimal(potential_profit) * self.fee_rate * leg.quantity).quantize(
                        Decimal("1"), rounding=ROUND_UP
                    )
                )
                max_fee = max(max_fee, fee)

        return max_fee

    def calculate_gross_profit(
        self,
        total_cost: int,
        guaranteed_return: int = PAYOUT_CENTS,
    ) -> int:
        """Calculate gross profit before fees.

        Args:
            total_cost: Total cost in cents
            guaranteed_return: Guaranteed payout in cents

        Returns:
            Gross profit in cents
        """
        return guaranteed_return - total_cost

    def calculate_net_profit(
        self,
        legs: list[ArbitrageLeg],
        guaranteed_return: int = PAYOUT_CENTS,
    ) -> int:
        """Calculate net profit after fees.

        Args:
            legs: List of arbitrage legs
            guaranteed_return: Guaranteed payout in cents

        Returns:
            Net profit in cents
        """
        total_cost = self.calculate_total_cost(legs)
        gross_profit = self.calculate_gross_profit(total_cost, guaranteed_return)
        fees = self.calculate_total_fees(legs)
        return gross_profit - fees

    def calculate_max_quantity(
        self,
        legs: list[ArbitrageLeg],
        orderbook_quantities: dict[str, int],
    ) -> int:
        """Calculate maximum quantity available across all legs.

        Limited by the minimum available quantity across all orderbooks.

        Args:
            legs: List of arbitrage legs
            orderbook_quantities: Dict mapping ticker to available quantity

        Returns:
            Maximum tradeable quantity
        """
        quantities = []
        for leg in legs:
            available = orderbook_quantities.get(leg.ticker, 0)
            quantities.append(available)

        return min(quantities) if quantities else 0

    def calculate_break_even_cost(
        self,
        num_legs: int,
        guaranteed_return: int = PAYOUT_CENTS,
    ) -> int:
        """Calculate break-even total cost accounting for fees.

        Args:
            num_legs: Number of legs in the trade
            guaranteed_return: Guaranteed payout

        Returns:
            Maximum cost that still yields profit
        """
        # Approximate: cost + max_fee = return
        # max_fee ≈ 0.7% * (return - avg_cost_per_leg)
        # Iterative approximation
        for cost in range(guaranteed_return - 1, 0, -1):
            avg_price = cost // num_legs
            max_profit = guaranteed_return - avg_price
            max_fee = int(
                Decimal(max_profit) * self.fee_rate * Decimal("1.5")  # Safety margin
            )
            if cost + max_fee < guaranteed_return:
                return cost

        return guaranteed_return - 1

    def is_profitable(
        self,
        legs: list[ArbitrageLeg],
        min_profit_cents: int = 1,
    ) -> bool:
        """Check if arbitrage opportunity is profitable.

        Args:
            legs: List of arbitrage legs
            min_profit_cents: Minimum required profit

        Returns:
            True if net profit >= min_profit_cents
        """
        return self.calculate_net_profit(legs) >= min_profit_cents

    def profit_summary(
        self,
        legs: list[ArbitrageLeg],
        guaranteed_return: int = PAYOUT_CENTS,
    ) -> dict:
        """Get complete profit summary.

        Args:
            legs: List of arbitrage legs
            guaranteed_return: Guaranteed payout

        Returns:
            Dict with all profit metrics
        """
        total_cost = self.calculate_total_cost(legs)
        gross_profit = self.calculate_gross_profit(total_cost, guaranteed_return)
        fees = self.calculate_total_fees(legs)
        net_profit = gross_profit - fees

        return {
            "total_cost_cents": total_cost,
            "guaranteed_return_cents": guaranteed_return,
            "gross_profit_cents": gross_profit,
            "estimated_fees_cents": fees,
            "net_profit_cents": net_profit,
            "profit_margin_pct": (
                (net_profit / total_cost * 100) if total_cost > 0 else 0
            ),
            "is_profitable": net_profit > 0,
        }
