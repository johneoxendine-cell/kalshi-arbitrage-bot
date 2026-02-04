"""Order lifecycle management."""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from config.logging_config import get_logger
from src.core.client import KalshiClient
from src.core.exceptions import OrderError
from src.data.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    Fill,
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
)

logger = get_logger(__name__)


class OrderGroupStatus(str, Enum):
    """Status of an order group."""

    PENDING = "pending"
    SUBMITTING = "submitting"
    PARTIAL = "partial"  # Some legs filled
    COMPLETE = "complete"  # All legs filled
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class OrderGroup:
    """Group of orders for an arbitrage trade."""

    id: str
    opportunity_id: str
    legs: list[ArbitrageLeg]
    orders: dict[str, Order] = field(default_factory=dict)  # ticker -> Order
    fills: list[Fill] = field(default_factory=list)
    status: OrderGroupStatus = OrderGroupStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        return self.status in (
            OrderGroupStatus.COMPLETE,
            OrderGroupStatus.FAILED,
            OrderGroupStatus.CANCELED,
        )

    @property
    def filled_legs(self) -> int:
        return sum(
            1
            for order in self.orders.values()
            if order.status == OrderStatus.EXECUTED
        )

    @property
    def total_filled_count(self) -> int:
        return sum(order.filled_count for order in self.orders.values())


