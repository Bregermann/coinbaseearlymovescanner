from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Coinbase Early Move Scanner"
    environment: Literal["local", "production", "test"] = "local"
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///./scanner.db"

    coinbase_rest_base_url: str = "https://api.coinbase.com/api/v3/brokerage"
    coinbase_ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    coinbase_product_refresh_seconds: int = 300
    coinbase_rest_timeout_seconds: float = 15.0
    coinbase_rest_max_retries: int = 4
    coinbase_ws_product_chunk_size: int = 80
    coinbase_ws_reconnect_min_seconds: float = 1.0
    coinbase_ws_reconnect_max_seconds: float = 60.0
    coinbase_ws_max_message_size_bytes: int = 67_108_864
    enable_level2: bool = True
    enable_candles_channel: bool = True
    bootstrap_history_days: int = 365
    analysis_history_days: int = 31
    bootstrap_concurrency: int = 1
    max_products_for_bootstrap_per_cycle: int = 9999

    quote_fresh_ideal_seconds: float = 2.0
    quote_fresh_warning_seconds: float = 10.0
    alert_max_quote_age_seconds: float = 10.0

    scan_interval_seconds: float = 10.0
    candidate_limit: int = 5
    min_early_move_score: float = 78.0
    min_risk_adjusted_opportunity_score: float = 78.0
    min_microcap_risk_adjusted_opportunity_score: float = 88.0
    min_microcap_early_move_score: float = 82.0
    min_microcap_liquidity_safety_score: float = 55.0
    min_microcap_volume_acceleration_score: float = 45.0
    min_catalyst_override_score: float = 85.0
    suppress_extended_24h_pct: float = 20.0

    market_cap_large_threshold_usd: float = 1_000_000_000.0
    market_cap_mid_threshold_usd: float = 250_000_000.0
    market_cap_small_threshold_usd: float = 50_000_000.0
    microcap_threshold_usd: float = 50_000_000.0
    microcap_refresh_seconds: int = 600
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coingecko_timeout_seconds: float = 15.0
    coingecko_platform_ids: str = ""

    catalyst_poll_seconds: int = 60
    catalyst_fresh_window_minutes: int = 360
    catalyst_source_urls: str = ""

    discord_alert_webhook_url: HttpUrl | None = None
    discord_catalyst_webhook_url: HttpUrl | None = None
    discord_debug_webhook_url: HttpUrl | None = None
    discord_timeout_seconds: float = 10.0
    discord_test_username: str = "Coinbase Early Move Scanner"
    discord_heartbeat_seconds: int = 3600
    discord_heartbeat_allow_primary: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return str(value).upper()

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_postgres_scheme(cls, value: str) -> str:
        value = str(value)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def alert_webhook(self) -> str | None:
        return str(self.discord_alert_webhook_url) if self.discord_alert_webhook_url else None

    @property
    def catalyst_webhook(self) -> str | None:
        if self.discord_catalyst_webhook_url:
            return str(self.discord_catalyst_webhook_url)
        return self.alert_webhook

    @property
    def debug_webhook(self) -> str | None:
        if self.discord_debug_webhook_url:
            return str(self.discord_debug_webhook_url)
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
