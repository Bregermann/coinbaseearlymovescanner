from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median, pstdev

from app.formatting import clamp
from app.types import BottomMetrics, BottomStage, CandlePoint, Candidate, LiquidityMetrics

LOOKBACKS = {
    900: timedelta(days=2),
    3600: timedelta(days=45),
    14400: timedelta(days=120),
    86400: timedelta(days=365),
}


def analyze_bottom_structure(
    candles: list[CandlePoint],
    current_price: float,
    now: datetime | None = None,
    *,
    benchmark_candles: dict[str, list[CandlePoint]] | None = None,
    liquidity: LiquidityMetrics | None = None,
) -> BottomMetrics:
    candles = sorted(candles, key=lambda item: item.start)
    now = now or (
        candles[-1].start + timedelta(seconds=candles[-1].granularity_seconds)
        if candles else datetime.now(timezone.utc)
    )
    if not candles or current_price <= 0:
        return empty_bottom_metrics()

    frames = {
        seconds: resample_candles(candles, seconds, now, lookback)
        for seconds, lookback in LOOKBACKS.items()
    }
    bars = frames[900] if len(frames[900]) >= 40 else frames[3600]
    if len(bars) < 20:
        return empty_bottom_metrics()

    drawdown, impulse = prior_move_context(frames[3600] or bars, current_price)
    support, touches = detect_support_zone(bars)
    channel = fit_channel(bars)
    resistance = choose_resistance(
        current_price,
        channel.get("upper_now"),
        horizontal_resistance(bars, current_price),
        indicator_resistance(bars, current_price),
    )
    distance = (
        (resistance - current_price) / current_price * 100.0
        if resistance is not None else None
    )
    atr_ratio = atr_compression_ratio(bars)
    bb_ratio = bollinger_compression_ratio(bars)
    dryup, expansion, volume_ratios = volume_setup(
        frames[900], frames[3600], frames[14400]
    )
    rsi = rsi_series([bar.close for bar in bars])
    macd_line, macd_signal, macd_hist = macd_series([bar.close for bar in bars])
    obv = obv_series(bars)
    failed_breakdown = has_failed_breakdown(bars, support)
    double_bottom = has_double_bottom(bars)
    rounded_bottom = has_rounded_bottom(bars)
    higher_low = has_higher_low_base(bars)
    post_capitulation = has_post_capitulation(bars, atr_ratio)
    breakout_retest = has_breakout_retest(bars, resistance)

    drawdown_score = score_drawdown(drawdown, impulse)
    downside_score = score_downside_loss(bars, atr_ratio)
    support_score = score_support(
        bars, support, touches, higher_low, failed_breakdown, double_bottom
    )
    compression_score = score_compression(atr_ratio, bb_ratio, channel)
    momentum_score, macd_state, rsi_state = score_momentum(
        bars, rsi, macd_line, macd_signal, macd_hist
    )
    accumulation_score = score_accumulation(bars, obv, liquidity)
    proximity_score = score_proximity(distance)
    relative_score, relative_state = score_relative_strength(
        candles, benchmark_candles or {}, now
    )
    timeframe_score = score_timeframe_context(
        frames[3600], frames[14400], frames[86400], current_price
    )
    volume_score = score_volume_setup(dryup, expansion, volume_ratios)
    pattern_score, pattern = score_patterns(
        channel=channel,
        impulse=impulse,
        double_bottom=double_bottom,
        rounded_bottom=rounded_bottom,
        higher_low=higher_low,
        post_capitulation=post_capitulation,
        breakout_retest=breakout_retest,
        failed_breakdown=failed_breakdown,
        compression_score=compression_score,
    )
    components = {
        "DrawdownScore": drawdown_score,
        "DownsideMomentumLossScore": downside_score,
        "SupportBehaviorScore": support_score,
        "VolatilityCompressionScore": compression_score,
        "MomentumReversalScore": momentum_score,
        "AccumulationScore": accumulation_score,
        "ResistanceProximityScore": proximity_score,
        "RelativeStrengthScore": relative_score,
        "VolumeDryupExpansionScore": volume_score,
        "PatternQualityScore": pattern_score,
        "MultiTimeframeContextScore": timeframe_score,
        **volume_ratios,
    }
    bottom_score = clamp(
        drawdown_score * 0.13
        + downside_score * 0.11
        + support_score * 0.13
        + compression_score * 0.12
        + momentum_score * 0.12
        + accumulation_score * 0.08
        + proximity_score * 0.08
        + relative_score * 0.06
        + volume_score * 0.07
        + pattern_score * 0.05
        + timeframe_score * 0.05
    )
    breakout_score = score_breakout(
        bars, current_price, resistance, expansion, volume_ratios, rsi,
        macd_line, macd_signal, macd_hist, obv, relative_score, liquidity
    )
    stage = classify_stage(
        bars, current_price, resistance, distance, bottom_score, breakout_score,
        breakout_retest, failed_breakdown, support_score, volume_score
    )
    invalidation = calculate_invalidation(bars, support)
    return BottomMetrics(
        stage=stage,
        pattern=pattern,
        bottom_score=round(bottom_score, 2),
        breakout_score=round(breakout_score, 2),
        prior_drawdown_pct=rounded(drawdown),
        prior_impulse_pct=rounded(impulse),
        support=rounded(support, 12),
        resistance=rounded(resistance, 12),
        invalidation=rounded(invalidation, 12),
        distance_to_breakout_pct=rounded(distance),
        atr_compression_ratio=rounded(atr_ratio, 3),
        bollinger_width_ratio=rounded(bb_ratio, 3),
        volume_dryup_ratio=rounded(dryup, 3),
        first_expansion_ratio=rounded(expansion, 3),
        relative_strength_score=round(relative_score, 2),
        macd_state=macd_state,
        rsi_state=rsi_state,
        bollinger_state="Compressed" if bb_ratio is not None and bb_ratio <= 0.72 else "Normal",
        relative_strength_state=relative_state,
        components={key: round(value, 2) for key, value in components.items()},
        reasons=build_reasons(
            drawdown, touches, atr_ratio, bb_ratio, distance, dryup, expansion,
            macd_state, rsi_state, relative_state, pattern, failed_breakdown
        ),
        risks=build_risks(current_price, support, resistance, drawdown, expansion),
    )


