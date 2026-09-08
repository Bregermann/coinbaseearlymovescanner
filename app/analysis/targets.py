from __future__ import annotations

from app.types import FibMetrics, StructureMetrics, TargetPlan


def generate_targets(price: float, structure: StructureMetrics, fib: FibMetrics | None = None) -> TargetPlan:
    if price <= 0:
        return TargetPlan(None, None, None, "No live price available.")
    structural_levels = set()
    for value in structure.highs.values():
        if value and value > price * 1.002:
            structural_levels.add(round(value, 12))
    for value in (structure.resistance, structure.confirmation):
        if value and value > price * 1.002:
            structural_levels.add(round(value, 12))
    fib_levels: set[float] = set()
    if fib is not None and fib.reliable and fib.confluence_score >= 60.0:
        for label in ("1.000", "1.272", "1.414", "1.618"):
            value = fib.extensions.get(label)
            if value and value > price * 1.002:
                fib_levels.add(round(value, 12))
    structural_levels.update(fib_levels)
    levels = sorted(structural_levels)

    base_low = structure.base_low or structure.support_low or price
    base_high = structure.base_high or structure.resistance or price
    measured_range = max(base_high - base_low, price * 0.003)

    tp1 = levels[0] if levels else price + measured_range
    tp2_candidates = [level for level in levels if level > tp1 * 1.002]
    tp2 = tp2_candidates[0] if tp2_candidates else tp1 + measured_range
    stretch_candidates = [level for level in levels if level > tp2 * 1.002]
    stretch = stretch_candidates[-1] if stretch_candidates else tp2 + measured_range * 1.5
    if tp2 <= tp1:
        tp2 = tp1 + measured_range
    if stretch <= tp2:
        stretch = tp2 + measured_range * 1.5
    return TargetPlan(
        tp1=tp1,
        tp2=tp2,
        stretch=stretch,
        rationale=(
            "Targets use observed resistance/wick levels and high-confidence Fibonacci extensions, "
            "then project the detected base range only when history lacks higher levels."
            if fib_levels else
            "Targets use nearest observed resistance/wick levels first, then project the detected base range when history lacks higher levels."
        ),
    )
