from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.types import Candidate, SetupStatus


@dataclass(slots=True)
class AlertDecision:
    should_send: bool
    alert_type: str
    reason: str
    close_existing: bool = False


class AlertDeduplicator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def decide(self, candidate: Candidate, active_alert: Any | None) -> AlertDecision:
        age = candidate.quote.quote_age_seconds
        if age is None or age > self.settings.alert_max_quote_age_seconds:
            return AlertDecision(False, "stale", "quote data is stale")

        if active_alert and active_alert.invalidation_price and candidate.quote.price <= active_alert.invalidation_price:
            return AlertDecision(True, "invalidation", "setup invalidated", close_existing=True)

        catalyst_strength = candidate.score.catalyst_score
        qualifies = candidate.score.early_move_score >= self.settings.min_early_move_score
        catalyst_override = catalyst_strength >= self.settings.min_catalyst_override_score
        if not qualifies and not catalyst_override:
            return AlertDecision(False, "below_threshold", "score below alert threshold")

        if active_alert is None:
            alert_type = "fast_move" if candidate.status == SetupStatus.FAST_MOVE else "new_setup"
            return AlertDecision(True, alert_type, "new qualifying setup")

        old_status = str(active_alert.status)
        new_status = str(candidate.status)
        if old_status != new_status and status_rank(new_status) > status_rank(old_status):
            return AlertDecision(True, "status_transition", f"{old_status} -> {new_status}")

        if active_alert.trigger_price and candidate.quote.price >= active_alert.trigger_price and old_status == SetupStatus.EARLY.value:
            return AlertDecision(True, "breakout_followup", "breakout trigger hit")

        old_score = float(active_alert.score or 0.0)
        if candidate.score.early_move_score - old_score >= 8.0:
            return AlertDecision(True, "major_score_change", "material score improvement")

        if catalyst_override and active_alert.catalyst_id is None:
            return AlertDecision(True, "new_major_catalyst", "new catalyst override")

        return AlertDecision(False, "duplicate", "no material setup change")


def status_rank(status: str) -> int:
    order = {
        SetupStatus.EARLY.value: 1,
        SetupStatus.CONFIRMING.value: 2,
        SetupStatus.FAST_MOVE.value: 3,
        SetupStatus.ALREADY_EXTENDED.value: 0,
    }
    return order.get(status, 0)
