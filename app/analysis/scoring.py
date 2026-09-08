from __future__ import annotations

from datetime import datetime
from typing import Any

from app.analysis.microcaps import microcap_score
from app.formatting import clamp
from app.timeutils import seconds_between, utc_now
from app.types import BottomMetrics, BottomStage, FibMetrics, LiquidityMetrics, MarketCapMetrics, ScoreBreakdown, StructureMetrics, TargetPlan, VolumeMetrics

MARKET_CAP_LARGE_LABEL = "LARGE / LIQUID"
MARKET_CAP_MID_LABEL = "MID CAP"
MARKET_CAP_SMALL_LABEL = "SMALL CAP"
MARKET_CAP_MICRO_LABEL = "MICROCAP"
MARKET_CAP_UNKNOWN_LABEL = "UNKNOWN"


def score_candidate(
    volume: VolumeMetrics,
    structure: StructureMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    quote_age_seconds: float | None,
    catalyst: dict[str, Any] | None = None,
    rotation_score: float = 0.0,
    microcap_threshold: float = 50_000_000.0,
    small_cap_threshold: float = 50_000_000.0,
    mid_cap_threshold: float = 250_000_000.0,
    large_cap_threshold: float = 1_000_000_000.0,
) -> ScoreBreakdown:
    liquidity_safety = score_liquidity_safety(volume, liquidity, market_cap, small_cap_threshold)
    cap_priority = score_market_cap_priority(market_cap.circulating_market_cap, small_cap_threshold, mid_cap_threshold, large_cap_threshold)
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
        "LiquiditySafetyScore": liquidity_safety,
        "MarketCapPriorityScore": cap_priority,
        "EarlyStageScore": score_early_stage(structure.extension_24h_pct),
        "FreshnessScore": score_freshness(quote_age_seconds),
    }
    catalyst_score = score_catalyst(catalyst)
    components["CatalystScore"] = catalyst_score
    penalties = {
        "ExtensionPenalty": extension_penalty(structure.extension_24h_pct),
        "AlreadyPumpedPenalty": already_pumped_penalty(structure.extension_24h_pct),
        "BadLiquidityPenalty": bad_liquidity_penalty(liquidity),
        "StaleDataPenalty": stale_data_penalty(quote_age_seconds),
        "BreakdownPenalty": 0.0,
        "ThinParticipationPenalty": thin_participation_penalty(volume, liquidity),
        "DilutionPenalty": dilution_penalty(market_cap),
    }
    technical_score = weighted_score(
        components,
        {
            "VolumeAccelerationScore": 0.22,
            "LiquiditySafetyScore": 0.18,
            "MarketCapPriorityScore": 0.12,
            "TurnoverScore": 0.08,
            "CompressionScore": 0.09,
            "HigherLowScore": 0.05,
            "ResistanceProximityScore": 0.08,
            "BaseProximityScore": 0.07,
            "EarlyStageScore": 0.05,
            "OrderBookScore": 0.04,
            "MicrocapScore": 0.01,
            "MicrocapRotationScore": 0.01,
        },
    )
    early_penalty_total = sum(
        penalties[key]
        for key in ["ExtensionPenalty", "AlreadyPumpedPenalty", "BadLiquidityPenalty", "StaleDataPenalty", "BreakdownPenalty"]
    )
    early_move_score = clamp(technical_score * 0.78 + catalyst_score * 0.22 - early_penalty_total)
    if catalyst_score >= 85.0:
        early_move_score = clamp(max(early_move_score, catalyst_score * 0.80 + technical_score * 0.20 - early_penalty_total * 0.35))
    return ScoreBreakdown(
        technical_score=round(technical_score, 2),
        catalyst_score=round(catalyst_score, 2),
        early_move_score=round(early_move_score, 2),
        components={key: round(value, 2) for key, value in components.items()},
        penalties={key: round(value, 2) for key, value in penalties.items()},
        reasons=build_reasons(volume, structure, liquidity, market_cap, catalyst, components, small_cap_threshold, mid_cap_threshold, large_cap_threshold),
        risks=build_risks(structure, liquidity, market_cap, catalyst, small_cap_threshold),
    )


