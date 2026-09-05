from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.types import Candidate, LiveQuote, SetupStatus


@dataclass(slots=True)
class CandidateDiagnostic:
    candidate: Candidate
    qualifies: bool
    should_alert: bool
    alert_decision: str | None
    suppression_reasons: list[str]
    warnings: list[str]
    candle_count: int
    upside_runway_pct: float | None
    quote_age_seconds: float | None


def quote_age_seconds_at(quote: LiveQuote, now: datetime) -> float | None:
    market_time = quote.exchange_time or quote.last_trade_time or quote.ingestion_time
    if market_time is None:
        return None
    if market_time.tzinfo is None:
        market_time = market_time.replace(tzinfo=timezone.utc)
    return max(0.0, (now - market_time.astimezone(timezone.utc)).total_seconds())


def qualification_blockers(candidate: Candidate, settings: Settings, quote_age_seconds: float | None = None) -> list[str]:
    reasons: list[str] = []
    age = quote_age_seconds if quote_age_seconds is not None else candidate.quote.quote_age_seconds
    catalyst_override = candidate.score.catalyst_score >= settings.min_catalyst_override_score
    score_qualifies = candidate.score.early_move_score >= settings.min_early_move_score

    if age is None or age > settings.alert_max_quote_age_seconds:
        reasons.append("stale data")
    if candidate.status == SetupStatus.ALREADY_EXTENDED and not catalyst_override:
        reasons.append("24h extension too high")
    if not score_qualifies and not catalyst_override:
        reasons.append("score below threshold")
    return reasons


def build_candidate_diagnostic(
    candidate: Candidate,
    settings: Settings,
    *,
    candle_count: int,
    active_alert: Any | None = None,
    alert_decision: Any | None = None,
    quote_age_seconds: float | None = None,
) -> CandidateDiagnostic:
    blockers = qualification_blockers(candidate, settings, quote_age_seconds)
    qualifies = not blockers
    should_alert = qualifies
    alert_reason = None
    suppression_reasons = list(blockers)

    if qualifies and alert_decision is not None:
        should_alert = bool(alert_decision.should_send)
        alert_reason = str(alert_decision.reason)
        if not alert_decision.should_send:
            suppression_reasons.append(map_dedupe_reason(str(alert_decision.reason), str(alert_decision.alert_type)))
    elif qualifies and active_alert is not None:
        should_alert = False
        suppression_reasons.append("already alerted / deduplicated")

    return CandidateDiagnostic(
        candidate=candidate,
        qualifies=qualifies,
        should_alert=should_alert,
        alert_decision=alert_reason,
        suppression_reasons=suppression_reasons,
        warnings=warning_reasons(candidate, candle_count),
        candle_count=candle_count,
        upside_runway_pct=upside_runway_pct(candidate),
        quote_age_seconds=quote_age_seconds if quote_age_seconds is not None else candidate.quote.quote_age_seconds,
    )


def map_dedupe_reason(reason: str, alert_type: str) -> str:
    text = f"{alert_type} {reason}".lower()
    if "cooldown" in text:
        return "cooldown"
    if "duplicate" in text or "material setup change" in text:
        return "already alerted / deduplicated"
    return reason or "dedupe"


def warning_reasons(candidate: Candidate, candle_count: int) -> list[str]:
    warnings: list[str] = []
    if candidate.market_cap.circulating_market_cap is None:
        warnings.append("market cap missing")
    if candle_count < 24 * 30:
        warnings.append("30d baseline incomplete")
    runway = upside_runway_pct(candidate)
    if runway is None:
        warnings.append("technical upside target unavailable")
    elif runway < 10.0:
        warnings.append("preferred upside runway below 10%")
    return warnings


def upside_runway_pct(candidate: Candidate) -> float | None:
    price = candidate.quote.price
    if price <= 0:
        return None
    targets = [candidate.targets.tp1, candidate.targets.tp2, candidate.targets.stretch]
    upsides = [((target - price) / price * 100.0) for target in targets if target and target > price]
    return max(upsides) if upsides else None


def diagnostics_status(
    diagnostics: list[CandidateDiagnostic],
    skipped: dict[str, int],
    *,
    products_checked: int,
) -> dict[str, Any]:
    score_counts = {
        "gte_50": sum(1 for item in diagnostics if item.candidate.score.early_move_score >= 50.0),
        "gte_60": sum(1 for item in diagnostics if item.candidate.score.early_move_score >= 60.0),
        "gte_70": sum(1 for item in diagnostics if item.candidate.score.early_move_score >= 70.0),
        "gte_80": sum(1 for item in diagnostics if item.candidate.score.early_move_score >= 80.0),
    }
    suppressed = Counter(skipped)
    for item in diagnostics:
        if item.should_alert:
            continue
        reason = item.suppression_reasons[0] if item.suppression_reasons else "unknown"
        suppressed[reason] += 1
    return {
        "products_checked": products_checked,
        "candidates_evaluated": len(diagnostics),
        "score_counts": score_counts,
        "qualifying_now": sum(1 for item in diagnostics if item.qualifies),
        "would_alert_now": sum(1 for item in diagnostics if item.should_alert),
        "suppressed_by": dict(sorted(suppressed.items())),
        "top_candidates": [candidate_diagnostic_dict(item) for item in diagnostics[:10]],
    }


def candidate_diagnostic_dict(item: CandidateDiagnostic) -> dict[str, Any]:
    candidate = item.candidate
    components = candidate.score.components
    penalties = candidate.score.penalties
    return {
        "symbol": candidate.symbol,
        "product_id": candidate.product_id,
        "price": candidate.quote.price,
        "price_change_24h_pct": candidate.quote.price_change_24h_pct,
        "market_cap": candidate.market_cap.circulating_market_cap,
        "early_move_score": candidate.score.early_move_score,
        "technical_score": candidate.score.technical_score,
        "catalyst_score": candidate.score.catalyst_score,
        "volume_acceleration_score": components.get("VolumeAccelerationScore"),
        "gooner_ema_score": components.get("GoonerEMAScore"),
        "trader_acceleration_score": components.get("TraderAccelerationScore"),
        "upside_runway_score": components.get("UpsideRunwayScore"),
        "upside_runway_pct": item.upside_runway_pct,
        "extension_penalty": penalties.get("ExtensionPenalty"),
        "quote_age_seconds": item.quote_age_seconds,
        "qualifies": item.qualifies,
        "should_alert": item.should_alert,
        "suppression_reasons": item.suppression_reasons,
        "warnings": item.warnings,
        "candle_count": item.candle_count,
    }