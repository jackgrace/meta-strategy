"""
Intra-day stop-loss.

Runs every 15 min during trading hours.

STOP-LOSS (turn OFF):
- Campaign name contains 'CC'
- Ad is currently ACTIVE
- Today's ad spend > $100
- Today's ad ROAS < 1.6
- Today's adset ROAS < 1.6

RESTART (turn ON):
- Campaign name contains 'CC'
- Ad is currently PAUSED (with " - OFF" marker)
- Today's ad spend > $1
- Today's ad ROAS > 1.6
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests

from meta_api import API_BASE, fetch_ad_statuses
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

STOP_SPEND_THRESHOLD = 100.0
STOP_ROAS_THRESHOLD = 1.6
STOP_ADSET_ROAS_THRESHOLD = 1.6

RESTART_SPEND_THRESHOLD = 1.0
RESTART_ROAS_THRESHOLD = 1.6


@dataclass
class StopLossAction:
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    action: str  # "would_pause" | "paused" | "would_activate" | "activated" | "failed"
    reason: str
    spend: float
    roas: float
    revenue: float
    purchases: int
    adset_spend: float
    adset_roas: float


def _fetch_today_metrics(config: Config) -> dict:
    """Fetch today's ad-level metrics for all ads (in AEST timezone)."""
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_name,adset_id,adset_name,spend,actions,action_values",
        "date_preset": "today",
        "limit": 500,
    }

    ads = {}
    page_count = 0

    while url:
        page_count += 1
        resp = requests.get(url, params=params if page_count == 1 else None, timeout=60)
        if not resp.ok:
            logger.error(f"Stop-loss metrics fetch error {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()

        data = resp.json()
        for row in data.get("data", []):
            spend = float(row.get("spend", 0))
            revenue = 0.0
            purchases = 0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                if a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))

            ads[row["ad_id"]] = {
                "ad_name": row.get("ad_name", "Unknown"),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "purchases": purchases,
                "roas": revenue / spend if spend > 0 else 0,
            }

        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched today's metrics for {len(ads)} ads across {page_count} pages")
    return ads


def _compute_adset_roas(ads: dict) -> dict:
    """Aggregate ROAS by adset_id from ad-level data."""
    adsets = defaultdict(lambda: {"spend": 0, "revenue": 0})
    for ad in ads.values():
        if ad["adset_id"]:
            adsets[ad["adset_id"]]["spend"] += ad["spend"]
            adsets[ad["adset_id"]]["revenue"] += ad["revenue"]

    return {
        adset_id: {
            "spend": v["spend"],
            "revenue": v["revenue"],
            "roas": v["revenue"] / v["spend"] if v["spend"] > 0 else 0,
        }
        for adset_id, v in adsets.items()
    }


def _update_ad_status(config: Config, ad_id: str, new_status: str) -> tuple[bool, str]:
    """Send status update to Meta. Returns (success, reason_or_message)."""
    url = f"{API_BASE}/{ad_id}"
    try:
        resp = requests.post(
            f"{url}?access_token={config.meta_access_token}",
            data={"status": new_status},
            timeout=30,
        )
        if resp.ok:
            return True, "ok"
        try:
            err = resp.json().get("error", {})
            reason = err.get("error_user_title", err.get("message", "Unknown"))
        except Exception:
            reason = f"HTTP {resp.status_code}"
        return False, reason
    except Exception as e:
        return False, str(e)[:100]


def _is_cc_campaign(campaign_name: str) -> bool:
    """Check if campaign name contains 'CC' as a whole word."""
    parts = [p.strip() for p in campaign_name.upper().replace("|", " ").split()]
    return "CC" in parts


