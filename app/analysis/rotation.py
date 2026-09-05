from __future__ import annotations

from app.formatting import clamp
from app.types import MarketCapMetrics, VolumeMetrics


def microcap_rotation_score(
    all_microcap_metrics: dict[str, MarketCapMetrics],
    all_volume_metrics: dict[str, VolumeMetrics],
    threshold: float = 15_000_000.0,
) -> dict[str, float]:
    active = 0
    dormant = []
    for asset, metrics in all_microcap_metrics.items():
        if metrics.circulating_market_cap is None or metrics.circulating_market_cap > threshold:
            continue
        volume = all_volume_metrics.get(asset)
        if volume and (volume.ratios.get("5m_vs_baseline", 0.0) >= 2.5 or volume.ratios.get("1h_vs_7d", 0.0) >= 3.0):
            active += 1
        else:
            dormant.append(asset)
    market_rotation = clamp(active * 22.0)
    return {asset: market_rotation for asset in dormant}
