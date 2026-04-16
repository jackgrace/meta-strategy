"""
Fatigue analysis engine.

Core philosophy:
- Fatigue is a TREND, not a snapshot. An ad with 1.2x ROAS that's stable is fine.
- Every ad is scored against its OWN baseline, not arbitrary thresholds.
- Ads are classified by their role in ASC before scoring.
- ASC spend allocation is itself a signal — if Meta is deprioritizing, listen.
"""

import logging
from dataclasses import dataclass, field
from collections import defaultdict

from meta_api import AdDayMetrics
from config import Config

logger = logging.getLogger(__name__)


@dataclass
class SignalDetail:
    """Individual fatigue signal with raw values."""
    name: str
    baseline_value: float
    recent_value: float
    pct_change: float  # Negative = improving for CTR/ROAS, positive = worsening
    raw_score: float   # 0-100 contribution before weighting
    weight: float
    weighted_score: float


@dataclass
class TrendStreak:
    """Day-over-day trend tracking for a single metric."""
    metric_name: str  # "roas", "cpa", "cpc", "ctr"
    direction: str  # "declining", "rising", "stable"
    consecutive_days: int  # Longest current streak in the bad direction
    days_bad_of_total: int  # e.g. 5 of 7 days declining
    total_days: int  # Total days analyzed
    first_value: float  # Value on first day of window
    last_value: float  # Value on most recent day
    pct_change_total: float  # Total change first -> last


def _analyze_streak(
    days: list[AdDayMetrics],
    attr: str,
    bad_direction: str,  # "up" or "down" — which direction is bad
) -> TrendStreak:
    """
    Analyze day-over-day streaks for a metric.

    bad_direction="up" means rising values are bad (CPC, CPA, CPM).
    bad_direction="down" means falling values are bad (ROAS, CTR).
    """
    values = [getattr(d, attr) for d in days]

    # Count days moving in the bad direction and track consecutive streak
    days_bad = 0
    max_consecutive = 0
    current_consecutive = 0

    for i in range(1, len(values)):
        prev, curr = values[i - 1], values[i]
        if prev == 0 and curr == 0:
            continue

        is_bad = (curr > prev) if bad_direction == "up" else (curr < prev)

        if is_bad:
            days_bad += 1
            current_consecutive += 1
            max_consecutive = max(max_consecutive, current_consecutive)
        else:
            current_consecutive = 0

    # Overall direction
    first_val = values[0] if values else 0
    last_val = values[-1] if values else 0
    total_change = _safe_pct_change(first_val, last_val)

    if bad_direction == "up":
        direction = "rising" if total_change > 5 else ("declining" if total_change < -5 else "stable")
    else:
        direction = "declining" if total_change < -5 else ("rising" if total_change > 5 else "stable")

    return TrendStreak(
        metric_name=attr,
        direction=direction,
        consecutive_days=max_consecutive,
        days_bad_of_total=days_bad,
        total_days=len(values) - 1,  # Transitions, not days
        first_value=first_val,
        last_value=last_val,
        pct_change_total=total_change,
    )


@dataclass
class LongTrendDetail:
    """Long-window (21d) trend analysis for slow-burn fatigue detection."""
    score: int  # 0-100, same scale as short window
    signals: list[SignalDetail] = field(default_factory=list)
    summary: str = ""  # e.g. "Slow CTR decline over 3 weeks"
    days_available: int = 0  # How many days of data we actually had


