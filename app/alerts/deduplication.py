from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.analysis.bottoms import candidate_alert_stage
from app.analysis.scoring import primary_score
from app.config import Settings, get_settings
from app.diagnostics import qualification_blockers
from app.types import BottomStage, Candidate, SetupStatus


@dataclass(slots=True)
class AlertDecision:
    should_send: bool
    alert_type: str
    reason: str
    close_existing: bool = False


class AlertDeduplicator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def decide(self, candidate: Candidate, active_alert: Any | None, quote_age_seconds: float | None = None) -> AlertDecision:
        age = quote_age_seconds if quote_age_seconds is not None else candidate.quote.quote_age_seconds
        if age is None or age > self.settings.alert_max_quote_age_seconds:
            return AlertDecision(False, "stale", "quote data is stale")

        if active_alert and active_alert.invalidation_price and candidate.quote.price <= active_alert.invalidation_price:
            return AlertDecision(True, "invalidation", "setup invalidated", close_existing=True)

        score = primary_score(candidate)
        if qualification_blockers(candidate, self.settings, age):
            return AlertDecision(False, "below_threshold", "candidate does not clear its alert-stage thresholds")

        if active_alert is None:
            alert_type = new_alert_type(candidate)
            return AlertDecision(True, alert_type, "new qualifying setup")

        old_status = str(active_alert.status)
        new_status = candidate_alert_stage(candidate)
        if old_status != new_status and status_rank(new_status) > status_rank(old_status):
            return AlertDecision(True, "status_transition", f"{old_status} -> {new_status}")

        if active_alert.trigger_price and candidate.quote.price >= active_alert.trigger_price and old_status == SetupStatus.EARLY.value:
            return AlertDecision(True, "breakout_followup", "breakout trigger hit")

        old_score = active_alert_score(active_alert)
        if score - old_score >= 8.0:
            return AlertDecision(True, "major_score_change", "material risk-adjusted score improvement")

        old_components = getattr(active_alert, "component_scores", None) or {}
        bottom_score = float(candidate.score.components.get("BottomScore", 0.0) or 0.0)
        old_bottom_score = float(old_components.get("BottomScore", 0.0) or 0.0)
        if bottom_score - old_bottom_score >= self.settings.bottom_score_material_change:
            return AlertDecision(True, "bottom_score_change", "material bottom-score improvement")

        breakout_score = float(candidate.score.components.get("BreakoutScore", 0.0) or 0.0)
        old_breakout_score = float(old_components.get("BreakoutScore", 0.0) or 0.0)
        if breakout_score - old_breakout_score >= self.settings.breakout_score_material_change:
            return AlertDecision(True, "breakout_score_change", "material breakout-score improvement")

        old_bottom = active_bottom_metrics(active_alert)
        new_distance = candidate.bottom.distance_to_breakout_pct if candidate.bottom else None
        old_distance = as_float(old_bottom.get("distance_to_breakout_pct"))
        if (
            new_distance is not None and old_distance is not None
            and old_distance - new_distance >= self.settings.breakout_distance_material_shrink_pct
        ):
            return AlertDecision(True, "breakout_distance_change", "breakout distance shrank materially")

        new_expansion = candidate.bottom.first_expansion_ratio if candidate.bottom else None
        old_expansion = as_float(old_bottom.get("first_expansion_ratio"))
        if (
            new_expansion is not None and old_expansion is not None and old_expansion > 0
            and new_expansion / old_expansion >= 1.75
            and breakout_score >= old_breakout_score + 4.0
        ):
            return AlertDecision(True, "volume_regime_change", "first-expansion volume regime strengthened")

        catalyst_override = candidate.score.catalyst_score >= self.settings.min_catalyst_override_score
        if catalyst_override and active_alert.catalyst_id is None:
            return AlertDecision(True, "new_major_catalyst", "new catalyst override")

        if is_new_setup_after_cooldown(candidate, active_alert, self.settings.bottom_setup_cooldown_minutes):
            return AlertDecision(True, "new_setup_cycle", "new base formed after the setup cooldown")

        return AlertDecision(False, "duplicate", "no material setup change")


def active_alert_score(active_alert: Any) -> float:
    components = getattr(active_alert, "component_scores", None) or {}
    if isinstance(components, dict) and components.get("RiskAdjustedOpportunityScore") is not None:
        return float(components["RiskAdjustedOpportunityScore"])
    return float(getattr(active_alert, "score", 0.0) or 0.0)


def status_rank(status: str) -> int:
    order = {
        SetupStatus.EARLY.value: 1,
        BottomStage.BOTTOM_FORMING.value: 2,
        BottomStage.PRE_BREAKOUT.value: 3,
        SetupStatus.CONFIRMING.value: 4,
        BottomStage.BREAKOUT_FIRING.value: 5,
        BottomStage.RETEST_HOLD.value: 6,
        SetupStatus.FAST_MOVE.value: 7,
        SetupStatus.ALREADY_EXTENDED.value: 0,
    }
    return order.get(status, 0)


def new_alert_type(candidate: Candidate) -> str:
    if candidate.status == SetupStatus.FAST_MOVE:
        return "fast_move"
    stage = candidate_alert_stage(candidate)
    return {
        BottomStage.BOTTOM_FORMING.value: "bottom_forming",
        BottomStage.PRE_BREAKOUT.value: "pre_breakout",
        BottomStage.BREAKOUT_FIRING.value: "breakout_firing",
        BottomStage.RETEST_HOLD.value: "retest_hold",
    }.get(stage, "new_setup")


def active_bottom_metrics(active_alert: Any) -> dict[str, Any]:
    raw = getattr(active_alert, "raw_candidate", None) or {}
    bottom = raw.get("bottom") if isinstance(raw, dict) else None
    return bottom if isinstance(bottom, dict) else {}


def is_new_setup_after_cooldown(
    candidate: Candidate,
    active_alert: Any,
    cooldown_minutes: int,
) -> bool:
    if candidate.bottom is None or candidate.bottom.stage not in {
        BottomStage.BOTTOM_FORMING,
        BottomStage.PRE_BREAKOUT,
    }:
        return False
    detection_time = getattr(active_alert, "detection_time", None)
    if not isinstance(detection_time, datetime):
        return False
    if detection_time.tzinfo is None:
        detection_time = detection_time.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - detection_time.astimezone(timezone.utc)).total_seconds() / 60.0
    if elapsed < cooldown_minutes:
        return False
    old_bottom = active_bottom_metrics(active_alert)
    old_stage = str(old_bottom.get("stage") or getattr(active_alert, "status", ""))
    if old_stage in {BottomStage.BREAKOUT_FIRING.value, BottomStage.RETEST_HOLD.value, SetupStatus.CONFIRMING.value}:
        return True
    old_support = as_float(old_bottom.get("support"))
    old_resistance = as_float(old_bottom.get("resistance"))
    support_changed = level_changed(old_support, candidate.bottom.support)
    resistance_changed = level_changed(old_resistance, candidate.bottom.resistance)
    return support_changed or resistance_changed


def level_changed(old: float | None, new: float | None) -> bool:
    return old is not None and new is not None and old > 0 and abs(new - old) / old >= 0.03


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
