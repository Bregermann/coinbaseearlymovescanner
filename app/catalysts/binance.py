from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("binance", ["https://www.binance.com/en/support/announcement/c-48"])
