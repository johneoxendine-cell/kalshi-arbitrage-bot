"""Position and exposure limit management."""

import asyncio
from dataclasses import dataclass
from typing import Optional

from config.logging_config import get_logger
from src.data.models import ArbitrageOpportunity, Position
from src.execution.position_tracker import PositionTracker

logger = get_logger(__name__)


@dataclass
class ExposureLimits:
    """Exposure limit configuration."""

    max_total_exposure_cents: int = 50000  # $500
    max_position_per_market: int = 100  # contracts
    max_exposure_per_market_cents: int = 10000  # $100
    max_concurrent_trades: int = 5


@dataclass
class ExposureCheck:
    """Result of pre-trade exposure check."""

    allowed: bool
    reason: Optional[str] = None
    max_allowed_quantity: int = 0
    current_exposure_cents: int = 0
    limit_exposure_cents: int = 0


class ExposureManager:
    """Manages position exposure and enforces limits.

    Pre-trade checks:
    - Total exposure doesn't exceed limit
    - Per-market position doesn't exceed limit
    - Per-market exposure doesn't exceed limit
    """

    def __init__(
        self,
        position_tracker: PositionTracker,
        limits: Optional[ExposureLimits] = None,
    ) -> None:
        """Initialize exposure manager.

        Args:
            position_tracker: Position tracking service
            limits: Exposure limits configuration
        """
        self.position_tracker = position_tracker
        self.limits = limits or ExposureLimits()
        self._lock = asyncio.Lock()

    async def check_trade(
        self,
        opportunity: ArbitrageOpportunity,
        quantity: int = 1,
    ) -> ExposureCheck:
        """Check if a trade is allowed under current exposure limits.

        Args:
            opportunity: Arbitrage opportunity to check
            quantity: Number of contracts per leg

        Returns:
            ExposureCheck with allowed status and details
        """
        async with self._lock:
            # Get current positions
            positions = self.position_tracker.positions
            current_total = self.position_tracker.get_total_exposure()

            # Calculate new exposure from trade
            new_exposure = opportunity.total_cost_cents * quantity

            # Check total exposure limit
            projected_total = current_total + new_exposure
            if projected_total > self.limits.max_total_exposure_cents:
                return ExposureCheck(
                    allowed=False,
                    reason=f"Would exceed total exposure limit: ${projected_total / 100:.2f} > ${self.limits.max_total_exposure_cents / 100:.2f}",
                    max_allowed_quantity=self._calculate_max_quantity(
                        current_total,
                        opportunity.total_cost_cents,
                        self.limits.max_total_exposure_cents,
                    ),
                    current_exposure_cents=current_total,
                    limit_exposure_cents=self.limits.max_total_exposure_cents,
                )

            # Check per-market limits
            for leg in opportunity.legs:
                position = positions.get(leg.ticker)
                current_contracts = position.contracts if position else 0

                if current_contracts + quantity > self.limits.max_position_per_market:
                    return ExposureCheck(
                        allowed=False,
                        reason=f"Would exceed position limit for {leg.ticker}: {current_contracts + quantity} > {self.limits.max_position_per_market}",
                        max_allowed_quantity=self.limits.max_position_per_market - current_contracts,
                        current_exposure_cents=current_total,
                        limit_exposure_cents=self.limits.max_total_exposure_cents,
                    )

                # Check per-market exposure
                current_market_exposure = position.market_exposure if position else 0
                new_market_exposure = current_market_exposure + (leg.price * quantity)

                if new_market_exposure > self.limits.max_exposure_per_market_cents:
                    return ExposureCheck(
                        allowed=False,
                        reason=f"Would exceed per-market exposure for {leg.ticker}",
                        max_allowed_quantity=self._calculate_max_quantity(
                            current_market_exposure,
                            leg.price,
                            self.limits.max_exposure_per_market_cents,
                        ),
                        current_exposure_cents=current_total,
                        limit_exposure_cents=self.limits.max_total_exposure_cents,
                    )

            # All checks passed
            return ExposureCheck(
                allowed=True,
                max_allowed_quantity=quantity,
                current_exposure_cents=current_total,
                limit_exposure_cents=self.limits.max_total_exposure_cents,
            )

    def _calculate_max_quantity(
        self,
        current: int,
        per_unit_cost: int,
        limit: int,
    ) -> int:
        """Calculate maximum quantity that fits within limit."""
        if per_unit_cost <= 0:
            return 0
        remaining = limit - current
        return max(0, remaining // per_unit_cost)

    async def get_available_exposure(self) -> int:
        """Get remaining available exposure in cents.

        Returns:
            Available exposure in cents
        """
        current = self.position_tracker.get_total_exposure()
        return max(0, self.limits.max_total_exposure_cents - current)

    async def get_utilization(self) -> float:
        """Get exposure utilization as percentage.

        Returns:
            Utilization 0-100
        """
        current = self.position_tracker.get_total_exposure()
        if self.limits.max_total_exposure_cents == 0:
            return 100.0
        return (current / self.limits.max_total_exposure_cents) * 100

    def get_market_limits(self, ticker: str) -> dict:
        """Get limits info for a specific market.

        Args:
            ticker: Market ticker

        Returns:
            Dict with current and limit values
        """
        position = self.position_tracker.get_position(ticker)
        current_contracts = position.contracts if position else 0
        current_exposure = position.market_exposure if position else 0

        return {
            "ticker": ticker,
            "current_contracts": current_contracts,
            "max_contracts": self.limits.max_position_per_market,
            "available_contracts": self.limits.max_position_per_market - current_contracts,
            "current_exposure_cents": current_exposure,
            "max_exposure_cents": self.limits.max_exposure_per_market_cents,
            "available_exposure_cents": max(
                0, self.limits.max_exposure_per_market_cents - current_exposure
            ),
        }

    def get_summary(self) -> dict:
        """Get exposure summary.

        Returns:
            Summary dictionary
        """
        current_total = self.position_tracker.get_total_exposure()
        exposure_by_market = self.position_tracker.get_exposure_by_market()

        return {
            "total_exposure_cents": current_total,
            "max_exposure_cents": self.limits.max_total_exposure_cents,
            "available_cents": max(0, self.limits.max_total_exposure_cents - current_total),
            "utilization_pct": (current_total / self.limits.max_total_exposure_cents * 100)
            if self.limits.max_total_exposure_cents > 0
            else 0,
            "markets_count": len(exposure_by_market),
            "per_market_limit_cents": self.limits.max_exposure_per_market_cents,
            "per_market_position_limit": self.limits.max_position_per_market,
        }

    async def adjust_quantity_for_limits(
        self,
        opportunity: ArbitrageOpportunity,
        desired_quantity: int,
    ) -> int:
        """Adjust trade quantity to fit within limits.

        Args:
            opportunity: Arbitrage opportunity
            desired_quantity: Desired quantity

        Returns:
            Maximum allowed quantity (may be less than desired)
        """
        check = await self.check_trade(opportunity, desired_quantity)

        if check.allowed:
            return desired_quantity

        # Binary search for maximum allowed quantity
        low, high = 0, desired_quantity

        while low < high:
            mid = (low + high + 1) // 2
            check = await self.check_trade(opportunity, mid)
            if check.allowed:
                low = mid
            else:
                high = mid - 1

        return low
