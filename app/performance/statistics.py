from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median
from typing import Iterable

from app.database.connection import session_scope
from app.database.repositories import AlertRepository
from app.formatting import pct
from app.timeutils import utc_now

HORIZONS = ["1h", "4h", "24h", "7d"]


async def performance_report(days: int = 30) -> str:
    since = utc_now() - timedelta(days=days)
    async with session_scope() as session:
        repo = AlertRepository(session)
        alerts = await repo.alerts_since(since)
        observations = await repo.observations_for_alerts([alert.id for alert in alerts])
    return build_report(alerts, observations, days)


def build_report(alerts: list[object], observations: list[object], days: int) -> str:
    lines = [f"Scanner performance - last {days}d", "", f"Alerts: {len(alerts)}"]
    by_alert = defaultdict(list)
    for observation in observations:
        if observation.observed_at is not None:
            by_alert[observation.alert_id].append(observation)
    lines.extend(summary_lines("All", alerts, observations))
    for status in sorted({str(alert.status) for alert in alerts}):
        subset = [alert for alert in alerts if str(alert.status) == status]
        subset_ids = {alert.id for alert in subset}
        subset_observations = [obs for obs in observations if obs.alert_id in subset_ids]
        lines.extend(summary_lines(status, subset, subset_observations))
    for tag in sorted(extract_tags(alerts)):
        subset = [alert for alert in alerts if tag in alert_tags(alert)]
        subset_ids = {alert.id for alert in subset}
        subset_observations = [obs for obs in observations if obs.alert_id in subset_ids]
        lines.extend(summary_lines(tag, subset, subset_observations))
    return "\n".join(lines)


def summary_lines(label: str, alerts: list[object], observations: list[object]) -> list[str]:
    if not alerts:
        return []
    lines = ["", f"[{label}]", f"alerts: {len(alerts)}"]
    for horizon in HORIZONS:
        values = [obs.return_pct for obs in observations if obs.horizon == horizon and obs.return_pct is not None]
        if values:
            lines.append(f"median return {horizon}: {pct(median(values))}")
    mfe = [obs.mfe_pct for obs in observations if obs.mfe_pct is not None]
    if mfe:
        lines.append(f"median maximum favorable excursion: {pct(median(mfe))}")
    lines.append(f"TP1 hit rate: {rate(observations, 'tp1_hit')}")
    lines.append(f"TP2 hit rate: {rate(observations, 'tp2_hit')}")
    lines.append(f"stretch hit rate: {rate(observations, 'stretch_hit')}")
    lines.append(f"invalidation rate: {rate(observations, 'invalidation_hit')}")
    return lines


def rate(observations: Iterable[object], attr: str) -> str:
    observations = [obs for obs in observations if obs.observed_at is not None]
    if not observations:
        return "n/a"
    hits = sum(1 for obs in observations if bool(getattr(obs, attr)))
    return pct(hits / len(observations) * 100.0, signed=False)


def extract_tags(alerts: list[object]) -> set[str]:
    tags: set[str] = set()
    for alert in alerts:
        tags.update(alert_tags(alert))
    return tags & {"microcap", "dormant_microcap", "volume_acceleration", "compression", "order_book", "catalyst", "upbit", "bithumb", "binance", "kraken", "okx", "bybit"}


def alert_tags(alert: object) -> list[str]:
    raw = getattr(alert, "raw_candidate", None) or {}
    return list(raw.get("tags", []))
