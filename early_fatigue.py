"""
Early Fatigue Detection Agent.

Spec:
- Scope: ACTIVE ads in non-TESTING campaigns, evaluated per market
- Recent window: last 3 days (days 1-3)
- Baseline window: prior 14 days (days 4-17), no overlap
- Tier 1 WATCH (leading): CTR decline ≥20% OR CPC increase ≥25%
- Tier 2 ACT (lagging): ROAS decline ≥30% OR CPA increase ≥35%
- Persistence: must breach same tier 2 consecutive days before alerting
- Cooldown: suppress re-alerts for 5 days after alerting (escalation bypasses)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from meta_api import AdDayMetrics
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

# Markets to evaluate separately
MARKETS = ["AU", "UK", "US", "CA", "EU", "UAE"]

# Tier thresholds
TIER1_CTR_DECLINE_PCT = 20
TIER1_CPC_INCREASE_PCT = 25
TIER2_ROAS_DECLINE_PCT = 30
TIER2_CPA_INCREASE_PCT = 35

# TOF detection — if an ad matches 2+ of these, it's likely top-of-funnel
# and low ROAS is expected (ASC uses it for prospecting/audience building)
TOF_MAX_FREQUENCY = 1.5
TOF_MIN_CTR_PERCENTILE = 0.5  # Above median CTR for evaluated ads
TOF_MIN_CPC_PERCENTILE = 0.5  # Below median CPC for evaluated ads

# Eligibility gates
MIN_ACTIVE_DAYS = 5
MIN_BASELINE_SPEND = 100.0  # Baseline total spend must exceed this

RECENT_DAYS = 2
BASELINE_DAYS = 3

# Persistence
CONSECUTIVE_DAYS_REQUIRED = 2
COOLDOWN_DAYS = 5

STATE_DIR = os.environ.get("STATE_DIR", "/app/state")
STATE_FILE = os.path.join(STATE_DIR, "fatigue_state.json")


@dataclass
class FatigueAlert:
    """A single ad flagged for fatigue."""
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    market: str
    tier: int  # 1 = WATCH, 2 = ACT
    is_escalation: bool  # True if just escalated from tier 1 → 2
    is_repeat: bool  # True if "still fatiguing" after cooldown expired
    days_since_first_flagged: int

    # Spend
    recent_daily_spend: float
    baseline_daily_spend: float

    # Deltas
    ctr_delta_pct: float
    cpc_delta_pct: float
    roas_delta_pct: float
    cpa_delta_pct: float

    # Baseline vs recent values
    baseline_ctr: float
    recent_ctr: float
    baseline_cpc: float
    recent_cpc: float
    baseline_roas: float
    recent_roas: float
    baseline_cpa: float
    recent_cpa: float

    breaching_metrics: list[str] = field(default_factory=list)

    # Edge case flags
    budget_adjusted: bool = False
    is_tof: bool = False  # Likely top-of-funnel — low ROAS expected
    tof_signals: list[str] = field(default_factory=list)  # Why we think it's TOF


@dataclass
class DailyLogEntry:
    """Per-ad daily log for back-testing."""
    ad_id: str
    market: str
    date: str
    tier_1_flag: bool
    tier_2_flag: bool
    ctr_delta: float
    cpc_delta: float
    roas_delta: float
    cpa_delta: float
    alert_sent: bool
    cooldown_active: bool


def _detect_market(campaign_name: str) -> str | None:
    """Extract market from campaign name."""
    name_upper = campaign_name.upper()
    for market in MARKETS:
        if market in name_upper:
            return market
    return None


def _pct_change(baseline: float, recent: float) -> float:
    if baseline == 0:
        return 0.0
    return ((recent - baseline) / abs(baseline)) * 100


def _avg(data: list[AdDayMetrics], attr: str) -> float:
    vals = [getattr(d, attr) for d in data]
    return sum(vals) / len(vals) if vals else 0


def _load_state() -> dict:
    """Load persisted state from disk."""
    if not os.path.exists(STATE_FILE):
        return {"ad_flags": {}, "cooldowns": {}, "first_flagged": {}}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load state file, starting fresh: {e}")
        return {"ad_flags": {}, "cooldowns": {}, "first_flagged": {}}


def _save_state(state: dict):
    """Persist state to disk."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    logger.info(f"State saved ({len(state.get('ad_flags', {}))} ad flags tracked)")


