"""Pytest configuration and shared fixtures."""

import pytest
import asyncio
from typing import Generator


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_settings():
    """Create mock settings for testing."""
    from unittest.mock import MagicMock
    from pathlib import Path

    settings = MagicMock()
    settings.kalshi_api_key_id = "test-key-id"
    settings.kalshi_private_key_path = Path("/tmp/test-key.pem")
    settings.environment.value = "development"
    settings.base_url = "https://demo-api.kalshi.co/trade-api/v2"
    settings.websocket_url = "wss://demo-api.kalshi.co/trade-api/v2/ws"
    settings.min_profit_cents = 2
    settings.max_position_per_market = 100
    settings.max_exposure_cents = 50000
    settings.max_daily_loss_cents = 10000
    settings.max_consecutive_losses = 5
    settings.cooldown_seconds = 300
    settings.read_rate_limit = 20
    settings.write_rate_limit = 10
    settings.slack_webhook_url = None
    settings.discord_webhook_url = None
    settings.prometheus_port = 8000
    settings.log_level = "INFO"
    settings.log_format = "console"

    return settings
