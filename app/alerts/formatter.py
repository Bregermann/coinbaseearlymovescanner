from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.analysis.bottoms import candidate_alert_stage
from app.analysis.scoring import book_depth_usd, dollar_volume_24h, market_cap_class, primary_score
from app.formatting import compact_money, money, pct, ratio
from app.types import BottomStage, Candidate, RotationClassification, SetupStatus

try:
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - only used on systems without tzdata
    ET = timezone(timedelta(hours=-5), "ET")

STATUS_COLORS = {
    SetupStatus.EARLY: 0x2ECC71,
    SetupStatus.CONFIRMING: 0xF1C40F,
    SetupStatus.FAST_MOVE: 0xE74C3C,
    SetupStatus.ALREADY_EXTENDED: 0x95A5A6,
    BottomStage.BOTTOM_FORMING: 0x3498DB,
    BottomStage.PRE_BREAKOUT: 0xF1C40F,
    BottomStage.BREAKOUT_FIRING: 0x2ECC71,
    BottomStage.RETEST_HOLD: 0x9B59B6,
}


def candidate_to_payload(candidate: Candidate, alert_type: str = "new_setup", quote_age_seconds: float | None = None) -> dict[str, Any]:
    embed = candidate_to_embed(candidate, alert_type=alert_type, quote_age_seconds=quote_age_seconds)
    return {"username": "Coinbase Early Move Scanner", "embeds": [embed]}