@dataclass
class AdFatigueReport:
    """Complete fatigue analysis for one ad."""
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    role: str  # "efficiency" | "engagement" | "low_data"
    fatigue_score: int  # 0-100
    alert_level: str  # "critical" | "warning" | "watch" | "healthy"
    days_active: int
    avg_daily_spend: float
    total_spend: float
    spend_share_pct: float  # % of total ASC spend

    # 7-day totals (matches Ads Manager view)
    total_7d_spend: float
    total_7d_revenue: float
    total_7d_purchases: int
    total_7d_clicks: int
    total_7d_impressions: int
    roas_7d: float        # total revenue / total spend (weighted, matches Ads Manager)
    cpa_7d: float         # total spend / total purchases (weighted)
    ctr_7d: float         # total clicks / total impressions * 100 (weighted)
    cpc_7d: float         # total spend / total clicks (weighted)

    # Recent 3-day averages (for trend detection)
    current_ctr: float
    current_cpc: float
    current_cpm: float
    current_cpa: float
    current_roas: float
    current_frequency: float

    # Signals breakdown
    signals: list[SignalDetail] = field(default_factory=list)
    summary: str = ""  # Human-readable summary of what's happening

    # Day-over-day streak tracking for key metrics
    streaks: list[TrendStreak] = field(default_factory=list)

    # Long-window trend (only populated when short window looks healthy
    # but long window reveals slow-burn fatigue)
    long_trend: LongTrendDetail | None = None


def _safe_pct_change(baseline: float, recent: float) -> float:
    """Calculate percentage change, handling zero baselines."""
    if baseline == 0:
        return 0.0
    return ((recent - baseline) / abs(baseline)) * 100


def _signal_to_score(pct_change: float, invert: bool = False) -> float:
    """
    Convert a percentage change into a 0-100 fatigue score.

    For metrics where INCREASE = bad (CPC, CPM, frequency):
        +50% change -> score ~70
        +100% change -> score ~90

    For metrics where DECREASE = bad (CTR, ROAS):
        invert=True flips the sign

    Returns 0 if the metric is improving.
    """
    if invert:
        pct_change = -pct_change

    if pct_change <= 0:
        return 0.0  # Metric is improving or stable

    # Sigmoid-like mapping: gradual ramp that saturates near 100
    # 10% change -> ~20, 25% -> ~45, 50% -> ~70, 100% -> ~90
    score = 100 * (1 - (1 / (1 + (pct_change / 40))))
    return min(score, 100)


def _classify_role(avg_roas: float, avg_ctr: float, config: Config) -> str:
    """
    Classify an ad's role in ASC.

    Efficiency: High ROAS — likely retargeting/bottom-funnel.
    Engagement: Low ROAS but high CTR — doing top/mid-funnel work.
    """
    if avg_roas >= config.efficiency_roas_floor:
        return "efficiency"
    elif avg_ctr >= config.engagement_ctr_floor:
        return "engagement"
    else:
        # Low ROAS AND low CTR — but if ASC is spending on it,
        # treat as engagement (benefit of the doubt to the algo)
        return "engagement"


def _generate_summary(signals: list[SignalDetail], role: str, fatigue_score: int) -> str:
    """Generate a human-readable fatigue summary."""
    if fatigue_score < 30:
        return "Performing within baseline — no fatigue detected."

    # Find the top 2 contributing signals
    ranked = sorted(signals, key=lambda s: s.weighted_score, reverse=True)
    top_signals = [s for s in ranked[:3] if s.weighted_score > 2]

    if not top_signals:
        return "Mild metric shifts — monitor over next 48h."

    parts = []
    for s in top_signals:
        direction = "up" if s.pct_change > 0 else "down"
        if s.name in ("ctr_decay", "roas_decay"):
            direction = "down" if s.pct_change > 0 else "up"  # Inverted metrics

        if s.name == "frequency_climb" and s.pct_change > 20:
            parts.append(f"frequency climbing ({s.recent_value:.1f})")
        elif s.name == "ctr_decay":
            parts.append(f"CTR dropping ({abs(s.pct_change):.0f}%)")
        elif s.name == "cpc_inflation":
            parts.append(f"CPC rising ({abs(s.pct_change):.0f}%)")
        elif s.name == "cpm_inflation":
            parts.append(f"CPM rising ({abs(s.pct_change):.0f}%)")
        elif s.name == "cpa_inflation":
            parts.append(f"CPA rising ({abs(s.pct_change):.0f}%)")
        elif s.name == "roas_decay":
            parts.append(f"ROAS declining ({abs(s.pct_change):.0f}%)")
        elif s.name == "spend_share_decline":
            parts.append("ASC deprioritizing spend")

    # Detect classic fatigue pattern
    signal_names = {s.name for s in top_signals}
    if "cpa_inflation" in signal_names and "roas_decay" in signal_names:
        return f"Efficiency eroding — {', '.join(parts)}"
    elif "frequency_climb" in signal_names and "ctr_decay" in signal_names:
        return f"Classic fatigue — {', '.join(parts)}"
    elif "cpm_inflation" in signal_names and "frequency_climb" in signal_names:
        return f"Audience saturation — {', '.join(parts)}"
    elif "spend_share_decline" in signal_names:
        return f"ASC pulling back — {', '.join(parts)}"
    else:
        return f"Declining performance — {', '.join(parts)}"


