from __future__ import annotations

import logging
from typing import Any

import httpx

from app.alerts.formatter import candidate_to_payload
from app.config import Settings, get_settings
from app.types import Candidate

logger = logging.getLogger(__name__)


class DiscordWebhookClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "DiscordWebhookClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.discord_timeout_seconds)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.discord_timeout_seconds)
        return self._client

    async def send_candidate(self, candidate: Candidate, alert_type: str = "new_setup") -> str | None:
        payload = candidate_to_payload(candidate, alert_type=alert_type)
        return await self.send_payload(payload, webhook_url=self.settings.alert_webhook)

    async def send_catalyst_payload(self, payload: dict[str, Any]) -> str | None:
        return await self.send_payload(payload, webhook_url=self.settings.catalyst_webhook)

    async def send_test_message(self) -> str | None:
        payload = {
            "username": self.settings.discord_test_username,
            "embeds": [
                {
                    "title": "TEST - Coinbase Early Move Scanner",
                    "description": "Discord webhook delivery is configured. This is not a trading alert.",
                    "color": 0x3498DB,
                    "fields": [
                        {"name": "Mode", "value": self.settings.environment, "inline": True},
                        {"name": "Source", "value": "Application test command", "inline": True},
                    ],
                }
            ],
        }
        return await self.send_payload(payload, webhook_url=self.settings.alert_webhook)

    async def send_payload(self, payload: dict[str, Any], webhook_url: str | None) -> str | None:
        if not webhook_url:
            logger.warning("discord_webhook_missing")
            return None
        try:
            response = await self.client.post(webhook_url, params={"wait": "true"}, json=payload)
            if response.status_code >= 400:
                logger.error(
                    "discord_webhook_failed",
                    extra={
                        "_status_code": response.status_code,
                        "_response_body": response.text[:500],
                    },
                )
                return None
            if response.content:
                data = response.json()
                return str(data.get("id")) if data.get("id") else None
            return None
        except httpx.RequestError as exc:
            logger.error("discord_webhook_request_failed", extra={"_error_type": type(exc).__name__})
            return None
        except ValueError:
            logger.error("discord_webhook_bad_json")
            return None
