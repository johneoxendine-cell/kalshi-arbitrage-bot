"""Alert notifications via Slack and Discord webhooks."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

import aiohttp

from config.logging_config import get_logger

logger = get_logger(__name__)


class AlertLevel(str, Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Alert message."""

    level: AlertLevel
    title: str
    message: str
    timestamp: datetime = None
    details: Optional[dict] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class AlertManager:
    """Manages alert notifications to Slack and Discord.

    Sends alerts for:
    - Circuit breaker trips (CRITICAL)
    - Large losses (ERROR)
    - Opportunities detected (INFO)
    - Trades executed (INFO)
    - Connection issues (WARNING)
    """

    LEVEL_COLORS = {
        AlertLevel.INFO: "#36a64f",  # Green
        AlertLevel.WARNING: "#ff9800",  # Orange
        AlertLevel.ERROR: "#f44336",  # Red
        AlertLevel.CRITICAL: "#9c27b0",  # Purple
    }

    LEVEL_EMOJI = {
        AlertLevel.INFO: ":information_source:",
        AlertLevel.WARNING: ":warning:",
        AlertLevel.ERROR: ":x:",
        AlertLevel.CRITICAL: ":rotating_light:",
    }

    def __init__(
        self,
        slack_webhook: Optional[str] = None,
        discord_webhook: Optional[str] = None,
        min_level: AlertLevel = AlertLevel.INFO,
        rate_limit_seconds: int = 60,
    ) -> None:
        """Initialize alert manager.

        Args:
            slack_webhook: Slack webhook URL
            discord_webhook: Discord webhook URL
            min_level: Minimum alert level to send
            rate_limit_seconds: Minimum seconds between same alerts
        """
        self.slack_webhook = slack_webhook
        self.discord_webhook = discord_webhook
        self.min_level = min_level
        self.rate_limit_seconds = rate_limit_seconds

        self._session: Optional[aiohttp.ClientSession] = None
        self._last_alerts: dict[str, datetime] = {}
        self._alert_counts: dict[str, int] = {}

        logger.info(
            "Alert manager initialized",
            slack_configured=bool(slack_webhook),
            discord_configured=bool(discord_webhook),
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _should_send(self, alert: Alert) -> bool:
        """Check if alert should be sent based on level and rate limit."""
        # Check level
        level_order = list(AlertLevel)
        if level_order.index(alert.level) < level_order.index(self.min_level):
            return False

        # Check rate limit
        alert_key = f"{alert.level}:{alert.title}"
        last_time = self._last_alerts.get(alert_key)

        if last_time:
            elapsed = (datetime.utcnow() - last_time).total_seconds()
            if elapsed < self.rate_limit_seconds:
                # Increment suppressed count
                self._alert_counts[alert_key] = self._alert_counts.get(alert_key, 0) + 1
                return False

        self._last_alerts[alert_key] = datetime.utcnow()
        self._alert_counts[alert_key] = 0
        return True

    async def send(self, alert: Alert) -> bool:
        """Send an alert to configured webhooks.

        Args:
            alert: Alert to send

        Returns:
            True if sent successfully
        """
        if not self._should_send(alert):
            logger.debug("Alert rate limited", title=alert.title)
            return False

        success = True
        tasks = []

        if self.slack_webhook:
            tasks.append(self._send_slack(alert))

        if self.discord_webhook:
            tasks.append(self._send_discord(alert))

        if not tasks:
            logger.warning("No webhooks configured")
            return False

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("Alert send failed", error=str(result))
                success = False
            elif not result:
                success = False

        return success

    async def _send_slack(self, alert: Alert) -> bool:
        """Send alert to Slack."""
        payload = {
            "attachments": [
                {
                    "color": self.LEVEL_COLORS[alert.level],
                    "title": f"{self.LEVEL_EMOJI[alert.level]} {alert.title}",
                    "text": alert.message,
                    "ts": int(alert.timestamp.timestamp()),
                    "footer": "Kalshi Arbitrage Bot",
                }
            ]
        }

        if alert.details:
            fields = [
                {"title": k, "value": str(v), "short": True}
                for k, v in alert.details.items()
            ]
            payload["attachments"][0]["fields"] = fields

        try:
            session = await self._get_session()
            async with session.post(self.slack_webhook, json=payload) as resp:
                success = resp.status == 200
                if not success:
                    logger.error("Slack webhook failed", status=resp.status)
                return success

        except Exception as e:
            logger.error("Slack send error", error=str(e))
            return False

    async def _send_discord(self, alert: Alert) -> bool:
        """Send alert to Discord."""
        # Discord uses decimal colors
        color_map = {
            AlertLevel.INFO: 3592283,  # Green
            AlertLevel.WARNING: 16750848,  # Orange
            AlertLevel.ERROR: 15930932,  # Red
            AlertLevel.CRITICAL: 10233904,  # Purple
        }

        payload = {
            "embeds": [
                {
                    "title": f"{self.LEVEL_EMOJI[alert.level]} {alert.title}",
                    "description": alert.message,
                    "color": color_map[alert.level],
                    "timestamp": alert.timestamp.isoformat(),
                    "footer": {"text": "Kalshi Arbitrage Bot"},
                }
            ]
        }

        if alert.details:
            fields = [
                {"name": k, "value": str(v), "inline": True}
                for k, v in alert.details.items()
            ]
            payload["embeds"][0]["fields"] = fields

        try:
            session = await self._get_session()
            async with session.post(self.discord_webhook, json=payload) as resp:
                success = resp.status in (200, 204)
                if not success:
                    logger.error("Discord webhook failed", status=resp.status)
                return success

        except Exception as e:
            logger.error("Discord send error", error=str(e))
            return False

    # Convenience methods for common alerts

    async def alert_opportunity_detected(
        self,
        arb_type: str,
        event_ticker: str,
        profit_cents: int,
    ) -> None:
        """Send alert for detected opportunity."""
        await self.send(
            Alert(
                level=AlertLevel.INFO,
                title="Arbitrage Opportunity Detected",
                message=f"Found {arb_type} arbitrage in {event_ticker}",
                details={
                    "Type": arb_type,
                    "Event": event_ticker,
                    "Profit": f"${profit_cents / 100:.2f}",
                },
            )
        )

    async def alert_trade_executed(
        self,
        event_ticker: str,
        profit_cents: int,
        legs: int,
    ) -> None:
        """Send alert for executed trade."""
        await self.send(
            Alert(
                level=AlertLevel.INFO,
                title="Trade Executed",
                message=f"Successfully executed {legs}-leg arbitrage",
                details={
                    "Event": event_ticker,
                    "Profit": f"${profit_cents / 100:.2f}",
                    "Legs": legs,
                },
            )
        )

    async def alert_trade_failed(
        self,
        event_ticker: str,
        error: str,
    ) -> None:
        """Send alert for failed trade."""
        await self.send(
            Alert(
                level=AlertLevel.ERROR,
                title="Trade Failed",
                message=f"Failed to execute arbitrage: {error}",
                details={
                    "Event": event_ticker,
                    "Error": error,
                },
            )
        )

    async def alert_circuit_breaker(
        self,
        reason: str,
        daily_loss: int,
        exposure: int,
    ) -> None:
        """Send alert for circuit breaker trip."""
        await self.send(
            Alert(
                level=AlertLevel.CRITICAL,
                title="Circuit Breaker Tripped",
                message=f"Trading halted: {reason}",
                details={
                    "Reason": reason,
                    "Daily Loss": f"${daily_loss / 100:.2f}",
                    "Exposure": f"${exposure / 100:.2f}",
                },
            )
        )

    async def alert_connection_issue(
        self,
        component: str,
        error: str,
    ) -> None:
        """Send alert for connection issue."""
        await self.send(
            Alert(
                level=AlertLevel.WARNING,
                title="Connection Issue",
                message=f"{component} connection problem",
                details={
                    "Component": component,
                    "Error": error,
                },
            )
        )

    async def alert_large_loss(
        self,
        loss_cents: int,
        market: str,
    ) -> None:
        """Send alert for large loss."""
        await self.send(
            Alert(
                level=AlertLevel.ERROR,
                title="Large Loss Detected",
                message=f"Significant loss on {market}",
                details={
                    "Loss": f"${loss_cents / 100:.2f}",
                    "Market": market,
                },
            )
        )
