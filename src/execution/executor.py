"""Arbitrage execution engine."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from config.logging_config import get_logger
from src.arbitrage.detector import ArbitrageDetector
from src.core.exceptions import CircuitBreakerOpenError, OrderError
from src.data.models import ArbitrageOpportunity, Orderbook
from src.data.orderbook_manager import OrderbookManager
from .order_manager import OrderGroup, OrderGroupStatus, OrderManager

logger = get_logger(__name__)


@dataclass
class ExecutionResult:
    """Result of executing an arbitrage opportunity."""

    opportunity_id: str
    order_group_id: Optional[str]
    success: bool
    profit_cents: int
    error: Optional[str] = None
    executed_at: datetime = None

    def __post_init__(self):
        if self.executed_at is None:
            self.executed_at = datetime.utcnow()


class Executor:
    """Executes arbitrage opportunities.

    Coordinates:
    - Pre-execution validation
    - Risk checks
    - Order submission
    - Post-execution tracking
    """

    def __init__(
        self,
        order_manager: OrderManager,
        orderbook_manager: OrderbookManager,
        detector: ArbitrageDetector,
        circuit_breaker: Optional[object] = None,
        max_concurrent: int = 3,
    ) -> None:
        """Initialize executor.

        Args:
            order_manager: Order management
            orderbook_manager: Orderbook state
            detector: Arbitrage detector for validation
            circuit_breaker: Optional circuit breaker for risk control
            max_concurrent: Max concurrent executions
        """
        self.order_manager = order_manager
        self.orderbook_manager = orderbook_manager
        self.detector = detector
        self.circuit_breaker = circuit_breaker
        self.max_concurrent = max_concurrent

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._execution_history: list[ExecutionResult] = []
        self._callbacks: list[Callable[[ExecutionResult], None]] = []

    def on_execution(self, callback: Callable[[ExecutionResult], None]) -> None:
        """Register callback for execution results.

        Args:
            callback: Function called with ExecutionResult
        """
        self._callbacks.append(callback)

    async def execute(
        self,
        opportunity: ArbitrageOpportunity,
        quantity: Optional[int] = None,
        validate: bool = True,
    ) -> ExecutionResult:
        """Execute an arbitrage opportunity.

        Args:
            opportunity: Opportunity to execute
            quantity: Contracts per leg (defaults to max available)
            validate: Whether to validate before execution

        Returns:
            Execution result
        """
        async with self._semaphore:
            return await self._execute_internal(opportunity, quantity, validate)

    async def _execute_internal(
        self,
        opportunity: ArbitrageOpportunity,
        quantity: Optional[int],
        validate: bool,
    ) -> ExecutionResult:
        """Internal execution logic."""
        logger.info(
            "Starting execution",
            opportunity_id=opportunity.id,
            type=opportunity.type.value,
            net_profit=opportunity.net_profit_cents,
        )

        # Check circuit breaker
        if self.circuit_breaker:
            try:
                # Circuit breaker check would go here
                pass
            except CircuitBreakerOpenError as e:
                return ExecutionResult(
                    opportunity_id=opportunity.id,
                    order_group_id=None,
                    success=False,
                    profit_cents=0,
                    error=str(e),
                )

        # Validate opportunity still exists
        if validate:
            orderbooks = self.orderbook_manager.get_all_orderbooks()
            if not self.detector.validate_opportunity(opportunity, orderbooks):
                logger.warning(
                    "Opportunity no longer valid",
                    opportunity_id=opportunity.id,
                )
                return ExecutionResult(
                    opportunity_id=opportunity.id,
                    order_group_id=None,
                    success=False,
                    profit_cents=0,
                    error="Opportunity no longer valid",
                )

        # Determine quantity
        qty = quantity or opportunity.max_quantity
        qty = min(qty, opportunity.max_quantity)

        if qty <= 0:
            return ExecutionResult(
                opportunity_id=opportunity.id,
                order_group_id=None,
                success=False,
                profit_cents=0,
                error="No quantity available",
            )

        try:
            # Create and submit order group
            group = await self.order_manager.create_order_group(opportunity, qty)
            group = await self.order_manager.submit_order_group(group)

            # Determine result
            if group.status == OrderGroupStatus.COMPLETE:
                profit = opportunity.net_profit_cents * qty
                result = ExecutionResult(
                    opportunity_id=opportunity.id,
                    order_group_id=group.id,
                    success=True,
                    profit_cents=profit,
                )
                logger.info(
                    "Execution successful",
                    opportunity_id=opportunity.id,
                    group_id=group.id,
                    profit_cents=profit,
                )

            elif group.status == OrderGroupStatus.PARTIAL:
                # Partial fill - leg risk realized
                result = ExecutionResult(
                    opportunity_id=opportunity.id,
                    order_group_id=group.id,
                    success=False,
                    profit_cents=0,  # Uncertain profit due to partial
                    error=f"Partial fill: {group.filled_legs}/{len(group.legs)} legs",
                )
                logger.error(
                    "Partial execution - leg risk!",
                    opportunity_id=opportunity.id,
                    group_id=group.id,
                    filled_legs=group.filled_legs,
                )

            else:
                result = ExecutionResult(
                    opportunity_id=opportunity.id,
                    order_group_id=group.id,
                    success=False,
                    profit_cents=0,
                    error=group.error or "Execution failed",
                )
                logger.error(
                    "Execution failed",
                    opportunity_id=opportunity.id,
                    error=group.error,
                )

        except OrderError as e:
            result = ExecutionResult(
                opportunity_id=opportunity.id,
                order_group_id=None,
                success=False,
                profit_cents=0,
                error=str(e),
            )
            logger.error(
                "Order error during execution",
                opportunity_id=opportunity.id,
                error=str(e),
            )

        except Exception as e:
            result = ExecutionResult(
                opportunity_id=opportunity.id,
                order_group_id=None,
                success=False,
                profit_cents=0,
                error=f"Unexpected error: {e}",
            )
            logger.exception(
                "Unexpected execution error",
                opportunity_id=opportunity.id,
            )

        # Record and notify
        self._execution_history.append(result)
        self._notify_callbacks(result)

        return result

    async def execute_batch(
        self,
        opportunities: list[ArbitrageOpportunity],
        max_parallel: int = 3,
    ) -> list[ExecutionResult]:
        """Execute multiple opportunities.

        Args:
            opportunities: Opportunities to execute
            max_parallel: Max parallel executions

        Returns:
            List of execution results
        """
        # Sort by profit (highest first)
        sorted_opps = sorted(
            opportunities,
            key=lambda o: o.net_profit_cents,
            reverse=True,
        )

        results = []
        for opp in sorted_opps[:max_parallel]:
            result = await self.execute(opp)
            results.append(result)

            # Stop if circuit breaker trips
            if self.circuit_breaker and not result.success:
                # Check if we should continue
                pass

        return results

    def _notify_callbacks(self, result: ExecutionResult) -> None:
        """Notify all registered callbacks."""
        for callback in self._callbacks:
            try:
                callback(result)
            except Exception as e:
                logger.error("Callback error", error=str(e))

    @property
    def execution_history(self) -> list[ExecutionResult]:
        """Get execution history."""
        return list(self._execution_history)

    @property
    def successful_executions(self) -> int:
        """Count of successful executions."""
        return sum(1 for r in self._execution_history if r.success)

    @property
    def total_profit_cents(self) -> int:
        """Total profit from all executions in cents."""
        return sum(r.profit_cents for r in self._execution_history)

    def clear_history(self) -> None:
        """Clear execution history."""
        self._execution_history.clear()
