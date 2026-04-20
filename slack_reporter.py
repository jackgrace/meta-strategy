"""
Slack reporter.
Formats fatigue analysis into a clean daily digest and sends via webhook.
"""

import logging
from datetime import datetime

import requests

from fatigue_analyzer import AdFatigueReport
from testing_analyzer import TestingAdReport
from early_fatigue import EarlyFatigueAd
from config import Config

logger = logging.getLogger(__name__)

ALERT_EMOJI = {
    "critical": "🔴",
    "warning": "🟡",
    "watch": "🔵",
    "healthy": "✅",
}

ROLE_LABEL = {
    "efficiency": "⚡ Efficiency",
    "engagement": "🎯 Engagement",
    "low_data": "📊 Low Data",
}


def _format_currency(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    return f"${value:.2f}"


def _trend_arrow(pct_change: float, invert: bool = False) -> str:
    """Return arrow + percentage. invert=True means negative = bad."""
    if invert:
        pct_change = -pct_change
    if abs(pct_change) < 3:
        return "→"
    arrow = "↑" if pct_change > 0 else "↓"
    return f"{arrow}{abs(pct_change):.0f}%"


def _format_streak_line(report: AdFatigueReport) -> str | None:
    """Format streak info for key metrics. Returns None if nothing notable."""
    notable = []
    for streak in report.streaks:
        # Only show if there's a real trend (3+ consecutive days or majority of days)
        if streak.consecutive_days >= 3 or (streak.days_bad_of_total >= 4 and streak.total_days >= 5):
            label = streak.metric_name.upper()
            first = _format_currency(streak.first_value) if streak.metric_name in ("cpa", "cpc") else f"{streak.first_value:.1f}{'x' if streak.metric_name == 'roas' else '%'}"
            last = _format_currency(streak.last_value) if streak.metric_name in ("cpa", "cpc") else f"{streak.last_value:.1f}{'x' if streak.metric_name == 'roas' else '%'}"

            if streak.consecutive_days >= 3:
                notable.append(f"{label} {streak.direction} {streak.consecutive_days} consecutive days ({first} → {last})")
            else:
                notable.append(f"{label} {streak.direction} {streak.days_bad_of_total} of {streak.total_days} days ({first} → {last})")

    if not notable:
        return None
    return "Trends: " + " │ ".join(notable)


def _format_ad_block(report: AdFatigueReport) -> str:
    """Format a single ad's fatigue report for Slack with clear period comparison."""
    role = ROLE_LABEL.get(report.role, report.role)

    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)
    cpa_signal = next((s for s in report.signals if s.name == "cpa_inflation"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)

    # Baseline (first 4d) vs Recent (last 3d)
    b_roas = f"{roas_signal.baseline_value:.2f}x" if roas_signal else "—"
    r_roas = f"{roas_signal.recent_value:.2f}x" if roas_signal else "—"
    b_cpa = _format_currency(cpa_signal.baseline_value) if cpa_signal and cpa_signal.baseline_value > 0 else "—"
    r_cpa = _format_currency(cpa_signal.recent_value) if cpa_signal and cpa_signal.recent_value > 0 else "—"
    b_cpc = _format_currency(cpc_signal.baseline_value) if cpc_signal else "—"
    r_cpc = _format_currency(cpc_signal.recent_value) if cpc_signal else "—"
    b_ctr = f"{ctr_signal.baseline_value:.2f}%" if ctr_signal else "—"
    r_ctr = f"{ctr_signal.recent_value:.2f}%" if ctr_signal else "—"
    b_freq = f"{freq_signal.baseline_value:.1f}" if freq_signal else "—"
    r_freq = f"{freq_signal.recent_value:.1f}" if freq_signal else "—"

    cpa_7d_display = _format_currency(report.cpa_7d) if report.cpa_7d > 0 else "n/a"

    lines = [
        f"*{report.ad_name}*",
        f"Campaign: `{report.campaign_name}`",
        f"Role: {role} │ Score: {report.fatigue_score}/100",
        f"*7d totals:* Spend: {_format_currency(report.total_7d_spend)} │ ROAS: {report.roas_7d:.2f}x │ CPA: {cpa_7d_display} │ {report.total_7d_purchases} purchases │ Revenue: {_format_currency(report.total_7d_revenue)}",
        f"*First 4d:* ROAS: {b_roas} │ CPA: {b_cpa} │ CPC: {b_cpc} │ CTR: {b_ctr} │ Freq: {b_freq}",
        f"*Last 3d:*  ROAS: {r_roas} │ CPA: {r_cpa} │ CPC: {r_cpc} │ CTR: {r_ctr} │ Freq: {r_freq}",
    ]

    streak_line = _format_streak_line(report)
    if streak_line:
        lines.append(streak_line)

    lines.append(f"_{report.summary}_")

    if report.long_trend and report.long_trend.summary:
        lines.append(f"⚠️ _{report.long_trend.summary}_ (score: {report.long_trend.score}/100)")

    return "\n".join(lines)


def _format_watch_block(report: AdFatigueReport) -> str:
    """Format a Watch ad with clear 7d totals + baseline vs recent comparison."""
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    cpa_signal = next((s for s in report.signals if s.name == "cpa_inflation"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)
    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)

    # Baseline (first 4d) vs Recent (last 3d) values from signals
    b_roas = f"{roas_signal.baseline_value:.2f}x" if roas_signal else "—"
    r_roas = f"{roas_signal.recent_value:.2f}x" if roas_signal else "—"
    b_cpa = _format_currency(cpa_signal.baseline_value) if cpa_signal and cpa_signal.baseline_value > 0 else "—"
    r_cpa = _format_currency(cpa_signal.recent_value) if cpa_signal and cpa_signal.recent_value > 0 else "—"
    b_cpc = _format_currency(cpc_signal.baseline_value) if cpc_signal else "—"
    r_cpc = _format_currency(cpc_signal.recent_value) if cpc_signal else "—"
    b_ctr = f"{ctr_signal.baseline_value:.2f}%" if ctr_signal else "—"
    r_ctr = f"{ctr_signal.recent_value:.2f}%" if ctr_signal else "—"
    b_freq = f"{freq_signal.baseline_value:.1f}" if freq_signal else "—"
    r_freq = f"{freq_signal.recent_value:.1f}" if freq_signal else "—"

    cpa_7d_display = _format_currency(report.cpa_7d) if report.cpa_7d > 0 else "n/a"

    lines = [
        f"• *{report.ad_name}*",
        f"  Campaign: `{report.campaign_name}` │ Score: {report.fatigue_score}/100",
        f"  *7d totals:* Spend: {_format_currency(report.total_7d_spend)} │ ROAS: {report.roas_7d:.2f}x │ CPA: {cpa_7d_display} │ {report.total_7d_purchases} purchases",
        f"  *First 4d:* ROAS: {b_roas} │ CPA: {b_cpa} │ CPC: {b_cpc} │ CTR: {b_ctr} │ Freq: {b_freq}",
        f"  *Last 3d:*  ROAS: {r_roas} │ CPA: {r_cpa} │ CPC: {r_cpc} │ CTR: {r_ctr} │ Freq: {r_freq}",
    ]

    streak_line = _format_streak_line(report)
    if streak_line:
        lines.append(f"  {streak_line}")

    if report.long_trend and report.long_trend.summary:
        lines.append(f"  ⚠️ _21d trend:_ {report.long_trend.summary} (score: {report.long_trend.score}/100)")

    return "\n".join(lines)


def build_slack_message(reports: list[AdFatigueReport]) -> dict:
    """Build the full Slack message payload with blocks."""
    now = datetime.now().strftime("%a %d %b %Y")

    # Group by alert level
    critical = [r for r in reports if r.alert_level == "critical"]
    warning = [r for r in reports if r.alert_level == "warning"]
    watch = [r for r in reports if r.alert_level == "watch"]
    healthy = [r for r in reports if r.alert_level == "healthy"]

    # Totals
    total_daily_spend = sum(r.avg_daily_spend for r in reports)
    total_7d_spend = sum(r.total_spend for r in reports)
    total_revenue = sum(r.current_roas * r.avg_daily_spend for r in reports)
    blended_roas = total_revenue / total_daily_spend if total_daily_spend > 0 else 0

    blocks = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"📊 Ad Fatigue Report — {now}"}
    })

    # Overview
    overview_lines = [
        f"*{len(reports)}* active ads │ Avg daily spend: *{_format_currency(total_daily_spend)}* │ 7d total: *{_format_currency(total_7d_spend)}*",
        f"Blended ROAS: *{blended_roas:.1f}x*",
    ]
    status_parts = []
    if critical:
        status_parts.append(f"🔴 {len(critical)} critical")
    if warning:
        status_parts.append(f"🟡 {len(warning)} warning")
    if watch:
        status_parts.append(f"🔵 {len(watch)} watch")
    status_parts.append(f"✅ {len(healthy)} healthy")
    overview_lines.append(" │ ".join(status_parts))

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(overview_lines)}
    })

    # Timeframe context
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "_Trends compare last 3 days vs prior 4 days (7-day window). Slow-burn trends compare last 7 days vs prior 14 days (21-day window)._"}]
    })

    blocks.append({"type": "divider"})

    # Critical ads
    if critical:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔴 *CRITICAL FATIGUE — {len(critical)} ad{'s' if len(critical) != 1 else ''}*"}
        })
        for r in critical:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_ad_block(r)}
            })
        blocks.append({"type": "divider"})

    # Warning ads
    if warning:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🟡 *WARNING — {len(warning)} ad{'s' if len(warning) != 1 else ''}*"}
        })
        for r in warning:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_ad_block(r)}
            })
        blocks.append({"type": "divider"})

    # Watch - full breakdown per ad
    if watch:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔵 *WATCH — {len(watch)} ad{'s' if len(watch) != 1 else ''}*"}
        })
        for r in watch:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_watch_block(r)}
            })
        blocks.append({"type": "divider"})

    # Healthy - count with spend context
    if healthy:
        healthy_spend = sum(r.avg_daily_spend for r in healthy)
        healthy_revenue = sum(r.current_roas * r.avg_daily_spend for r in healthy)
        healthy_roas = healthy_revenue / healthy_spend if healthy_spend > 0 else 0
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"✅ *{len(healthy)} ads performing within baseline* │ {_format_currency(healthy_spend)}/day │ ROAS: {healthy_roas:.1f}x"}
        })

    # No fatigue detected
    if not critical and not warning and not watch:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *All ads performing within baseline — no fatigue detected.*"}
        })

    return {"blocks": blocks}