def _avg_metrics(data: list[AdDayMetrics], attr: str) -> float:
    """Average a metric across a list of daily data points."""
    vals = [getattr(d, attr) for d in data]
    return sum(vals) / len(vals) if vals else 0


def _score_window(
    baseline_days: list[AdDayMetrics],
    recent_days_data: list[AdDayMetrics],
    weights: dict,
    total_spend_all: float,
    total_days: int,
) -> tuple[int, list[SignalDetail]]:
    """
    Score fatigue signals for a given baseline vs recent window.
    Returns (score, signals).
    """
    baseline = {k: _avg_metrics(baseline_days, k) for k in ("ctr", "cpc", "cpm", "cpa", "frequency", "roas", "spend")}
    recent = {k: _avg_metrics(recent_days_data, k) for k in ("ctr", "cpc", "cpm", "cpa", "frequency", "roas", "spend")}

    # Spend share
    avg_daily_total = total_spend_all / total_days if total_days > 0 else 1
    baseline_spend_share = baseline["spend"] / avg_daily_total * 100 if avg_daily_total > 0 else 0
    recent_spend_share = recent["spend"] / avg_daily_total * 100 if avg_daily_total > 0 else 0

    signal_defs = [
        ("ctr_decay",           "ctr",       True),
        ("cpc_inflation",       "cpc",       False),
        ("cpm_inflation",       "cpm",       False),
        ("cpa_inflation",       "cpa",       False),
        ("frequency_climb",     "frequency", False),
        ("roas_decay",          "roas",      True),
    ]

    signals = []
    for name, metric, invert in signal_defs:
        change = _safe_pct_change(baseline[metric], recent[metric])
        score = _signal_to_score(change, invert=invert)
        stored_change = -change if invert else change
        signals.append(SignalDetail(
            name=name,
            baseline_value=baseline[metric],
            recent_value=recent[metric],
            pct_change=stored_change,
            raw_score=score,
            weight=weights[name],
            weighted_score=score * weights[name],
        ))

    # Spend share decline
    share_change = _safe_pct_change(baseline_spend_share, recent_spend_share)
    share_score = _signal_to_score(share_change, invert=True)
    signals.append(SignalDetail(
        name="spend_share_decline",
        baseline_value=baseline_spend_share,
        recent_value=recent_spend_share,
        pct_change=-share_change,
        raw_score=share_score,
        weight=weights["spend_share_decline"],
        weighted_score=share_score * weights["spend_share_decline"],
    ))

    fatigue_score = int(min(100, sum(s.weighted_score for s in signals)))
    return fatigue_score, signals


