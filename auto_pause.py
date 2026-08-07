"""
Auto-pause agent.

Finds ads that are older than 14 days with less than $20 spend in the
last 7 days, pauses them, and adds " - OFF" to the name.

Skips any ad whose ad name OR adset name contains 'RUN' — this is a
manual grace marker for freshly-reactivated adsets whose 7d spend is
dominated by an OFF period. Remove the RUN marker to re-enable
auto-pause consideration.

Dry-run by default — set AUTO_PAUSE_ENABLED=true to go live.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

from meta_api import API_BASE
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

SPEND_THRESHOLD = 15.0
LOOKBACK_DAYS = 7
MIN_AD_AGE_DAYS = 7


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


def _fetch_14d_spend(config: Config) -> dict[str, dict]:
    """
    Lightweight API call: fetch aggregate 14-day spend per ad.
    No daily breakdown — returns one row per ad with total spend.
    Much faster than the full insights fetch.
    """
    end_date = datetime.now().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=LOOKBACK_DAYS - 1)

    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_name,adset_name,spend",
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 500,
        "filtering": '[{"field":"ad.effective_status","operator":"IN","value":["ACTIVE","IN_REVIEW","WITH_ISSUES"]}]',
    }

    ad_spend: dict[str, dict] = {}
    page_count = 0

    while url:
        page_count += 1
        resp = requests.get(url, params=params if page_count == 1 else None, timeout=60)
        if not resp.ok:
            logger.error(f"Pause spend fetch error {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()

        data = resp.json()
        for row in data.get("data", []):
            ad_spend[row["ad_id"]] = {
                "spend": float(row.get("spend", 0)),
                "ad_name": row.get("ad_name", "Unknown"),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "adset_name": row.get("adset_name", "Unknown"),
            }

        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched 14d spend for {len(ad_spend)} ads across {page_count} pages")
    return ad_spend


def find_pause_candidates(
    ad_statuses: dict[str, dict],
    config: Config,
) -> list[PauseCandidate]:
    """Find ads created >14 days ago with <$20 spend in last 14 days."""

    # Lightweight fetch: aggregate 14d spend per ad (no daily breakdown)
    ad_spend = _fetch_14d_spend(config)

    today = datetime.now().date()
    candidates = []
    skipped_young = 0
    skipped_off = 0
    skipped_run = 0
    skipped_paused = 0
    skipped_campaign = 0

    # Check all ads we have status for
    all_ad_ids = set(ad_spend.keys()) | set(ad_statuses.keys())

    skipped_parent_off = 0

    for ad_id in all_ad_ids:
        info = ad_statuses.get(ad_id, {})
        status = info.get("status", "UNKNOWN")
        campaign_status = info.get("campaign_status", "UNKNOWN")
        adset_status = info.get("adset_status", "UNKNOWN")
        created_time = info.get("created_time", "")

        # Skip already paused/off ads
        if status not in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES", "PENDING_REVIEW"):
            skipped_paused += 1
            continue

        # Skip if parent campaign or adset isn't ACTIVE — Meta will reject
        # the pause call anyway, and the ad isn't delivering.
        if campaign_status != "ACTIVE" or adset_status != "ACTIVE":
            skipped_parent_off += 1
            continue

        spend_data = ad_spend.get(ad_id, {})
        # Prefer name from spend data (insights), fall back to /ads endpoint
        ad_name = spend_data.get("ad_name") or info.get("name", "Unknown")
        campaign_name = spend_data.get("campaign_name") or info.get("campaign_name", "Unknown")
        adset_name = spend_data.get("adset_name") or info.get("adset_name", "Unknown")

        # Only check target campaigns (CC, VALUE, or SCALE, whole word)
        campaign_parts = [p.strip() for p in campaign_name.upper().replace("|", " ").split()]
        if not any(k in campaign_parts for k in ("CC", "VALUE", "SCALE")):
            skipped_campaign += 1
            continue

        # Skip ads with OFF in name
        if "OFF" in ad_name.upper():
            skipped_off += 1
            continue

        # Skip if the ad or its adset carries a RUN marker — manual grace
        # after a reactivation so a just-restarted ad doesn't get killed on
        # stale 7d spend from the OFF period. User removes RUN when ready.
        if "RUN" in ad_name.upper() or "RUN" in adset_name.upper():
            skipped_run += 1
            continue

        # Check ad age
        if not created_time:
            continue
        try:
            created_date = datetime.fromisoformat(created_time.replace("+0000", "+00:00")).date()
        except (ValueError, AttributeError):
            continue

        ad_age = (today - created_date).days
        if ad_age < MIN_AD_AGE_DAYS:
            skipped_young += 1
            continue

        total_spend = spend_data.get("spend", 0)

        if total_spend < SPEND_THRESHOLD:
            candidates.append(PauseCandidate(
                ad_id=ad_id,
                ad_name=ad_name,
                campaign_name=campaign_name,
                adset_name=adset_name,
                total_spend_14d=total_spend,
                days_with_data=0,
                last_spend_date="—",
                effective_status=status,
                created_date=created_date.isoformat(),
                ad_age_days=ad_age,
                action_taken="pending",
            ))

    candidates.sort(key=lambda c: c.total_spend_14d)

    logger.info(
        f"Auto-pause: {len(all_ad_ids)} ads checked │ "
        f"{skipped_paused} already paused │ {skipped_parent_off} parent off │ "
        f"{skipped_campaign} not CC/VALUE/SCALE │ {skipped_off} already OFF │ "
        f"{skipped_run} RUN marker │ "
        f"{skipped_young} too young (<{MIN_AD_AGE_DAYS}d) │ "
        f"{len(candidates)} candidates (<${SPEND_THRESHOLD} in {LOOKBACK_DAYS}d)"
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

        url = f"{API_BASE}/{c.ad_id}"

        try:
            # Step 1: pause
            resp = requests.post(
                f"{url}?access_token={config.meta_access_token}",
                data={"status": "PAUSED"},
                timeout=30,
            )
            if not resp.ok:
                try:
                    err = resp.json().get("error", {})
                    reason = err.get("error_user_title", err.get("message", "Unknown error"))
                except Exception:
                    reason = f"HTTP {resp.status_code}"
                c.action_taken = f"skipped: {reason}"
                logger.warning(f"Could not pause {c.ad_id}: {reason}")
                continue

            # Step 2: rename with OFF suffix (separate call to avoid creative validation)
            new_name = f"{c.ad_name} - OFF"
            try:
                rename_resp = requests.post(
                    f"{url}?access_token={config.meta_access_token}",
                    data={"name": new_name},
                    timeout=30,
                )
                if rename_resp.ok:
                    c.action_taken = "paused"
                    logger.info(f"Paused ad {c.ad_id} ({c.ad_name} → {new_name})")
                else:
                    c.action_taken = "paused (rename failed)"
                    logger.warning(f"Paused {c.ad_id} but rename failed: {rename_resp.text[:200]}")
            except Exception as e:
                c.action_taken = "paused (rename failed)"
                logger.warning(f"Paused {c.ad_id} but rename raised: {e}")
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
            f"Rule: CC/VALUE/SCALE campaigns │ created >{MIN_AD_AGE_DAYS} days ago │ <${SPEND_THRESHOLD:.0f} spend in last {LOOKBACK_DAYS} days │ ad+adset name!contains RUN\n"
            f"Total 14d spend on flagged ads: *${total_spend:.2f}*"
        )}
    })

    blocks.append({"type": "divider"})

    # Slack has a 50-block limit — cap tight to leave room for headers/dividers
    MAX_DISPLAY = 15
    displayed = candidates[:MAX_DISPLAY]

    # Split into paused vs needs manual action
    # "paused (rename failed)" counts as paused since the status change succeeded
    paused = [c for c in candidates if c.action_taken in ("paused", "paused (rename failed)")]
    manual = [c for c in candidates if c.action_taken.startswith("skipped:")]
    would_pause = [c for c in candidates if c.action_taken == "would_pause"]

    if not dry_run and manual:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f"✅ *{len(paused)} paused* via API │ "
                f"⚠️ *{len(manual)} need manual pause* (Meta API won't update these)"
            )}
        })
        blocks.append({"type": "divider"})

    if manual and not dry_run:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "⚠️ *Manual pause needed:*"}
        })
        for c in manual[:MAX_DISPLAY]:
            # Strip "skipped: " prefix to just show Meta's error reason
            reason = c.action_taken.replace("skipped: ", "", 1) if c.action_taken.startswith("skipped:") else c.action_taken
            lines = [
                f"• *{c.ad_name}*",
                f"  Campaign: `{c.campaign_name}` │ Adset: `{c.adset_name}`",
                f"  14d spend: ${c.total_spend_14d:.2f} │ Created: {c.created_date} ({c.ad_age_days}d old)",
                f"  _Reason: {reason}_",
            ]
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)}
            })
        blocks.append({"type": "divider"})

    if paused:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *Paused via API:*"}
        })

    show_list = would_pause if dry_run else paused
    for c in show_list[:MAX_DISPLAY]:
        if c.action_taken == "paused":
            status_emoji = "✅"
        elif c.action_taken == "would_pause":
            status_emoji = "👀"
        elif c.action_taken.startswith("skipped:"):
            status_emoji = "⚠️"
        else:
            status_emoji = "❌"

        lines = [
            f"{status_emoji} *{c.ad_name}*",
            f"Campaign: `{c.campaign_name}` │ Adset: `{c.adset_name}`",
            f"14d spend: ${c.total_spend_14d:.2f} │ Last spend: {c.last_spend_date} │ Created: {c.created_date} ({c.ad_age_days}d old)",
        ]

        if c.action_taken.startswith("skipped:"):
            lines.append(f"_{c.action_taken}_")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })

    show_list_len = len(would_pause) if dry_run else len(paused)
    overflow = show_list_len - min(MAX_DISPLAY, show_list_len)
    if overflow > 0:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more paused ads not shown_"}]
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