def build_roas_warning_message(reports: list[AdFatigueReport], config: Config) -> dict | None:
    """
    Build a separate ROAS warning message.
    Flags ads with 7d spend > threshold AND ROAS < threshold.
    Returns None if no ads match.
    """
    flagged = [
        r for r in reports
        if r.total_7d_spend >= config.roas_warning_min_spend_7d
        and r.roas_7d < config.roas_warning_threshold
        and r.roas_7d > 0  # Exclude ads with no purchases
        and "OFF" not in r.ad_name.upper()  # Skip ads already turned off
    ]

    if not flagged:
        return None

    # Sort by total spend descending (biggest waste at top)
    flagged.sort(key=lambda r: r.total_7d_spend, reverse=True)

    now = datetime.now().strftime("%a %d %b %Y")
    total_flagged_spend = sum(r.total_7d_spend for r in flagged)

    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"⚠️ Low ROAS Warning — {now}"}
    })

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*{len(flagged)} ads* with 7d spend >${config.roas_warning_min_spend_7d:.0f} "
            f"and ROAS below {config.roas_warning_threshold}x │ "
            f"Total 7d spend on flagged ads: *{_format_currency(total_flagged_spend)}*"
        )}
    })

    blocks.append({"type": "divider"})

    for r in flagged:
        cpa_display = f"CPA: {_format_currency(r.cpa_7d)}" if r.cpa_7d > 0 else "CPA: n/a"
        purchases_display = f"{r.total_7d_purchases} purchases" if r.total_7d_purchases > 0 else "0 purchases"

        # Find ROAS streak if available
        roas_streak = next((s for s in r.streaks if s.metric_name == "roas"), None)
        cpa_streak = next((s for s in r.streaks if s.metric_name == "cpa"), None)

        trend_parts = []
        if roas_streak and (roas_streak.consecutive_days >= 3 or roas_streak.days_bad_of_total >= 4):
            if roas_streak.consecutive_days >= 3:
                trend_parts.append(f"ROAS {roas_streak.direction} {roas_streak.consecutive_days} consecutive days ({roas_streak.first_value:.1f}x → {roas_streak.last_value:.1f}x)")
            else:
                trend_parts.append(f"ROAS {roas_streak.direction} {roas_streak.days_bad_of_total} of {roas_streak.total_days} days")
        if cpa_streak and cpa_streak.last_value > 0 and (cpa_streak.consecutive_days >= 3 or cpa_streak.days_bad_of_total >= 4):
            if cpa_streak.consecutive_days >= 3:
                trend_parts.append(f"CPA {cpa_streak.direction} {cpa_streak.consecutive_days} consecutive days ({_format_currency(cpa_streak.first_value)} → {_format_currency(cpa_streak.last_value)})")
            else:
                trend_parts.append(f"CPA {cpa_streak.direction} {cpa_streak.days_bad_of_total} of {cpa_streak.total_days} days")

        lines = [
            f"*{r.ad_name}*",
            f"Campaign: `{r.campaign_name}`",
            f"7d spend: *{_format_currency(r.total_7d_spend)}* │ {purchases_display} │ Revenue: {_format_currency(r.total_7d_revenue)}",
            f"*ROAS: {r.roas_7d:.2f}x* │ {cpa_display} │ CTR: {r.ctr_7d:.2f}% │ CPC: {_format_currency(r.cpc_7d)}",
        ]

        if trend_parts:
            lines.append(" │ ".join(trend_parts))

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })

    blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_Threshold: 7d spend >${config.roas_warning_min_spend_7d:.0f} with ROAS <{config.roas_warning_threshold}x. All metrics are 7-day weighted totals (matches Ads Manager)._"}]
    })

    return {"blocks": blocks}