def empty_bottom_metrics() -> BottomMetrics:
    return BottomMetrics(
        stage=BottomStage.NONE, pattern="None", bottom_score=0.0,
        breakout_score=0.0, prior_drawdown_pct=None, prior_impulse_pct=None,
        support=None, resistance=None, invalidation=None,
        distance_to_breakout_pct=None, atr_compression_ratio=None,
        bollinger_width_ratio=None, volume_dryup_ratio=None,
        first_expansion_ratio=None, relative_strength_score=50.0,
        macd_state="N/A", rsi_state="N/A", bollinger_state="N/A",
        relative_strength_state="N/A",
    )


def resample_candles(
    candles: list[CandlePoint],
    interval: int,
    now: datetime,
    lookback: timedelta,
) -> list[CandlePoint]:
    eligible = [
        candle for candle in candles
        if now - lookback <= candle.start < now
        and candle.start + timedelta(seconds=candle.granularity_seconds) <= now
        and candle.granularity_seconds <= interval
        and interval % candle.granularity_seconds == 0
    ]
    if not eligible:
        return []
    available = {candle.granularity_seconds for candle in eligible}
    preferred = (
        [interval, 60] if interval <= 300 else
        [900, 300, 60] if interval == 900 else
        [3600, 900, 300, 60] if interval == 3600 else
        [3600, 1800, 900, 300, 60]
    )
    source_seconds = next((value for value in preferred if value in available), max(available))
    buckets: dict[int, list[CandlePoint]] = {}
    for candle in eligible:
        if candle.granularity_seconds != source_seconds:
            continue
        bucket = int(candle.start.timestamp()) // interval * interval
        buckets.setdefault(bucket, []).append(candle)
    result: list[CandlePoint] = []
    for bucket, rows in sorted(buckets.items()):
        bucket_start = datetime.fromtimestamp(bucket, tz=timezone.utc)
        if bucket_start + timedelta(seconds=interval) > now:
            continue
        rows.sort(key=lambda item: item.start)
        result.append(CandlePoint(
            product_id=rows[0].product_id,
            start=bucket_start,
            granularity_seconds=interval,
            open=rows[0].open,
            high=max(row.high for row in rows),
            low=min(row.low for row in rows),
            close=rows[-1].close,
            volume=sum(row.volume for row in rows),
            quote_volume=sum(row.quote_volume for row in rows),
        ))
    return result


def prior_move_context(bars: list[CandlePoint], price: float) -> tuple[float | None, float | None]:
    if len(bars) < 12:
        return None, None
    search = bars[:-4] if len(bars) > 16 else bars
    high_index = max(range(len(search)), key=lambda index: search[index].high)
    high = search[high_index].high
    prior = search[max(0, high_index - 24 * 14): high_index + 1]
    low = min((bar.low for bar in prior), default=high)
    drawdown = (price - high) / high * 100.0 if high > 0 else None
    impulse = (high - low) / low * 100.0 if low > 0 else None
    return drawdown, impulse


