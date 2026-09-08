from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.analysis.bottoms import candidate_alert_stage
from app.analysis.scoring import primary_score
from app.config import Settings
from app.formatting import clamp
from app.types import (
    BottomStage,
    Candidate,
    HoldAssessment,
    PortfolioHolding,
    RotationClassification,
    RotationRecommendation,
    RotationSource,
    SetupStatus,
)

CLASSIFICATION_ORDER = {
    RotationClassification.NO_ROTATION: 0,
    RotationClassification.WATCH_ROTATION: 1,
    RotationClassification.PARTIAL_ROTATION: 2,
    RotationClassification.STRONG_ROTATION: 3,
}


def apply_portfolio_rotation(
    candidates: list[Candidate],
    holdings: list[PortfolioHolding],
    settings: Settings,
    *,
    recent_recommendations: Iterable[Any] = (),
    now: datetime | None = None,
) -> list[HoldAssessment]:
    now = now or datetime.now(timezone.utc)
    candidate_map = {candidate.symbol.upper(): candidate for candidate in candidates}
    assessments = [
        assess_holding(holding, candidate_map.get(holding.ticker.upper()))
        for holding in holdings
    ]
    recent = list(recent_recommendations)
    for candidate in candidates:
        candidate.rotation = compare_candidate_to_portfolio(
            candidate,
            assessments,
            settings,
            recent_recommendations=recent,
            now=now,
        )
        candidate.score.components["RelativeOpportunityScore"] = round(
            candidate.rotation.relative_opportunity_score,
            2,
        )
        if candidate.rotation.classification != RotationClassification.NO_ROTATION:
            candidate.tags.append(candidate.rotation.classification.value.lower())
        if (
            candidate.rotation.classification in {
                RotationClassification.PARTIAL_ROTATION,
                RotationClassification.STRONG_ROTATION,
            }
            and candidate.fib
            and candidate.fib.reliable
            and candidate.fib.confluence_score >= settings.fib_high_confluence_score
            and candidate.bottom
            and candidate.bottom.stage != BottomStage.NONE
            and primary_score(candidate) >= 70.0
            and float(candidate.score.components.get("VolumeAccelerationScore", 0.0) or 0.0) >= 60.0
            and float(candidate.score.penalties.get("ExtensionPenalty", 0.0) or 0.0) < 10.0
        ):
            candidate.tags.append("reflexive_fib_rotation_edge")
        candidate.tags = list(dict.fromkeys(candidate.tags))
    return assessments


def assess_holding(holding: PortfolioHolding, candidate: Candidate | None) -> HoldAssessment:
    if candidate is None:
        return HoldAssessment(
            ticker=holding.ticker,
            quantity=holding.quantity,
            current_price=None,
            current_position_value=holding.current_position_value,
            hold_score=50.0,
            expected_upside_pct=None,
            risk_reward=None,
            deterioration_score=0.0,
            protected=False,
            protection_reason=None,
            components={"DataAvailabilityScore": 0.0},
            reasons=["Holding is not currently evaluable in the Coinbase scan universe."],
        )
    score = candidate.score
    components = score.components
    scanner = primary_score(candidate)
    volume = float(components.get("VolumeAccelerationScore", 0.0) or 0.0)
    liquidity = float(components.get("LiquiditySafetyScore", 0.0) or 0.0)
    upside = float(components.get("UpsideRunwayScore", 42.0) or 42.0)
    relative_strength = (
        candidate.bottom.relative_strength_score
        if candidate.bottom else float(components.get("RelativeStrengthScore", 50.0) or 50.0)
    )
    trend = hold_trend_score(candidate)
    support = support_integrity_score(candidate)
    participation = clamp(
        volume * 0.75
        + float(components.get("TraderAccelerationScore", 50.0) or 50.0) * 0.15
        + float(components.get("BuyerAccelerationScore", 50.0) or 50.0) * 0.10
    )
    deterioration = holding_deterioration_score(candidate, volume, relative_strength, support)
    hold_score = clamp(
        scanner * 0.31
        + trend * 0.18
        + relative_strength * 0.12
        + participation * 0.12
        + support * 0.10
        + upside * 0.08
        + liquidity * 0.07
        + float(components.get("OrderBookScore", 0.0) or 0.0) * 0.02
        - deterioration * 0.20
    )
    protected, protection_reason = rotation_protection(candidate, trend, support, participation, relative_strength)
    if protected:
        hold_score = max(hold_score, 78.0)
    position_value = holding.current_position_value
    if position_value is None:
        position_value = holding.quantity * candidate.quote.price
    expected_upside = next_target_upside_pct(candidate)
    risk_reward = candidate_risk_reward(candidate)
    reasons = holding_reasons(candidate, deterioration, protected, protection_reason)
    return HoldAssessment(
        ticker=holding.ticker,
        quantity=holding.quantity,
        current_price=candidate.quote.price,
        current_position_value=round(position_value, 2) if position_value is not None else None,
        hold_score=round(hold_score, 2),
        expected_upside_pct=round(expected_upside, 2) if expected_upside is not None else None,
        risk_reward=round(risk_reward, 2) if risk_reward is not None else None,
        deterioration_score=round(deterioration, 2),
        protected=protected,
        protection_reason=protection_reason,
        components={
            "ScannerScore": round(scanner, 2),
            "TrendStrengthScore": round(trend, 2),
            "RelativeStrengthScore": round(relative_strength, 2),
            "ParticipationScore": round(participation, 2),
            "SupportIntegrityScore": round(support, 2),
            "UpsideRunwayScore": round(upside, 2),
            "LiquiditySafetyScore": round(liquidity, 2),
            "DeteriorationScore": round(deterioration, 2),
        },
        reasons=reasons,
    )


