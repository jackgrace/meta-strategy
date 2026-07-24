"""
Midnight restart rules.

Runs daily at 12:05am AEST.

ADSET rule — turn ON adset IF:
- Parent campaign is ACTIVE
- Parent campaign had spend > $1 yesterday
- Campaign name contains CC, SCALE, or VALUE
- Adset name does NOT contain 'OFF'
- Adset is currently PAUSED

AD rule — turn ON ad IF:
- Parent campaign is ACTIVE
- Parent campaign had spend > $5 yesterday
- Ad name does NOT contain 'OFF'
- Ad is currently PAUSED
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

from meta_api import API_BASE
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

MIN_YESTERDAY_CAMPAIGN_SPEND = 1.0            # adset rule
MIN_YESTERDAY_CAMPAIGN_SPEND_ADS = 5.0        # ad rule
TARGET_KEYWORDS = {"CC", "SCALE", "VALUE"}    # adset rule only


@dataclass
class MidnightAction:
    adset_id: str
    adset_name: str
    campaign_name: str
    campaign_yesterday_spend: float
    action: str  # "would_activate" | "activated" | "skipped" | "failed"
    reason: str


@dataclass
class MidnightAdAction:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    campaign_yesterday_spend: float
    action: str
    reason: str


def _is_target_campaign(name: str) -> bool:
    parts = [p.strip() for p in name.upper().replace("|", " ").split()]
    return any(k in parts for k in TARGET_KEYWORDS)


def _http_get_json(config: Config, url: str, params: dict | None = None) -> dict:
    """GET with retry on transient/rate-limit errors."""
    for attempt in range(5):
        try:
            resp = requests.get(url, params=params, timeout=120)

            is_retryable_400 = False
            if resp.status_code == 400:
                try:
                    err = resp.json().get("error", {})
                    code = err.get("code")
                    subcode = err.get("error_subcode")
                    msg = (err.get("message", "") + " " + err.get("error_user_msg", "")).lower()
                    is_retryable_400 = (
                        err.get("is_transient") is True
                        or code in (1, 2, 4, 17, 32)
                        or subcode in (1504018, 1487742, 1504044)
                        or any(k in msg for k in ("temporarily", "limit reached", "too many", "try again", "load", "unavailable"))
                    )
                except Exception:
                    pass

            if (resp.status_code in (403, 500, 502, 503, 504) or is_retryable_400) and attempt < 4:
                wait = [30, 60, 120, 240][attempt]
                logger.warning(f"Midnight fetch {resp.status_code}, retrying in {wait}s: {resp.text[:200]}")
                time.sleep(wait)
                continue

            if not resp.ok:
                raise requests.exceptions.HTTPError(
                    f"Meta {resp.status_code}: {resp.text[:400]}",
                    response=resp,
                )
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 4:
                wait = [30, 60, 120, 240][attempt]
                logger.warning(f"Midnight fetch network error, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    return {}


def _fetch_yesterday_campaign_spend(config: Config) -> dict:
    """
    Return {campaign_id: {"name": str, "spend": float, "effective_status": str}}
    for all campaigns that had any activity yesterday.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "campaign",
        "fields": "campaign_id,campaign_name,spend",
        "date_preset": "yesterday",
        "limit": 200,
        "filtering": '[{"field":"impressions","operator":"GREATER_THAN","value":"0"}]',
    }

    result = {}
    page_count = 0
    while url:
        page_count += 1
        data = _http_get_json(config, url, params if page_count == 1 else None)
        for row in data.get("data", []):
            cid = row.get("campaign_id")
            if not cid:
                continue
            result[cid] = {
                "name": row.get("campaign_name", "Unknown"),
                "spend": float(row.get("spend", 0)),
            }
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched yesterday's spend for {len(result)} campaigns")
    return result


def _fetch_campaign_statuses(config: Config, campaign_ids: set[str]) -> dict:
    """Batch-fetch effective_status for a set of campaigns."""
    statuses: dict = {}
    if not campaign_ids:
        return statuses

    id_list = list(campaign_ids)
    for i in range(0, len(id_list), 50):
        batch = id_list[i:i + 50]
        url = f"{API_BASE}/"
        params = {
            "access_token": config.meta_access_token,
            "ids": ",".join(batch),
            "fields": "effective_status,name",
        }
        data = _http_get_json(config, url, params)
        for cid, cdata in data.items():
            statuses[cid] = {
                "effective_status": cdata.get("effective_status", "UNKNOWN"),
                "name": cdata.get("name", "Unknown"),
            }
    return statuses


