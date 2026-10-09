"""
Ad-level disaster stop, all campaigns. Runs every 15 minutes.

Catches an ad blowing through budget today on obviously bad traffic, without
waiting for late-reported purchases. Pause only — no OFF marker.

An ad at ROAS >= 1.2 today is never paused, whatever its cost per ATC.
Otherwise, pause an ACTIVE ad for the rest of today if either:
  A. today spend > $120 & 0 purchases today
  B. today spend > $150 & today cost/ATC > max(3x peers' 7-day cost/ATC, $30)
     (0 ATCs today counts; peers = the other ads in the same adset over the
      last 7 days incl. today — the ad itself is left out)

Restart:
  - same day, if neither check applies any more (late purchases / ATCs);
  - at 12:05am, any ad that hit a check yesterday is switched back on,
    including TESTING ads (which the midnight restart otherwise leaves off).

Skips ads / adsets with OFF or RUN in the name.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from config import Config
from meta_api import fetch_ad_statuses
from stop_loss import _update_ad_status
from testing_ad_rules import _aggregate, _fetch_daily, _names, _today

logger = logging.getLogger(__name__)

DISASTER_STOP_ENABLED = True
NO_PURCHASE_SPEND = 120.0      # ~2x target cost per purchase
ATC_SPEND = 150.0
ATC_PEER_MULT = 3.0
ATC_FLOOR = 30.0               # never trigger below $30 per ATC
PEER_DAYS = 7
PROTECT_ROAS = 1.2             # ad ROAS today >= this -> never paused


@dataclass
class DisasterAction:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    spend_today: float
    roas_today: float
    action: str  # would_pause | paused | would_activate | activated | failed
    reason: str


def _fetch(config: Config, since: date, until: date) -> list[dict]:
    rows = _fetch_daily(config, None, lambda _name: True, "all", since, until)
    logger.info(f"Disaster stop: fetched {len(rows)} daily ad rows {since}..{until}")
    return rows


def evaluate(rows: list[dict], as_of: date) -> dict[str, str]:
    """ad_id -> reason for ads hitting a disaster check on `as_of`."""
    window = [r for r in rows if as_of - timedelta(days=PEER_DAYS - 1) <= r["date"] <= as_of]
    day_ads = _aggregate([r for r in rows if r["date"] == as_of], "ad_id")
    ads7 = _aggregate(window, "ad_id")
    adsets7 = _aggregate(window, "adset_id")
    reasons: dict[str, str] = {}
    for ad_id, d in day_ads.items():
        spend = d["spend"]
        if spend and d["revenue"] / spend >= PROTECT_ROAS:
            continue
        if spend > NO_PURCHASE_SPEND and d["purchases"] == 0:
            reasons[ad_id] = f"today ${spend:.2f} > ${NO_PURCHASE_SPEND:.0f} with 0 purchases"
            continue
        if spend <= ATC_SPEND:
            continue
        a7 = ads7[ad_id]
        s7 = adsets7[d["meta"]["adset_id"]]
        peer_atcs = s7["atcs"] - a7["atcs"]
        peer_cpa = (s7["spend"] - a7["spend"]) / peer_atcs if peer_atcs > 0 else 0
        limit = max(ATC_PEER_MULT * peer_cpa, ATC_FLOOR)
        cpa = spend / d["atcs"] if d["atcs"] else float("inf")
        if cpa > limit:
            cpa_txt = "0 ATCs" if d["atcs"] == 0 else f"cost/ATC ${cpa:.2f}"
            base = f"{ATC_PEER_MULT:g}x peers' 7d ${peer_cpa:.2f}" if peer_cpa else "no peer data"
            reasons[ad_id] = f"today ${spend:.2f} > ${ATC_SPEND:.0f} & {cpa_txt} > ${limit:.2f} ({base}, floor ${ATC_FLOOR:.0f})"
    return reasons


def _act(config, ad_id, d, name, adset_name, status, verb, why, dry_run) -> DisasterAction:
    spend = d["spend"]
    roas = d["revenue"] / spend if spend else 0
    make = lambda action, reason: DisasterAction(ad_id, name, adset_name, d["meta"]["campaign_name"], spend, roas, action, reason)
    if dry_run:
        return make(f"would_{verb}", why)
    ok, err = _update_ad_status(config, ad_id, status)
    done = "paused" if verb == "pause" else "activated"
    return make(done if ok else "failed", why if ok else f"{why} — {verb} failed: {err}")


def run_disaster_stop(config: Config, dry_run: bool = False) -> list[DisasterAction]:
    """Every 15 min: pause disaster ads for today; restart ones that recovered."""
    if not DISASTER_STOP_ENABLED:
        return []
    today = _today()
    rows = _fetch(config, today - timedelta(days=PEER_DAYS - 1), today)
    flagged = evaluate(rows, today)
    day_ads = _aggregate([r for r in rows if r["date"] == today], "ad_id")
    recover = {a for a, d in day_ads.items() if d["spend"] > NO_PURCHASE_SPEND and a not in flagged}
    if not (flagged or recover):
        return []

    info = fetch_ad_statuses(config, ad_ids=set(flagged) | recover)
    actions: list[DisasterAction] = []
    for ad_id in sorted(set(flagged) | recover, key=lambda a: -day_ads[a]["spend"]):
        d = day_ads[ad_id]
        status = info.get(ad_id, {}).get("status")
        name, adset_name, ok = _names(info.get(ad_id, {}), d["meta"])
        if not ok:
            continue
        if ad_id in flagged and status == "ACTIVE":
            actions.append(_act(config, ad_id, d, name, adset_name, "PAUSED", "pause",
                                flagged[ad_id] + " — paused for today", dry_run))
        elif ad_id in recover and status == "PAUSED":
            actions.append(_act(config, ad_id, d, name, adset_name, "ACTIVE", "activate",
                                "disaster checks no longer apply today", dry_run))
    return actions


def run_disaster_midnight(config: Config, dry_run: bool = False) -> list[DisasterAction]:
    """12:05am: switch back on ads the disaster stop paused yesterday."""
    if not DISASTER_STOP_ENABLED:
        return []
    yesterday = _today() - timedelta(days=1)
    rows = _fetch(config, yesterday - timedelta(days=PEER_DAYS - 1), yesterday)
    flagged = evaluate(rows, yesterday)
    if not flagged:
        return []
    day_ads = _aggregate([r for r in rows if r["date"] == yesterday], "ad_id")
    info = fetch_ad_statuses(config, ad_ids=set(flagged))
    actions: list[DisasterAction] = []
    for ad_id in flagged:
        d = day_ads[ad_id]
        name, adset_name, ok = _names(info.get(ad_id, {}), d["meta"])
        if ok and info.get(ad_id, {}).get("status") == "PAUSED":
            actions.append(_act(config, ad_id, d, name, adset_name, "ACTIVE", "activate",
                                f"new day — paused yesterday ({flagged[ad_id]})", dry_run))
    return actions


def send_disaster_report(actions: list[DisasterAction], dry_run: bool, config: Config, title: str) -> bool:
    if not actions:
        return True
    mode = "DRY RUN" if dry_run else "LIVE"
    groups = (
        ("🚨 Paused for today", ("paused", "would_pause")),
        ("▶️ Back on", ("activated", "would_activate")),
        ("⚠️ Failed", ("failed",)),
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🚨 {title} — {len(actions)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* All campaigns. Today: spend>${NO_PURCHASE_SPEND:.0f} & 0 purchases, or "
            f"spend>${ATC_SPEND:.0f} & cost/ATC > {ATC_PEER_MULT:g}x peers' 7d (min ${ATC_FLOOR:.0f}) → paused for today, "
            f"back on at midnight. Ads at ≥{PROTECT_ROAS}x today are never paused. No OFF added."
        )}]},
    ]
    for heading, kinds in groups:
        group = [a for a in actions if a.action in kinds]
        if group:
            lines = [
                f"• *{a.ad_name}* — `{a.campaign_name}` / `{a.adset_name}`\n"
                f"   today: ${a.spend_today:,.2f} @ {a.roas_today:.2f}x │ _{a.reason}_"
                for a in group[:15]
            ]
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{heading}*\n" + "\n".join(lines)}})
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected disaster report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send disaster report: {e}")
        return False