def compare_candidate_to_portfolio(
    candidate: Candidate,
    holdings: list[HoldAssessment],
    settings: Settings,
    *,
    recent_recommendations: list[Any],
    now: datetime,
) -> RotationRecommendation:
    candidate_score = rotation_candidate_score(candidate)
    candidate_rr = candidate_risk_reward(candidate)
    relative_score = relative_opportunity_score(candidate, candidate_score, candidate_rr)
    sources: list[RotationSource] = []
    for holding in holdings:
        if holding.ticker.upper() == candidate.symbol.upper():
            continue
        sources.append(
            evaluate_source(
                candidate,
                candidate_score,
                candidate_rr,
                holding,
                settings,
                recent_recommendations,
                now,
            )
        )
    sources.sort(
        key=lambda source: (
            CLASSIFICATION_ORDER[source.classification],
            not source.protected,
            source.rotation_advantage,
            source.source_position_value or 0.0,
        ),
        reverse=True,
    )
    best = sources[0] if sources else None
    classification = best.classification if best else RotationClassification.NO_ROTATION
    confidence = best.confidence if best else 0.0
    return RotationRecommendation(
        classification=classification,
        destination_ticker=candidate.symbol,
        candidate_score=round(candidate_score, 2),
        relative_opportunity_score=round(relative_score, 2),
        candidate_risk_reward=round(candidate_rr, 2) if candidate_rr is not None else None,
        suggested_percentage=best.suggested_percentage if best else 0.0,
        confidence=round(confidence, 2),
        best_source=best,
        sources=sources[:3],
    )


