"""Application settings using Pydantic Settings."""

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Kalshi API
    kalshi_api_key_id: str = Field(..., description="Kalshi API key ID")
    kalshi_private_key_path: Path = Field(..., description="Path to RSA private key PEM file")
    environment: Environment = Field(default=Environment.DEVELOPMENT)

    # Trading thresholds
    min_profit_cents: int = Field(default=2, ge=1, description="Minimum profit in cents to execute trade")
    max_position_per_market: int = Field(default=100, ge=1, description="Max contracts per market")
    max_exposure_cents: int = Field(default=50000, ge=100, description="Maximum total exposure in cents")
    max_daily_loss_cents: int = Field(default=10000, ge=100, description="Maximum daily loss in cents")
    max_consecutive_losses: int = Field(default=5, ge=1, description="Max consecutive losses before circuit breaker")
    cooldown_seconds: int = Field(default=300, ge=60, description="Cooldown period after circuit breaker trip")

    # Rate limiting
    read_rate_limit: int = Field(default=20, ge=1, description="Read requests per second")
    write_rate_limit: int = Field(default=10, ge=1, description="Write requests per second")

    # Monitoring
    slack_webhook_url: Optional[str] = Field(default=None)
    discord_webhook_url: Optional[str] = Field(default=None)
    prometheus_port: int = Field(default=8000)

    # Logging
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")

    @field_validator("kalshi_private_key_path")
    @classmethod
    def validate_key_path(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"Private key file not found: {v}")
        return v

    @property
    def base_url(self) -> str:
        """Get the API base URL based on environment."""
        if self.environment == Environment.PRODUCTION:
            return "https://api.elections.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"

    @property
    def websocket_url(self) -> str:
        """Get the WebSocket URL based on environment."""
        if self.environment == Environment.PRODUCTION:
            return "wss://api.elections.kalshi.com/trade-api/v2/ws"
        return "wss://demo-api.kalshi.co/trade-api/v2/ws"


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
