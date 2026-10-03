"""
SCALE + TESTING ad 7-day retire. Runs every 15 minutes alongside the stop-loss.
Retired ads are marked OFF, so midnight restart leaves them off.

Rule (last 7 complete days), campaigns with SCALE (incl. SCALE | CBO) or TESTING
in the name, each flag-controlled:
- ad 7d spend > $150 (SCALE) / $100 (TESTING) AND ad 7d ROAS < 1.2 AND adset 7d ROAS < 1.5
- ad created at least 3 days ago
- ad and adset names don't contain OFF or RUN
- also retires the last active ad in an adset (its numbers are the adset's)
→ pause ad + append " - OFF" (midnight restart skips it).
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import requests

from config import Config
from meta_api import API_BASE, fetch_ad_statuses
from stop_loss import _is_scale_campaign, _is_testing_campaign, _update_ad_status

logger = logging.getLogger(__name__)

SCALE_RETIRE_ENABLED = True
TESTING_RETIRE_ADS_ENABLED = True  # same rule applied to TESTING campaigns
RETIRE_SPEND_THRESHOLD = 150.0          # SCALE
TESTING_RETIRE_SPEND_THRESHOLD = 100.0  # TESTING
RETIRE_ROAS_THRESHOLD = 1.2
RETIRE_ADSET_ROAS_THRESHOLD = 1.5
MIN_AD_AGE_DAYS = 3


def _rename(config: Config, object_id: str, new_name: str) -> tuple[bool, str]:
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{API_BASE}/{object_id}?access_token={config.meta_access_token}",
                data={"name": new_name},
                timeout=30,
            )
        except requests.RequestException as e:
            err = str(e)[:200]
        else:
            if resp.ok:
                return True, ""
            try:
                e = resp.json().get("error", {})
                err = e.get("error_user_msg") or e.get("error_user_title") or e.get("message") or resp.text[:200]
            except ValueError:
                err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if attempt < 2:
            time.sleep(3)
    logger.warning(f"Rename {object_id} failed: {err}")
    return False, err


# ad_id -> date a rename failure was last posted to Slack (in-memory).
_rename_failure_reported: dict[str, object] = {}


@dataclass
class ScaleRetireAction:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    spend_7d: float
    roas_7d: float
    purchases_7d: int
    adset_roas_7d: float
    action: str  # "would_retire" | "retired" | "paused (rename failed)" | "failed" | "protected"
    reason: str = ""


def _fetch_ads_7d(config: Config, keyword: str, matcher) -> dict[str, dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_name,spend,actions,action_values",
        "date_preset": "last_7d",
        "limit": 200,
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"' + keyword + '"}]'
        ),
    }
    ads: dict[str, dict] = {}
    first = True
    while url:
        resp = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params if first else None, timeout=120)
                transient_400 = False
                if resp.status_code == 400:
                    try:
                        err = resp.json().get("error", {})
                        transient_400 = err.get("is_transient") is True or err.get("code") in (1, 2, 4, 17, 32)
                    except ValueError:
                        pass
                if (resp.status_code in (403, 500, 502, 503, 504) or transient_400) and attempt < 4:
                    wait = [30, 60, 120, 240][attempt]
                    logger.warning(f"Scale-retire fetch {resp.status_code}, retrying in {wait}s: {resp.text[:200]}")
                    time.sleep(wait)
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < 4:
                    time.sleep([30, 60, 120, 240][attempt])
                else:
                    raise
        if not resp.ok:
            raise requests.exceptions.HTTPError(f"Meta {resp.status_code}: {resp.text[:400]}", response=resp)

        data = resp.json()
        for row in data.get("data", []):
            if not matcher(row.get("campaign_name", "")):
                continue
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
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", "Unknown"),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "purchases": purchases,
                "roas": revenue / spend if spend > 0 else 0,
            }
        url = data.get("paging", {}).get("next")
        first = False
    logger.info(f"Retire-7d: fetched 7d metrics for {len(ads)} {keyword} ads")
    return ads


def run_scale_retire(config: Config, dry_run: bool = False) -> list[ScaleRetireAction]:
    if not (SCALE_RETIRE_ENABLED or TESTING_RETIRE_ADS_ENABLED):
        logger.info("Retire-7d: DISABLED for SCALE and TESTING — skipping")
        return []

    ads: dict[str, dict] = {}
    if SCALE_RETIRE_ENABLED:
        ads.update(_fetch_ads_7d(config, "SCALE", _is_scale_campaign))
    if TESTING_RETIRE_ADS_ENABLED:
        testing = _fetch_ads_7d(config, "TESTING", _is_testing_campaign)
        for a in testing.values():
            a["testing"] = True
        ads.update(testing)

    by_adset: dict[str, list[str]] = defaultdict(list)
    adset_spend: dict[str, float] = defaultdict(float)
    adset_rev: dict[str, float] = defaultdict(float)
    for ad_id, a in ads.items():
        by_adset[a["adset_id"]].append(ad_id)
        adset_spend[a["adset_id"]] += a["spend"]
        adset_rev[a["adset_id"]] += a["revenue"]
    def adset_roas(adset_id: str) -> float:
        return adset_rev[adset_id] / adset_spend[adset_id] if adset_spend[adset_id] > 0 else 0

    candidates = [
        ad_id for ad_id, a in ads.items()
        if a["spend"] > (TESTING_RETIRE_SPEND_THRESHOLD if a.get("testing") else RETIRE_SPEND_THRESHOLD)
        and a["roas"] < RETIRE_ROAS_THRESHOLD
        and adset_roas(a["adset_id"]) < RETIRE_ADSET_ROAS_THRESHOLD
    ]
    if not candidates:
        return []

    info = fetch_ad_statuses(config, ad_ids=set(candidates))

    actions: list[ScaleRetireAction] = []
    for ad_id in sorted(candidates, key=lambda x: -ads[x]["spend"]):
        a = ads[ad_id]
        ad_info = info.get(ad_id, {})
        name = ad_info.get("name", a["ad_name"])
        adset_name = ad_info.get("adset_name", a["adset_name"])
        status = ad_info.get("status", "UNKNOWN")
        if any(m in name.upper() for m in ("OFF", "RUN")) or any(m in adset_name.upper() for m in ("OFF", "RUN")):
            continue
        if status in ("DELETED", "ARCHIVED"):
            continue
        try:
            created = datetime.fromisoformat(ad_info.get("created_time", ""))
        except ValueError:
            continue
        if datetime.now(timezone.utc) - created < timedelta(days=MIN_AD_AGE_DAYS):
            continue

        adset_id = a["adset_id"]
        as_roas = adset_roas(adset_id)
        act = ScaleRetireAction(
            ad_id=ad_id, ad_name=name, adset_name=adset_name, campaign_name=a["campaign_name"],
            spend_7d=a["spend"], roas_7d=a["roas"], purchases_7d=a["purchases"],
            adset_roas_7d=as_roas, action="would_retire",
        )

        if not dry_run:
            # Already paused (e.g. an earlier rename failed): only retry the rename.
            ok, reason = (True, "") if status == "PAUSED" else _update_ad_status(config, ad_id, "PAUSED")
            if not ok:
                act.action, act.reason = "failed", reason
                logger.warning(f"Scale-retire: failed to pause {ad_id}: {reason}")
            else:
                renamed, err = _rename(config, ad_id, f"{name} - OFF")
                if renamed:
                    act.action = "retired"
                    logger.info(f"Scale-retire: retired {ad_id} ({name}) — 7d ${a['spend']:.2f} @ {a['roas']:.2f}x")
                else:
                    act.action, act.reason = "paused (rename failed)", err
                    today = datetime.now(timezone.utc).date()
                    if _rename_failure_reported.get(ad_id) == today:
                        continue  # already in Slack today; don't repeat every 15 min
                    _rename_failure_reported[ad_id] = today
        elif status == "PAUSED":
            continue
        actions.append(act)
    return actions


def send_scale_retire_report(actions: list[ScaleRetireAction], dry_run: bool, config: Config) -> bool:
    if not actions:
        logger.info("Scale-retire: nothing to retire — skipping Slack")
        return True

    mode = "DRY RUN" if dry_run else "LIVE"
    retired = [a for a in actions if a.action in ("retired", "would_retire")]
    rename_failed = [a for a in actions if a.action == "paused (rename failed)"]
    protected = [a for a in actions if a.action == "protected"]
    failed = [a for a in actions if a.action == "failed"]
    if not (retired or failed or rename_failed):
        logger.info(f"Scale-retire: nothing retired ({len(protected)} protected) — skipping Slack")
        return True

    def line(a: ScaleRetireAction) -> str:
        extra = f" _({a.reason})_" if a.reason else ""
        return (
            f"• *{a.ad_name}* — adset `{a.adset_name}`\n"
            f"   7d: ${a.spend_7d:,.0f} @ {a.roas_7d:.2f}x, {a.purchases_7d}p │ adset {a.adset_roas_7d:.2f}x{extra}"
        )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🪦 SCALE + TESTING ads retired (7d) — {len(retired)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* Ad 7d spend > ${RETIRE_SPEND_THRESHOLD:,.0f} (SCALE) / ${TESTING_RETIRE_SPEND_THRESHOLD:,.0f} (TESTING) & ad 7d ROAS < {RETIRE_ROAS_THRESHOLD} "
            f"& adset 7d ROAS < {RETIRE_ADSET_ROAS_THRESHOLD} & ad ≥ {MIN_AD_AGE_DAYS} days old → pause + mark OFF. "
            f"Remove OFF from the name to bring one back."
        )}]},
    ]
    for title, group in (
        ("Retired", retired),
        ("Paused, but OFF rename failed — will restart at midnight unless you add OFF", rename_failed),
        ("Protected (kept running)", protected),
        ("Failed", failed),
    ):
        if group:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(line(a) for a in group[:15])}})

    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected scale-retire report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send scale-retire report: {e}")
        return False
