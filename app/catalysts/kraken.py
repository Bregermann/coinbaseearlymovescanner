from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("kraken", ["https://blog.kraken.com/category/product/asset-listings"])
