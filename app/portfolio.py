from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import Settings
from app.types import PortfolioHolding

logger = logging.getLogger(__name__)


def load_portfolio(settings: Settings) -> list[PortfolioHolding]:
    if not settings.portfolio_rotation_enabled:
        return []
    raw: Any = None
    source = ""
    if settings.portfolio_holdings_json.strip():
        source = "PORTFOLIO_HOLDINGS_JSON"
        try:
            raw = json.loads(settings.portfolio_holdings_json)
        except json.JSONDecodeError:
            logger.warning("portfolio_inline_json_invalid")
            return []
    else:
        path = Path(settings.portfolio_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        source = str(path)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("portfolio_file_invalid", extra={"_path": str(path)})
            return []

    rows = raw.get("holdings", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        logger.warning("portfolio_holdings_not_a_list", extra={"_source": source})
        return []
    holdings: dict[str, PortfolioHolding] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        quantity = optional_float(row.get("quantity"))
        if not ticker or quantity is None or quantity <= 0:
            continue
        holding = PortfolioHolding(
            ticker=ticker,
            quantity=quantity,
            cost_basis=optional_float(row.get("cost_basis")),
            current_position_value=optional_float(row.get("current_position_value")),
        )
        holdings[ticker] = holding
    return list(holdings.values())


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