def _fetch_paused_adsets(config: Config) -> list[dict]:
    """
    Fetch all PAUSED adsets in the account.
    Returns list of {id, name, campaign_id, effective_status}.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/adsets"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,name,campaign_id,effective_status",
        "limit": 500,
        # Only fetch adsets that are individually paused (not blocked by
        # parent state). Meta's effective_status for these is "PAUSED".
        "filtering": '[{"field":"effective_status","operator":"IN","value":["PAUSED"]}]',
    }

    result = []
    page_count = 0
    while url:
        page_count += 1
        data = _http_get_json(config, url, params if page_count == 1 else None)
        for row in data.get("data", []):
            result.append({
                "id": row.get("id"),
                "name": row.get("name", "Unknown"),
                "campaign_id": row.get("campaign_id"),
                "effective_status": row.get("effective_status", "UNKNOWN"),
            })
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched {len(result)} PAUSED adsets across {page_count} pages")
    return result


def _fetch_paused_ads(config: Config) -> list[dict]:
    """
    Fetch all PAUSED ads in the account.
    Returns list of {id, name, campaign_id, adset_name}.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/ads"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,name,campaign_id,adset{name},effective_status",
        "limit": 500,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["PAUSED"]}]',
    }

    result = []
    page_count = 0
    while url:
        page_count += 1
        data = _http_get_json(config, url, params if page_count == 1 else None)
        for row in data.get("data", []):
            adset = row.get("adset") or {}
            result.append({
                "id": row.get("id"),
                "name": row.get("name", "Unknown"),
                "campaign_id": row.get("campaign_id"),
                "adset_name": adset.get("name", "Unknown"),
                "effective_status": row.get("effective_status", "UNKNOWN"),
            })
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched {len(result)} PAUSED ads across {page_count} pages")
    return result


def _activate_adset(config: Config, adset_id: str) -> tuple[bool, str]:
    """POST status=ACTIVE. Returns (success, reason)."""
    url = f"{API_BASE}/{adset_id}"
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{url}?access_token={config.meta_access_token}",
                data={"status": "ACTIVE"},
                timeout=60,
            )
            if resp.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep([5, 15][attempt])
                continue
            if resp.ok:
                return True, "ok"
            try:
                err = resp.json().get("error", {})
                reason = err.get("error_user_title", err.get("message", "Unknown"))
            except Exception:
                reason = f"HTTP {resp.status_code}"
            return False, reason
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 2:
                time.sleep([5, 15][attempt])
            else:
                return False, str(e)[:100]
    return False, "no response"


def run_midnight_restart(config: Config, dry_run: bool = False) -> tuple[list[MidnightAction], list[MidnightAdAction]]:
    """Reactivate qualifying adsets that got paused during the day."""
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Midnight restart check [{mode}] ===")

    # Step 1: yesterday's spend per campaign
    campaign_spend = _fetch_yesterday_campaign_spend(config)

    # Step 2: filter to campaigns matching keywords + spend > $1 yesterday
    qualifying_campaign_ids: set[str] = set()
    for cid, cdata in campaign_spend.items():
        if cdata["spend"] > MIN_YESTERDAY_CAMPAIGN_SPEND and _is_target_campaign(cdata["name"]):
            qualifying_campaign_ids.add(cid)

    logger.info(
        f"{len(qualifying_campaign_ids)} campaigns qualify (CC/SCALE/VALUE + "
        f"spend > ${MIN_YESTERDAY_CAMPAIGN_SPEND:.0f} yesterday)"
    )

    if not qualifying_campaign_ids:
        return []

    # Step 3: confirm current campaign statuses (must be ACTIVE right now)
    campaign_statuses = _fetch_campaign_statuses(config, qualifying_campaign_ids)
    active_campaign_ids = {
        cid for cid, info in campaign_statuses.items()
        if info["effective_status"] == "ACTIVE"
    }
    logger.info(f"{len(active_campaign_ids)} of those campaigns are currently ACTIVE")

    if not active_campaign_ids:
        return []

    # Step 4: fetch PAUSED adsets, filter to qualifying campaigns and no OFF
    paused_adsets = _fetch_paused_adsets(config)
    actions: list[MidnightAction] = []
    activated = 0
    failed = 0
    skipped_off = 0
    skipped_wrong_campaign = 0

    for adset in paused_adsets:
        cid = adset["campaign_id"]
        if cid not in active_campaign_ids:
            skipped_wrong_campaign += 1
            continue

        if "OFF" in adset["name"].upper():
            skipped_off += 1
            continue

        campaign_name = campaign_statuses.get(cid, {}).get("name", "Unknown")
        yesterday_spend = campaign_spend.get(cid, {}).get("spend", 0)

        if dry_run:
            actions.append(MidnightAction(
                adset_id=adset["id"],
                adset_name=adset["name"],
                campaign_name=campaign_name,
                campaign_yesterday_spend=yesterday_spend,
                action="would_activate",
                reason="dry run",
            ))
            continue

        ok, reason = _activate_adset(config, adset["id"])
        if ok:
            activated += 1
            logger.info(f"MIDNIGHT RESTART: Activated {adset['id']} ({adset['name']}) — campaign {campaign_name} yesterday spend ${yesterday_spend:.2f}")
            actions.append(MidnightAction(
                adset_id=adset["id"],
                adset_name=adset["name"],
                campaign_name=campaign_name,
                campaign_yesterday_spend=yesterday_spend,
                action="activated",
                reason=reason,
            ))
        else:
            failed += 1
            logger.warning(f"MIDNIGHT RESTART: Failed to activate {adset['id']}: {reason}")
            actions.append(MidnightAction(
                adset_id=adset["id"],
                adset_name=adset["name"],
                campaign_name=campaign_name,
                campaign_yesterday_spend=yesterday_spend,
                action="failed",
                reason=reason,
            ))

    logger.info(
        f"Midnight ADSET restart complete: {activated} activated │ "
        f"{failed} failed │ {skipped_off} skipped OFF │ "
        f"{skipped_wrong_campaign} skipped (campaign not qualifying)"
    )

    # === AD-level midnight restart ===
    ad_actions = _run_ad_level_midnight(config, campaign_spend, dry_run)
    return actions, ad_actions