def apply_risk_adjusted_opportunity(
    score: ScoreBreakdown,
    volume: VolumeMetrics,
    structure: StructureMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    targets: TargetPlan,
    price: float,
    bottom: BottomMetrics | None = None,
    fib: FibMetrics | None = None,
) -> ScoreBreakdown:
    upside_pct = max_target_upside_pct(targets, price)
    score.components["UpsideRunwayScore"] = round(score_upside_runway(upside_pct), 2)
    if fib is not None and fib.reliable:
        score.components["FibConfluenceScore"] = round(fib.confluence_score, 2)
        score.components["FibSwingConfidenceScore"] = round(fib.swing_confidence, 2)
        score.components["FibSignalScore"] = round(score_fib_signal(fib.signal), 2)
    if bottom is not None:
        score.components["BottomScore"] = bottom.bottom_score
        score.components["BreakoutScore"] = bottom.breakout_score
        score.components["RelativeStrengthScore"] = bottom.relative_strength_score
        score.components["FirstExpansionScore"] = bottom.components.get("VolumeDryupExpansionScore", 0.0)
        score.components["StructuralOpportunityScore"] = round(
            structural_opportunity_score(score, bottom),
            2,
        )
    score.components["RiskAdjustedOpportunityScore"] = round(
        risk_adjusted_opportunity_score(score, volume, structure, liquidity, market_cap, bottom),
        2,
    )
    return score


def primary_score(value: Any) -> float:
    score = getattr(value, "score", value)
    components = getattr(score, "components", {}) or {}
    risk_score = components.get("RiskAdjustedOpportunityScore")
    if risk_score is not None:
        return float(risk_score)
    return float(getattr(score, "early_move_score", 0.0) or 0.0)


def risk_adjusted_opportunity_score(
    score: ScoreBreakdown,
    volume: VolumeMetrics,
    structure: StructureMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    bottom: BottomMetrics | None = None,
) -> float:
    components = score.components
    penalties = score.penalties
    if bottom is None:
        raw = (
            score.early_move_score * 0.20
            + components.get("UpsideRunwayScore", 42.0) * 0.18
            + components.get("LiquiditySafetyScore", 0.0) * 0.24
            + components.get("VolumeAccelerationScore", 0.0) * 0.15
            + components.get("MarketCapPriorityScore", 0.0) * 0.11
            + components.get("EarlyStageScore", 0.0) * 0.07
            + components.get("OrderBookScore", 0.0) * 0.05
        )
    else:
        raw = (
            score.early_move_score * 0.15
            + components.get("UpsideRunwayScore", 42.0) * 0.14
            + components.get("LiquiditySafetyScore", 0.0) * 0.19
            + components.get("VolumeAccelerationScore", 0.0) * 0.10
            + components.get("MarketCapPriorityScore", 0.0) * 0.09
            + components.get("EarlyStageScore", 0.0) * 0.05
            + components.get("OrderBookScore", 0.0) * 0.04
            + bottom.bottom_score * 0.16
            + bottom.breakout_score * 0.08
        )
    booster = (
        components.get("TraderAccelerationScore", 0.0) * 0.05
        + components.get("GoonerEMAScore", 0.0) * 0.04
        + score.catalyst_score * 0.07
        + components.get("FibConfluenceScore", 0.0) * 0.05
        + components.get("FibSignalScore", 0.0) * 0.03
    )
    penalty = (
        penalties.get("ExtensionPenalty", 0.0) * 0.55
        + penalties.get("BadLiquidityPenalty", 0.0) * 0.45
        + penalties.get("ThinParticipationPenalty", 0.0) * 0.70
        + penalties.get("DilutionPenalty", 0.0) * 0.45
        + penalties.get("StaleDataPenalty", 0.0) * 0.70
        + penalties.get("AlreadyPumpedPenalty", 0.0) * 0.35
    )
    weighted_result = raw + booster - penalty
    if bottom is not None and bottom.stage != BottomStage.NONE:
        structural_penalty = (
            penalties.get("BadLiquidityPenalty", 0.0) * 0.20
            + penalties.get("ThinParticipationPenalty", 0.0) * 0.25
            + penalties.get("DilutionPenalty", 0.0) * 0.20
            + penalties.get("StaleDataPenalty", 0.0) * 0.70
            + penalties.get("ExtensionPenalty", 0.0) * 0.20
            + penalties.get("AlreadyPumpedPenalty", 0.0) * 0.25
        )
        structural_result = (
            components.get("StructuralOpportunityScore", 0.0)
            + score.catalyst_score * 0.04
            - structural_penalty
        )
        weighted_result = max(weighted_result, structural_result)
    return clamp(weighted_result)