def send_roas_warning(reports: list[AdFatigueReport], config: Config) -> bool:
    """Build and send the ROAS warning as a separate Slack message."""
    payload = build_roas_warning_message(reports, config)

    if payload is None:
        logger.info("No ads triggered ROAS warning — skipping")
        return True  # Not an error, just nothing to report

    try:
        resp = requests.post(
            config.slack_webhook_url,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("ROAS warning sent successfully")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send ROAS warning: {e}")
        return False


def build_testing_missed_opps_message(reports: list[TestingAdReport]) -> dict | None:
    """
    Build a Slack message for testing campaign paused ads with ATC metrics
    at or better than the testing campaign baseline.

    Caps display at top 20 by ATC rate to stay within Slack's block limit.
    """
    if not reports:
        return None

    # Slack limits messages to 50 blocks — cap display to keep room for header/divider/footer
    MAX_DISPLAY = 20

    now = datetime.now().strftime("%a %d %b %Y")
    baseline_atc_rate = reports[0].baseline_atc_rate
    baseline_cost_atc = reports[0].baseline_cost_per_atc
    total_spend = sum(r.total_spend for r in reports)

    # Show strongest signals first: both baselines met, then ATC rate above
    reports.sort(
        key=lambda r: (
            r.atc_rate_vs_baseline in ("at", "above") and r.cost_atc_vs_baseline in ("at", "below"),
            r.atc_rate,
        ),
        reverse=True,
    )

    displayed = reports[:MAX_DISPLAY]
    overflow = len(reports) - len(displayed)

    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🔍 Testing — Paused Ads with Strong ATC — {now}"}
    })

    summary_text = (
        f"*{len(reports)} paused ads* with ATC at or better than testing campaign baseline\n"
        f"Testing baseline (active ads): ATC rate *{baseline_atc_rate:.2f}%* │ Cost/ATC *{_format_currency(baseline_cost_atc)}*\n"
        f"Total 30d spend on flagged ads: *{_format_currency(total_spend)}*"
    )
    if overflow > 0:
        summary_text += f"\n_Showing top {MAX_DISPLAY} by ATC rate. {overflow} more flagged ads not shown._"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": summary_text}
    })

    blocks.append({"type": "divider"})

    for r in displayed:
        atc_rate_icon = "✅" if r.atc_rate_vs_baseline in ("at", "above") else "—"
        cost_atc_icon = "✅" if r.cost_atc_vs_baseline in ("at", "below") else "—"

        # Show purchases/ROAS if they exist
        purchase_text = f"{r.total_purchases} purchases"
        if r.total_purchases > 0:
            purchase_text += f" │ ROAS: {r.roas:.2f}x │ Revenue: {_format_currency(r.total_revenue)}"

        lines = [
            f"*{r.ad_name}*",
            f"Campaign: `{r.campaign_name}` │ Adset: `{r.adset_name}` │ Status: `{r.effective_status}`",
            f"30d spend: {_format_currency(r.total_spend)} │ {r.total_clicks} clicks │ {r.total_add_to_carts} ATCs │ {purchase_text}",
            f"{atc_rate_icon} ATC rate: *{r.atc_rate:.2f}%* (baseline: {baseline_atc_rate:.2f}%) │ {cost_atc_icon} Cost/ATC: *{_format_currency(r.cost_per_atc)}* (baseline: {_format_currency(baseline_cost_atc)})",
            f"CTR: {r.ctr:.2f}% │ CPC: {_format_currency(r.cpc)} │ {r.days_active} days of data",
        ]

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })

    blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "_These paused ads had ATC metrics at or better than your active testing ads. Based on 30-day window. Consider reactivating with more budget/time._"}]
    })

    return {"blocks": blocks}