def find_swings(
    bars: list[CandlePoint], *, highs: bool, radius: int = 2
) -> list[tuple[int, float]]:
    values = [bar.high if highs else bar.low for bar in bars]
    result: list[tuple[int, float]] = []
    for index in range(radius, len(values) - radius):
        window = values[index - radius:index + radius + 1]
        if (highs and values[index] == max(window)) or (
            not highs and values[index] == min(window)
        ):
            result.append((index, values[index]))
    return result


def detect_support_zone(bars: list[CandlePoint]) -> tuple[float | None, int]:
    recent = bars[-96:]
    candidates = [value for _, value in find_swings(recent, highs=False)]
    candidates = candidates or [bar.low for bar in recent]
    if not candidates:
        return None, 0
    cutoff = percentile(candidates, 0.40)
    support = median([value for value in candidates if value <= cutoff] or candidates)
    tolerance = max(0.004, median_true_range_pct(recent) * 0.0045)
    touches = sum(
        1 for bar in recent
        if bar.low <= support * (1 + tolerance)
        and bar.close >= support * (1 - tolerance)
    )
    return support, touches


def fit_channel(bars: list[CandlePoint]) -> dict[str, float]:
    recent = bars[-160:]
    high_fit = linear_fit(find_swings(recent, highs=True)[-8:])
    low_fit = linear_fit(find_swings(recent, highs=False)[-8:])
    if high_fit is None or low_fit is None:
        return {}
    high_slope, high_intercept, high_r2 = high_fit
    low_slope, low_intercept, low_r2 = low_fit
    x_now = len(recent) - 1
    upper = high_intercept + high_slope * x_now
    lower = low_intercept + low_slope * x_now
    x_start = max(0, x_now - 40)
    width_start = (
        high_intercept + high_slope * x_start
        - low_intercept - low_slope * x_start
    )
    narrowing = clamp((1 - (upper - lower) / width_start) * 100) if width_start > 0 else 0.0
    scale = median(bar.close for bar in recent) or 1.0
    return {
        "high_slope_pct": high_slope / scale * 100,
        "low_slope_pct": low_slope / scale * 100,
        "upper_now": upper,
        "lower_now": lower,
        "high_r2": high_r2,
        "low_r2": low_r2,
        "narrowing_score": narrowing,
    }


def horizontal_resistance(bars: list[CandlePoint], price: float) -> float | None:
    swings = [value for _, value in find_swings(bars[-192:], highs=True) if value > price * 1.002]
    if not swings:
        values = sorted(bar.high for bar in bars[-192:] if bar.high > price * 1.002)
        return percentile(values, 0.35) if values else None
    clusters: list[list[float]] = []
    for value in sorted(swings):
        cluster = next(
            (row for row in clusters if abs(value - median(row)) / median(row) <= 0.012),
            None,
        )
        if cluster is None:
            clusters.append([value])
        else:
            cluster.append(value)
    repeated = [median(row) for row in clusters if len(row) >= 2]
    actionable = [value for value in repeated if value <= price * 1.15]
    return min(actionable or repeated or swings)


def indicator_resistance(bars: list[CandlePoint], price: float) -> float | None:
    closes = [bar.close for bar in bars]
    candidates = [
        values[-1] for period in (20, 50)
        if (values := ema_series(closes, period)) and values[-1] > price * 1.001
    ]
    recent = bars[-96:]
    volume = sum(bar.quote_volume for bar in recent)
    if volume > 0:
        vwap = sum(
            ((bar.high + bar.low + bar.close) / 3) * bar.quote_volume
            for bar in recent
        ) / volume
        if vwap > price * 1.001:
            candidates.append(vwap)
    return min(candidates) if candidates else None


def choose_resistance(price: float, *levels: float | None) -> float | None:
    valid = [
        level for level in levels
        if level is not None and price * 0.985 <= level <= price * 1.18
    ]
    above = [level for level in valid if level > price * 1.001]
    return min(above) if above else max(valid) if valid else None


def atr_compression_ratio(bars: list[CandlePoint]) -> float | None:
    ranges = true_ranges(bars)
    if len(ranges) < 24:
        return None
    prior = median(ranges[-32:-8])
    return median(ranges[-8:]) / prior if prior > 0 else None


def bollinger_compression_ratio(bars: list[CandlePoint]) -> float | None:
    closes = [bar.close for bar in bars]
    if len(closes) < 60:
        return None
    widths = []
    for index in range(19, len(closes)):
        window = closes[index - 19:index + 1]
        center = sum(window) / len(window)
        widths.append(4 * pstdev(window) / center if center > 0 else 0.0)
    prior = median(widths[-43:-3]) if len(widths) >= 43 else median(widths[:-3])
    return median(widths[-3:]) / prior if prior > 0 else None


