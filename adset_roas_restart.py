"""
Intra-day adset restart on ROAS. Runs every 15 minutes.

Turn a PAUSED adset back ON during the day when:
- its campaign is ACTIVE (delivering)
- its name does not contain OFF
- its campaign is not a TESTING campaign (paused testing adsets stay off)
- today it has spent > $50 and its ROAS today is > 1.1

Leaves alone any adset the intraday adset stop-loss would still pause
(spend over its threshold with ROAS under its threshold), so the two rules
can't flip an adset on and off; the stop-loss restarts those itself once
ROAS recovers. Every other paused adset in an active campaign comes back at
the midnight restart anyway.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass

import requests

from config import Config
from meta_api import fetch_adset_statuses
from midnight_restart import _activate_adset, _fetch_campaign_statuses
from stop_loss import (_fetch_today_metrics, _is_cbo_campaign, _is_testing_campaign,
                       SCALE_ADSET_ENABLED, SCALE_ADSET_SPEND_THRESHOLD, SCALE_ADSET_ROAS_THRESHOLD,
                       CBO_ADSET_ENABLED, CBO_ADSET_SPEND_THRESHOLD, CBO_ADSET_ROAS_THRESHOLD,
                       TESTING_ADSET_STOP_ENABLED, TESTING_ADSET_STOP_SPEND_THRESHOLD, TESTING_ADSET_STOP_ROAS_THRESHOLD)

logger = logging.getLogger(__name__)

ADSET_ROAS_RESTART_ENABLED = False   # off — the adset stop-loss handles its own recovery restart
RESTART_ROAS = 1.1
MIN_SPEND_TODAY = 50.0


@dataclass
class AdsetRestart:
    adset_id: str
    adset_name: str
    campaign_name: str
    spend: float
    roas: float
    action: str   # would_activate | activated | failed
    reason: str


def _stop_loss_would_pause(campaign_name: str, spend: float, roas: float) -> bool:
    """True when the intraday adset stop-loss would pause this adset right now."""
    if _is_testing_campaign(campaign_name):
        return TESTING_ADSET_STOP_ENABLED and spend > TESTING_ADSET_STOP_SPEND_THRESHOLD and roas < TESTING_ADSET_STOP_ROAS_THRESHOLD
    if _is_cbo_campaign(campaign_name):
        return CBO_ADSET_ENABLED and spend > CBO_ADSET_SPEND_THRESHOLD and roas < CBO_ADSET_ROAS_THRESHOLD
    return SCALE_ADSET_ENABLED and spend > SCALE_ADSET_SPEND_THRESHOLD and roas < SCALE_ADSET_ROAS_THRESHOLD


def run_adset_roas_restart(config: Config, dry_run: bool = False) -> list[AdsetRestart]:
    if not ADSET_ROAS_RESTART_ENABLED:
        return []
    today = _fetch_today_metrics(config)
    adsets = defaultdict(lambda: {"spend": 0.0, "revenue": 0.0, "campaign_id": "", "campaign_name": "", "name": ""})
    for ad in today.values():
        if not ad.get("adset_id"):
            continue
        t = adsets[ad["adset_id"]]
        t["spend"] += ad["spend"]
        t["revenue"] += ad["revenue"]
        t["campaign_id"] = ad.get("campaign_id", "")
        t["campaign_name"] = ad.get("campaign_name", "")
        t["name"] = ad.get("adset_name", "")

    candidates = {
        aid: t for aid, t in adsets.items()
        if t["spend"] > MIN_SPEND_TODAY and t["revenue"] / t["spend"] > RESTART_ROAS
    }
    if not candidates:
        return []

    info = fetch_adset_statuses(config, set(candidates))
    paused = {aid: t for aid, t in candidates.items()
              if info.get(aid, {}).get("status") == "PAUSED"
              and "OFF" not in info.get(aid, {}).get("name", t["name"]).upper()}
    if not paused:
        return []
    campaigns = _fetch_campaign_statuses(config, {t["campaign_id"] for t in paused.values()})

    actions: list[AdsetRestart] = []
    for aid, t in paused.items():
        if campaigns.get(t["campaign_id"], {}).get("effective_status") != "ACTIVE":
            continue
        if _is_testing_campaign(t["campaign_name"]):
            continue  # paused TESTING adsets stay paused
        roas = t["revenue"] / t["spend"]
        if _stop_loss_would_pause(t["campaign_name"], t["spend"], roas):
            continue  # the adset stop-loss still applies — it restarts it when ROAS recovers
        roas = t["revenue"] / t["spend"]
        name = info.get(aid, {}).get("name", t["name"])
        why = f"today ${t['spend']:.2f} at {roas:.2f}x > {RESTART_ROAS}"
        if dry_run:
            actions.append(AdsetRestart(aid, name, t["campaign_name"], t["spend"], roas, "would_activate", why))
            continue
        ok, reason = _activate_adset(config, aid)
        actions.append(AdsetRestart(aid, name, t["campaign_name"], t["spend"], roas,
                                    "activated" if ok else "failed", why if ok else f"{why} — {reason}"))
        logger.info(f"ADSET ROAS RESTART: {'activated' if ok else 'failed'} {aid} ({name}) — {why}")
    return actions


def send_adset_roas_restart_report(actions: list[AdsetRestart], dry_run: bool, config: Config) -> bool:
    if not actions:
        return True
    mode = "DRY RUN" if dry_run else "LIVE"
    lines = [
        f"• *{a.adset_name}* — `{a.campaign_name}` │ today ${a.spend:,.2f} @ {a.roas:.2f}x"
        + ("" if a.action in ("activated", "would_activate") else f" _({a.action}: {a.reason})_")
        for a in actions[:20]
    ]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"▶️ Adsets back on (ROAS) — {len(actions)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* Paused adsets in active campaigns, no OFF in name, today spend > ${MIN_SPEND_TODAY:.0f} "
            f"and ROAS > {RESTART_ROAS} → switched back on."
        )}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send adset ROAS restart report: {e}")
        return False
