"""
Slack reporter.
Formats fatigue analysis into a clean daily digest and sends via webhook.
"""

import logging
from datetime import datetime

import requests

from fatigue_analyzer import AdFatigueReport
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
    """Format a single ad's fatigue report for Slack."""
    role = ROLE_LABEL.get(report.role, report.role)

    # Find key signal changes for display
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    cpm_signal = next((s for s in report.signals if s.name == "cpm_inflation"), None)
    cpa_signal = next((s for s in report.signals if s.name == "cpa_inflation"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)
    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)

    cpa_display = f"CPA: {_format_currency(report.current_cpa)} {_trend_arrow(cpa_signal.pct_change if cpa_signal else 0)}" if report.current_cpa > 0 else "CPA: n/a"

    lines = [
        f"*{report.ad_name}*",
        f"Campaign: `{report.campaign_name}`",
        f"Role: {role} │ Score: {report.fatigue_score}/100",
        f"Spend: {_format_currency(report.avg_daily_spend)}/day ({report.spend_share_pct:.1f}% of total) │ 7d total: {_format_currency(report.total_spend)}",
        f"CTR: {report.current_ctr:.2f}% {_trend_arrow(ctr_signal.pct_change if ctr_signal else 0, invert=True)} │ CPC: {_format_currency(report.current_cpc)} {_trend_arrow(cpc_signal.pct_change if cpc_signal else 0)} │ CPM: {_format_currency(report.current_cpm)} {_trend_arrow(cpm_signal.pct_change if cpm_signal else 0)}",
        f"ROAS: {report.current_roas:.1f}x {_trend_arrow(roas_signal.pct_change if roas_signal else 0, invert=True)} │ {cpa_display} │ Freq: {report.current_frequency:.1f} {_trend_arrow(freq_signal.pct_change if freq_signal else 0)}",
    ]

    streak_line = _format_streak_line(report)
    if streak_line:
        lines.append(streak_line)

    lines.append(f"_{report.summary}_")

    if report.long_trend and report.long_trend.summary:
        lines.append(f"⚠️ _{report.long_trend.summary}_ (score: {report.long_trend.score}/100)")

    return "\n".join(lines)


def _format_watch_block(report: AdFatigueReport) -> str:
    """Format a Watch ad with full signal breakdown explaining WHY it's flagged."""
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    cpm_signal = next((s for s in report.signals if s.name == "cpm_inflation"), None)
    cpa_signal = next((s for s in report.signals if s.name == "cpa_inflation"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)
    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)
    share_signal = next((s for s in report.signals if s.name == "spend_share_decline"), None)

    # Build the "why" — list signals that are actually moving
    reasons = []
    for signal, label, invert in [
        (ctr_signal, "CTR", True),
        (cpc_signal, "CPC", False),
        (cpm_signal, "CPM", False),
        (cpa_signal, "CPA", False),
        (freq_signal, "Frequency", False),
        (roas_signal, "ROAS", True),
        (share_signal, "Spend share", True),
    ]:
        if signal and signal.raw_score > 10:
            direction = "down" if (signal.pct_change > 0) == invert else "up"
            reasons.append(f"{label} {direction} {abs(signal.pct_change):.0f}%")

    why_text = " │ ".join(reasons) if reasons else "Mild shifts across multiple signals"

    cpa_display = f"CPA: {_format_currency(report.current_cpa)}" if report.current_cpa > 0 else "CPA: n/a"

    lines = [
        f"• *{report.ad_name}*",
        f"  Campaign: `{report.campaign_name}` │ Score: {report.fatigue_score}/100",
        f"  Spend: {_format_currency(report.avg_daily_spend)}/day │ ROAS: {report.current_roas:.1f}x │ {cpa_display} │ CTR: {report.current_ctr:.2f}% │ CPC: {_format_currency(report.current_cpc)} │ Freq: {report.current_frequency:.1f}",
        f"  _Why (7d):_ {why_text}",
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
        if r.total_spend >= config.roas_warning_min_spend_7d
        and r.current_roas < config.roas_warning_threshold
        and r.current_roas > 0  # Exclude ads with no purchases
        and "OFF" not in r.ad_name.upper()  # Skip ads already turned off
    ]

    if not flagged:
        return None

    # Sort by total spend descending (biggest waste at top)
    flagged.sort(key=lambda r: r.total_spend, reverse=True)

    now = datetime.now().strftime("%a %d %b %Y")
    total_flagged_spend = sum(r.total_spend for r in flagged)

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
        cpa_display = f"CPA: {_format_currency(r.current_cpa)}" if r.current_cpa > 0 else "CPA: n/a"

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
            f"7d spend: *{_format_currency(r.total_spend)}* ({_format_currency(r.avg_daily_spend)}/day) │ *ROAS: {r.current_roas:.1f}x* │ {cpa_display}",
            f"CTR: {r.current_ctr:.2f}% │ CPC: {_format_currency(r.current_cpc)} │ Freq: {r.current_frequency:.1f}",
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
        "elements": [{"type": "mrkdwn", "text": f"_Threshold: 7d spend >${config.roas_warning_min_spend_7d:.0f} with ROAS <{config.roas_warning_threshold}x. ROAS based on last 3 days._"}]
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
