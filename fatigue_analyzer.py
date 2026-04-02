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

    # Current metrics (recent window averages)
    current_ctr: float
    current_cpc: float
    current_cpm: float
    current_roas: float
    current_frequency: float

    # Signals breakdown
    signals: list[SignalDetail] = field(default_factory=list)
    summary: str = ""  # Human-readable summary of what's happening


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
        elif s.name == "roas_decay":
            parts.append(f"ROAS declining ({abs(s.pct_change):.0f}%)")
        elif s.name == "spend_share_decline":
            parts.append("ASC deprioritizing spend")

    # Detect classic fatigue pattern
    signal_names = {s.name for s in top_signals}
    if "frequency_climb" in signal_names and "ctr_decay" in signal_names:
        return f"Classic fatigue — {', '.join(parts)}"
    elif "cpm_inflation" in signal_names and "frequency_climb" in signal_names:
        return f"Audience saturation — {', '.join(parts)}"
    elif "spend_share_decline" in signal_names:
        return f"ASC pulling back — {', '.join(parts)}"
    else:
        return f"Declining performance — {', '.join(parts)}"


def analyze_fatigue(
    all_metrics: list[AdDayMetrics],
    config: Config,
) -> list[AdFatigueReport]:
    """
    Analyze fatigue for all ads.

    1. Group metrics by ad
    2. Split into baseline vs recent windows
    3. Classify each ad's role
    4. Score fatigue signals against ad's own baseline
    5. Weight signals by role
    """

    # Group by ad_id
    ads: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in all_metrics:
        ads[m.ad_id].append(m)

    # Calculate total spend across all ads for spend share
    total_spend_all = sum(m.spend for m in all_metrics)

    reports = []

    for ad_id, days in ads.items():
        # Sort by date
        days.sort(key=lambda d: d.date)

        # Need minimum data
        if len(days) < 3:
            continue

        total_spend = sum(d.spend for d in days)
        avg_daily_spend = total_spend / len(days)

        # Skip low-spend ads
        if avg_daily_spend < config.min_spend_threshold:
            continue

        # Split into baseline and recent windows
        split_idx = max(1, len(days) - config.recent_days)
        baseline_days = days[:split_idx]
        recent_days_data = days[split_idx:]

        if not baseline_days or not recent_days_data:
            continue

        # Calculate averages for each window
        def avg(data: list[AdDayMetrics], attr: str) -> float:
            vals = [getattr(d, attr) for d in data]
            return sum(vals) / len(vals) if vals else 0

        baseline = {
            "ctr": avg(baseline_days, "ctr"),
            "cpc": avg(baseline_days, "cpc"),
            "cpm": avg(baseline_days, "cpm"),
            "frequency": avg(baseline_days, "frequency"),
            "roas": avg(baseline_days, "roas"),
            "spend": avg(baseline_days, "spend"),
        }

        recent = {
            "ctr": avg(recent_days_data, "ctr"),
            "cpc": avg(recent_days_data, "cpc"),
            "cpm": avg(recent_days_data, "cpm"),
            "frequency": avg(recent_days_data, "frequency"),
            "roas": avg(recent_days_data, "roas"),
            "spend": avg(recent_days_data, "spend"),
        }

        # Overall averages for role classification
        overall_roas = avg(days, "roas")
        overall_ctr = avg(days, "ctr")

        # Classify role
        role = _classify_role(overall_roas, overall_ctr, config)
        weights = config.efficiency_weights if role == "efficiency" else config.engagement_weights

        # Spend share
        spend_share = (total_spend / total_spend_all * 100) if total_spend_all > 0 else 0
        baseline_spend_share = baseline["spend"] / (total_spend_all / len(days)) * 100 if total_spend_all > 0 else 0
        recent_spend_share = recent["spend"] / (total_spend_all / len(days)) * 100 if total_spend_all > 0 else 0

        # Calculate each fatigue signal
        signals = []

        # CTR decay (decrease = bad -> invert)
        ctr_change = _safe_pct_change(baseline["ctr"], recent["ctr"])
        ctr_score = _signal_to_score(ctr_change, invert=True)
        signals.append(SignalDetail(
            name="ctr_decay",
            baseline_value=baseline["ctr"],
            recent_value=recent["ctr"],
            pct_change=-ctr_change,  # Store as negative when declining
            raw_score=ctr_score,
            weight=weights["ctr_decay"],
            weighted_score=ctr_score * weights["ctr_decay"],
        ))

        # CPC inflation (increase = bad)
        cpc_change = _safe_pct_change(baseline["cpc"], recent["cpc"])
        cpc_score = _signal_to_score(cpc_change)
        signals.append(SignalDetail(
            name="cpc_inflation",
            baseline_value=baseline["cpc"],
            recent_value=recent["cpc"],
            pct_change=cpc_change,
            raw_score=cpc_score,
            weight=weights["cpc_inflation"],
            weighted_score=cpc_score * weights["cpc_inflation"],
        ))

        # CPM inflation (increase = bad)
        cpm_change = _safe_pct_change(baseline["cpm"], recent["cpm"])
        cpm_score = _signal_to_score(cpm_change)
        signals.append(SignalDetail(
            name="cpm_inflation",
            baseline_value=baseline["cpm"],
            recent_value=recent["cpm"],
            pct_change=cpm_change,
            raw_score=cpm_score,
            weight=weights["cpm_inflation"],
            weighted_score=cpm_score * weights["cpm_inflation"],
        ))

        # Frequency climb (increase = bad)
        freq_change = _safe_pct_change(baseline["frequency"], recent["frequency"])
        freq_score = _signal_to_score(freq_change)
        signals.append(SignalDetail(
            name="frequency_climb",
            baseline_value=baseline["frequency"],
            recent_value=recent["frequency"],
            pct_change=freq_change,
            raw_score=freq_score,
            weight=weights["frequency_climb"],
            weighted_score=freq_score * weights["frequency_climb"],
        ))

        # ROAS decay (decrease = bad -> invert)
        roas_change = _safe_pct_change(baseline["roas"], recent["roas"])
        roas_score = _signal_to_score(roas_change, invert=True)
        signals.append(SignalDetail(
            name="roas_decay",
            baseline_value=baseline["roas"],
            recent_value=recent["roas"],
            pct_change=-roas_change,
            raw_score=roas_score,
            weight=weights["roas_decay"],
            weighted_score=roas_score * weights["roas_decay"],
        ))

        # Spend share decline (decrease = ASC pulling back -> invert)
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

        # Total fatigue score
        fatigue_score = int(min(100, sum(s.weighted_score for s in signals)))

        # Alert level
        if fatigue_score >= config.critical_threshold:
            alert_level = "critical"
        elif fatigue_score >= config.warning_threshold:
            alert_level = "warning"
        elif fatigue_score >= config.watch_threshold:
            alert_level = "watch"
        else:
            alert_level = "healthy"

        summary = _generate_summary(signals, role, fatigue_score)

        ad_meta = days[0]  # Use first day for name/campaign info

        reports.append(AdFatigueReport(
            ad_id=ad_id,
            ad_name=ad_meta.ad_name,
            campaign_name=ad_meta.campaign_name,
            adset_name=ad_meta.adset_name,
            role=role,
            fatigue_score=fatigue_score,
            alert_level=alert_level,
            days_active=len(days),
            avg_daily_spend=avg_daily_spend,
            total_spend=total_spend,
            spend_share_pct=spend_share,
            current_ctr=recent["ctr"],
            current_cpc=recent["cpc"],
            current_cpm=recent["cpm"],
            current_roas=recent["roas"],
            current_frequency=recent["frequency"],
            signals=signals,
            summary=summary,
        ))

    # Sort by fatigue score descending
    reports.sort(key=lambda r: r.fatigue_score, reverse=True)

    logger.info(
        f"Analyzed {len(reports)} ads: "
        f"{sum(1 for r in reports if r.alert_level == 'critical')} critical, "
        f"{sum(1 for r in reports if r.alert_level == 'warning')} warning, "
        f"{sum(1 for r in reports if r.alert_level == 'watch')} watch, "
        f"{sum(1 for r in reports if r.alert_level == 'healthy')} healthy"
    )

    return reports
