from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner() -> OfficialPageScanner:
    return OfficialPageScanner("okx", ["https://www.okx.com/help/section/announcements-new-listings"])
