"""Pydantic data models for Kalshi trading bot."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class OrderSide(str, Enum):
    """Order side (YES or NO)."""

    YES = "yes"
    NO = "no"


class OrderAction(str, Enum):
    """Order action (buy or sell)."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Order type."""

    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    """Order status."""

    RESTING = "resting"
    PENDING = "pending"
    CANCELED = "canceled"
    EXECUTED = "executed"
    PARTIAL = "partial"


class ArbitrageType(str, Enum):
    """Type of arbitrage opportunity."""

    MULTI_OUTCOME = "multi_outcome"
    TIME_BASED = "time_based"
    CORRELATED = "correlated"


class OrderbookLevel(BaseModel):
    """Single price level in orderbook."""

    price: int = Field(..., ge=1, le=99, description="Price in cents")
    quantity: int = Field(..., ge=0, description="Number of contracts")

    @property
    def price_decimal(self) -> Decimal:
        """Get price as decimal (0.01 to 0.99)."""
        return Decimal(self.price) / 100


class Orderbook(BaseModel):
    """Market orderbook state.

    Note: Kalshi returns only bids. The implied ask for YES
    at price X is a NO bid at (100 - X).
    """

    ticker: str
    yes_bids: list[OrderbookLevel] = Field(default_factory=list)
    no_bids: list[OrderbookLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def best_yes_bid(self) -> Optional[int]:
        """Best (highest) YES bid price in cents."""
        if not self.yes_bids:
            return None
        return max(level.price for level in self.yes_bids)

    @property
    def best_no_bid(self) -> Optional[int]:
        """Best (highest) NO bid price in cents."""
        if not self.no_bids:
            return None
        return max(level.price for level in self.no_bids)

    @property
    def best_yes_ask(self) -> Optional[int]:
        """Best (lowest) YES ask price in cents.

        Implied from NO bids: YES ask at X = NO bid at (100 - X).
        """
        if not self.no_bids:
            return None
        best_no = self.best_no_bid
        if best_no is None:
            return None
        return 100 - best_no

    @property
    def best_no_ask(self) -> Optional[int]:
        """Best (lowest) NO ask price in cents.

        Implied from YES bids: NO ask at X = YES bid at (100 - X).
        """
        if not self.yes_bids:
            return None
        best_yes = self.best_yes_bid
        if best_yes is None:
            return None
        return 100 - best_yes

    @property
    def yes_ask_quantity(self) -> int:
        """Quantity available at best YES ask."""
        if not self.no_bids:
            return 0
        best_price = self.best_no_bid
        return sum(
            level.quantity for level in self.no_bids if level.price == best_price
        )

    def get_acquisition_cost(self, side: OrderSide, quantity: int = 1) -> Optional[int]:
        """Get cost to acquire contracts in cents.

        Args:
            side: YES or NO
            quantity: Number of contracts

        Returns:
            Total cost in cents, or None if not enough liquidity
        """
        if side == OrderSide.YES:
            ask_price = self.best_yes_ask
            return ask_price * quantity if ask_price else None
        else:
            ask_price = self.best_no_ask
            return ask_price * quantity if ask_price else None


class Market(BaseModel):
    """Kalshi market information."""

    ticker: str = Field(..., description="Market ticker")
    event_ticker: str = Field(..., description="Parent event ticker")
    title: str = Field(default="")
    subtitle: str = Field(default="")
    status: str = Field(default="open")
    yes_bid: Optional[int] = Field(default=None, description="Best YES bid in cents")
    yes_ask: Optional[int] = Field(default=None, description="Best YES ask in cents")
    no_bid: Optional[int] = Field(default=None, description="Best NO bid in cents")
    no_ask: Optional[int] = Field(default=None, description="Best NO ask in cents")
    volume: int = Field(default=0)
    open_interest: int = Field(default=0)
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None

    @field_validator("yes_bid", "yes_ask", "no_bid", "no_ask", mode="before")
    @classmethod
    def validate_price(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < 1 or v > 99):
            return None
        return v

    @property
    def mid_price(self) -> Optional[float]:
        """Mid price between best bid and ask."""
        if self.yes_bid and self.yes_ask:
            return (self.yes_bid + self.yes_ask) / 2
        return None

    @property
    def spread(self) -> Optional[int]:
        """Spread between best bid and ask in cents."""
        if self.yes_bid and self.yes_ask:
            return self.yes_ask - self.yes_bid
        return None


class Order(BaseModel):
    """Order information."""

    order_id: str
    ticker: str
    client_order_id: Optional[str] = None
    side: OrderSide
    action: OrderAction
    type: OrderType
    status: OrderStatus
    price: int = Field(..., ge=1, le=99, description="Price in cents")
    count: int = Field(..., ge=1, description="Total contracts")
    remaining_count: int = Field(default=0, description="Unfilled contracts")
    created_time: Optional[datetime] = None

    @property
    def filled_count(self) -> int:
        """Number of filled contracts."""
        return self.count - self.remaining_count

    @property
    def is_complete(self) -> bool:
        """Whether order is fully filled or canceled."""
        return self.status in (OrderStatus.EXECUTED, OrderStatus.CANCELED)


class Fill(BaseModel):
    """Trade fill information."""

    fill_id: str
    order_id: str
    ticker: str
    side: OrderSide
    action: OrderAction
    price: int = Field(..., ge=1, le=99)
    count: int = Field(..., ge=1)
    created_time: datetime
    is_taker: bool = False

    @property
    def total_cents(self) -> int:
        """Total cost/proceeds in cents."""
        return self.price * self.count


class Position(BaseModel):
    """Portfolio position."""

    ticker: str
    market_exposure: int = Field(default=0, description="Exposure in cents")
    position: int = Field(default=0, description="Net contracts (positive=YES)")
    resting_orders_count: int = Field(default=0)
    total_traded: int = Field(default=0)

    @property
    def side(self) -> Optional[OrderSide]:
        """Position side (YES if positive, NO if negative)."""
        if self.position > 0:
            return OrderSide.YES
        elif self.position < 0:
            return OrderSide.NO
        return None

    @property
    def contracts(self) -> int:
        """Absolute number of contracts."""
        return abs(self.position)


class ArbitrageLeg(BaseModel):
    """Single leg of an arbitrage trade."""

    ticker: str
    side: OrderSide
    action: OrderAction
    price: int = Field(..., ge=1, le=99)
    quantity: int = Field(..., ge=1)

    @property
    def cost_cents(self) -> int:
        """Cost of this leg in cents."""
        return self.price * self.quantity


class ArbitrageOpportunity(BaseModel):
    """Detected arbitrage opportunity."""

    id: str = Field(..., description="Unique opportunity ID")
    type: ArbitrageType
    event_ticker: str
    legs: list[ArbitrageLeg]
    total_cost_cents: int = Field(..., description="Total cost to execute")
    guaranteed_return_cents: int = Field(default=100, description="Guaranteed payout")
    gross_profit_cents: int = Field(..., description="Profit before fees")
    estimated_fees_cents: int = Field(..., description="Estimated trading fees")
    net_profit_cents: int = Field(..., description="Profit after fees")
    max_quantity: int = Field(..., ge=1, description="Max contracts available")
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    confidence: float = Field(default=1.0, ge=0, le=1, description="Confidence score")

    @property
    def profit_margin(self) -> float:
        """Profit margin as percentage."""
        if self.total_cost_cents == 0:
            return 0.0
        return (self.net_profit_cents / self.total_cost_cents) * 100

    @property
    def is_profitable(self) -> bool:
        """Whether opportunity is profitable after fees."""
        return self.net_profit_cents > 0