def volume_setup(
    bars_15m: list[CandlePoint],
    bars_1h: list[CandlePoint],
    bars_4h: list[CandlePoint],
) -> tuple[float | None, float | None, dict[str, float]]:
    ratio_15m, dryup = latest_vs_prior(bars_15m, 20, dryup_window=4)
    ratio_1h, _ = latest_vs_prior(bars_1h, 24)
    ratio_4h, _ = latest_vs_prior(bars_4h, 42)
    available = [value for value in (ratio_15m, ratio_1h, ratio_4h) if value is not None]
    ratios = {
        "15mVolumeVsPrior20": ratio_15m or 0.0,
        "1hVolumeVsPrior24": ratio_1h or 0.0,
        "4hVolumeVsPrior42": ratio_4h or 0.0,
    }
    return dryup, max(available) if available else None, ratios


def latest_vs_prior(
    bars: list[CandlePoint], count: int, *, dryup_window: int = 0
) -> tuple[float | None, float | None]:
    if len(bars) < max(8, count // 2):
        return None, None
    prior = [bar.quote_volume for bar in bars[-count - 1:-1] if bar.quote_volume > 0]
    baseline = median(prior) if prior else 0.0
    ratio = bars[-1].quote_volume / baseline if baseline > 0 else None
    dryup = None
    if dryup_window and len(prior) >= dryup_window * 2:
        older = median(prior[:-dryup_window])
        dryup = median(prior[-dryup_window:]) / older if older > 0 else None
    return ratio, dryup


def has_failed_breakdown(bars: list[CandlePoint], support: float | None) -> bool:
    if support is None:
        return False
    return any(
        bar.low < support * 0.992 and bar.close > support * 1.001
        for bar in bars[-10:]
    )


def has_double_bottom(bars: list[CandlePoint]) -> bool:
    recent = bars[-160:]
    lows = find_swings(recent, highs=False)
    for left, right in zip(lows[-5:-1], lows[-4:]):
        if right[0] - left[0] < 4:
            continue
        if abs(right[1] - left[1]) / max(left[1], right[1]) > 0.025:
            continue
        rebound = max(bar.high for bar in recent[left[0]:right[0] + 1])
        if rebound >= min(left[1], right[1]) * 1.025:
            return True
    return False


def has_rounded_bottom(bars: list[CandlePoint]) -> bool:
    values = [bar.close for bar in bars[-80:]]
    if len(values) < 30:
        return False
    centers = [median(part) for part in split_values(values, 5)]
    return (
        centers[0] > centers[2]
        and centers[4] > centers[2]
        and centers[4] > centers[3] > centers[2]
    )


def has_higher_low_base(bars: list[CandlePoint]) -> bool:
    lows = [value for _, value in find_swings(bars[-120:], highs=False)]
    if len(lows) < 3:
        return False
    latest = lows[-3:]
    return latest[2] > latest[1] > latest[0] or latest[2] > latest[0] * 1.005


def has_post_capitulation(
    bars: list[CandlePoint], atr_ratio: float | None
) -> bool:
    recent = bars[-192:]
    if len(recent) < 30:
        return False
    volumes = [bar.quote_volume for bar in recent if bar.quote_volume > 0]
    ranges = true_ranges(recent)
    volume_baseline = median(volumes) if volumes else 0.0
    range_baseline = median(ranges) if ranges else 0.0
    for bar in recent[:-8]:
        decline = (bar.close - bar.open) / bar.open * 100 if bar.open > 0 else 0.0
        if (
            decline <= -3.5
            and bar.high - bar.low >= range_baseline * 1.8
            and bar.quote_volume >= volume_baseline * 1.8
        ):
            return atr_ratio is None or atr_ratio <= 0.9
    return False


def has_breakout_retest(
    bars: list[CandlePoint], resistance: float | None
) -> bool:
    if resistance is None or len(bars) < 8:
        return False
    recent = bars[-10:]
    for index, bar in enumerate(recent[:-2]):
        if bar.close <= resistance * 1.002:
            continue
        retest = recent[index + 1:]
        return (
            any(
                item.low <= resistance * 1.015 and item.close >= resistance * 0.997
                for item in retest
            )
            and recent[-1].close >= resistance
        )
    return False


def score_drawdown(drawdown: float | None, impulse: float | None) -> float:
    if drawdown is None:
        return 0.0
    decline = abs(min(0.0, drawdown))
    if 15 <= decline <= 50:
        score = 82 + max(0.0, 18 - abs(decline - 30) * 0.7)
    elif 8 <= decline < 15:
        score = 35 + (decline - 8) * 6
    elif 50 < decline <= 70:
        score = 78 - (decline - 50) * 1.8
    else:
        score = 10.0
    if impulse is not None and impulse >= 20:
        score += min(10.0, (impulse - 20) * 0.2)
    return clamp(score)


def score_downside_loss(
    bars: list[CandlePoint], atr_ratio: float | None
) -> float:
    if len(bars) < 24:
        return 0.0
    recent, prior = bars[-8:], bars[-32:-8]
    recent_red = [abs(bar.close - bar.open) for bar in recent if bar.close < bar.open]
    prior_red = [abs(bar.close - bar.open) for bar in prior if bar.close < bar.open]
    body_ratio = (
        median(recent_red) / median(prior_red)
        if recent_red and prior_red and median(prior_red) > 0 else 0.8
    )
    recent_sell = sum(bar.quote_volume for bar in recent if bar.close < bar.open) / len(recent)
    prior_sell = sum(bar.quote_volume for bar in prior if bar.close < bar.open) / len(prior)
    sell_ratio = recent_sell / prior_sell if prior_sell > 0 else 0.8
    return clamp(
        contraction_score(atr_ratio) * 0.45
        + contraction_score(body_ratio) * 0.30
        + contraction_score(sell_ratio) * 0.25
    )


def score_support(
    bars: list[CandlePoint],
    support: float | None,
    touches: int,
    higher_low: bool,
    failed_breakdown: bool,
    double_bottom: bool,
) -> float:
    wick_ratios = []
    for bar in bars[-48:]:
        size = bar.high - bar.low
        if size > 0:
            wick_ratios.append((min(bar.open, bar.close) - bar.low) / size)
    wick = median(wick_ratios) if wick_ratios else 0.0
    score = min(55.0, touches * 8.0) + clamp(wick * 100) * 0.20
    score += 16 if higher_low else 0
    score += 15 if failed_breakdown else 0
    score += 12 if double_bottom else 0
    return clamp(score if support is not None else score * 0.5)


def score_compression(
    atr_ratio: float | None,
    bb_ratio: float | None,
    channel: dict[str, float],
) -> float:
    return clamp(
        contraction_score(atr_ratio) * 0.42
        + contraction_score(bb_ratio) * 0.42
        + channel.get("narrowing_score", 0.0) * 0.16
    )


def score_momentum(
    bars: list[CandlePoint],
    rsi: list[float | None],
    macd_line: list[float],
    macd_signal: list[float],
    macd_hist: list[float],
) -> tuple[float, str, str]:
    hist_curl = len(macd_hist) >= 4 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]
    line_curl = len(macd_line) >= 3 and macd_line[-1] > macd_line[-2] > macd_line[-3]
    cross = (
        len(macd_line) >= 2
        and macd_line[-2] <= macd_signal[-2]
        and macd_line[-1] > macd_signal[-1]
    )
    divergence = bullish_rsi_divergence(bars, rsi)
    latest = next((value for value in reversed(rsi) if value is not None), None)
    prior = next((value for value in reversed(rsi[:-3]) if value is not None), None)
    score = (
        (35 if hist_curl else 0)
        + (22 if line_curl else 0)
        + (28 if cross else 0)
        + (30 if divergence else 18 if latest is not None and prior is not None and latest > prior and latest < 72 else 0)
    )
    macd_state = "Bullish cross" if cross else "Bullish curl" if hist_curl or line_curl else "Not confirmed"
    rsi_state = (
        "Bullish divergence" if divergence else
        f"Rising ({latest:.0f})" if latest is not None and prior is not None and latest > prior else
        f"{latest:.0f}" if latest is not None else "N/A"
    )
    return clamp(score), macd_state, rsi_state


