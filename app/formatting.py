from __future__ import annotations


def money(value: float | None, precision: int | None = None) -> str:
    if value is None:
        return "n/a"
    abs_value = abs(value)
    if precision is None:
        if abs_value >= 100:
            precision = 2
        elif abs_value >= 1:
            precision = 4
        else:
            precision = 6
    return f"${value:,.{precision}f}"


def compact_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.2f}"


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.1f}%"


def ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}x"


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