def rotation_to_payload(candidate: Candidate, quote_age_seconds: float | None = None) -> dict[str, Any]:
    rotation = candidate.rotation
    source = rotation.best_source if rotation else None
    if rotation is None or source is None:
        raise ValueError("candidate has no portfolio rotation recommendation")
    trigger = candidate.structure.breakout_trigger
    description = (
        f"{candidate.symbol} has overtaken {source.ticker} in expected near-term setup quality.\n"
        f"This is a conditional, partial funding idea, not an instruction to liquidate a position."
    )
    fields = [
        {
            "name": "Relative Opportunity",
            "value": (
                f"{candidate.symbol}: {rotation.candidate_score:.0f}\n"
                f"{source.ticker}: {source.hold_score:.0f}\n"
                f"Difference: {source.rotation_advantage:+.0f}"
            ),
            "inline": True,
        },
        {"name": "Fibonacci", "value": fibonacci_field(candidate), "inline": False},
        {
            "name": "Suggested",
            "value": (
                f"Watch {source.ticker} -> {candidate.symbol} if {candidate.symbol} clears {money(trigger)}"
                if rotation.classification == RotationClassification.WATCH_ROTATION else
                f"Trim ~{source.suggested_percentage:.0f}% {source.ticker} -> {candidate.symbol}"
            ),
            "inline": False,
        },
        {"name": "Why", "value": source.reason, "inline": False},
    ]
    return {
        "username": "Coinbase Early Move Scanner",
        "embeds": [
            {
                "title": f"🔄 {rotation.classification.value.replace('_', ' ')}: {source.ticker} -> {candidate.symbol}",
                "description": description,
                "color": 0xF39C12,
                "fields": fields,
                "footer": {"text": footer(candidate, quote_age_seconds=quote_age_seconds)},
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        ],
    }


def candidate_to_embed(candidate: Candidate, alert_type: str = "new_setup", quote_age_seconds: float | None = None) -> dict[str, Any]:
    symbol = candidate.symbol
    stage = candidate_alert_stage(candidate)
    title_prefix = {
        BottomStage.BOTTOM_FORMING.value: "🔵",
        BottomStage.PRE_BREAKOUT.value: "🟡",
        BottomStage.BREAKOUT_FIRING.value: "🟢",
        BottomStage.RETEST_HOLD.value: "🟣",
    }.get(stage, "⚡" if alert_type in {"breakout_followup", "status_transition"} else "🚨")
    if alert_type == "invalidation":
        title_prefix = "❌"
    title = (
        f"{title_prefix} {stage}: {symbol}"
        if candidate.bottom and candidate.bottom.stage != BottomStage.NONE
        else f"{title_prefix} {symbol} — {stage}"
    )
    if "reflexive_fib_rotation_edge" in candidate.tags:
        title = f"🚨 REFLEXIVE + FIB + ROTATION EDGE: {symbol}"
    subtitle = " + ".join(human_tag(tag) for tag in candidate.tags[:3]) or "Chart/Liquidity"
    fields = [
        {"name": "Snapshot", "value": snapshot_field(candidate), "inline": False},
        {"name": "Bottom / Breakout", "value": bottom_field(candidate), "inline": False},
        {"name": "Liquidity Safety", "value": liquidity_safety_field(candidate), "inline": True},
        {"name": "Participation", "value": volume_field(candidate), "inline": True},
        {"name": "Structure", "value": structure_field(candidate), "inline": True},
        {"name": "Fibonacci", "value": fibonacci_field(candidate), "inline": False},
        {"name": "Targets", "value": targets_field(candidate), "inline": True},
        {"name": "Rotation", "value": rotation_field(candidate), "inline": False},
        {"name": "WHY NOW", "value": why_now(candidate), "inline": False},
        {"name": "RISKS", "value": risks(candidate), "inline": False},
    ]
    if candidate.catalyst:
        fields.insert(-2, {"name": "Catalyst", "value": catalyst_field(candidate), "inline": False})
    embed = {
        "title": title,
        "description": subtitle,
        "color": STATUS_COLORS.get(candidate.bottom.stage if candidate.bottom else candidate.status, 0x3498DB),
        "fields": fields,
        "footer": {"text": footer(candidate, quote_age_seconds=quote_age_seconds)},
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if candidate.catalyst and candidate.status == SetupStatus.FAST_MOVE:
        embed["description"] = str(candidate.catalyst.get("catalyst_type") or "FAST-MOVE CATALYST")
    return embed


def snapshot_field(candidate: Candidate) -> str:
    quote = candidate.quote
    cap = candidate.market_cap
    return "\n".join(
        [
            f"Price: {money(quote.price)}",
            f"24h: {pct(quote.price_change_24h_pct)}",
            f"Market Cap: {compact_money(cap.circulating_market_cap)}",
            f"Market Cap Class: {market_cap_class(cap)}",
            f"FDV: {compact_money(cap.fdv)}",
            f"24h Coinbase Volume: {compact_money(dollar_volume_24h(candidate.volume))}",
            f"Risk-Adjusted Opportunity: {primary_score(candidate):.0f} / 100",
            f"Early Move Score: {candidate.score.early_move_score:.0f} / 100",
        ]
    )


def liquidity_safety_field(candidate: Candidate) -> str:
    components = candidate.score.components
    liquidity = candidate.liquidity
    safety = components.get("LiquiditySafetyScore")
    traders = components.get("TraderCount")
    trader_text = f"{traders:,.0f}" if isinstance(traders, (int, float)) else "N/A"
    return "\n".join(
        [
            f"LIQUIDITY SAFETY: {format_score(safety)} / 100",
            f"SPREAD: {pct(liquidity.spread_pct, signed=False)}",
            f"BOOK DEPTH +/-1%: {compact_money(book_depth_usd(liquidity, '1'))}",
            f"COINBASE TRADERS: {trader_text}",
        ]
    )


def volume_field(candidate: Candidate) -> str:
    r = candidate.volume.ratios
    return "\n".join(
        [
            f"5m {ratio(r.get('5m_vs_baseline'))} | 15m {ratio(r.get('15m_vs_baseline'))}",
            f"1h {ratio(r.get('1h_vs_7d'))} | 4h {ratio(r.get('4h_vs_7d'))}",
            f"First expansion 15m {ratio(r.get('15m_vs_prior_20'))}",
            "Traders N/A | Buyers N/A" if candidate.score.components.get("TraderCount") is None else "Trader participation available",
        ]
    )


def structure_field(candidate: Candidate) -> str:
    s = candidate.structure
    support = "n/a"
    if s.support_low and s.support_high:
        support = f"{money(s.support_low)}-{money(s.support_high)}"
    return "\n".join(
        [
            f"Base / Support: {support}",
            f"Breakout Trigger: {money(s.breakout_trigger)}",
            f"Confirmation: {money(s.confirmation)}",
            f"Invalidation: {money(s.invalidation)}",
        ]
    )


def bottom_field(candidate: Candidate) -> str:
    bottom = candidate.bottom
    if bottom is None:
        return "No mature bottoming structure detected."
    drawdown = pct(bottom.prior_drawdown_pct)
    distance = pct(bottom.distance_to_breakout_pct)
    return "\n".join(
        [
            f"Stage: {bottom.stage.value}",
            f"Pattern: {bottom.pattern}",
            f"{bottom.pattern} | drawdown {drawdown}",
            f"Bottom {bottom.bottom_score:.0f} | Breakout {bottom.breakout_score:.0f}",
            f"Support {money(bottom.support)} | Resistance {money(bottom.resistance)}",
            f"Distance {distance} | Invalid {money(bottom.invalidation)}",
            f"{bottom.macd_state} | {bottom.relative_strength_state}",
        ]
    )


def fibonacci_field(candidate: Candidate) -> str:
    fib = candidate.fib
    if fib is None or not fib.reliable:
        return "Fib: N/A (no reliable active impulse)"
    level_618 = fib.retracements.get("0.618")
    status_618 = "N/A"
    if level_618:
        status_618 = "ABOVE / HOLDING" if candidate.quote.price >= level_618 else "BELOW"
    next_extension = next(
        (
            (label, value)
            for label, value in fib.extensions.items()
            if label != "1.000" and value > candidate.quote.price
        ),
        None,
    )
    extension_text = f"{next_extension[0]} {money(next_extension[1])}" if next_extension else "N/A"
    lines = [
            f"Impulse ({fib.timeframe}): {money(fib.anchor_low)} -> {money(fib.anchor_high)}",
            f"State: {fib.signal} | Swing confidence {fib.swing_confidence:.0f}",
            f"Support {money(fib.nearest_support)} | Resistance {money(fib.nearest_resistance)}",
            f"0.618: {money(level_618)} ({status_618})",
            f"Next extension: {extension_text} | Confluence {fib.confluence_score:.0f}/100",
    ]
    available = [
        f"{label} {value:.0f}%"
        for label, value in fib.target_probabilities.items()
        if value is not None
    ]
    if available:
        lines.append(f"Historical reach: {' | '.join(available[:3])} ({fib.probability_confidence})")
    return "\n".join(lines)


def rotation_field(candidate: Candidate) -> str:
    rotation = candidate.rotation
    if rotation is None:
        return "Portfolio not configured; no holdings were inferred."
    source = rotation.best_source
    if source is None:
        return "NO_ROTATION: no eligible funding source."
    if rotation.classification == RotationClassification.NO_ROTATION:
        return f"NO_ROTATION\n{source.ticker}: {source.reason}"
    action = (
        f"Watch {source.ticker} -> {candidate.symbol}"
        if rotation.classification == RotationClassification.WATCH_ROTATION else
        f"Trim ~{source.suggested_percentage:.0f}% {source.ticker} -> {candidate.symbol}"
    )
    protected = [item.ticker for item in rotation.sources if item.protected]
    lines = [
        rotation.classification.value,
        action,
        f"Candidate {rotation.candidate_score:.0f} | Hold {source.hold_score:.0f} | Advantage {source.rotation_advantage:+.0f}",
        source.reason,
    ]
    if protected:
        lines.append(f"Do not rotate: {', '.join(protected)} (ROTATION_PROTECTED)")
    return "\n".join(lines)


def targets_field(candidate: Candidate) -> str:
    price = candidate.quote.price
    targets = candidate.targets
    return "\n".join(
        [
            target_line("TP1", targets.tp1, price),
            target_line("TP2", targets.tp2, price),
            target_line("Stretch", targets.stretch, price),
        ]
    )


def target_line(label: str, target: float | None, price: float) -> str:
    if target is None or price <= 0:
        return f"{label}: n/a"
    upside = (target - price) / price * 100.0
    return f"{label}: {money(target)} ({pct(upside)})"


def catalyst_field(candidate: Candidate) -> str:
    catalyst = candidate.catalyst
    if not catalyst:
        return "None detected."
    lines = [str(catalyst.get("headline") or "Official announcement")]
    announced = catalyst.get("announcement_time")
    detected = catalyst.get("first_detected_time")
    if isinstance(announced, datetime):
        lines.append(f"Announced: {announced.astimezone(ET).strftime('%I:%M:%S %p ET')}")
    if isinstance(detected, datetime):
        lines.append(f"Detected: {detected.astimezone(ET).strftime('%I:%M:%S %p ET')}")
    if catalyst.get("url"):
        lines.append(f"Official source: {catalyst['url']}")
    return "\n".join(lines)


def why_now(candidate: Candidate) -> str:
    reasons = list(candidate.bottom.reasons if candidate.bottom else [])
    reasons.extend(candidate.score.reasons)
    reasons = list(dict.fromkeys(reasons)) or ["Multiple early market-structure signals are improving while price remains actionable."]
    return " ".join(reasons[:4])


def risks(candidate: Candidate) -> str:
    values = list(candidate.bottom.risks if candidate.bottom else [])
    values.extend(candidate.score.risks)
    return "\n".join(list(dict.fromkeys(values)) or ["No specific risk flags generated."])


def footer(candidate: Candidate, quote_age_seconds: float | None = None) -> str:
    age = quote_age_seconds if quote_age_seconds is not None else candidate.quote.quote_age_seconds
    age_text = "unknown" if age is None else f"{age:.1f} sec"
    now = datetime.now(ET)
    hour = ((now.hour - 1) % 12) + 1
    now_et = f"{now:%b} {now.day}, {now.year} {hour}:{now.minute:02d}:{now.second:02d} {now:%p} ET"
    return f"LIVE COINBASE DATA\nQuote age: {age_text}\n{now_et}"


def human_tag(tag: str) -> str:
    return tag.replace("_", " ").title()


def format_score(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.0f}"


def candidate_to_record(candidate: Candidate) -> dict[str, Any]:
    return {
        "product_id": candidate.product_id,
        "symbol": candidate.symbol,
        "status": candidate_alert_stage(candidate),
        "created_at": datetime.now(timezone.utc),
        "early_move_score": candidate.score.early_move_score,
        "technical_score": candidate.score.technical_score,
        "catalyst_score": candidate.score.catalyst_score,
        "component_scores": candidate.score.components,
        "penalties": candidate.score.penalties,
        "reasons": candidate.score.reasons,
        "risks": candidate.score.risks,
        "metrics": candidate_metrics(candidate),
        "tags": candidate.tags,
    }


def candidate_to_alert_record(candidate: Candidate, alert_type: str, catalyst_id: int | None = None) -> dict[str, Any]:
    return {
        "product_id": candidate.product_id,
        "symbol": candidate.symbol,
        "status": candidate_alert_stage(candidate),
        "alert_type": alert_type,
        "detection_time": datetime.now(timezone.utc),
        "detection_price": candidate.quote.price,
        "score": primary_score(candidate),
        "component_scores": candidate.score.components,
        "reasons": candidate.score.reasons,
        "risks": candidate.score.risks,
        "support_low": candidate.structure.support_low,
        "support_high": candidate.structure.support_high,
        "trigger_price": candidate.structure.breakout_trigger,
        "invalidation_price": candidate.structure.invalidation,
        "tp1": candidate.targets.tp1,
        "tp2": candidate.targets.tp2,
        "stretch": candidate.targets.stretch,
        "catalyst_id": catalyst_id,
        "raw_candidate": candidate_metrics(candidate),
    }


def candidate_metrics(candidate: Candidate) -> dict[str, Any]:
    return {
        "quote_age_seconds": candidate.quote.quote_age_seconds,
        "risk_adjusted_opportunity_score": primary_score(candidate),
        "liquidity_safety_score": candidate.score.components.get("LiquiditySafetyScore"),
        "market_cap_class": market_cap_class(candidate.market_cap),
        "dollar_volume_24h": dollar_volume_24h(candidate.volume),
        "coinbase_traders": None,
        "bottom": bottom_metrics(candidate),
        "fibonacci": fibonacci_metrics(candidate),
        "rotation": rotation_metrics(candidate),
        "volume": {"volume_quote": candidate.volume.volume_quote, "ratios": candidate.volume.ratios},
        "structure": {
            "base_low": candidate.structure.base_low,
            "base_high": candidate.structure.base_high,
            "support_low": candidate.structure.support_low,
            "support_high": candidate.structure.support_high,
            "resistance": candidate.structure.resistance,
            "breakout_trigger": candidate.structure.breakout_trigger,
            "confirmation": candidate.structure.confirmation,
            "invalidation": candidate.structure.invalidation,
            "extension_24h_pct": candidate.structure.extension_24h_pct,
        },
        "liquidity": {
            "spread_pct": candidate.liquidity.spread_pct,
            "bid_depth_usd": candidate.liquidity.bid_depth_usd,
            "ask_depth_usd": candidate.liquidity.ask_depth_usd,
            "book_depth_1_pct": book_depth_usd(candidate.liquidity, "1"),
            "imbalance": candidate.liquidity.imbalance,
        },
        "tags": candidate.tags,
        "market_cap": {
            "circulating_market_cap": candidate.market_cap.circulating_market_cap,
            "fdv": candidate.market_cap.fdv,
            "volume_to_market_cap": candidate.market_cap.volume_to_market_cap,
        },
    }


def bottom_metrics(candidate: Candidate) -> dict[str, Any] | None:
    bottom = candidate.bottom
    if bottom is None:
        return None
    return {
        "stage": bottom.stage.value,
        "pattern": bottom.pattern,
        "bottom_score": bottom.bottom_score,
        "breakout_score": bottom.breakout_score,
        "prior_drawdown_pct": bottom.prior_drawdown_pct,
        "prior_impulse_pct": bottom.prior_impulse_pct,
        "support": bottom.support,
        "resistance": bottom.resistance,
        "invalidation": bottom.invalidation,
        "distance_to_breakout_pct": bottom.distance_to_breakout_pct,
        "atr_compression_ratio": bottom.atr_compression_ratio,
        "bollinger_width_ratio": bottom.bollinger_width_ratio,
        "volume_dryup_ratio": bottom.volume_dryup_ratio,
        "first_expansion_ratio": bottom.first_expansion_ratio,
        "relative_strength_score": bottom.relative_strength_score,
        "macd_state": bottom.macd_state,
        "rsi_state": bottom.rsi_state,
        "relative_strength_state": bottom.relative_strength_state,
        "components": bottom.components,
    }


def fibonacci_metrics(candidate: Candidate) -> dict[str, Any] | None:
    fib = candidate.fib
    if fib is None:
        return None
    return {
        "reliable": fib.reliable,
        "anchor_low": fib.anchor_low,
        "anchor_high": fib.anchor_high,
        "anchor_timestamp_low": fib.anchor_timestamp_low.isoformat() if fib.anchor_timestamp_low else None,
        "anchor_timestamp_high": fib.anchor_timestamp_high.isoformat() if fib.anchor_timestamp_high else None,
        "timeframe": fib.timeframe,
        "swing_confidence": fib.swing_confidence,
        "retracements": fib.retracements,
        "extensions": fib.extensions,
        "current_retracement": fib.current_retracement,
        "nearest_support": fib.nearest_support,
        "nearest_resistance": fib.nearest_resistance,
        "golden_pocket_low": fib.golden_pocket_low,
        "golden_pocket_high": fib.golden_pocket_high,
        "signal": fib.signal,
        "confluence_score": fib.confluence_score,
        "tags": fib.tags,
        "target_probabilities": fib.target_probabilities,
        "probability_confidence": fib.probability_confidence,
    }


def rotation_metrics(candidate: Candidate) -> dict[str, Any] | None:
    rotation = candidate.rotation
    if rotation is None:
        return None
    return {
        "classification": rotation.classification.value,
        "destination_ticker": rotation.destination_ticker,
        "candidate_score": rotation.candidate_score,
        "relative_opportunity_score": rotation.relative_opportunity_score,
        "candidate_risk_reward": rotation.candidate_risk_reward,
        "suggested_percentage": rotation.suggested_percentage,
        "confidence": rotation.confidence,
        "best_source": rotation_source_metrics(rotation.best_source),
        "sources": [rotation_source_metrics(source) for source in rotation.sources],
    }


def rotation_source_metrics(source: Any | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "ticker": source.ticker,
        "classification": source.classification.value,
        "hold_score": source.hold_score,
        "rotation_advantage": source.rotation_advantage,
        "suggested_percentage": source.suggested_percentage,
        "source_price": source.source_price,
        "source_position_value": source.source_position_value,
        "source_risk_reward": source.source_risk_reward,
        "deterioration_score": source.deterioration_score,
        "confidence": source.confidence,
        "protected": source.protected,
        "reason": source.reason,
    }


def rotation_to_record(candidate: Candidate, discord_message_id: str | None = None) -> dict[str, Any] | None:
    rotation = candidate.rotation
    source = rotation.best_source if rotation else None
    if rotation is None or source is None or rotation.classification == RotationClassification.NO_ROTATION:
        return None
    return {
        "created_at": datetime.now(timezone.utc),
        "source_asset": source.ticker,
        "destination_asset": candidate.symbol,
        "classification": rotation.classification.value,
        "suggested_percentage": rotation.suggested_percentage,
        "source_price": source.source_price,
        "destination_price": candidate.quote.price,
        "source_score": source.hold_score,
        "destination_score": rotation.candidate_score,
        "rotation_advantage": source.rotation_advantage,
        "source_risk_reward": source.source_risk_reward,
        "destination_risk_reward": rotation.candidate_risk_reward,
        "fib_state": candidate.fib.signal if candidate.fib and candidate.fib.reliable else None,
        "reason": source.reason,
        "confidence": rotation.confidence,
        "discord_message_id": discord_message_id,
        "raw": rotation_metrics(candidate),
    }