def send_testing_missed_opps(reports: list[TestingAdReport], config: Config) -> bool:
    """Build and send the testing missed opportunities report."""
    payload = build_testing_missed_opps_message(reports)

    if payload is None:
        logger.info("No testing missed opportunities found — skipping")
        return True

    try:
        resp = requests.post(
            config.slack_webhook_url,
            json=payload,
            timeout=10,
        )
        if not resp.ok:
            logger.error(
                f"Slack rejected testing report: {resp.status_code} — "
                f"body: {resp.text[:500]} │ blocks: {len(payload.get('blocks', []))}"
            )
            return False
        logger.info(f"Testing missed opportunities report sent ({len(payload.get('blocks', []))} blocks)")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send testing missed opportunities report: {e}")
        return False


def build_early_fatigue_message(alerts: list[EarlyFatigueAd]) -> dict | None:
    """Build per-market Slack digest for early fatigue signals."""
    if not alerts:
        return None

    now = datetime.now().strftime("%a %d %b %Y")

    # Group by market, then by tier
    by_market: dict[str, dict[int, list]] = {}
    for a in alerts:
        if a.market not in by_market:
            by_market[a.market] = {1: [], 2: []}
        by_market[a.market][a.tier].append(a)

    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🔥 Early Fatigue Signals — {now}"}
    })

    total_watch = sum(1 for a in alerts if a.tier == 1)
    total_act = sum(1 for a in alerts if a.tier == 2)
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*{len(alerts)} ads* across *{len(by_market)} markets* showing fatigue signals\n"
            f"🟡 {total_watch} WATCH (leading) │ 🔴 {total_act} ACT (lagging)\n"
            f"_Baseline: prior 14 days │ Recent: last 3 days │ 2-day persistence required_"
        )}
    })

    blocks.append({"type": "divider"})

    # Cap total ads displayed to stay within Slack block limits
    ads_shown = 0
    MAX_ADS = 25

    for market in sorted(by_market.keys()):
        tiers = by_market[market]
        act_ads = tiers[2]
        watch_ads = tiers[1]

        if not act_ads and not watch_ads:
            continue

        # Market header
        market_summary_parts = []
        if act_ads:
            market_summary_parts.append(f"🔴 {len(act_ads)} ACT")
        if watch_ads:
            market_summary_parts.append(f"🟡 {len(watch_ads)} WATCH")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{market}* — {' │ '.join(market_summary_parts)}"}
        })

        # ACT ads first (more urgent)
        for a in act_ads:
            if ads_shown >= MAX_ADS:
                break
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_fatigue_ad(a)}
            })
            ads_shown += 1

        # Then WATCH ads
        for a in watch_ads:
            if ads_shown >= MAX_ADS:
                break
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_fatigue_ad(a)}
            })
            ads_shown += 1

        blocks.append({"type": "divider"})

    overflow = len(alerts) - ads_shown
    if overflow > 0:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more ads not shown_"}]
        })

    return {"blocks": blocks}