def score_accumulation(
    bars: list[CandlePoint],
    obv: list[float],
    liquidity: LiquidityMetrics | None,
) -> float:
    if len(bars) < 20 or len(obv) < 20:
        return 0.0
    price_slope = normalized_slope([bar.close for bar in bars[-20:]])
    obv_slope = normalized_slope(obv[-20:])
    score = 50.0
    if obv_slope > 0 and price_slope <= 0.05:
        score += 30
    elif obv_slope > 0:
        score += 15
    delta_proxy = sum(
        bar.quote_volume if bar.close >= bar.open else -bar.quote_volume
        for bar in bars[-20:]
    )
    score += 12 if delta_proxy > 0 else 0
    if liquidity and liquidity.imbalance is not None:
        score += clamp(liquidity.imbalance * 40, -15, 15)
    return clamp(score)


def score_proximity(distance: float | None) -> float:
    if distance is None:
        return 15.0
    if -1.5 <= distance <= 1:
        return 100.0
    if distance <= 5:
        return 100 - (distance - 1) * 5
    if distance <= 8:
        return 80 - (distance - 5) * 12
    if distance <= 12:
        return 35 - (distance - 8) * 5
    return 10.0


def score_relative_strength(
    candles: list[CandlePoint],
    benchmarks: dict[str, list[CandlePoint]],
    now: datetime,
) -> tuple[float, str]:
    coin_return = return_over(candles, now, timedelta(hours=4))
    market_returns = [
        value for rows in benchmarks.values()
        if (value := return_over(rows, now, timedelta(hours=4))) is not None
    ]
    if coin_return is None or not market_returns:
        return 50.0, "N/A"
    excess = coin_return - sum(market_returns) / len(market_returns)
    score = clamp(50 + excess * 6)
    state = (
        "Improving vs BTC/ETH" if excess >= 1 else
        "Holding up vs BTC/ETH" if excess >= 0 else
        "Lagging BTC/ETH"
    )
    return score, state


