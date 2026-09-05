from __future__ import annotations

from app.catalysts.base import OfficialPageScanner


def build_scanner(urls: list[str]) -> OfficialPageScanner | None:
    if not urls:
        return None
    return OfficialPageScanner("projects", urls)