def _activate_ad(config: Config, ad_id: str) -> tuple[bool, str]:
    """POST status=ACTIVE on an ad. Returns (success, reason)."""
    url = f"{API_BASE}/{ad_id}"
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{url}?access_token={config.meta_access_token}",
                data={"status": "ACTIVE"},
                timeout=60,
            )
            if resp.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep([5, 15][attempt])
                continue
            if resp.ok:
                return True, "ok"
            try:
                err = resp.json().get("error", {})
                reason = err.get("error_user_title", err.get("message", "Unknown"))
            except Exception:
                reason = f"HTTP {resp.status_code}"
            return False, reason
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 2:
                time.sleep([5, 15][attempt])
            else:
                return False, str(e)[:100]
    return False, "no response"


def _run_ad_level_midnight(config: Config, campaign_spend: dict, dry_run: bool) -> list[MidnightAdAction]:
    """
    Ad-level midnight restart.

    Turn ON ad IF:
    - Parent campaign ACTIVE + had spend > $5 yesterday
    - Ad name does NOT contain 'OFF'
    - Ad currently PAUSED
    """
    # Which campaigns spent > $5 yesterday?
    qualifying_ids = {
        cid for cid, cdata in campaign_spend.items()
        if cdata["spend"] > MIN_YESTERDAY_CAMPAIGN_SPEND_ADS
    }
    logger.info(
        f"AD midnight: {len(qualifying_ids)} campaigns spent > ${MIN_YESTERDAY_CAMPAIGN_SPEND_ADS:.0f} yesterday"
    )
    if not qualifying_ids:
        return []

    # Confirm those campaigns are currently ACTIVE
    campaign_statuses = _fetch_campaign_statuses(config, qualifying_ids)
    active_ids = {
        cid for cid, info in campaign_statuses.items()
        if info["effective_status"] == "ACTIVE"
    }
    logger.info(f"AD midnight: {len(active_ids)} of those campaigns are currently ACTIVE")
    if not active_ids:
        return []

    paused_ads = _fetch_paused_ads(config)

    actions: list[MidnightAdAction] = []
    activated = 0
    failed = 0
    skipped_off = 0
    skipped_wrong_campaign = 0

    for ad in paused_ads:
        cid = ad["campaign_id"]
        if cid not in active_ids:
            skipped_wrong_campaign += 1
            continue

        if "OFF" in ad["name"].upper():
            skipped_off += 1
            continue

        campaign_name = campaign_statuses.get(cid, {}).get("name", "Unknown")
        yesterday_spend = campaign_spend.get(cid, {}).get("spend", 0)

        if dry_run:
            actions.append(MidnightAdAction(
                ad_id=ad["id"],
                ad_name=ad["name"],
                adset_name=ad["adset_name"],
                campaign_name=campaign_name,
                campaign_yesterday_spend=yesterday_spend,
                action="would_activate",
                reason="dry run",
            ))
            continue

        ok, reason = _activate_ad(config, ad["id"])
        if ok:
            activated += 1
            logger.info(f"MIDNIGHT AD RESTART: Activated {ad['id']} ({ad['name']}) — campaign {campaign_name} yesterday ${yesterday_spend:.2f}")
            actions.append(MidnightAdAction(
                ad_id=ad["id"],
                ad_name=ad["name"],
                adset_name=ad["adset_name"],
                campaign_name=campaign_name,
                campaign_yesterday_spend=yesterday_spend,
                action="activated",
                reason=reason,
            ))
        else:
            failed += 1
            logger.warning(f"MIDNIGHT AD RESTART: Failed to activate {ad['id']}: {reason}")
            actions.append(MidnightAdAction(
                ad_id=ad["id"],
                ad_name=ad["name"],
                adset_name=ad["adset_name"],
                campaign_name=campaign_name,
                campaign_yesterday_spend=yesterday_spend,
                action="failed",
                reason=reason,
            ))

    logger.info(
        f"Midnight AD restart complete: {activated} activated │ "
        f"{failed} failed │ {skipped_off} skipped OFF │ "
        f"{skipped_wrong_campaign} skipped (campaign not qualifying)"
    )
    return actions