def _generate_long_trend_summary(signals: list[SignalDetail], score: int) -> str:
    """Generate summary for slow-burn fatigue detected on the 21-day window."""
    if score < 25:
        return ""

    ranked = sorted(signals, key=lambda s: s.weighted_score, reverse=True)
    top = [s for s in ranked[:3] if s.weighted_score > 2]

    if not top:
        return "Gradual decline across multiple signals over 3 weeks"

    parts = []
    for s in top:
        if s.name == "ctr_decay":
            parts.append(f"CTR down {abs(s.pct_change):.0f}%")
        elif s.name == "cpc_inflation":
            parts.append(f"CPC up {abs(s.pct_change):.0f}%")
        elif s.name == "cpm_inflation":
            parts.append(f"CPM up {abs(s.pct_change):.0f}%")
        elif s.name == "cpa_inflation":
            parts.append(f"CPA up {abs(s.pct_change):.0f}%")
        elif s.name == "frequency_climb":
            parts.append(f"freq up {abs(s.pct_change):.0f}%")
        elif s.name == "roas_decay":
            parts.append(f"ROAS down {abs(s.pct_change):.0f}%")
        elif s.name == "spend_share_decline":
            parts.append("spend share declining")

    return f"Slow burn over 21d — {', '.join(parts)}"