def _check_promo_blackout(config: Config) -> bool:
    """Check if today is in a promo/sale window (skip evaluation)."""
    promo_file = os.path.join(os.path.dirname(__file__), "promo_dates.json")
    if not os.path.exists(promo_file):
        return False
    try:
        with open(promo_file, "r") as f:
            promo_config = json.load(f)
        today = datetime.now(AEST).strftime("%Y-%m-%d")
        promo_dates = promo_config.get("dates", [])
        if today in promo_dates:
            logger.info(f"Promo blackout active ({today}) — skipping fatigue evaluation")
            return True
        for window in promo_config.get("windows", []):
            if window.get("start") <= today <= window.get("end"):
                logger.info(f"Promo window active ({window}) — skipping fatigue evaluation")
                return True
    except (json.JSONDecodeError, OSError):
        pass
    return False


def _detect_budget_change(baseline_days: list[AdDayMetrics], recent_days: list[AdDayMetrics]) -> bool:
    """Check if daily spend changed >50% between baseline and recent (budget adjustment)."""
    b_avg_spend = _avg(baseline_days, "spend")
    r_avg_spend = _avg(recent_days, "spend")
    if b_avg_spend == 0:
        return False
    change = abs(_pct_change(b_avg_spend, r_avg_spend))
    return change > 50