def _format_fatigue_ad(a) -> str:
    """Format a single fatigue alert ad for Slack."""
    tier_emoji = "🔴" if a.tier == 2 else "🟡"
    tier_label = "ACT" if a.tier == 2 else "WATCH"

    status_parts = []
    if a.is_escalation:
        status_parts.append("⬆️ ESCALATED")
    if a.is_repeat:
        status_parts.append("🔁 still fatiguing")
    if a.budget_adjusted:
        status_parts.append("💰 budget changed >50%")
    status_text = f" │ {' │ '.join(status_parts)}" if status_parts else ""

    breaching_text = " │ ".join(a.breaching_metrics)

    lines = [
        f"{tier_emoji} *{a.ad_name}* — {tier_label}{status_text}",
        f"Campaign: `{a.campaign_name}`",
        f"Spend: {_format_currency(a.recent_daily_spend)}/day (baseline: {_format_currency(a.baseline_daily_spend)}/day)",
        f"*14d baseline:* ROAS: {a.baseline_roas:.2f}x │ CPA: {_format_currency(a.baseline_cpa)} │ CTR: {a.baseline_ctr:.2f}% │ CPC: {_format_currency(a.baseline_cpc)}",
        f"*Last 3d:*     ROAS: {a.recent_roas:.2f}x │ CPA: {_format_currency(a.recent_cpa)} │ CTR: {a.recent_ctr:.2f}% │ CPC: {_format_currency(a.recent_cpc)}",
        f"Breaching: {breaching_text}",
    ]

    if a.days_since_first_flagged > 0:
        lines.append(f"First flagged {a.days_since_first_flagged} days ago")

    return "\n".join(lines)


def send_early_fatigue_report(alerts: list, config: Config) -> bool:
    """Send the early fatigue Slack digest."""
    payload = build_early_fatigue_message(alerts)

    if payload is None:
        logger.info("No early fatigue alerts — skipping")
        return True

    try:
        resp = requests.post(
            config.slack_webhook_url,
            json=payload,
            timeout=10,
        )
        if not resp.ok:
            logger.error(
                f"Slack rejected fatigue report: {resp.status_code} — "
                f"body: {resp.text[:500]} │ blocks: {len(payload.get('blocks', []))}"
            )
            return False
        logger.info(f"Early fatigue report sent ({len(payload.get('blocks', []))} blocks, {len(alerts)} alerts)")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send early fatigue report: {e}")
        return False


def send_slack_report(reports: list[AdFatigueReport], config: Config) -> bool:
    """Format and send the fatigue report to Slack."""
    payload = build_slack_message(reports)

    try:
        resp = requests.post(
            config.slack_webhook_url,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Slack report sent successfully")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send Slack report: {e}")
        return False
