"""
Auto-pause agent.

Finds ads that are older than 14 days with less than $20 spend in the
last 14 days, pauses them, and adds " - OFF" to the name.

Dry-run by default — set AUTO_PAUSE_ENABLED=true to go live.
"""

import logging
import os
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

from meta_api import AdDayMetrics, API_BASE
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

SPEND_THRESHOLD = 20.0
LOOKBACK_DAYS = 14
MIN_AD_AGE_DAYS = 14


@dataclass
class PauseCandidate:
    """An ad that qualifies for auto-pause."""
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    total_spend_14d: float
    days_with_data: int
    last_spend_date: str
    effective_status: str
    created_date: str
    ad_age_days: int
    action_taken: str  # "would_pause" (dry run) or "paused" (live)


def find_pause_candidates(
    all_metrics: list[AdDayMetrics],
    ad_statuses: dict[str, dict],
    config: Config,
) -> list[PauseCandidate]:
    """Find ads created >14 days ago with <$20 spend in last 14 days."""

    ads: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in all_metrics:
        ads[m.ad_id].append(m)

    cutoff = (datetime.now().date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    today = datetime.now().date()

    candidates = []

    for ad_id, all_days in ads.items():
        all_days.sort(key=lambda d: d.date)

        info = ad_statuses.get(ad_id, {})
        status = info.get("status", "UNKNOWN")
        created_time = info.get("created_time", "")

        # Skip already paused/off ads
        if status not in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES", "PENDING_REVIEW"):
            continue

        # Skip ads already with OFF in name
        ad_name = all_days[0].ad_name
        if "OFF" in ad_name.upper():
            continue

        # Check ad age: must be created > 14 days ago
        if not created_time:
            continue
        try:
            created_date = datetime.fromisoformat(created_time.replace("+0000", "+00:00")).date()
        except (ValueError, AttributeError):
            continue

        ad_age = (today - created_date).days
        if ad_age < MIN_AD_AGE_DAYS:
            continue

        # Check spend in last 14 days
        recent_days = [d for d in all_days if d.date >= cutoff]
        total_spend = sum(d.spend for d in recent_days) if recent_days else 0

        if total_spend < SPEND_THRESHOLD:
            last_spend_date = max((d.date for d in all_days if d.spend > 0), default="none")

            candidates.append(PauseCandidate(
                ad_id=ad_id,
                ad_name=ad_name,
                campaign_name=all_days[0].campaign_name,
                adset_name=all_days[0].adset_name,
                total_spend_14d=total_spend,
                days_with_data=len(recent_days),
                last_spend_date=last_spend_date,
                effective_status=status,
                created_date=created_date.isoformat(),
                ad_age_days=ad_age,
                action_taken="pending",
            ))

    candidates.sort(key=lambda c: c.total_spend_14d)

    logger.info(
        f"Auto-pause: {len(candidates)} ads older than {MIN_AD_AGE_DAYS}d "
        f"with <${SPEND_THRESHOLD} spend in {LOOKBACK_DAYS}d"
    )

    return candidates


def execute_pause(candidates: list[PauseCandidate], config: Config, dry_run: bool = True) -> list[PauseCandidate]:
    """
    Pause candidates and rename with OFF suffix.
    In dry_run mode, just marks them as would_pause.
    """
    for c in candidates:
        if dry_run:
            c.action_taken = "would_pause"
            continue

        new_name = f"{c.ad_name} - OFF"
        url = f"{API_BASE}/{c.ad_id}"
        params = {
            "access_token": config.meta_access_token,
            "status": "PAUSED",
            "name": new_name,
        }

        try:
            resp = requests.post(url, params=params, timeout=30)
            if resp.ok:
                c.action_taken = "paused"
                logger.info(f"Paused ad {c.ad_id} ({c.ad_name} → {new_name})")
            else:
                c.action_taken = f"failed: {resp.status_code}"
                logger.error(f"Failed to pause {c.ad_id}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            c.action_taken = f"error: {str(e)[:100]}"
            logger.error(f"Error pausing {c.ad_id}: {e}")

    return candidates


def build_pause_slack_message(candidates: list[PauseCandidate], dry_run: bool) -> dict | None:
    """Build Slack message showing pause candidates/results."""
    if not candidates:
        return None

    now = datetime.now(AEST).strftime("%a %d %b %Y")
    mode = "DRY RUN" if dry_run else "LIVE"
    total_spend = sum(c.total_spend_14d for c in candidates)

    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🔌 Auto-Pause {'Preview' if dry_run else 'Report'} — {now}"}
    })

    action_text = "would be paused" if dry_run else "have been paused"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* *{len(candidates)} ads* {action_text}\n"
            f"Rule: created >{MIN_AD_AGE_DAYS} days ago AND <${SPEND_THRESHOLD:.0f} spend in last {LOOKBACK_DAYS} days\n"
            f"Total 14d spend on flagged ads: *${total_spend:.2f}*"
        )}
    })

    blocks.append({"type": "divider"})

    MAX_DISPLAY = 30
    displayed = candidates[:MAX_DISPLAY]

    for c in displayed:
        status_emoji = "✅" if c.action_taken == "paused" else "👀" if c.action_taken == "would_pause" else "❌"

        lines = [
            f"{status_emoji} *{c.ad_name}*",
            f"Campaign: `{c.campaign_name}` │ Adset: `{c.adset_name}`",
            f"14d spend: ${c.total_spend_14d:.2f} │ Last spend: {c.last_spend_date} │ Created: {c.created_date} ({c.ad_age_days}d old)",
        ]

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })

    overflow = len(candidates) - len(displayed)
    if overflow > 0:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more ads not shown_"}]
        })

    blocks.append({"type": "divider"})

    if dry_run:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_This is a dry run — no ads were modified. Set AUTO_PAUSE_ENABLED=true in Railway to go live._"}]
        })

    return {"blocks": blocks}


def send_pause_report(candidates: list[PauseCandidate], dry_run: bool, config: Config) -> bool:
    """Send the pause report to Slack."""
    payload = build_pause_slack_message(candidates, dry_run)

    if payload is None:
        logger.info("No pause candidates found — skipping")
        return True

    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected pause report: {resp.status_code} — {resp.text[:300]}")
            return False
        logger.info(f"Pause report sent ({len(candidates)} candidates, {'dry run' if dry_run else 'live'})")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send pause report: {e}")
        return False