def evaluate_source(
    candidate: Candidate,
    candidate_score: float,
    candidate_rr: float | None,
    holding: HoldAssessment,
    settings: Settings,
    recent_recommendations: list[Any],
    now: datetime,
) -> RotationSource:
    advantage = candidate_score - holding.hold_score
    reason_parts: list[str] = []
    classification = RotationClassification.NO_ROTATION
    suggested = 0.0
    confidence = rotation_confidence_from_inputs(candidate, holding, candidate_score)
    if holding.protected:
        reason_parts.append(f"ROTATION_PROTECTED: {holding.protection_reason or 'relative-strength leadership remains intact'}")
    elif holding.current_price is None:
        reason_parts.append("holding lacks current Coinbase evaluation data")
    elif (
        holding.current_position_value is not None
        and holding.current_position_value < settings.rotation_min_position_value_usd
    ):
        reason_parts.append("position is too small to be a useful funding source")
    elif candidate_is_extended(candidate):
        reason_parts.append("candidate is already extended")
    elif advantage < settings.rotation_watch_score_difference:
        reason_parts.append("score advantage is too small")
    else:
        classification = RotationClassification.WATCH_ROTATION
        reason_parts.append("candidate is materially stronger, but full confirmation is not yet present")
        rr_improvement = risk_reward_improvement(candidate_rr, holding.risk_reward)
        partial_ready = (
            advantage >= settings.rotation_partial_score_difference
            and candidate_rr is not None
            and candidate_rr >= settings.rotation_min_candidate_risk_reward
            and rr_improvement is not None
            and rr_improvement >= settings.rotation_min_risk_reward_improvement
            and holding.deterioration_score >= settings.rotation_min_holding_deterioration
            and confidence >= settings.rotation_min_confidence
        )
        if partial_ready:
            classification = RotationClassification.PARTIAL_ROTATION
            span = max(1.0, settings.rotation_strong_score_difference - settings.rotation_partial_score_difference)
            progress = clamp((advantage - settings.rotation_partial_score_difference) / span * 100.0) / 100.0
            suggested = settings.rotation_partial_min_pct + progress * (
                settings.rotation_partial_max_pct - settings.rotation_partial_min_pct
            )
            reason_parts = [source_reason(candidate, holding)]
        strong_ready = (
            partial_ready
            and advantage >= settings.rotation_strong_score_difference
            and holding.deterioration_score >= settings.rotation_min_holding_deterioration + 20.0
            and confidence >= max(78.0, settings.rotation_min_confidence)
            and candidate_confirmation(candidate)
        )
        if strong_ready:
            classification = RotationClassification.STRONG_ROTATION
            suggested = min(
                settings.rotation_strong_max_pct,
                settings.rotation_partial_max_pct + (advantage - settings.rotation_strong_score_difference) * 0.5,
            )
            reason_parts = [source_reason(candidate, holding)]

    churn_reason = anti_churn_reason(
        holding.ticker,
        candidate.symbol,
        advantage,
        settings,
        recent_recommendations,
        now,
    )
    if churn_reason and classification != RotationClassification.NO_ROTATION:
        classification = RotationClassification.NO_ROTATION
        suggested = 0.0
        reason_parts = [churn_reason]
    return RotationSource(
        ticker=holding.ticker,
        classification=classification,
        hold_score=holding.hold_score,
        rotation_advantage=round(advantage, 2),
        suggested_percentage=round(clamp(suggested, 0.0, settings.rotation_strong_max_pct), 1),
        source_price=holding.current_price,
        source_position_value=holding.current_position_value,
        source_risk_reward=holding.risk_reward,
        deterioration_score=holding.deterioration_score,
        confidence=round(confidence, 2),
        protected=holding.protected,
        reason="; ".join(reason_parts) or "no material rotation edge",
    )


def rotation_candidate_score(candidate: Candidate) -> float:
    components = candidate.score.components
    fib = candidate.fib
    fib_score = fib.confluence_score if fib and fib.reliable else 40.0
    relative_strength = candidate.bottom.relative_strength_score if candidate.bottom else 50.0
    score = (
        primary_score(candidate) * 0.68
        + float(components.get("UpsideRunwayScore", 42.0) or 42.0) * 0.09
        + float(components.get("LiquiditySafetyScore", 0.0) or 0.0) * 0.08
        + float(components.get("VolumeAccelerationScore", 0.0) or 0.0) * 0.08
        + fib_score * 0.05
        + relative_strength * 0.02
    )
    return clamp(score - float(candidate.score.penalties.get("ExtensionPenalty", 0.0) or 0.0) * 0.18)


def relative_opportunity_score(candidate: Candidate, candidate_score: float, risk_reward: float | None) -> float:
    rr_score = clamp((risk_reward or 0.0) * 18.0)
    stage_bonus = 5.0 if candidate_confirmation(candidate) else 0.0
    return clamp(candidate_score * 0.88 + rr_score * 0.12 + stage_bonus)


