from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.timeutils import utc_now

logger = logging.getLogger(__name__)

LISTING_KEYWORDS = ["listing", "list", "trading support", "new trading", "will open trading", "market added", "마켓 추가", "거래지원"]
KRW_KEYWORDS = ["krw", "원화", "korean won"]
CATALYST_KEYWORDS = [
    "partnership",
    "upgrade",
    "mainnet",
    "integration",
    "burn",
    "buyback",
    "migration",
    "governance",
    "airdrop",
    "earn",
    "collateral",
    "margin",
    "zero fee",
]


@dataclass(slots=True)
class CatalystEvent:
    source: str
    external_id: str
    headline: str
    url: str
    announcement_time: datetime | None
    first_detected_time: datetime
    affected_assets: list[str]
    catalyst_type: str
    strength: float
    raw: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "headline": self.headline,
            "url": self.url,
            "announcement_time": self.announcement_time,
            "first_detected_time": self.first_detected_time,
            "affected_assets": self.affected_assets,
            "catalyst_type": self.catalyst_type,
            "strength": self.strength,
            "raw": self.raw,
        }


class CatalystScanner:
    source: str = "generic"

    async def poll(self, known_assets: Iterable[str]) -> list[CatalystEvent]:
        raise NotImplementedError


class OfficialPageScanner(CatalystScanner):
    def __init__(self, source: str, urls: list[str], client: httpx.AsyncClient | None = None) -> None:
        self.source = source
        self.urls = urls
        self.client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._seen_in_process: set[str] = set()

    async def poll(self, known_assets: Iterable[str]) -> list[CatalystEvent]:
        known = {asset.upper() for asset in known_assets}
        events: list[CatalystEvent] = []
        for url in self.urls:
            try:
                response = await self.client.get(url, headers={"cache-control": "no-cache", "user-agent": "coinbase-early-move-scanner/1.0"})
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning("catalyst_source_error", extra={"_source": self.source, "_url": url, "_error": str(exc)})
                continue
            events.extend(self._events_from_html(response.text, base_url=url, known_assets=known))
        return events

    def _events_from_html(self, html: str, base_url: str, known_assets: set[str]) -> list[CatalystEvent]:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.find_all("a")
        events: list[CatalystEvent] = []
        for anchor in anchors[:200]:
            text = " ".join(anchor.get_text(" ", strip=True).split())
            if len(text) < 8:
                continue
            href = anchor.get("href") or base_url
            url = urljoin(base_url, href)
            if not is_relevant_headline(text):
                continue
            assets = extract_assets(text, known_assets)
            if not assets:
                continue
            external_id = stable_external_id(self.source, url, text)
            if external_id in self._seen_in_process:
                continue
            self._seen_in_process.add(external_id)
            catalyst_type = classify_catalyst(text)
            events.append(
                CatalystEvent(
                    source=self.source,
                    external_id=external_id,
                    headline=text,
                    url=url,
                    announcement_time=None,
                    first_detected_time=utc_now(),
                    affected_assets=assets,
                    catalyst_type=catalyst_type,
                    strength=strength_for(self.source, text, catalyst_type),
                    raw={"headline": text, "url": url},
                )
            )
        return events


class StaticUrlScanner(OfficialPageScanner):
    pass


def is_relevant_headline(headline: str) -> bool:
    text = headline.lower()
    if ("perpetual" in text or "futures" in text) and "spot" not in text:
        return False
    return any(keyword in text for keyword in LISTING_KEYWORDS + CATALYST_KEYWORDS)


def extract_assets(headline: str, known_assets: set[str]) -> list[str]:
    tokens = set(re.findall(r"(?<![A-Za-z0-9])([A-Z0-9]{2,12})(?![A-Za-z0-9])", headline))
    tokens |= set(re.findall(r"\(([A-Z0-9]{2,12})\)", headline))
    tokens |= set(re.findall(r"(?<![A-Za-z0-9])([A-Z0-9]{2,12})/(?:USD|USDT|USDC|KRW)(?![A-Za-z0-9])", headline))
    excluded = {"USD", "USDC", "USDT", "KRW", "EUR", "BTC", "ETH", "NFT", "API", "VIP", "UP", "TO", "NEW"}
    return sorted(({token.upper() for token in tokens} & known_assets) - excluded)


def classify_catalyst(headline: str) -> str:
    text = headline.lower()
    if any(keyword in text for keyword in KRW_KEYWORDS) and any(keyword in text for keyword in LISTING_KEYWORDS):
        return "KRW_LISTING"
    if any(keyword in text for keyword in LISTING_KEYWORDS):
        return "SPOT_LISTING"
    if "margin" in text:
        return "MARGIN_ADDITION"
    if "earn" in text:
        return "EARN_SUPPORT"
    if "collateral" in text:
        return "COLLATERAL_ADDITION"
    return "PROJECT_CATALYST"


def strength_for(source: str, headline: str, catalyst_type: str) -> float:
    source_weight = {
        "upbit": 96.0,
        "bithumb": 92.0,
        "binance": 95.0,
        "coinbase": 82.0,
        "kraken": 80.0,
        "okx": 84.0,
        "bybit": 82.0,
        "projects": 65.0,
    }.get(source, 60.0)
    if catalyst_type == "KRW_LISTING":
        source_weight += 4.0
    if catalyst_type == "SPOT_LISTING":
        source_weight += 2.0
    if "futures" in headline.lower() and "spot" not in headline.lower():
        source_weight -= 15.0
    return min(100.0, source_weight)


def stable_external_id(source: str, url: str, headline: str) -> str:
    digest = hashlib.sha256(f"{source}|{url}|{headline}".encode("utf-8")).hexdigest()[:24]
    return digest
