from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("bybit", ["https://announcements.bybit.com/en/?category=new_crypto"])
