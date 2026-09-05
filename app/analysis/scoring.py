from __future__ import annotations

from datetime import datetime
from typing import Any

from app.analysis.microcaps import microcap_score
from app.formatting import clamp
from app.timeutils import seconds_between, utc_now
from app.types import LiquidityMetrics, MarketCapMetrics, ScoreBreakdown, StructureMetrics, VolumeMetrics


def score_candidate(
    volume: VolumeMetrics,
    structure: StructureMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    quote_age_seconds: float | None,
    catalyst: dict[str, Any] | None = None,
    rotation_score: float = 0.0,
    microcap_threshold: float = 15_000_000.0,
) -> ScoreBreakdown:
    components = {
        "VolumeAccelerationScore": score_volume_acceleration(volume),
        "TurnoverScore": score_turnover(market_cap.volume_to_market_cap),
        "CompressionScore": structure.compression_score,
        "HigherLowScore": structure.higher_low_score,
        "ResistanceProximityScore": structure.resistance_proximity_score,
        "BaseProximityScore": structure.base_proximity_score,
        "DormancyScore": max(structure.compression_score, structure.base_proximity_score) * 0.7,
        "MicrocapScore": microcap_score(market_cap.circulating_market_cap, microcap_threshold),
        "MicrocapRotationScore": rotation_score,
        "OrderBookScore": liquidity.order_book_score,
        "LiquidityShockScore": liquidity.liquidity_vacuum_score,
        "FreshnessScore": score_freshness(quote_age_seconds),
    }
    catalyst_score = score_catalyst(catalyst)
    components["CatalystScore"] = catalyst_score
    penalties = {
        "ExtensionPenalty": extension_penalty(structure.extension_24h_pct),
        "AlreadyPumpedPenalty": already_pumped_penalty(structure.extension_24h_pct),
        "BadLiquidityPenalty": bad_liquidity_penalty(liquidity.spread_pct),
        "StaleDataPenalty": stale_data_penalty(quote_age_seconds),
        "BreakdownPenalty": 0.0,
    }
    technical_score = weighted_score(
        components,
        {
            "VolumeAccelerationScore": 0.26,
            "TurnoverScore": 0.10,
            "CompressionScore": 0.12,
            "HigherLowScore": 0.07,
            "ResistanceProximityScore": 0.10,
            "BaseProximityScore": 0.12,
            "DormancyScore": 0.06,
            "MicrocapScore": 0.06,
            "MicrocapRotationScore": 0.04,
            "OrderBookScore": 0.05,
            "LiquidityShockScore": 0.02,
        },
    )
    penalty_total = sum(penalties.values())
    early_move_score = clamp(technical_score * 0.78 + catalyst_score * 0.22 - penalty_total)
    if catalyst_score >= 85.0:
        early_move_score = clamp(max(early_move_score, catalyst_score * 0.80 + technical_score * 0.20 - penalty_total * 0.35))
    return ScoreBreakdown(
        technical_score=round(technical_score, 2),
        catalyst_score=round(catalyst_score, 2),
        early_move_score=round(early_move_score, 2),
        components={key: round(value, 2) for key, value in components.items()},
        penalties={key: round(value, 2) for key, value in penalties.items()},
        reasons=build_reasons(volume, structure, liquidity, market_cap, catalyst, components),
        risks=build_risks(structure, liquidity, market_cap, catalyst),
    )


def score_volume_acceleration(volume: VolumeMetrics) -> float:
    short = max(volume.ratios.get("5m_vs_baseline", 0.0), volume.ratios.get("15m_vs_baseline", 0.0))
    medium = max(volume.ratios.get("1h_vs_7d", 0.0), volume.ratios.get("4h_vs_7d", 0.0))
    mismatch_bonus = clamp(volume.volume_vs_price_acceleration * 8.0)
    return clamp(short * 17.0 + medium * 8.0 + mismatch_bonus * 0.25)


def score_turnover(volume_to_market_cap: float | None) -> float:
    if volume_to_market_cap is None:
        return 0.0
    if volume_to_market_cap >= 15.0:
        return 100.0
    if volume_to_market_cap >= 8.0:
        return 85.0
    if volume_to_market_cap >= 3.0:
        return 65.0
    return clamp(volume_to_market_cap * 16.0)


def score_freshness(age_seconds: float | None) -> float:
    if age_seconds is None:
        return 0.0
    if age_seconds <= 2:
        return 100.0
    if age_seconds <= 10:
        return 80.0 - (age_seconds - 2.0) * 5.0
    return 0.0