def analyze_fatigue(
    all_metrics: list[AdDayMetrics],
    config: Config,
) -> list[AdFatigueReport]:
    """
    Analyze fatigue for all ads using two windows:

    Short window (7d): 4d baseline vs 3d recent — catches acute fatigue.
    Long window (21d): 14d baseline vs 7d recent — catches slow-burn decay.

    The long window only surfaces when the short window looks healthy,
    so it doesn't double up noise on ads already flagged.
    """

    # Group by ad_id
    ads: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in all_metrics:
        ads[m.ad_id].append(m)

    # Calculate total spend across all ads for spend share
    total_spend_all = sum(m.spend for m in all_metrics)

    reports = []
    skipped_low_data = 0
    skipped_low_spend = 0

    for ad_id, all_days in ads.items():
        # Sort by date
        all_days.sort(key=lambda d: d.date)

        # Use last 7 days for short window (may have up to 21 days of data)
        short_days = all_days[-config.lookback_days:]

        # Need minimum data for short window
        if len(short_days) < 3:
            skipped_low_data += 1
            continue

        total_spend = sum(d.spend for d in short_days)
        avg_daily_spend = total_spend / len(short_days)

        # Skip low-spend ads
        if avg_daily_spend < config.min_spend_threshold:
            skipped_low_spend += 1
            continue

        # Short window: split into baseline and recent
        short_split = max(1, len(short_days) - config.recent_days)
        short_baseline = short_days[:short_split]
        short_recent = short_days[short_split:]

        if not short_baseline or not short_recent:
            continue

        # Role classification uses the short window
        overall_roas = _avg_metrics(short_days, "roas")
        overall_ctr = _avg_metrics(short_days, "ctr")
        role = _classify_role(overall_roas, overall_ctr, config)
        weights = config.efficiency_weights if role == "efficiency" else config.engagement_weights

        # Spend share (based on short window)
        spend_share = (total_spend / total_spend_all * 100) if total_spend_all > 0 else 0

        # Score short window
        fatigue_score, signals = _score_window(
            short_baseline, short_recent, weights,
            total_spend_all, len(short_days),
        )

        # Recent metrics for display
        recent_metrics = {k: _avg_metrics(short_recent, k) for k in ("ctr", "cpc", "cpm", "cpa", "roas", "frequency")}

        # Alert level from short window
        if fatigue_score >= config.critical_threshold:
            alert_level = "critical"
        elif fatigue_score >= config.warning_threshold:
            alert_level = "warning"
        elif fatigue_score >= config.watch_threshold:
            alert_level = "watch"
        else:
            alert_level = "healthy"

        summary = _generate_summary(signals, role, fatigue_score)

        # Long window: only analyze if short window says healthy/watch
        # (no point double-flagging ads already critical/warning)
        # Constrain to last 21 days even if more data is available (testing pulls 90d)
        long_trend = None
        long_days = all_days[-config.long_lookback_days:]
        if alert_level in ("healthy", "watch") and len(long_days) >= 10:
            long_split = max(1, len(long_days) - config.long_recent_days)
            long_baseline = long_days[:long_split]
            long_recent = long_days[long_split:]

            if long_baseline and long_recent:
                long_score, long_signals = _score_window(
                    long_baseline, long_recent, weights,
                    total_spend_all, len(long_days),
                )

                # Only surface if the long window reveals meaningful decay
                if long_score >= config.long_trend_threshold:
                    long_summary = _generate_long_trend_summary(long_signals, long_score)
                    long_trend = LongTrendDetail(
                        score=long_score,
                        signals=long_signals,
                        summary=long_summary,
                        days_available=len(long_days),
                    )

                    # Upgrade healthy -> watch if long trend is significant
                    if alert_level == "healthy" and long_score >= config.watch_threshold:
                        alert_level = "watch"
                        summary = f"Short-term stable, but: {long_summary}"

        # 7-day weighted totals (matches Ads Manager calculations)
        total_7d_spend = sum(d.spend for d in short_days)
        total_7d_revenue = sum(d.revenue for d in short_days)
        total_7d_purchases = sum(d.purchases for d in short_days)
        total_7d_clicks = sum(d.clicks for d in short_days)
        total_7d_impressions = sum(d.impressions for d in short_days)

        roas_7d = total_7d_revenue / total_7d_spend if total_7d_spend > 0 else 0
        cpa_7d = total_7d_spend / total_7d_purchases if total_7d_purchases > 0 else 0
        ctr_7d = (total_7d_clicks / total_7d_impressions * 100) if total_7d_impressions > 0 else 0
        cpc_7d = total_7d_spend / total_7d_clicks if total_7d_clicks > 0 else 0

        # Day-over-day streak detection on the short window
        streaks = [
            _analyze_streak(short_days, "roas", bad_direction="down"),
            _analyze_streak(short_days, "cpa", bad_direction="up"),
            _analyze_streak(short_days, "cpc", bad_direction="up"),
            _analyze_streak(short_days, "ctr", bad_direction="down"),
        ]

        ad_meta = all_days[0]

        reports.append(AdFatigueReport(
            ad_id=ad_id,
            ad_name=ad_meta.ad_name,
            campaign_name=ad_meta.campaign_name,
            adset_name=ad_meta.adset_name,
            role=role,
            fatigue_score=fatigue_score,
            alert_level=alert_level,
            days_active=len(all_days),
            avg_daily_spend=avg_daily_spend,
            total_spend=total_spend,
            spend_share_pct=spend_share,
            total_7d_spend=total_7d_spend,
            total_7d_revenue=total_7d_revenue,
            total_7d_purchases=total_7d_purchases,
            total_7d_clicks=total_7d_clicks,
            total_7d_impressions=total_7d_impressions,
            roas_7d=roas_7d,
            cpa_7d=cpa_7d,
            ctr_7d=ctr_7d,
            cpc_7d=cpc_7d,
            current_ctr=recent_metrics["ctr"],
            current_cpc=recent_metrics["cpc"],
            current_cpm=recent_metrics["cpm"],
            current_cpa=recent_metrics["cpa"],
            current_roas=recent_metrics["roas"],
            current_frequency=recent_metrics["frequency"],
            signals=signals,
            summary=summary,
            streaks=streaks,
            long_trend=long_trend,
        ))

    # Sort by fatigue score descending
    reports.sort(key=lambda r: r.fatigue_score, reverse=True)

    long_trend_count = sum(1 for r in reports if r.long_trend)
    logger.info(
        f"Total unique ads from API: {len(ads)} │ "
        f"Skipped: {skipped_low_data} low data, {skipped_low_spend} low spend (<${config.min_spend_threshold}/day)"
    )
    logger.info(
        f"Analyzed {len(reports)} ads: "
        f"{sum(1 for r in reports if r.alert_level == 'critical')} critical, "
        f"{sum(1 for r in reports if r.alert_level == 'warning')} warning, "
        f"{sum(1 for r in reports if r.alert_level == 'watch')} watch, "
        f"{sum(1 for r in reports if r.alert_level == 'healthy')} healthy"
        f"{f', {long_trend_count} with slow-burn trends' if long_trend_count else ''}"
    )

    return reports