def structural_opportunity_score(
    score: ScoreBreakdown,
    bottom: BottomMetrics,
) -> float:
    components = score.components
    liquidity = components.get("LiquiditySafetyScore", 0.0)
    cap_priority = components.get("MarketCapPriorityScore", 0.0)
    volume = components.get("VolumeAccelerationScore", 0.0)
    upside = components.get("UpsideRunwayScore", 42.0)
    if bottom.stage == BottomStage.PRE_BREAKOUT:
        raw = (
            bottom.bottom_score * 0.50
            + bottom.breakout_score * 0.18
            + liquidity * 0.14
            + cap_priority * 0.08
            + upside * 0.06
            + volume * 0.04
        )
    elif bottom.stage == BottomStage.BREAKOUT_FIRING:
        raw = (
            bottom.bottom_score * 0.30
            + bottom.breakout_score * 0.34
            + liquidity * 0.15
            + cap_priority * 0.09
            + upside * 0.07
            + volume * 0.05
        )
    elif bottom.stage == BottomStage.RETEST_HOLD:
        raw = (
            bottom.bottom_score * 0.38
            + bottom.breakout_score * 0.24
            + liquidity * 0.16
            + cap_priority * 0.09
            + upside * 0.08
            + volume * 0.05
        )
    else:
        raw = (
            bottom.bottom_score * 0.60
            + liquidity * 0.17
            + cap_priority * 0.10
            + upside * 0.08
            + volume * 0.05
        )
    fib_boost = (
        components.get("FibConfluenceScore", 0.0) * 0.04
        + components.get("FibSignalScore", 0.0) * 0.02
    )
    return clamp(raw + fib_boost)


def score_fib_signal(signal: str) -> float:
    return {
        "0.382 BOUNCE": 62.0,
        "0.500 RECLAIM": 76.0,
        "GOLDEN POCKET TEST": 68.0,
        "0.618 RECLAIM": 88.0,
        "0.618 SUPPORT CONFIRMED": 92.0,
        "0.786 DEEP RETRACEMENT": 55.0,
        "1.000 BREAKOUT": 90.0,
        "1.272 EXTENSION TEST": 42.0,
        "1.414 EXTENSION TEST": 30.0,
        "1.618 EXTENSION TARGET": 15.0,
        "LOSS OF 0.618": 15.0,
        "LOSS OF 0.786": 0.0,
        "ACTIVE IMPULSE": 45.0,
    }.get(signal, 0.0)


def score_volume_acceleration(volume: VolumeMetrics) -> float:
    short = max(volume.ratios.get("5m_vs_baseline", 0.0), volume.ratios.get("15m_vs_baseline", 0.0))
    medium = max(volume.ratios.get("1h_vs_7d", 0.0), volume.ratios.get("4h_vs_7d", 0.0))
    first_expansion = max(
        volume.ratios.get("15m_vs_prior_20", 0.0),
        volume.ratios.get("1h_vs_prior_24", 0.0),
        volume.ratios.get("4h_vs_prior_42", 0.0),
    )
    mismatch_bonus = clamp(volume.volume_vs_price_acceleration * 8.0)
    mature_surge = clamp(short * 17.0 + medium * 8.0 + mismatch_bonus * 0.25)
    first_expansion_score = clamp((first_expansion - 0.75) * 38.0)
    return max(mature_surge, first_expansion_score)


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


def score_early_stage(extension_24h_pct: float | None) -> float:
    if extension_24h_pct is None:
        return 65.0
    if extension_24h_pct <= 0.0:
        return 50.0
    if extension_24h_pct <= 4.0:
        return 100.0
    if extension_24h_pct <= 10.0:
        return 92.0
    if extension_24h_pct <= 15.0:
        return 70.0
    if extension_24h_pct <= 20.0:
        return 40.0
    return 5.0


def score_liquidity_safety(
    volume: VolumeMetrics,
    liquidity: LiquidityMetrics,
    market_cap: MarketCapMetrics,
    small_cap_threshold: float = 50_000_000.0,
) -> float:
    spread = score_spread(liquidity.spread_pct)
    dollar_volume = score_dollar_volume(dollar_volume_24h(volume))
    depth_1 = score_depth_usd(book_depth_usd(liquidity, "1"))
    depth_2 = score_depth_usd(book_depth_usd(liquidity, "2")) * 0.90
    depth = max(depth_1, depth_2)
    cap = score_liquidity_market_cap(market_cap.circulating_market_cap, small_cap_threshold)
    book = liquidity.order_book_score
    return clamp(spread * 0.24 + dollar_volume * 0.25 + depth * 0.24 + cap * 0.17 + book * 0.10)