def score_catalyst(catalyst: dict[str, Any] | None) -> float:
    if not catalyst:
        return 0.0
    strength = float(catalyst.get("strength", 0.0) or 0.0)
    announcement_time = catalyst.get("announcement_time")
    first_detected_time = catalyst.get("first_detected_time") or utc_now()
    if not isinstance(announcement_time, datetime):
        return clamp(min(strength, 45.0))
    delay = seconds_between(first_detected_time if isinstance(first_detected_time, datetime) else utc_now(), announcement_time)
    if delay is not None:
        strength -= min(35.0, delay / 60.0 * 2.0)
    return clamp(strength)


def extension_penalty(extension_24h_pct: float | None) -> float:
    if extension_24h_pct is None or extension_24h_pct <= 8.0:
        return 0.0
    return clamp((extension_24h_pct - 8.0) * 1.5, 0.0, 30.0)


def already_pumped_penalty(extension_24h_pct: float | None) -> float:
    if extension_24h_pct is None or extension_24h_pct < 20.0:
        return 0.0
    return clamp(20.0 + (extension_24h_pct - 20.0) * 1.2, 20.0, 65.0)


def bad_liquidity_penalty(spread_pct: float | None) -> float:
    if spread_pct is None:
        return 6.0
    if spread_pct <= 0.75:
        return 0.0
    return clamp((spread_pct - 0.75) * 12.0, 0.0, 35.0)


def stale_data_penalty(age_seconds: float | None) -> float:
    if age_seconds is None:
        return 35.0
    if age_seconds <= 10.0:
        return 0.0
    return clamp(35.0 + (age_seconds - 10.0) * 2.0, 35.0, 90.0)


def weighted_score(components: dict[str, float], weights: dict[str, float]) -> float:
    return clamp(sum(components.get(key, 0.0) * weight for key, weight in weights.items()))


def build_reasons(
    volume: VolumeMetrics,
    structure: StructureMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    catalyst: dict[str, Any] | None,
    components: dict[str, float],
) -> list[str]:
    reasons: list[str] = []
    if volume.ratios.get("5m_vs_baseline", 0.0) >= 2.0:
        reasons.append(f"5m Coinbase volume is {volume.ratios['5m_vs_baseline']:.1f}x its recent baseline.")
    if volume.ratios.get("1h_vs_7d", 0.0) >= 2.0:
        reasons.append(f"1h volume is {volume.ratios['1h_vs_7d']:.1f}x the 7d hourly baseline.")
    if volume.volume_vs_price_acceleration >= 2.0:
        reasons.append("Volume acceleration is materially ahead of price acceleration.")
    if structure.base_proximity_score >= 70.0:
        reasons.append("Price remains close to the detected base/support area.")
    if structure.compression_score >= 65.0:
        reasons.append("Recent price action is compressed relative to its multi-day range.")
    if market_cap.circulating_market_cap and market_cap.circulating_market_cap <= 15_000_000:
        reasons.append("The asset is in the dormant Coinbase microcap bucket.")
    if liquidity.liquidity_vacuum_score >= 60.0:
        reasons.append("Order book shows relatively light ask depth above the current price.")
    if catalyst:
        reasons.append(f"Fresh catalyst detected: {catalyst.get('headline', 'official announcement')}.")
    if not reasons and components.get("VolumeAccelerationScore", 0.0) > 0:
        reasons.append("Multiple early technical/liquidity factors are improving at the same time.")
    return reasons[:5]


def build_risks(
    structure: StructureMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    catalyst: dict[str, Any] | None,
) -> list[str]:
    risks: list[str] = []
    if liquidity.spread_pct is None or liquidity.spread_pct > 0.75:
        risks.append("Thin or wide liquidity.")
    if market_cap.circulating_market_cap is None:
        risks.append("Circulating supply metadata unavailable.")
    elif market_cap.circulating_market_cap <= 15_000_000:
        risks.append("Microcap volatility and slippage risk.")
    if structure.distance_from_resistance_pct is not None and structure.distance_from_resistance_pct < 1.0:
        risks.append("Resistance is directly overhead.")
    if not catalyst:
        risks.append("No confirmed tier-1 catalyst found.")
    if structure.extension_24h_pct and structure.extension_24h_pct >= 15.0:
        risks.append("The move is becoming extended on the 24h window.")
    return risks[:5]
