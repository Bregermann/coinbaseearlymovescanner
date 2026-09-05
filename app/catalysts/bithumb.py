from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("bithumb", ["https://feed.bithumb.com/notice?category=&page=1"])
