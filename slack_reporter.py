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
    role = ROLE_LABEL.get(report.role, report.role)

    # Find key signal changes for display
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    cpm_signal = next((s for s in report.signals if s.name == "cpm_inflation"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)
    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)

    lines = [
        f"*{report.ad_name}*",
        f"Campaign: `{report.campaign_name}`",
        f"Role: {role} │ Score: {report.fatigue_score}/100",
        f"Spend: {_format_currency(report.avg_daily_spend)}/day ({report.spend_share_pct:.1f}% of total) │ 7d total: {_format_currency(report.total_spend)}",
        f"CTR: {report.current_ctr:.2f}% {_trend_arrow(ctr_signal.pct_change if ctr_signal else 0, invert=True)} │ CPC: {_format_currency(report.current_cpc)} {_trend_arrow(cpc_signal.pct_change if cpc_signal else 0)} │ CPM: {_format_currency(report.current_cpm)} {_trend_arrow(cpm_signal.pct_change if cpm_signal else 0)}",
        f"ROAS: {report.current_roas:.1f}x {_trend_arrow(roas_signal.pct_change if roas_signal else 0, invert=True)} │ Freq: {report.current_frequency:.1f} {_trend_arrow(freq_signal.pct_change if freq_signal else 0)}",
        f"_{report.summary}_",
    ]

    return "\n".join(lines)


def _format_watch_block(report: AdFatigueReport) -> str:
    """Format a Watch ad with full signal breakdown explaining WHY it's flagged."""
    ctr_signal = next((s for s in report.signals if s.name == "ctr_decay"), None)
    cpc_signal = next((s for s in report.signals if s.name == "cpc_inflation"), None)
    cpm_signal = next((s for s in report.signals if s.name == "cpm_inflation"), None)
    freq_signal = next((s for s in report.signals if s.name == "frequency_climb"), None)
    roas_signal = next((s for s in report.signals if s.name == "roas_decay"), None)
    share_signal = next((s for s in report.signals if s.name == "spend_share_decline"), None)

    # Build the "why" — list signals that are actually moving
    reasons = []
    for signal, label, invert in [
        (ctr_signal, "CTR", True),
        (cpc_signal, "CPC", False),
        (cpm_signal, "CPM", False),
        (freq_signal, "Frequency", False),
        (roas_signal, "ROAS", True),
        (share_signal, "Spend share", True),
    ]:
        if signal and signal.raw_score > 10:
            direction = "down" if (signal.pct_change > 0) == invert else "up"
            reasons.append(f"{label} {direction} {abs(signal.pct_change):.0f}%")

    why_text = " │ ".join(reasons) if reasons else "Mild shifts across multiple signals"

    lines = [
        f"• *{report.ad_name}*",
        f"  Campaign: `{report.campaign_name}` │ Score: {report.fatigue_score}/100",
        f"  Spend: {_format_currency(report.avg_daily_spend)}/day │ ROAS: {report.current_roas:.1f}x │ CTR: {report.current_ctr:.2f}% │ CPC: {_format_currency(report.current_cpc)} │ Freq: {report.current_frequency:.1f}",
        f"  _Why:_ {why_text}",
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