def score_timeframe_context(
    bars_1h: list[CandlePoint],
    bars_4h: list[CandlePoint],
    bars_1d: list[CandlePoint],
    price: float,
) -> float:
    score = 35.0
    for bars, weight in ((bars_1h, 15.0), (bars_4h, 20.0)):
        if len(bars) < 4:
            continue
        recent_low = min(bar.low for bar in bars[-3:])
        prior_low = min(bar.low for bar in bars[-6:-3]) if len(bars) >= 6 else recent_low
        if recent_low >= prior_low * 0.995:
            score += weight
    if len(bars_1d) >= 5:
        high = max(bar.high for bar in bars_1d[-30:])
        drawdown = (price - high) / high * 100 if high > 0 else 0.0
        if -50 <= drawdown <= -12:
            score += 20.0
        elif drawdown > -5:
            score += 5.0
    return clamp(score)


def score_volume_setup(
    dryup: float | None,
    expansion: float | None,
    ratios: dict[str, float],
) -> float:
    score = 35.0
    if dryup is not None:
        score += 35 if dryup <= 0.55 else 24 if dryup <= 0.80 else 10 if dryup <= 1 else 0
    if expansion is not None:
        score += min(35.0, (expansion - 1) * 20) if 1.2 <= expansion < 3.5 else 28 if expansion >= 3.5 else 0
    score += 8 if ratios.get("15mVolumeVsPrior20", 0) >= 1.15 else 0
    return clamp(score)


def score_patterns(
    *,
    channel: dict[str, float],
    impulse: float | None,
    double_bottom: bool,
    rounded_bottom: bool,
    higher_low: bool,
    post_capitulation: bool,
    breakout_retest: bool,
    failed_breakdown: bool,
    compression_score: float,
) -> tuple[float, str]:
    high_slope = channel.get("high_slope_pct", 0.0)
    low_slope = channel.get("low_slope_pct", 0.0)
    descending = high_slope < -0.01 and low_slope < 0.01
    parallel = descending and abs(high_slope - low_slope) <= max(
        abs(high_slope), abs(low_slope), 0.01
    ) * 0.65
    wedge = (
        descending and high_slope < low_slope * 1.15
        and channel.get("narrowing_score", 0) >= 20
    )
    found: list[tuple[str, float]] = []
    if parallel and impulse is not None and impulse >= 15:
        found.append(("Descending Channel / Bull Flag", 90))
    elif descending:
        found.append(("Descending Channel", 72))
    if wedge:
        found.append(("Falling Wedge", 88))
    for enabled, label, score in (
        (double_bottom, "Double Bottom", 82),
        (rounded_bottom, "Rounded Bottom", 78),
        (higher_low, "Higher-Low Base", 82),
        (post_capitulation, "Post-Capitulation Base", 78),
        (breakout_retest, "Breakout / Retest", 92),
        (failed_breakdown, "Failed Breakdown Reclaim", 92),
    ):
        if enabled:
            found.append((label, score))
    if not found and compression_score >= 65:
        found.append(("Volatility Compression Near Support", 68))
    if not found:
        return 25.0, "Developing Base"
    found.sort(key=lambda item: item[1], reverse=True)
    return min(100.0, found[0][1] + min(8, (len(found) - 1) * 2)), " / ".join(
        label for label, _ in found[:2]
    )


