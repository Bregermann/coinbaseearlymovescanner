from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.analysis.scoring import book_depth_usd, dollar_volume_24h, market_cap_class, primary_score
from app.formatting import compact_money, money, pct, ratio
from app.types import Candidate, SetupStatus

try:
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - only used on systems without tzdata
    ET = timezone(timedelta(hours=-5), "ET")

STATUS_COLORS = {
    SetupStatus.EARLY: 0x2ECC71,
    SetupStatus.CONFIRMING: 0xF1C40F,
    SetupStatus.FAST_MOVE: 0xE74C3C,
    SetupStatus.ALREADY_EXTENDED: 0x95A5A6,
}


def candidate_to_payload(candidate: Candidate, alert_type: str = "new_setup", quote_age_seconds: float | None = None) -> dict[str, Any]:
    embed = candidate_to_embed(candidate, alert_type=alert_type, quote_age_seconds=quote_age_seconds)
    return {"username": "Coinbase Early Move Scanner", "embeds": [embed]}


def candidate_to_embed(candidate: Candidate, alert_type: str = "new_setup", quote_age_seconds: float | None = None) -> dict[str, Any]:
    symbol = candidate.symbol
    title_prefix = "⚡" if alert_type in {"breakout_followup", "status_transition"} else "🚨"
    if alert_type == "invalidation":
        title_prefix = "❌"
    title = f"{title_prefix} {symbol} — {candidate.status.value}"
    subtitle = " + ".join(human_tag(tag) for tag in candidate.tags[:3]) or "Chart/Liquidity"
    fields = [
        {"name": "Snapshot", "value": snapshot_field(candidate), "inline": False},
        {"name": "Liquidity Safety", "value": liquidity_safety_field(candidate), "inline": True},
        {"name": "Volume Acceleration", "value": volume_field(candidate), "inline": True},
        {"name": "Structure", "value": structure_field(candidate), "inline": True},
        {"name": "Targets", "value": targets_field(candidate), "inline": True},
        {"name": "Catalyst", "value": catalyst_field(candidate), "inline": False},
        {"name": "WHY NOW", "value": why_now(candidate), "inline": False},
        {"name": "RISKS", "value": risks(candidate), "inline": False},
    ]
    embed = {
        "title": title,
        "description": subtitle,
        "color": STATUS_COLORS.get(candidate.status, 0x3498DB),
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
            f"Volume / Market Cap: {pct(cap.volume_to_market_cap, signed=False)}",
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
            f"MARKET CAP CLASS: {market_cap_class(candidate.market_cap)}",
            f"24H DOLLAR VOLUME: {compact_money(dollar_volume_24h(candidate.volume))}",
            f"SPREAD: {pct(liquidity.spread_pct, signed=False)}",
            f"BOOK DEPTH +/-1%: {compact_money(book_depth_usd(liquidity, '1'))}",
            f"COINBASE TRADERS: {trader_text}",
            f"RISK-ADJUSTED OPPORTUNITY: {primary_score(candidate):.0f} / 100",
        ]
    )


def volume_field(candidate: Candidate) -> str:
    r = candidate.volume.ratios
    return "\n".join(
        [
            f"5m: {ratio(r.get('5m_vs_baseline'))}",
            f"15m: {ratio(r.get('15m_vs_baseline'))}",
            f"1h: {ratio(r.get('1h_vs_7d'))}",
            f"4h: {ratio(r.get('4h_vs_7d'))}",
            f"24h vs 7d: {ratio(r.get('24h_vs_7d'))}",
            f"24h vs 30d: {ratio(r.get('24h_vs_30d'))}",
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
    reasons = candidate.score.reasons or ["Multiple early market-structure signals are improving while price remains actionable."]
    return " ".join(reasons[:4])


def risks(candidate: Candidate) -> str:
    return "\n".join(candidate.score.risks or ["No specific risk flags generated."])


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
        "status": candidate.status.value,
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
        "status": candidate.status.value,
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
