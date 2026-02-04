"""Position and P&L tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config.logging_config import get_logger
from src.core.client import KalshiClient
from src.data.models import Fill, OrderAction, OrderSide, Position

logger = get_logger(__name__)


@dataclass
class PositionSummary:
    """Summary of current positions."""

    total_positions: int = 0
    total_exposure_cents: int = 0
    total_unrealized_pnl_cents: int = 0
    positions_by_ticker: dict = field(default_factory=dict)


@dataclass
class PnLSummary:
    """Profit and loss summary."""

    realized_pnl_cents: int = 0
    unrealized_pnl_cents: int = 0
    total_fees_cents: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    @property
    def total_pnl_cents(self) -> int:
        return self.realized_pnl_cents + self.unrealized_pnl_cents

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades


class PositionTracker:
    """Tracks positions and calculates P&L.

    Monitors:
    - Current portfolio positions
    - Realized P&L from closed trades
    - Unrealized P&L from open positions
    - Trading fees
    """

    # Kalshi fee rate
    FEE_RATE = 0.007

    def __init__(self, client: KalshiClient) -> None:
        """Initialize position tracker.

        Args:
            client: Kalshi API client
        """
        self.client = client
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._balance_cents: int = 0
        self._last_sync: Optional[datetime] = None

    async def sync_positions(self) -> dict[str, Position]:
        """Sync positions from API.

        Returns:
            Dict mapping ticker to position
        """
        response = await self.client.get_positions()
        self._positions.clear()

        for pos_data in response.get("market_positions", []):
            position = Position(
                ticker=pos_data.get("ticker", ""),
                market_exposure=pos_data.get("market_exposure", 0),
                position=pos_data.get("position", 0),
                resting_orders_count=pos_data.get("resting_orders_count", 0),
                total_traded=pos_data.get("total_traded", 0),
            )
            self._positions[position.ticker] = position

        self._last_sync = datetime.utcnow()

        logger.info(
            "Positions synced",
            count=len(self._positions),
        )

        return dict(self._positions)

    async def sync_balance(self) -> int:
        """Sync account balance from API.

        Returns:
            Balance in cents
        """
        response = await self.client.get_balance()
        self._balance_cents = response.get("balance", 0)

        logger.info("Balance synced", balance_cents=self._balance_cents)

        return self._balance_cents

    async def sync_fills(self, limit: int = 100) -> list[Fill]:
        """Sync recent fills from API.

        Args:
            limit: Maximum fills to fetch

        Returns:
            List of fills
        """
        response = await self.client.get_fills(limit=limit)
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

        self._fills = fills
        return fills

    def get_position(self, ticker: str) -> Optional[Position]:
        """Get position for a ticker.

        Args:
            ticker: Market ticker

        Returns:
            Position or None
        """
        return self._positions.get(ticker)

    def get_position_summary(self) -> PositionSummary:
        """Get summary of all positions.

        Returns:
            Position summary
        """
        summary = PositionSummary()

        for ticker, position in self._positions.items():
            if position.contracts > 0:
                summary.total_positions += 1
                summary.total_exposure_cents += position.market_exposure
                summary.positions_by_ticker[ticker] = {
                    "contracts": position.contracts,
                    "side": position.side.value if position.side else None,
                    "exposure_cents": position.market_exposure,
                }

        return summary

    def calculate_pnl(self) -> PnLSummary:
        """Calculate P&L from fills.

        Returns:
            P&L summary
        """
        pnl = PnLSummary()

        # Group fills by ticker
        fills_by_ticker: dict[str, list[Fill]] = {}
        for fill in self._fills:
            fills_by_ticker.setdefault(fill.ticker, []).append(fill)

        # Calculate realized P&L for closed positions
        for ticker, fills in fills_by_ticker.items():
            ticker_pnl = self._calculate_ticker_pnl(fills)
            pnl.realized_pnl_cents += ticker_pnl["realized"]
            pnl.total_fees_cents += ticker_pnl["fees"]
            pnl.total_trades += ticker_pnl["trades"]

        # Unrealized P&L would require current prices
        # This is a simplification
        pnl.unrealized_pnl_cents = 0

        return pnl

    def _calculate_ticker_pnl(self, fills: list[Fill]) -> dict:
        """Calculate P&L for a single ticker's fills.

        Args:
            fills: Fills for one ticker

        Returns:
            Dict with realized, fees, trades
        """
        result = {"realized": 0, "fees": 0, "trades": len(fills)}

        # Simple FIFO calculation
        buys: list[tuple[int, int]] = []  # (price, count)
        sells: list[tuple[int, int]] = []

        for fill in sorted(fills, key=lambda f: f.created_time):
            if fill.action == OrderAction.BUY:
                buys.append((fill.price, fill.count))
            else:
                sells.append((fill.price, fill.count))

            # Calculate fee
            if fill.action == OrderAction.BUY:
                potential_profit = 100 - fill.price
            else:
                potential_profit = fill.price
            fee = int(potential_profit * self.FEE_RATE * fill.count)
            result["fees"] += fee

        # Match buys with sells (FIFO)
        while buys and sells:
            buy_price, buy_count = buys[0]
            sell_price, sell_count = sells[0]

            matched = min(buy_count, sell_count)
            pnl = (sell_price - buy_price) * matched
            result["realized"] += pnl

            # Update remaining
            if buy_count > matched:
                buys[0] = (buy_price, buy_count - matched)
            else:
                buys.pop(0)

            if sell_count > matched:
                sells[0] = (sell_price, sell_count - matched)
            else:
                sells.pop(0)

        return result

    def get_exposure_by_market(self) -> dict[str, int]:
        """Get exposure in cents by market.

        Returns:
            Dict mapping ticker to exposure
        """
        return {
            ticker: pos.market_exposure
            for ticker, pos in self._positions.items()
            if pos.market_exposure > 0
        }

    def get_total_exposure(self) -> int:
        """Get total exposure across all markets.

        Returns:
            Total exposure in cents
        """
        return sum(pos.market_exposure for pos in self._positions.values())

    @property
    def balance_cents(self) -> int:
        """Current balance in cents."""
        return self._balance_cents

    @property
    def balance_dollars(self) -> float:
        """Current balance in dollars."""
        return self._balance_cents / 100

    @property
    def positions(self) -> dict[str, Position]:
        """Current positions."""
        return dict(self._positions)

    @property
    def last_sync(self) -> Optional[datetime]:
        """Time of last sync."""
        return self._last_sync