class OrderManager:
    """Manages order creation, tracking, and lifecycle.

    Handles:
    - Creating order groups for arbitrage opportunities
    - Submitting orders with IOC (immediate-or-cancel)
    - Tracking fills and partial fills
    - Handling leg risk (partial execution)
    """

    def __init__(self, client: KalshiClient) -> None:
        """Initialize order manager.

        Args:
            client: Kalshi API client
        """
        self.client = client
        self._order_groups: dict[str, OrderGroup] = {}
        self._orders: dict[str, Order] = {}  # order_id -> Order
        self._lock = asyncio.Lock()

    async def create_order_group(
        self,
        opportunity: ArbitrageOpportunity,
        quantity: int = 1,
    ) -> OrderGroup:
        """Create an order group for an arbitrage opportunity.

        Args:
            opportunity: Detected arbitrage opportunity
            quantity: Number of contracts per leg

        Returns:
            Created order group
        """
        # Adjust leg quantities
        legs = []
        for leg in opportunity.legs:
            adjusted_leg = ArbitrageLeg(
                ticker=leg.ticker,
                side=leg.side,
                action=leg.action,
                price=leg.price,
                quantity=min(quantity, opportunity.max_quantity),
            )
            legs.append(adjusted_leg)

        group = OrderGroup(
            id=str(uuid.uuid4()),
            opportunity_id=opportunity.id,
            legs=legs,
        )

        async with self._lock:
            self._order_groups[group.id] = group

        logger.info(
            "Order group created",
            group_id=group.id,
            opportunity_id=opportunity.id,
            num_legs=len(legs),
            quantity=quantity,
        )

        return group

    async def submit_order(
        self,
        leg: ArbitrageLeg,
        client_order_id: Optional[str] = None,
        use_ioc: bool = True,
    ) -> Order:
        """Submit a single order.

        Args:
            leg: Arbitrage leg to execute
            client_order_id: Optional client-side order ID
            use_ioc: Use immediate-or-cancel order type

        Returns:
            Created order
        """
        client_id = client_order_id or str(uuid.uuid4())

        # Determine price parameter based on side
        price_params = {}
        if leg.side == OrderSide.YES:
            price_params["yes_price"] = leg.price
        else:
            price_params["no_price"] = leg.price

        try:
            response = await self.client.create_order(
                ticker=leg.ticker,
                side=leg.side.value,
                action=leg.action.value,
                count=leg.quantity,
                type="limit",  # IOC is limit with short expiration
                client_order_id=client_id,
                **price_params,
            )

            order_data = response.get("order", response)
            order = self._parse_order(order_data)

            async with self._lock:
                self._orders[order.order_id] = order

            logger.info(
                "Order submitted",
                order_id=order.order_id,
                ticker=leg.ticker,
                side=leg.side.value,
                action=leg.action.value,
                price=leg.price,
                quantity=leg.quantity,
            )

            return order

        except Exception as e:
            logger.error(
                "Order submission failed",
                ticker=leg.ticker,
                error=str(e),
            )
            raise OrderError(f"Failed to submit order: {e}")

    async def submit_order_group(
        self,
        group: OrderGroup,
        use_ioc: bool = True,
    ) -> OrderGroup:
        """Submit all orders in a group.

        Uses IOC orders to minimize leg risk. If any leg fails,
        attempts to cancel remaining orders.

        Args:
            group: Order group to submit
            use_ioc: Use immediate-or-cancel orders

        Returns:
            Updated order group
        """
        group.status = OrderGroupStatus.SUBMITTING

        try:
            # Submit all legs
            for leg in group.legs:
                client_id = f"{group.id}-{leg.ticker}"
                order = await self.submit_order(leg, client_id, use_ioc)
                group.orders[leg.ticker] = order

            # Check results
            filled_count = sum(
                1
                for order in group.orders.values()
                if order.status == OrderStatus.EXECUTED
            )

            if filled_count == len(group.legs):
                group.status = OrderGroupStatus.COMPLETE
                group.completed_at = datetime.utcnow()
                logger.info("Order group complete", group_id=group.id)

            elif filled_count > 0:
                group.status = OrderGroupStatus.PARTIAL
                logger.warning(
                    "Order group partial fill - leg risk!",
                    group_id=group.id,
                    filled=filled_count,
                    total=len(group.legs),
                )

            else:
                group.status = OrderGroupStatus.FAILED
                group.error = "No legs filled"

        except Exception as e:
            group.status = OrderGroupStatus.FAILED
            group.error = str(e)
            logger.error("Order group failed", group_id=group.id, error=str(e))

            # Attempt to cancel any submitted orders
            await self._cancel_group_orders(group)

        return group

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order.

        Args:
            order_id: Order ID to cancel

        Returns:
            True if cancellation successful
        """
        try:
            await self.client.cancel_order(order_id)

            async with self._lock:
                if order_id in self._orders:
                    self._orders[order_id].status = OrderStatus.CANCELED

            logger.info("Order canceled", order_id=order_id)
            return True

        except Exception as e:
            logger.error("Cancel failed", order_id=order_id, error=str(e))
            return False

    async def _cancel_group_orders(self, group: OrderGroup) -> None:
        """Cancel all unfilled orders in a group."""
        for ticker, order in group.orders.items():
            if order.status == OrderStatus.RESTING:
                await self.cancel_order(order.order_id)

    async def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get current order status.

        Args:
            order_id: Order ID

        Returns:
            Order with current status
        """
        try:
            response = await self.client.get(f"/portfolio/orders/{order_id}")
            order = self._parse_order(response.get("order", response))

            async with self._lock:
                self._orders[order_id] = order

            return order

        except Exception as e:
            logger.error("Failed to get order status", order_id=order_id, error=str(e))
            return self._orders.get(order_id)

    async def get_fills(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> list[Fill]:
        """Get recent trade fills.

        Args:
            ticker: Optional ticker filter
            limit: Maximum fills to return

        Returns:
            List of fills
        """
        response = await self.client.get_fills(ticker=ticker, limit=limit)
        fills = []

        for fill_data in response.get("fills", []):
            fill = Fill(
                fill_id=fill_data.get("fill_id", ""),
                order_id=fill_data.get("order_id", ""),
                ticker=fill_data.get("ticker", ""),
                side=OrderSide(fill_data.get("side", "yes")),
                action=OrderAction(fill_data.get("action", "buy")),
                price=fill_data.get("price", 0),
                count=fill_data.get("count", 0),
                created_time=datetime.fromisoformat(
                    fill_data.get("created_time", "").replace("Z", "+00:00")
                ),
                is_taker=fill_data.get("is_taker", False),
            )
            fills.append(fill)

        return fills

    def get_order_group(self, group_id: str) -> Optional[OrderGroup]:
        """Get order group by ID."""
        return self._order_groups.get(group_id)

    def get_pending_groups(self) -> list[OrderGroup]:
        """Get all non-complete order groups."""
        return [
            group
            for group in self._order_groups.values()
            if not group.is_complete
        ]

    def _parse_order(self, data: dict) -> Order:
        """Parse order from API response."""
        return Order(
            order_id=data.get("order_id", ""),
            ticker=data.get("ticker", ""),
            client_order_id=data.get("client_order_id"),
            side=OrderSide(data.get("side", "yes")),
            action=OrderAction(data.get("action", "buy")),
            type=OrderType(data.get("type", "limit")),
            status=OrderStatus(data.get("status", "pending")),
            price=data.get("yes_price") or data.get("no_price") or 0,
            count=data.get("count", 0),
            remaining_count=data.get("remaining_count", 0),
            created_time=self._parse_datetime(data.get("created_time")),
        )

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Parse datetime from API response."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