def analyze_early_fatigue(
    all_metrics: list[AdDayMetrics],
    ad_statuses: dict[str, str],
    config: Config,
) -> tuple[list[FatigueAlert], list[DailyLogEntry]]:
    """
    Run the fatigue detection pipeline per the spec.
    Returns (alerts_to_send, daily_log_entries).
    """

    # Check promo blackout
    if _check_promo_blackout(config):
        return [], []

    today_str = datetime.now(AEST).strftime("%Y-%m-%d")

    # Filter to SCALE campaigns, exclude ads with OFF in name
    scale_metrics = [
        m for m in all_metrics
        if "SCALE" in m.campaign_name.upper()
        and "OFF" not in m.ad_name.upper()
    ]

    if not scale_metrics:
        logger.info("No SCALE campaign data found")
        return [], []

    # Group by ad
    ads: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in scale_metrics:
        ads[m.ad_id].append(m)

    # Load persisted state
    state = _load_state()
    yesterday_flags = state.get("ad_flags", {})
    cooldowns = state.get("cooldowns", {})
    first_flagged = state.get("first_flagged", {})

    # Today's flags (will be saved at the end)
    today_flags: dict[str, dict] = {}

    alerts: list[FatigueAlert] = []
    log_entries: list[DailyLogEntry] = []

    skipped_not_active = 0
    skipped_low_history = 0
    skipped_low_spend = 0
    evaluated = 0

    # First pass: collect recent CPC/CTR/frequency for all eligible ads
    # so we can compute medians for TOF detection
    eligible_recent_cpcs = []
    eligible_recent_ctrs = []
    eligible_ads_data: list[tuple] = []  # (ad_id, all_days, market, recent, baseline, recent_spend, baseline_spend)

    for ad_id, all_days in ads.items():
        all_days.sort(key=lambda d: d.date)

        status = ad_statuses.get(ad_id, "UNKNOWN")
        if status != "ACTIVE":
            skipped_not_active += 1
            continue

        market = _detect_market(all_days[0].campaign_name) or "OTHER"

        if len(all_days) < MIN_ACTIVE_DAYS:
            skipped_low_history += 1
            continue

        recent = all_days[-RECENT_DAYS:]
        baseline = all_days[-(RECENT_DAYS + BASELINE_DAYS):-RECENT_DAYS]

        if len(baseline) < 7:
            skipped_low_history += 1
            continue

        recent_spend = sum(d.spend for d in recent)
        baseline_spend = sum(d.spend for d in baseline)
        if baseline_spend < MIN_BASELINE_SPEND:
            skipped_low_spend += 1
            continue

        # Collect for median calculation
        r_clicks = sum(d.clicks for d in recent)
        r_impressions = sum(d.impressions for d in recent)
        if r_clicks > 0:
            eligible_recent_cpcs.append(recent_spend / r_clicks)
        if r_impressions > 0:
            eligible_recent_ctrs.append(r_clicks / r_impressions * 100)

        eligible_ads_data.append((ad_id, all_days, market, recent, baseline, recent_spend, baseline_spend))

    # Compute medians for TOF detection
    eligible_recent_cpcs.sort()
    eligible_recent_ctrs.sort()
    median_cpc = eligible_recent_cpcs[len(eligible_recent_cpcs) // 2] if eligible_recent_cpcs else 0
    median_ctr = eligible_recent_ctrs[len(eligible_recent_ctrs) // 2] if eligible_recent_ctrs else 0

    if median_cpc > 0:
        logger.info(f"TOF detection medians: CPC=${median_cpc:.2f}, CTR={median_ctr:.2f}%")

    # Second pass: evaluate each ad
    for ad_id, all_days, market, recent, baseline, recent_spend, baseline_spend in eligible_ads_data:

        evaluated += 1

        # Calculate weighted metrics (matches Ads Manager)
        b_clicks = sum(d.clicks for d in baseline)
        b_impressions = sum(d.impressions for d in baseline)
        b_purchases = sum(d.purchases for d in baseline)
        b_revenue = sum(d.revenue for d in baseline)

        r_clicks = sum(d.clicks for d in recent)
        r_impressions = sum(d.impressions for d in recent)
        r_purchases = sum(d.purchases for d in recent)
        r_revenue = sum(d.revenue for d in recent)

        b_ctr = (b_clicks / b_impressions * 100) if b_impressions > 0 else 0
        b_cpc = (baseline_spend / b_clicks) if b_clicks > 0 else 0
        b_roas = (b_revenue / baseline_spend) if baseline_spend > 0 else 0
        b_cpa = (baseline_spend / b_purchases) if b_purchases > 0 else 0

        r_ctr = (r_clicks / r_impressions * 100) if r_impressions > 0 else 0
        r_cpc = (recent_spend / r_clicks) if r_clicks > 0 else 0
        r_roas = (r_revenue / recent_spend) if recent_spend > 0 else 0
        r_cpa = (recent_spend / r_purchases) if r_purchases > 0 else 0

        ctr_delta = _pct_change(b_ctr, r_ctr)
        cpc_delta = _pct_change(b_cpc, r_cpc)
        roas_delta = _pct_change(b_roas, r_roas)
        cpa_delta = _pct_change(b_cpa, r_cpa)

        # Tier 1 — WATCH (leading indicators)
        tier1_breaches = []
        if ctr_delta <= -TIER1_CTR_DECLINE_PCT:
            tier1_breaches.append(f"CTR {ctr_delta:+.0f}%")
        if cpc_delta >= TIER1_CPC_INCREASE_PCT:
            tier1_breaches.append(f"CPC {cpc_delta:+.0f}%")
        tier1_flag = len(tier1_breaches) > 0

        # Tier 2 — ACT (lagging indicators)
        # Gate 3: need ≥2 purchases in recent window for ROAS/CPA checks
        tier2_breaches = []
        if r_purchases >= 2:
            if roas_delta <= -TIER2_ROAS_DECLINE_PCT and b_roas > 0:
                tier2_breaches.append(f"ROAS {roas_delta:+.0f}%")
            if cpa_delta >= TIER2_CPA_INCREASE_PCT and b_cpa > 0:
                tier2_breaches.append(f"CPA {cpa_delta:+.0f}%")
        tier2_flag = len(tier2_breaches) > 0

        # Budget change detection
        budget_adjusted = _detect_budget_change(baseline, recent)

        # Store today's flag state
        ad_key = f"{ad_id}:{market}"
        today_flags[ad_key] = {
            "tier1": tier1_flag,
            "tier2": tier2_flag,
            "date": today_str,
        }

        # Log entry for back-testing
        log_entries.append(DailyLogEntry(
            ad_id=ad_id,
            market=market,
            date=today_str,
            tier_1_flag=tier1_flag,
            tier_2_flag=tier2_flag,
            ctr_delta=round(ctr_delta, 1),
            cpc_delta=round(cpc_delta, 1),
            roas_delta=round(roas_delta, 1),
            cpa_delta=round(cpa_delta, 1),
            alert_sent=False,
            cooldown_active=ad_key in cooldowns,
        ))

        # TOF detection: check if this ad looks like top-of-funnel
        # Low CPC + low frequency + high CTR = prospecting ad, low ROAS expected
        tof_signals = []
        r_frequency = _avg(recent, "frequency")
        r_atc_total = sum(d.add_to_carts for d in recent)

        if r_cpc > 0 and r_cpc < median_cpc:
            tof_signals.append(f"CPC ${r_cpc:.2f} below median ${median_cpc:.2f}")
        if r_frequency < TOF_MAX_FREQUENCY:
            tof_signals.append(f"Freq {r_frequency:.1f} (reaching new users)")
        if r_ctr > median_ctr and median_ctr > 0:
            tof_signals.append(f"CTR {r_ctr:.2f}% above median {median_ctr:.2f}%")
        if r_atc_total > 0:
            tof_signals.append(f"{r_atc_total} ATCs in 3d")

        is_tof = len(tof_signals) >= 2

        # Determine highest tier breaching
        active_tier = 0
        breaching = []

        if tier2_flag:
            if is_tof:
                # Downgrade ACT → info tier (tier 3) for TOF ads
                active_tier = 3
                breaching = tier2_breaches + tier1_breaches
            else:
                active_tier = 2
                breaching = tier2_breaches + tier1_breaches
        elif tier1_flag:
            active_tier = 1
            breaching = tier1_breaches

        if active_tier == 0:
            continue

        # Escalation check: tier 1 → tier 2 bypasses cooldown
        was_tier1_only = cooldowns.get(ad_key, {}).get("tier", 0) == 1
        is_escalation = active_tier == 2 and was_tier1_only

        # Cooldown check
        cooldown_info = cooldowns.get(ad_key)
        if cooldown_info and not is_escalation:
            cooldown_expires = cooldown_info.get("expires", "")
            if today_str < cooldown_expires:
                # Still in cooldown
                continue
            # Cooldown expired but still breaching — re-alert as "still fatiguing"
            is_repeat = True
        else:
            is_repeat = False if not cooldown_info else True

        # Track first flagged date
        if ad_key not in first_flagged:
            first_flagged[ad_key] = today_str

        first_date = datetime.strptime(first_flagged[ad_key], "%Y-%m-%d")
        today_date = datetime.strptime(today_str, "%Y-%m-%d")
        days_since = (today_date - first_date).days

        b_daily_spend = baseline_spend / len(baseline)
        r_daily_spend = recent_spend / RECENT_DAYS

        alert = FatigueAlert(
            ad_id=ad_id,
            ad_name=all_days[0].ad_name,
            campaign_name=all_days[0].campaign_name,
            adset_name=all_days[0].adset_name,
            market=market,
            tier=active_tier,
            is_escalation=is_escalation,
            is_repeat=is_repeat,
            days_since_first_flagged=days_since,
            recent_daily_spend=r_daily_spend,
            baseline_daily_spend=b_daily_spend,
            ctr_delta_pct=ctr_delta,
            cpc_delta_pct=cpc_delta,
            roas_delta_pct=roas_delta,
            cpa_delta_pct=cpa_delta,
            baseline_ctr=b_ctr,
            recent_ctr=r_ctr,
            baseline_cpc=b_cpc,
            recent_cpc=r_cpc,
            baseline_roas=b_roas,
            recent_roas=r_roas,
            baseline_cpa=b_cpa,
            recent_cpa=r_cpa,
            breaching_metrics=breaching,
            budget_adjusted=budget_adjusted,
            is_tof=is_tof,
            tof_signals=tof_signals if is_tof else [],
        )
        alerts.append(alert)

        # Set cooldown
        expires = (today_date + timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        cooldowns[ad_key] = {"tier": active_tier, "expires": expires, "date": today_str}

        # Update log entry
        log_entries[-1].alert_sent = True

    # Clean up old cooldowns and first_flagged entries
    for key in list(cooldowns.keys()):
        if cooldowns[key].get("expires", "") < today_str:
            if key not in today_flags:
                del cooldowns[key]

    for key in list(first_flagged.keys()):
        if key not in today_flags:
            del first_flagged[key]

    # Save state
    state["ad_flags"] = today_flags
    state["cooldowns"] = cooldowns
    state["first_flagged"] = first_flagged
    _save_state(state)

    logger.info(
        f"Early fatigue (SCALE campaigns): {len(ads)} ads │ "
        f"{skipped_not_active} not active │ "
        f"{skipped_low_history} low history │ {skipped_low_spend} low spend (<${MIN_BASELINE_SPEND:.0f}/14d) │ "
        f"{evaluated} evaluated │ {len(alerts)} alerts"
    )

    return alerts, log_entries
