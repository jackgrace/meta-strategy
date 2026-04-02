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


def _format_ad_block(report: AdFatigueReport) -> str:
    """Format a single ad's fatigue report for Slack."""
    emoji = ALERT_EMOJI[report.alert_level]
    role = ROLE_LABEL.get(report.role, report.role)

    # Find key signal changes for display
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    cpm_signal = next((s for s in report.signals if s.name == "cpm_inflation"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)
    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)

    lines = [
        f"*{report.ad_name}*",
        f"Role: {role} │ Score: {report.fatigue_score}/100",
        f"Spend: {_format_currency(report.avg_daily_spend)}/day ({report.spend_share_pct:.1f}% of total) │ Freq: {report.current_frequency:.1f} {_trend_arrow(freq_signal.pct_change if freq_signal else 0)}",
        f"CTR: {report.current_ctr:.2f}% {_trend_arrow(ctr_signal.pct_change if ctr_signal else 0, invert=True)} │ CPC: {_format_currency(report.current_cpc)} {_trend_arrow(cpc_signal.pct_change if cpc_signal else 0)} │ ROAS: {report.current_roas:.1f}x {_trend_arrow(roas_signal.pct_change if roas_signal else 0, invert=True)}",
        f"_{report.summary}_",
    ]

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
    total_spend = sum(r.avg_daily_spend for r in reports)
    total_revenue = sum(r.current_roas * r.avg_daily_spend for r in reports)
    blended_roas = total_revenue / total_spend if total_spend > 0 else 0

    blocks = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"📊 Ad Fatigue Report — {now}"}
    })

    # Overview
    overview_parts = [
        f"*{len(reports)}* active ads │ ",
        f"Avg daily spend: *{_format_currency(total_spend)}* │ ",
        f"Blended ROAS: *{blended_roas:.1f}x*",
    ]
    if critical:
        overview_parts.append(f" │ 🔴 *{len(critical)} critical*")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "".join(overview_parts)}
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

    # Watch - collapsed summary
    if watch:
        watch_names = ", ".join(r.ad_name[:30] for r in watch[:5])
        suffix = f" +{len(watch) - 5} more" if len(watch) > 5 else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔵 *WATCH — {len(watch)} ads:* {watch_names}{suffix}"}
        })

    # Healthy - just a count
    if healthy:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"✅ *{len(healthy)} ads performing within baseline*"}
        })

    # No fatigue detected
    if not critical and not warning and not watch:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *All ads performing within baseline — no fatigue detected.*"}
        })

    return {"blocks": blocks}


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
