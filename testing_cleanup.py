"""
TESTING ad clean-up. Runs daily at 12:05am AEST, before the midnight restart.

Looks at every ACTIVE ad in a campaign whose name contains TESTING, created
more than 7 days ago, over the last 7 complete days (same as Ads Manager's
"Last 7 days", today excluded):

  1. 7d spend > $175 & 7d ROAS < 1.0  -> pause + rename "<name> - OFF LOW ROAS"
  2. 7d spend < $10                    -> pause + rename "<name> - OFF LOW SPEND"

SCALE campaigns (SCALE as a word in the name, incl. SCALE | CBO) get rule 2
(LOW SPEND) only.

Ads with no delivery in the window count as $0 spend. Skips ads / adsets
with OFF or RUN in the name. OFF keeps them off at midnight.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from config import Config
from meta_api import API_BASE
from midnight_restart import _http_get_json
from scale_retire import _fetch_ads_7d, _rename
from stop_loss import _is_scale_campaign, _is_testing_campaign, _update_ad_status

logger = logging.getLogger(__name__)

TESTING_CLEANUP_ENABLED = True
SCALE_LOW_SPEND_ENABLED = True
MIN_AD_AGE_DAYS = 7
LOW_ROAS_ENABLED = False      # OFF LOW ROAS paused; OFF LOW SPEND still runs
LOW_ROAS_SPEND = 175.0
LOW_ROAS = 1.0
LOW_SPEND = 10.0


@dataclass
class CleanupAction:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    spend_7d: float
    roas_7d: float
    label: str      # "LOW ROAS" | "LOW SPEND"
    action: str     # would_retire | retired | paused (rename failed) | failed
    reason: str = ""


def _group(campaign_name: str) -> str | None:
    if _is_testing_campaign(campaign_name):
        return "testing" if TESTING_CLEANUP_ENABLED else None
    if _is_scale_campaign(campaign_name):
        return "scale" if SCALE_LOW_SPEND_ENABLED else None
    return None


def _fetch_active_ads(config: Config) -> list[dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/ads"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,name,created_time,campaign{name},adset{name}",
        "limit": 500,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
    }
    ads = []
    first = True
    while url:
        data = _http_get_json(config, url, params if first else None)
        first = False
        for row in data.get("data", []):
            campaign = (row.get("campaign") or {}).get("name", "")
            group = _group(campaign)
            if not group:
                continue
            ads.append({
                "group": group,
                "id": row["id"],
                "name": row.get("name", "Unknown"),
                "created_time": row.get("created_time", ""),
                "campaign_name": campaign,
                "adset_name": (row.get("adset") or {}).get("name", "Unknown"),
            })
        url = data.get("paging", {}).get("next")
    logger.info(f"Clean-up: {len(ads)} active TESTING/SCALE ads")
    return ads


def run_testing_cleanup(config: Config, dry_run: bool = False) -> list[CleanupAction]:
    if not (TESTING_CLEANUP_ENABLED or SCALE_LOW_SPEND_ENABLED):
        logger.info("Clean-up: DISABLED — skipping")
        return []

    active = _fetch_active_ads(config)
    if not active:
        return []
    metrics: dict[str, dict] = {}
    # Upper and title case in case Meta's CONTAIN filter is case-sensitive.
    if any(a["group"] == "testing" for a in active):
        for kw in ("TESTING", "Testing"):
            metrics.update(_fetch_ads_7d(config, kw, _is_testing_campaign))
    if any(a["group"] == "scale" for a in active):
        for kw in ("SCALE", "Scale"):
            metrics.update(_fetch_ads_7d(config, kw, _is_scale_campaign))

    now = datetime.now(timezone.utc)
    actions: list[CleanupAction] = []
    for ad in active:
        upper = (ad["name"] + " " + ad["adset_name"]).upper()
        if "OFF" in upper or "RUN" in upper:
            continue
        try:
            created = datetime.fromisoformat(ad["created_time"])
        except ValueError:
            continue
        if now - created < timedelta(days=MIN_AD_AGE_DAYS):
            continue

        m = metrics.get(ad["id"], {})
        spend = m.get("spend", 0.0)
        roas = m.get("revenue", 0.0) / spend if spend else 0.0
        if LOW_ROAS_ENABLED and ad["group"] == "testing" and spend > LOW_ROAS_SPEND and roas < LOW_ROAS:
            label, why = "LOW ROAS", f"7d spend ${spend:.2f} > ${LOW_ROAS_SPEND:.0f} & ROAS {roas:.2f} < {LOW_ROAS}"
        elif spend < LOW_SPEND:
            label, why = "LOW SPEND", f"7d spend ${spend:.2f} < ${LOW_SPEND:.0f}"
        else:
            continue

        act = CleanupAction(ad["id"], ad["name"], ad["adset_name"], ad["campaign_name"],
                            spend, roas, label, "would_retire", why)
        if not dry_run:
            ok, err = _update_ad_status(config, ad["id"], "PAUSED")
            if not ok:
                act.action, act.reason = "failed", f"{why} — pause failed: {err}"
            else:
                renamed, rerr = _rename(config, ad["id"], f"{ad['name']} - OFF {label}")
                if renamed:
                    act.action = "retired"
                else:
                    act.action, act.reason = "paused (rename failed)", f"{why} — rename failed: {rerr}"
        logger.info(f"Testing clean-up: {act.action} {ad['id']} ({ad['name']}) — {act.reason}")
        actions.append(act)
    return actions


def send_testing_cleanup_report(actions: list[CleanupAction], dry_run: bool, config: Config) -> bool:
    if not actions:
        return True
    mode = "DRY RUN" if dry_run else "LIVE"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🧹 Ad clean-up — {len(actions)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* Ads older than {MIN_AD_AGE_DAYS} days, last 7 days. TESTING: "
            f"spend>${LOW_ROAS_SPEND:.0f} & ROAS<{LOW_ROAS} → OFF LOW ROAS │ TESTING + SCALE: spend<${LOW_SPEND:.0f} → OFF LOW SPEND. "
            f"Remove OFF from the name to bring one back."
        )}]},
    ]
    for label in ("LOW ROAS", "LOW SPEND"):
        group = [a for a in actions if a.label == label]
        if not group:
            continue
        lines = []
        for a in group[:25]:
            note = "" if a.action in ("retired", "would_retire") else f" *({a.action}: {a.reason})*"
            lines.append(f"• *{a.ad_name}* — `{a.campaign_name}` / `{a.adset_name}` │ 7d ${a.spend_7d:,.2f} @ {a.roas_7d:.2f}x{note}")
        if len(group) > 25:
            lines.append(f"_+{len(group) - 25} more_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*OFF {label}*\n" + "\n".join(lines)}})
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected testing clean-up report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send testing clean-up report: {e}")
        return False
