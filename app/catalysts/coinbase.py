from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("coinbase", ["https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/new-asset-listings", "https://www.coinbase.com/blog"])