def score_breakout(
    bars: list[CandlePoint],
    price: float,
    resistance: float | None,
    expansion: float | None,
    ratios: dict[str, float],
    rsi: list[float | None],
    macd_line: list[float],
    macd_signal: list[float],
    macd_hist: list[float],
    obv: list[float],
    relative_score: float,
    liquidity: LiquidityMetrics | None,
) -> float:
    clearance = 0.0
    if resistance and resistance > 0:
        above = (price - resistance) / resistance * 100
        clearance = 100 if 0.1 <= above <= 5 else 70 if above > 0 else score_proximity(-above) * 0.65
    volume_score = clamp(((expansion or 0) - 0.8) * 48)
    if ratios.get("15mVolumeVsPrior20", 0) >= 1.2:
        volume_score = max(volume_score, 58)
    macd_score = 0.0
    if len(macd_hist) >= 3 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]:
        macd_score += 55
    if len(macd_line) >= 2 and macd_line[-2] <= macd_signal[-2] and macd_line[-1] > macd_signal[-1]:
        macd_score += 40
    latest_rsi = next((value for value in reversed(rsi) if value is not None), None)
    rsi_score = 80 if latest_rsi is not None and 48 <= latest_rsi <= 70 else 35 if latest_rsi is not None and latest_rsi < 78 else 0
    obv_score = 0.0
    if len(obv) >= 20:
        obv_score = 100 if obv[-1] > max(obv[-20:-1]) else 65 if normalized_slope(obv[-10:]) > 0 else 20
    closes = [bar.close for bar in bars]
    ema20, ema50 = ema_series(closes, 20), ema_series(closes, 50)
    ema_score = (50 if ema20 and price >= ema20[-1] else 0) + (50 if ema50 and price >= ema50[-1] else 0)
    recent = bars[-96:]
    total_volume = sum(bar.quote_volume for bar in recent)
    vwap = sum(
        ((bar.high + bar.low + bar.close) / 3) * bar.quote_volume
        for bar in recent
    ) / total_volume if total_volume > 0 else None
    return clamp(
        clearance * 0.22 + volume_score * 0.22 + clamp(macd_score) * 0.15
        + rsi_score * 0.08 + obv_score * 0.10
        + (80 if vwap is not None and price >= vwap else 25) * 0.08
        + ema_score * 0.08
        + (liquidity.liquidity_vacuum_score if liquidity else 40) * 0.04
        + relative_score * 0.03
    )


def classify_stage(
    bars: list[CandlePoint],
    price: float,
    resistance: float | None,
    distance: float | None,
    bottom_score: float,
    breakout_score: float,
    breakout_retest: bool,
    failed_breakdown: bool,
    support_score: float,
    volume_score: float,
) -> BottomStage:
    if breakout_retest and bottom_score >= 62 and breakout_score >= 55:
        return BottomStage.RETEST_HOLD
    breakout = resistance is not None and price >= resistance * 1.0005
    close_break = bool(
        bars and resistance is not None and bars[-1].close >= resistance * 0.999
    )
    if breakout and close_break and breakout_score >= 58:
        return BottomStage.BREAKOUT_FIRING
    if (
        bottom_score >= 68 and distance is not None and -0.5 <= distance <= 8
        and (breakout_score >= 38 or volume_score >= 58)
    ):
        return BottomStage.PRE_BREAKOUT
    if bottom_score >= 72 or (
        bottom_score >= 66 and failed_breakdown and support_score >= 65
    ):
        return BottomStage.BOTTOM_FORMING
    return BottomStage.NONE


def calculate_invalidation(
    bars: list[CandlePoint], support: float | None
) -> float | None:
    if support is None:
        return None
    atr = median(true_ranges(bars[-20:])) if len(bars) >= 3 else 0.0
    return max(0.0, support - max(atr * 0.45, support * 0.012))


def build_reasons(
    drawdown: float | None,
    touches: int,
    atr_ratio: float | None,
    bb_ratio: float | None,
    distance: float | None,
    dryup: float | None,
    expansion: float | None,
    macd_state: str,
    rsi_state: str,
    relative_state: str,
    pattern: str,
    failed_breakdown: bool,
) -> list[str]:
    reasons: list[str] = []
    if drawdown is not None and drawdown <= -12:
        reasons.append(f"{abs(drawdown):.0f}% retracement from the recent local high reset extension.")
    if touches >= 3:
        reasons.append(f"Support was defended {touches} times in the active base.")
    if atr_ratio is not None and atr_ratio <= 0.8:
        reasons.append("ATR is contracting as downside momentum fades.")
    if bb_ratio is not None and bb_ratio <= 0.75:
        reasons.append("Bollinger width is compressed versus its recent baseline.")
    if distance is not None and -0.5 <= distance <= 8:
        reasons.append(f"Price is {max(0.0, distance):.1f}% below the detected breakout boundary.")
    if dryup is not None and dryup <= 0.8 and expansion is not None and expansion >= 1.15:
        reasons.append(f"Volume dried up, then began its first {expansion:.1f}x expansion.")
    if macd_state in {"Bullish curl", "Bullish cross"}:
        reasons.append(f"MACD shows a {macd_state.lower()} before full price expansion.")
    if "Bullish" in rsi_state:
        reasons.append("RSI is showing bullish divergence.")
    if relative_state.startswith("Improving"):
        reasons.append("Relative strength is improving versus BTC/ETH.")
    if failed_breakdown:
        reasons.append("A failed support breakdown was reclaimed.")
    return (reasons or [f"{pattern} structure is developing near the detected base."])[:6]


