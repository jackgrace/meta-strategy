"""
TESTING adset 7-day retire. Runs daily at 12:05am AEST, just before the
midnight restart, so a chronic loser is marked OFF before it can be revived.

Rule (last 7 complete days):
- Campaign name contains TESTING
- Adset name does NOT contain OFF or RUN
- 7d spend > $250 AND 7d ROAS < 1.4
→ pause adset + append " - OFF" to its name (midnight restart skips OFF).

Remove OFF from the name to bring an adset back manually.
"""

import logging
import time
from dataclasses import dataclass

import requests

from config import Config
from meta_api import API_BASE, fetch_adset_statuses
from stop_loss import _update_ad_status

logger = logging.getLogger(__name__)

TESTING_RETIRE_ENABLED = True
RETIRE_SPEND_THRESHOLD = 250.0
RETIRE_ROAS_THRESHOLD = 1.4


@dataclass
class RetireAction:
    adset_id: str
    adset_name: str
    campaign_name: str
    spend_7d: float
    revenue_7d: float
    roas_7d: float
    purchases_7d: int
    action: str  # "would_retire" | "retired" | "paused (rename failed)" | "failed"
    reason: str = ""


def _fetch_testing_adsets_7d(config: Config) -> dict[str, dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "adset",
        "fields": "adset_id,adset_name,campaign_name,spend,actions,action_values",
        "date_preset": "last_7d",
        "limit": 200,
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"TESTING"},'
            '{"field":"adset.name","operator":"NOT_CONTAIN","value":"OFF"}]'
        ),
    }
    adsets: dict[str, dict] = {}
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
                    logger.warning(f"Testing-retire fetch {resp.status_code}, retrying in {wait}s: {resp.text[:200]}")
                    time.sleep(wait)
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < 4:
                    time.sleep([30, 60, 120, 240][attempt])
                else:
                    raise
        if not resp.ok:
            raise requests.exceptions.HTTPError(f"Meta {resp.status_code}: {resp.text[:400]}", response=resp)

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
            adsets[row["adset_id"]] = {
                "adset_name": row.get("adset_name", "Unknown"),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "purchases": purchases,
                "roas": revenue / spend if spend > 0 else 0,
            }
        url = data.get("paging", {}).get("next")
        first = False
    logger.info(f"Testing-retire: fetched 7d metrics for {len(adsets)} TESTING adsets")
    return adsets


def _rename(config: Config, object_id: str, new_name: str) -> bool:
    try:
        resp = requests.post(
            f"{API_BASE}/{object_id}?access_token={config.meta_access_token}",
            data={"name": new_name},
            timeout=30,
        )
        if not resp.ok:
            logger.warning(f"Rename {object_id} failed: {resp.text[:200]}")
        return resp.ok
    except requests.RequestException as e:
        logger.warning(f"Rename {object_id} raised: {e}")
        return False


def run_testing_retire(config: Config, dry_run: bool = False) -> list[RetireAction]:
    if not TESTING_RETIRE_ENABLED:
        logger.info("Testing-retire: DISABLED via TESTING_RETIRE_ENABLED flag — skipping")
        return []

    metrics = _fetch_testing_adsets_7d(config)
    candidates = {
        aid: m for aid, m in metrics.items()
        if m["spend"] > RETIRE_SPEND_THRESHOLD and m["roas"] < RETIRE_ROAS_THRESHOLD
    }
    if not candidates:
        return []

    info = fetch_adset_statuses(config, set(candidates))
    actions: list[RetireAction] = []
    for adset_id, m in sorted(candidates.items(), key=lambda x: -x[1]["spend"]):
        adset_info = info.get(adset_id, {})
        name = adset_info.get("name", m["adset_name"])
        status = adset_info.get("status", "UNKNOWN")
        upper = name.upper()
        if "OFF" in upper or "RUN" in upper:
            continue
        if status in ("DELETED", "ARCHIVED"):
            continue

        a = RetireAction(
            adset_id=adset_id, adset_name=name, campaign_name=m["campaign_name"],
            spend_7d=m["spend"], revenue_7d=m["revenue"], roas_7d=m["roas"],
            purchases_7d=m["purchases"], action="would_retire",
        )
        if not dry_run:
            ok, reason = _update_ad_status(config, adset_id, "PAUSED")
            if not ok:
                a.action, a.reason = "failed", reason
                logger.warning(f"Testing-retire: failed to pause {adset_id}: {reason}")
            elif _rename(config, adset_id, f"{name} - OFF"):
                a.action = "retired"
                logger.info(f"Testing-retire: retired {adset_id} ({name}) — 7d spend ${m['spend']:.2f}, ROAS {m['roas']:.2f}")
            else:
                a.action = "paused (rename failed)"
        actions.append(a)
    return actions


def send_testing_retire_report(actions: list[RetireAction], dry_run: bool, config: Config) -> bool:
    if not actions:
        logger.info("Testing-retire: nothing to retire — skipping Slack")
        return True

    mode = "DRY RUN" if dry_run else "LIVE"
    done = [a for a in actions if a.action in ("retired", "would_retire", "paused (rename failed)")]
    failed = [a for a in actions if a.action == "failed"]
    total_spend = sum(a.spend_7d for a in done)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🪦 TESTING adsets retired (7d) — {len(done)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* Rule: TESTING │ 7d spend > ${RETIRE_SPEND_THRESHOLD:.0f} & 7d ROAS < {RETIRE_ROAS_THRESHOLD} "
            f"→ pause + mark OFF (midnight restart skips). ${total_spend:,.0f} of 7d spend across retired adsets. "
            f"Remove OFF from the name to bring one back."
        )}]},
        {"type": "divider"},
    ]
    for a in (done + failed)[:20]:
        note = "" if a.action in ("retired", "would_retire") else f"\n_{a.action}{': ' + a.reason if a.reason else ''}_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*{a.adset_name}*\nCampaign: `{a.campaign_name}`\n"
            f"7d: spend ${a.spend_7d:.2f} │ ROAS {a.roas_7d:.2f}x │ Rev ${a.revenue_7d:.2f} │ {a.purchases_7d} purchases{note}"
        )}})

    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected testing-retire report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send testing-retire report: {e}")
        return False