def candidate_risk_reward(candidate: Candidate) -> float | None:
    price = candidate.quote.price
    invalidation = candidate.structure.invalidation
    if candidate.bottom and candidate.bottom.invalidation:
        invalidation = candidate.bottom.invalidation
    targets = sorted(
        target for target in (
            candidate.targets.tp1,
            candidate.targets.tp2,
            candidate.targets.stretch,
            candidate.fib.extensions.get("1.272") if candidate.fib and candidate.fib.reliable else None,
            candidate.fib.extensions.get("1.618") if candidate.fib and candidate.fib.reliable else None,
        )
        if target is not None and target > price
    )
    if invalidation is None or invalidation >= price or not targets:
        return None
    reward = targets[0] - price
    risk = price - invalidation
    return reward / risk if risk > 0 else None


def next_target_upside_pct(candidate: Candidate) -> float | None:
    price = candidate.quote.price
    targets = sorted(
        value for value in (candidate.targets.tp1, candidate.targets.tp2, candidate.targets.stretch)
        if value is not None and value > price
    )
    return (targets[0] - price) / price * 100.0 if targets and price > 0 else None


def hold_trend_score(candidate: Candidate) -> float:
    stage = candidate_alert_stage(candidate)
    stage_scores = {
        BottomStage.BREAKOUT_FIRING.value: 95.0,
        BottomStage.RETEST_HOLD.value: 88.0,
        BottomStage.PRE_BREAKOUT.value: 78.0,
        BottomStage.BOTTOM_FORMING.value: 62.0,
        SetupStatus.CONFIRMING.value: 85.0,
        SetupStatus.EARLY.value: 70.0,
        SetupStatus.FAST_MOVE.value: 82.0,
        SetupStatus.ALREADY_EXTENDED.value: 38.0,
    }
    return stage_scores.get(stage, 50.0)


def support_integrity_score(candidate: Candidate) -> float:
    price = candidate.quote.price
    support = candidate.structure.support_low
    if candidate.bottom and candidate.bottom.support:
        support = candidate.bottom.support
    if support is None or support <= 0:
        return 50.0
    distance = (price - support) / support * 100.0
    if distance < -1.0:
        return 10.0
    if distance <= 5.0:
        return 92.0
    if distance <= 15.0:
        return 76.0
    if distance <= 30.0:
        return 52.0
    return 30.0


def holding_deterioration_score(
    candidate: Candidate,
    volume_score: float,
    relative_strength: float,
    support_score: float,
) -> float:
    penalties = candidate.score.penalties
    score = (
        float(penalties.get("ExtensionPenalty", 0.0) or 0.0) * 1.15
        + float(penalties.get("AlreadyPumpedPenalty", 0.0) or 0.0) * 0.55
        + clamp(55.0 - volume_score) * 0.40
        + clamp(52.0 - relative_strength) * 0.28
        + clamp(55.0 - support_score) * 0.38
    )
    short_volume = max(
        candidate.volume.ratios.get("5m_vs_baseline", 0.0),
        candidate.volume.ratios.get("15m_vs_baseline", 0.0),
    )
    if (candidate.quote.price_change_24h_pct or 0.0) >= 15.0 and short_volume < 0.9:
        score += 18.0
    if candidate.status == SetupStatus.ALREADY_EXTENDED:
        score += 18.0
    if candidate.fib and candidate.fib.reliable:
        extension_1618 = candidate.fib.extensions.get("1.618")
        if extension_1618 and candidate.quote.price >= extension_1618 * 0.97:
            score += 16.0
    return clamp(score)


def rotation_protection(
    candidate: Candidate,
    trend: float,
    support: float,
    participation: float,
    relative_strength: float,
) -> tuple[bool, str | None]:
    extended = float(candidate.score.penalties.get("ExtensionPenalty", 0.0) or 0.0) >= 15.0
    breakout = candidate.bottom and candidate.bottom.stage in {BottomStage.BREAKOUT_FIRING, BottomStage.RETEST_HOLD}
    if breakout and candidate.bottom and candidate.bottom.breakout_score >= 68.0 and support >= 65.0:
        return True, "breakout/retest structure remains intact"
    if trend >= 82.0 and relative_strength >= 68.0 and participation >= 60.0 and not extended:
        return True, "relative strength and participation remain strong above support"
    if primary_score(candidate) >= 85.0 and support >= 70.0 and not extended:
        return True, "current holding remains a top risk-adjusted setup"
    return False, None


