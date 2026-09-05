from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("upbit", ["https://upbit.com/service_center/notice", "https://sg.upbit.com/service_center/notice"])