def build_ad_midnight_slack_message(actions: list[MidnightAdAction], dry_run: bool) -> dict | None:
    if not actions:
        return None
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    activated = [a for a in actions if a.action in ("activated", "would_activate")]
    failed = [a for a in actions if a.action == "failed"]

    blocks = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🌅 Midnight Ad Restart — {now}"}
    })

    parts = []
    if activated:
        verb = "would activate" if dry_run else "activated"
        parts.append(f"🟢 {len(activated)} {verb}")
    if failed:
        parts.append(f"⚠️ {len(failed)} failed")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(parts) + "\n"
            f"_Rule: campaign ACTIVE & yesterday spend > "
            f"${MIN_YESTERDAY_CAMPAIGN_SPEND_ADS:.0f}; ad PAUSED & name does NOT contain 'OFF'_"
        )}
    })
    blocks.append({"type": "divider"})

    MAX_DISPLAY = 20

    def _fmt(a: MidnightAdAction, emoji: str) -> str:
        return (
            f"{emoji} *{a.ad_name}*\n"
            f"Campaign: `{a.campaign_name}` │ Adset: `{a.adset_name}` │ yesterday spend ${a.campaign_yesterday_spend:.2f}"
        )

    if activated:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "🟢 *Activated*"}})
        for a in activated[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fmt(a, "🟢")}})
        overflow = len(activated) - min(MAX_DISPLAY, len(activated))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    if failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⚠️ *Failed*"}})
        for a in failed[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fmt(a, "⚠️") + f"\n_{a.reason}_"}})

    return {"blocks": blocks}


def send_ad_midnight_report(actions: list[MidnightAdAction], dry_run: bool, config: Config) -> bool:
    payload = build_ad_midnight_slack_message(actions, dry_run)
    if payload is None:
        logger.info("No midnight ad restart actions — skipping Slack")
        return True
    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected midnight ad report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send midnight ad report: {e}")
        return False


def build_midnight_slack_message(actions: list[MidnightAction], dry_run: bool) -> dict | None:
    if not actions:
        return None

    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    activated = [a for a in actions if a.action in ("activated", "would_activate")]
    failed = [a for a in actions if a.action == "failed"]

    blocks = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🌅 Midnight Adset Restart — {now}"}
    })

    parts = []
    if activated:
        verb = "would activate" if dry_run else "activated"
        parts.append(f"🟢 {len(activated)} {verb}")
    if failed:
        parts.append(f"⚠️ {len(failed)} failed")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(parts) + "\n"
            f"_Rule: campaign ACTIVE & contains CC/SCALE/VALUE & yesterday spend > "
            f"${MIN_YESTERDAY_CAMPAIGN_SPEND:.0f}; adset PAUSED & name does NOT contain 'OFF'_"
        )}
    })
    blocks.append({"type": "divider"})

    MAX_DISPLAY = 20

    def _fmt(a: MidnightAction, emoji: str) -> str:
        return (
            f"{emoji} *{a.adset_name}*\n"
            f"Campaign: `{a.campaign_name}` │ yesterday spend ${a.campaign_yesterday_spend:.2f}"
        )

    if activated:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "🟢 *Activated*"}})
        for a in activated[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fmt(a, "🟢")}})
        overflow = len(activated) - min(MAX_DISPLAY, len(activated))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    if failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⚠️ *Failed*"}})
        for a in failed[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fmt(a, "⚠️") + f"\n_{a.reason}_"}})

    return {"blocks": blocks}


def send_midnight_report(actions: list[MidnightAction], dry_run: bool, config: Config) -> bool:
    payload = build_midnight_slack_message(actions, dry_run)
    if payload is None:
        logger.info("No midnight restart actions — skipping Slack")
        return True
    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected midnight report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send midnight report: {e}")
        return False