def run_stop_loss(config: Config, dry_run: bool = False) -> list[StopLossAction]:
    """
    Run intra-day stop-loss and restart checks.
    Returns list of actions taken.
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Stop-loss check [{mode}] ===")

    # Fetch today's metrics
    today_ads = _fetch_today_metrics(config)
    if not today_ads:
        logger.info("No ad data for today")
        return []

    # Compute adset ROAS
    adset_roas = _compute_adset_roas(today_ads)

    # Fetch statuses for ads we have data for
    ad_ids = set(today_ads.keys())
    try:
        ad_info = fetch_ad_statuses(config, ad_ids=ad_ids)
    except Exception as e:
        logger.error(f"Status fetch failed: {e}")
        return []

    actions: list[StopLossAction] = []
    stop_count = 0
    restart_count = 0
    fail_count = 0

    for ad_id, ad in today_ads.items():
        # Only CC campaigns
        if not _is_cc_campaign(ad["campaign_name"]):
            continue

        info = ad_info.get(ad_id, {})
        status = info.get("status", "UNKNOWN")
        current_name = info.get("name", ad["ad_name"])

        adset_data = adset_roas.get(ad["adset_id"], {})
        adset_spend = adset_data.get("spend", 0)
        adset_r = adset_data.get("roas", 0)

        # STOP-LOSS: ACTIVE + breaches thresholds
        if (status == "ACTIVE"
            and ad["spend"] > STOP_SPEND_THRESHOLD
            and ad["roas"] < STOP_ROAS_THRESHOLD
            and adset_r < STOP_ADSET_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, ad_id, "PAUSED")
                if success:
                    action = "paused"
                    stop_count += 1
                    logger.info(f"STOP-LOSS: Paused {ad_id} ({ad['ad_name']}) — spend ${ad['spend']:.2f}, ROAS {ad['roas']:.2f}, adset ROAS {adset_r:.2f}")
                else:
                    action = "failed"
                    fail_count += 1
                    logger.warning(f"STOP-LOSS: Failed to pause {ad_id}: {reason}")

            actions.append(StopLossAction(
                ad_id=ad_id,
                ad_name=ad["ad_name"],
                campaign_name=ad["campaign_name"],
                adset_name=ad["adset_name"],
                action=action,
                reason=reason,
                spend=ad["spend"],
                roas=ad["roas"],
                revenue=ad["revenue"],
                purchases=ad["purchases"],
                adset_spend=adset_spend,
                adset_roas=adset_r,
            ))
            continue

        # RESTART: currently PAUSED (with OFF marker) + passes thresholds
        is_off_ad = "OFF" in current_name.upper()
        if (status != "ACTIVE" and is_off_ad
            and ad["spend"] > RESTART_SPEND_THRESHOLD
            and ad["roas"] > RESTART_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, ad_id, "ACTIVE")
                if success:
                    action = "activated"
                    restart_count += 1
                    logger.info(f"RESTART: Activated {ad_id} ({ad['ad_name']}) — spend ${ad['spend']:.2f}, ROAS {ad['roas']:.2f}")
                else:
                    action = "failed"
                    fail_count += 1
                    logger.warning(f"RESTART: Failed to activate {ad_id}: {reason}")

            actions.append(StopLossAction(
                ad_id=ad_id,
                ad_name=ad["ad_name"],
                campaign_name=ad["campaign_name"],
                adset_name=ad["adset_name"],
                action=action,
                reason=reason,
                spend=ad["spend"],
                roas=ad["roas"],
                revenue=ad["revenue"],
                purchases=ad["purchases"],
                adset_spend=adset_spend,
                adset_roas=adset_r,
            ))

    logger.info(f"Stop-loss complete: {stop_count} paused, {restart_count} activated, {fail_count} failed")
    return actions


def build_stop_loss_slack_message(actions: list[StopLossAction], dry_run: bool) -> dict | None:
    """
    Build the intra-day Slack message.
    If no actions, still send a compact status update so you know it ran.
    """
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    paused = [a for a in actions if a.action in ("paused", "would_pause")]
    activated = [a for a in actions if a.action in ("activated", "would_activate")]
    failed = [a for a in actions if a.action == "failed"]

    # No actions taken → quiet check-in (only send if failures)
    if not paused and not activated and not failed:
        return None

    blocks = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"⛔ Stop-Loss Report — {now}"}
    })

    summary_parts = []
    if paused:
        summary_parts.append(f"🔴 {len(paused)} paused")
    if activated:
        summary_parts.append(f"🟢 {len(activated)} activated")
    if failed:
        summary_parts.append(f"⚠️ {len(failed)} failed")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(summary_parts) + "\n"
            f"_Rule: CC campaigns │ Today's metrics │ "
            f"Stop: spend>${STOP_SPEND_THRESHOLD:.0f} & ROAS<{STOP_ROAS_THRESHOLD} & adset ROAS<{STOP_ADSET_ROAS_THRESHOLD} │ "
            f"Restart: spend>${RESTART_SPEND_THRESHOLD:.0f} & ROAS>{RESTART_ROAS_THRESHOLD}_"
        )}
    })

    blocks.append({"type": "divider"})

    MAX_DISPLAY = 15

    def _format_action(a: StopLossAction, emoji: str) -> str:
        cpa_line = ""
        if a.purchases > 0:
            cpa = a.spend / a.purchases
            cpa_line = f" │ CPA: ${cpa:.2f}"
        return (
            f"{emoji} *{a.ad_name}*\n"
            f"Campaign: `{a.campaign_name}` │ Adset: `{a.adset_name}`\n"
            f"Ad: spend ${a.spend:.2f} │ ROAS {a.roas:.2f}x │ Rev ${a.revenue:.2f} │ {a.purchases} purchases{cpa_line}\n"
            f"Adset: spend ${a.adset_spend:.2f} │ ROAS {a.adset_roas:.2f}x"
        )

    if paused:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "🔴 *Paused (stop-loss triggered)*"}})
        for a in paused[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _format_action(a, "🔴")}})
        overflow = len(paused) - min(MAX_DISPLAY, len(paused))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    if activated:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "🟢 *Activated (restart triggered)*"}})
        for a in activated[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _format_action(a, "🟢")}})
        overflow = len(activated) - min(MAX_DISPLAY, len(activated))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    if failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⚠️ *Failed (needs manual action)*"}})
        for a in failed[:MAX_DISPLAY]:
            lines = _format_action(a, "⚠️") + f"\n_{a.reason}_"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines}})

    return {"blocks": blocks}


def send_stop_loss_report(actions: list[StopLossAction], dry_run: bool, config: Config) -> bool:
    """Send stop-loss report to Slack. Quiet if nothing happened."""
    payload = build_stop_loss_slack_message(actions, dry_run)
    if payload is None:
        return True

    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected stop-loss report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send stop-loss report: {e}")
        return False