def score_spread(spread_pct: float | None) -> float:
    if spread_pct is None:
        return 40.0
    if spread_pct <= 0.03:
        return 100.0
    if spread_pct <= 0.05:
        return 96.0
    if spread_pct <= 0.10:
        return 90.0
    if spread_pct <= 0.25:
        return 75.0
    if spread_pct <= 0.50:
        return 55.0
    if spread_pct <= 1.0:
        return 30.0
    return clamp(30.0 - (spread_pct - 1.0) * 10.0)


def score_dollar_volume(value: float | None) -> float:
    if value is None or value <= 0:
        return 0.0
    if value >= 100_000_000:
        return 100.0
    if value >= 25_000_000:
        return 92.0
    if value >= 10_000_000:
        return 82.0
    if value >= 2_000_000:
        return 62.0
    if value >= 500_000:
        return 42.0
    if value >= 100_000:
        return 24.0
    return 10.0


def score_depth_usd(value: float) -> float:
    if value >= 5_000_000:
        return 100.0
    if value >= 1_000_000:
        return 92.0
    if value >= 250_000:
        return 76.0
    if value >= 50_000:
        return 55.0
    if value >= 10_000:
        return 32.0
    if value > 0:
        return 12.0
    return 0.0


def score_liquidity_market_cap(market_cap: float | None, small_cap_threshold: float) -> float:
    if market_cap is None or market_cap <= 0:
        return 55.0
    if market_cap >= 250_000_000:
        return 95.0
    if market_cap >= small_cap_threshold:
        return 76.0
    if market_cap >= small_cap_threshold * 0.20:
        return 45.0
    return 25.0


def score_market_cap_priority(
    market_cap: float | None,
    small_cap_threshold: float = 50_000_000.0,
    mid_cap_threshold: float = 250_000_000.0,
    large_cap_threshold: float = 1_000_000_000.0,
) -> float:
    if market_cap is None or market_cap <= 0:
        return 55.0
    if market_cap >= large_cap_threshold:
        return 92.0
    if market_cap >= mid_cap_threshold:
        return 100.0
    if market_cap >= small_cap_threshold:
        return 82.0
    if market_cap >= small_cap_threshold * 0.20:
        return 42.0
    return 28.0


def market_cap_class(
    value: MarketCapMetrics | float | None,
    small_cap_threshold: float = 50_000_000.0,
    mid_cap_threshold: float = 250_000_000.0,
    large_cap_threshold: float = 1_000_000_000.0,
) -> str:
    market_cap = value.circulating_market_cap if isinstance(value, MarketCapMetrics) else value
    if market_cap is None or market_cap <= 0:
        return MARKET_CAP_UNKNOWN_LABEL
    if market_cap >= large_cap_threshold:
        return MARKET_CAP_LARGE_LABEL
    if market_cap >= mid_cap_threshold:
        return MARKET_CAP_MID_LABEL
    if market_cap >= small_cap_threshold:
        return MARKET_CAP_SMALL_LABEL
    return MARKET_CAP_MICRO_LABEL


def score_upside_runway(upside_pct: float | None) -> float:
    if upside_pct is None:
        return 42.0
    if upside_pct <= 0:
        return 0.0
    if upside_pct < 5.0:
        return clamp(upside_pct * 3.0)
    if upside_pct < 10.0:
        return 15.0 + (upside_pct - 5.0) * 8.0
    if upside_pct < 20.0:
        return 55.0 + (upside_pct - 10.0) * 2.7
    if upside_pct < 40.0:
        return 82.0 + (upside_pct - 20.0) * 0.9
    return 100.0


def max_target_upside_pct(targets: TargetPlan, price: float) -> float | None:
    if price <= 0:
        return None
    upsides = [((target - price) / price * 100.0) for target in [targets.tp1, targets.tp2, targets.stretch] if target and target > price]
    return max(upsides) if upsides else None