def build_risks(
    price: float,
    support: float | None,
    resistance: float | None,
    drawdown: float | None,
    expansion: float | None,
) -> list[str]:
    risks = []
    if support is None:
        risks.append("Support confidence is limited by sparse candle history.")
    elif price < support:
        risks.append("Price is below the detected support zone.")
    if resistance is None:
        risks.append("No reliable breakout boundary is available yet.")
    if drawdown is not None and drawdown <= -55:
        risks.append("The decline remains deep enough to indicate trend damage.")
    if expansion is None or expansion < 1:
        risks.append("The first volume expansion is not confirmed yet.")
    return risks[:4]


def contraction_score(value: float | None) -> float:
    if value is None:
        return 45.0
    if value <= 0.50:
        return 100.0
    if value <= 0.75:
        return 90 - (value - 0.50) * 40
    if value <= 1:
        return 80 - (value - 0.75) * 120
    return clamp(50 - (value - 1) * 80)


def bullish_rsi_divergence(
    bars: list[CandlePoint], values: list[float | None]
) -> bool:
    window_size = min(120, len(bars))
    lows = find_swings(bars[-window_size:], highs=False)
    if len(lows) < 2:
        return False
    left, right = lows[-2], lows[-1]
    offset = len(bars) - window_size
    left_rsi = values[left[0] + offset]
    right_rsi = values[right[0] + offset]
    return (
        left_rsi is not None and right_rsi is not None
        and right[1] <= left[1] * 1.008
        and right_rsi >= left_rsi + 2
    )


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [
        max(0.0, values[index] - values[index - 1])
        for index in range(1, len(values))
    ]
    losses = [
        max(0.0, values[index - 1] - values[index])
        for index in range(1, len(values))
    ]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    result[period] = rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        average_gain = (
            average_gain * (period - 1) + gains[index - 1]
        ) / period
        average_loss = (
            average_loss * (period - 1) + losses[index - 1]
        ) / period
        result[index] = rsi_value(average_gain, average_loss)
    return result


def rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss <= 0:
        return 100.0
    relative = average_gain / average_loss
    return 100 - 100 / (1 + relative)


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def macd_series(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    fast, slow = ema_series(values, 12), ema_series(values, 26)
    line = [left - right for left, right in zip(fast, slow)]
    signal = ema_series(line, 9)
    return line, signal, [
        left - right for left, right in zip(line, signal)
    ]


def obv_series(bars: list[CandlePoint]) -> list[float]:
    if not bars:
        return []
    result = [0.0]
    for previous, current in zip(bars, bars[1:]):
        direction = (
            1 if current.close > previous.close
            else -1 if current.close < previous.close else 0
        )
        result.append(result[-1] + direction * current.quote_volume)
    return result


def true_ranges(bars: list[CandlePoint]) -> list[float]:
    if not bars:
        return []
    result = [bars[0].high - bars[0].low]
    for previous, current in zip(bars, bars[1:]):
        result.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return result


def median_true_range_pct(bars: list[CandlePoint]) -> float:
    ranges = true_ranges(bars)
    close = median(bar.close for bar in bars) if bars else 0.0
    return median(ranges) / close * 100 if ranges and close > 0 else 0.0


def return_over(
    candles: list[CandlePoint], now: datetime, window: timedelta
) -> float | None:
    rows = sorted(
        [bar for bar in candles if now - window <= bar.start < now],
        key=lambda bar: bar.start,
    )
    if len(rows) < 2 or rows[0].open <= 0:
        return None
    return (rows[-1].close - rows[0].open) / rows[0].open * 100


def linear_fit(
    points: list[tuple[int, float]]
) -> tuple[float, float, float] | None:
    if len(points) < 2:
        return None
    xs, ys = [float(row[0]) for row in points], [float(row[1]) for row in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator <= 0:
        return None
    slope = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)
    ) / denominator
    intercept = mean_y - slope * mean_x
    total = sum((value - mean_y) ** 2 for value in ys)
    residual = sum(
        (y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys)
    )
    r_squared = 1 - residual / total if total > 0 else 1.0
    return slope, intercept, clamp(r_squared, 0.0, 1.0)


def normalized_slope(values: list[float]) -> float:
    fit = linear_fit(list(enumerate(values)))
    scale = median(abs(value) for value in values) if values else 0.0
    return fit[0] / scale * 100 if fit and scale > 0 else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, max(0, round((len(values) - 1) * q)))
    return values[index]


def split_values(values: list[float], count: int) -> list[list[float]]:
    size = max(1, len(values) // count)
    result = [
        values[index * size:(index + 1) * size]
        for index in range(count - 1)
    ]
    result.append(values[(count - 1) * size:])
    return result


def rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def candidate_alert_stage(candidate: Candidate) -> str:
    if candidate.bottom and candidate.bottom.stage != BottomStage.NONE:
        return candidate.bottom.stage.value
    return candidate.status.value