def candidate_is_extended(candidate: Candidate) -> bool:
    if candidate.status == SetupStatus.ALREADY_EXTENDED:
        return True
    if float(candidate.score.penalties.get("ExtensionPenalty", 0.0) or 0.0) >= 15.0:
        return True
    if candidate.fib and candidate.fib.reliable:
        extension = candidate.fib.extensions.get("1.618")
        if extension and candidate.quote.price >= extension:
            return True
    return False


def candidate_confirmation(candidate: Candidate) -> bool:
    if candidate.bottom and candidate.bottom.stage in {BottomStage.BREAKOUT_FIRING, BottomStage.RETEST_HOLD}:
        return True
    if candidate.status in {SetupStatus.CONFIRMING, SetupStatus.FAST_MOVE}:
        return True
    if candidate.fib and candidate.fib.signal in {
        "0.500 RECLAIM", "0.618 RECLAIM", "0.618 SUPPORT CONFIRMED", "1.000 BREAKOUT"
    }:
        return True
    return False


def risk_reward_improvement(candidate_rr: float | None, holding_rr: float | None) -> float | None:
    if candidate_rr is None or holding_rr is None:
        return None
    return candidate_rr - holding_rr


def rotation_confidence_from_inputs(candidate: Candidate, holding: HoldAssessment, candidate_score: float) -> float:
    fib_score = candidate.fib.confluence_score if candidate.fib and candidate.fib.reliable else 42.0
    return clamp(
        candidate_score * 0.38
        + float(candidate.score.components.get("LiquiditySafetyScore", 0.0) or 0.0) * 0.17
        + float(candidate.score.components.get("VolumeAccelerationScore", 0.0) or 0.0) * 0.15
        + fib_score * 0.13
        + holding.deterioration_score * 0.17
    )


def source_reason(candidate: Candidate, holding: HoldAssessment) -> str:
    source = holding.reasons[0] if holding.reasons else "holding momentum is cooling"
    destination = "candidate participation and risk/reward are materially stronger"
    if candidate.fib and candidate.fib.reliable:
        destination = f"candidate shows {candidate.fib.signal.lower()} with improving setup quality"
    return f"{source}; {destination}"


def holding_reasons(
    candidate: Candidate,
    deterioration: float,
    protected: bool,
    protection_reason: str | None,
) -> list[str]:
    if protected:
        return [f"ROTATION_PROTECTED: {protection_reason}"]
    reasons: list[str] = []
    if candidate.status == SetupStatus.ALREADY_EXTENDED:
        reasons.append("Holding is extended from its recent base.")
    if float(candidate.score.components.get("VolumeAccelerationScore", 0.0) or 0.0) < 40.0:
        reasons.append("Volume participation is cooling.")
    if deterioration >= 50.0:
        reasons.append("Relative setup quality has materially deteriorated.")
    if candidate.structure.support_low and candidate.quote.price < candidate.structure.support_low:
        reasons.append("Price has lost detected support.")
    return reasons or ["Holding remains neutral; no material deterioration signal."]


def anti_churn_reason(
    source: str,
    destination: str,
    advantage: float,
    settings: Settings,
    recent_recommendations: list[Any],
    now: datetime,
) -> str | None:
    for row in recent_recommendations:
        timestamp = aware_datetime(getattr(row, "created_at", None) or getattr(row, "timestamp", None))
        if timestamp is None:
            continue
        row_source = str(getattr(row, "source_asset", "")).upper()
        row_destination = str(getattr(row, "destination_asset", "")).upper()
        age = now - timestamp
        if row_source == source.upper() and row_destination == destination.upper():
            if age < timedelta(minutes=settings.rotation_cooldown_minutes):
                return "rotation cooldown is active for this pair"
        if row_source == destination.upper() and row_destination == source.upper():
            if (
                age < timedelta(minutes=settings.rotation_reverse_hysteresis_minutes)
                and advantage < settings.rotation_partial_score_difference + settings.rotation_hysteresis_extra_difference
            ):
                return "reverse-rotation hysteresis is active"
    return None


def aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