def dollar_volume_24h(volume: VolumeMetrics | None) -> float | None:
    if volume is None:
        return None
    value = volume.volume_quote.get("24h")
    return float(value) if value is not None else None


def book_depth_usd(liquidity: LiquidityMetrics, window: str = "1") -> float:
    bid = float(liquidity.bid_depth_usd.get(window, 0.0) or 0.0)
    ask = float(liquidity.ask_depth_usd.get(window, 0.0) or 0.0)
    return min(bid, ask) if bid > 0 and ask > 0 else max(bid, ask)


def extension_penalty(extension_24h_pct: float | None) -> float:
    if extension_24h_pct is None or extension_24h_pct <= 8.0:
        return 0.0
    return clamp((extension_24h_pct - 8.0) * 1.5, 0.0, 30.0)


def already_pumped_penalty(extension_24h_pct: float | None) -> float:
    if extension_24h_pct is None or extension_24h_pct < 20.0:
        return 0.0
    return clamp(20.0 + (extension_24h_pct - 20.0) * 1.2, 20.0, 65.0)


def bad_liquidity_penalty(liquidity: LiquidityMetrics) -> float:
    spread = liquidity.spread_pct
    if spread is None:
        spread_penalty = 6.0
    elif spread <= 0.75:
        spread_penalty = 0.0
    else:
        spread_penalty = clamp((spread - 0.75) * 12.0, 0.0, 35.0)
    depth = book_depth_usd(liquidity, "1")
    if depth <= 0:
        depth_penalty = 8.0
    elif depth < 10_000:
        depth_penalty = 10.0
    elif depth < 50_000:
        depth_penalty = 5.0
    else:
        depth_penalty = 0.0
    return clamp(spread_penalty + depth_penalty, 0.0, 35.0)


def thin_participation_penalty(volume: VolumeMetrics, liquidity: LiquidityMetrics) -> float:
    dollar_volume = dollar_volume_24h(volume) or 0.0
    penalty = 0.0
    if dollar_volume < 100_000:
        penalty += 20.0
    elif dollar_volume < 500_000:
        penalty += 14.0
    elif dollar_volume < 1_000_000:
        penalty += 8.0
    depth = book_depth_usd(liquidity, "1")
    if depth <= 0:
        penalty += 8.0
    elif depth < 10_000:
        penalty += 8.0
    elif depth < 50_000:
        penalty += 4.0
    return clamp(penalty, 0.0, 35.0)


def dilution_penalty(market_cap: MarketCapMetrics) -> float:
    cap = market_cap.circulating_market_cap
    fdv = market_cap.fdv
    if cap is None or fdv is None or cap <= 0 or fdv <= cap:
        return 0.0
    ratio = fdv / cap
    if ratio >= 10.0:
        return 18.0
    if ratio >= 5.0:
        return 10.0
    if ratio >= 3.0:
        return 5.0
    return 0.0


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
    small_cap_threshold: float,
    mid_cap_threshold: float,
    large_cap_threshold: float,
) -> list[str]:
    reasons: list[str] = []
    cap_class = market_cap_class(market_cap, small_cap_threshold, mid_cap_threshold, large_cap_threshold)
    if cap_class in {MARKET_CAP_MID_LABEL, MARKET_CAP_LARGE_LABEL} and components.get("LiquiditySafetyScore", 0.0) >= 70.0:
        reasons.append(f"{cap_class.title()} liquidity is strong while the move is still developing.")
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
    if cap_class == MARKET_CAP_MICRO_LABEL:
        reasons.append("Microcap setup is being treated as secondary and must clear the exceptional-quality bar.")
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
    small_cap_threshold: float,
) -> list[str]:
    risks: list[str] = []
    if liquidity.spread_pct is None or liquidity.spread_pct > 0.75:
        risks.append("Thin or wide liquidity.")
    if market_cap.circulating_market_cap is None:
        risks.append("Circulating supply metadata unavailable.")
    elif market_cap.circulating_market_cap < small_cap_threshold:
        risks.append("Microcap volatility and slippage risk.")
    if structure.distance_from_resistance_pct is not None and structure.distance_from_resistance_pct < 1.0:
        risks.append("Resistance is directly overhead.")
    if not catalyst:
        risks.append("No confirmed tier-1 catalyst found.")
    if structure.extension_24h_pct and structure.extension_24h_pct >= 15.0:
        risks.append("The move is becoming extended on the 24h window.")
    return risks[:5]
