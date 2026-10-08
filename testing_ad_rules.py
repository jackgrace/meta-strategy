"""
TESTING ad rules (last 7 days incl. today). Runs every 15 minutes.

For each campaign with TESTING or TRYBE in the name, the campaign's 7-day average
CPC (spend / link clicks) and cost per ATC (spend / ATCs) are the baseline.
An ACTIVE ad is paused and marked " - OFF" if any of:
  1. ad 7d spend > $30  & ad CPC > 3x campaign avg CPC       (0 clicks counts)
  2. ad 7d spend > $60  & 0 ATCs
  3. ad 7d spend > $125 & (0 purchases
                           OR (ad cost/ATC > 1.5x campaign avg & ad 7d ROAS < 1.4))
SCALE campaigns (SCALE as a word in the name, incl. SCALE | CBO): baseline
is the ad's own adset over the same 7 days. An ACTIVE ad is paused + OFF if:
  ad 7d spend > $150 & ROAS < 1.2
  & cost/ATC > 1.3x adset avg cost/ATC                     (0 ATCs counts)
  & ATC-to-purchase rate < adset avg (purchases / ATCs)
Skips ads / adsets with OFF or RUN in the name. Testing ads are never
restarted at midnight, so a retired ad stays off until OFF is removed.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from config import Config
from meta_api import API_BASE, fetch_ad_statuses
from scale_retire import _rename
from stop_loss import _is_scale_campaign, _update_ad_status

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

TESTING_AD_RULES_ENABLED = True
CAMPAIGN_KEYWORDS = ("TESTING", "TRYBE")

SCALE_AD_RULE_ENABLED = True
SCALE_SPEND = 150.0
SCALE_ROAS = 1.2
SCALE_CPA_MULT = 1.3
LOOKBACK_DAYS = 7
CPC_SPEND = 30.0
CPC_MULT = 3.0
NO_ATC_SPEND = 60.0
ATC_SPEND = 125.0
ATC_MULT = 1.5
ATC_ROAS = 1.4

# ad_id -> date a rename failure was last posted to Slack (in-memory).
_rename_failure_reported: dict[str, object] = {}


@dataclass
class TestingAdAction:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    spend_7d: float
    roas_7d: float
    action: str  # "would_retire" | "retired" | "paused (rename failed)" | "failed"
    reason: str


def _matches(campaign_name: str) -> bool:
    name = campaign_name.upper()
    return any(k in name for k in CAMPAIGN_KEYWORDS)


def _fetch_ads(config: Config, keywords, matcher) -> dict[str, dict]:
    ads: dict[str, dict] = {}
    # Query each keyword in upper and title case in case Meta's CONTAIN
    # filter is case-sensitive; results are merged by ad_id.
    for keyword in sorted({v for k in keywords for v in (k, k.title())}):
        ads.update(_fetch_ads_for_keyword(config, keyword, matcher))
    logger.info(f"7d ad rules: fetched {len(ads)} ads ({'/'.join(keywords)})")
    return ads


def _fetch_ads_for_keyword(config: Config, keyword: str, matcher) -> dict[str, dict]:
    today = datetime.now(AEST).date()
    since = today - timedelta(days=LOOKBACK_DAYS - 1)
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend,actions,action_values",
        "time_range": f'{{"since":"{since}","until":"{today}"}}',
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
                    logger.warning(f"Testing ad rules fetch {resp.status_code}, retrying in {wait}s: {resp.text[:200]}")
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
            atcs = clicks = purchases = 0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                if a.get("action_type") == "add_to_cart":
                    atcs = int(float(a.get("value", 0)))
                elif a.get("action_type") == "link_click":
                    clicks = int(float(a.get("value", 0)))
                elif a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))
            ads[row["ad_id"]] = {
                "ad_name": row.get("ad_name", "Unknown"),
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", "Unknown"),
                "campaign_id": row.get("campaign_id", ""),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "atcs": atcs,
                "clicks": clicks,
                "purchases": purchases,
            }
        url = data.get("paging", {}).get("next")
        first = False
    return ads


def _failing_rule(ad: dict, avg_cpc: float, avg_cpa: float) -> str | None:
    spend = ad["spend"]
    roas = ad["revenue"] / spend if spend > 0 else 0
    cpc = spend / ad["clicks"] if ad["clicks"] > 0 else float("inf")
    cpa = spend / ad["atcs"] if ad["atcs"] > 0 else float("inf")
    if spend > CPC_SPEND and avg_cpc > 0 and cpc > CPC_MULT * avg_cpc:
        cpc_txt = "0 link clicks" if ad["clicks"] == 0 else f"CPC ${cpc:.2f}"
        return f"spend ${spend:.2f}>${CPC_SPEND:.0f} & {cpc_txt} > {CPC_MULT:g}x campaign avg ${avg_cpc:.2f}"
    if spend > NO_ATC_SPEND and ad["atcs"] == 0:
        return f"spend ${spend:.2f}>${NO_ATC_SPEND:.0f} & 0 ATCs"
    if spend > ATC_SPEND and ad["purchases"] == 0:
        return f"spend ${spend:.2f}>${ATC_SPEND:.0f} & 0 purchases"
    if spend > ATC_SPEND and avg_cpa > 0 and cpa > ATC_MULT * avg_cpa and roas < ATC_ROAS:
        return (f"spend ${spend:.2f}>${ATC_SPEND:.0f} & cost/ATC ${cpa:.2f} > {ATC_MULT:g}x campaign avg "
                f"${avg_cpa:.2f} & ROAS {roas:.2f}<{ATC_ROAS}")
    return None


def _scale_failing_rule(ad: dict, adset_cpa: float, adset_atc_to_p: float) -> str | None:
    spend = ad["spend"]
    roas = ad["revenue"] / spend if spend > 0 else 0
    cpa = spend / ad["atcs"] if ad["atcs"] > 0 else float("inf")
    atc_to_p = ad["purchases"] / ad["atcs"] if ad["atcs"] > 0 else 0.0
    if (spend > SCALE_SPEND and roas < SCALE_ROAS
            and adset_cpa > 0 and cpa > SCALE_CPA_MULT * adset_cpa
            and atc_to_p < adset_atc_to_p):
        cpa_txt = "0 ATCs" if ad["atcs"] == 0 else f"cost/ATC ${cpa:.2f}"
        return (f"spend ${spend:.2f}>${SCALE_SPEND:.0f} & ROAS {roas:.2f}<{SCALE_ROAS} & {cpa_txt} > "
                f"{SCALE_CPA_MULT:g}x adset avg ${adset_cpa:.2f} & ATC→purchase {atc_to_p:.0%} < adset {adset_atc_to_p:.0%}")
    return None


def _group_totals(ads: dict[str, dict], key: str) -> dict:
    totals = defaultdict(lambda: {"spend": 0.0, "clicks": 0, "atcs": 0, "purchases": 0})
    for a in ads.values():
        t = totals[a[key]]
        t["spend"] += a["spend"]
        t["clicks"] += a["clicks"]
        t["atcs"] += a["atcs"]
        t["purchases"] += a["purchases"]
    return totals


def run_testing_ad_rules(config: Config, dry_run: bool = False) -> list[TestingAdAction]:
    ads: dict[str, dict] = {}
    candidates: dict[str, str] = {}

    if TESTING_AD_RULES_ENABLED:
        testing_ads = _fetch_ads(config, CAMPAIGN_KEYWORDS, _matches)
        ads.update(testing_ads)
        camp = _group_totals(testing_ads, "campaign_id")
        for ad_id, a in testing_ads.items():
            c = camp[a["campaign_id"]]
            avg_cpc = c["spend"] / c["clicks"] if c["clicks"] else 0
            avg_cpa = c["spend"] / c["atcs"] if c["atcs"] else 0
            why = _failing_rule(a, avg_cpc, avg_cpa)
            if why:
                candidates[ad_id] = why

    if SCALE_AD_RULE_ENABLED:
        # Testing/Trybe campaigns keep their own rules even if they also say SCALE.
        scale_ads = _fetch_ads(config, ("SCALE",), lambda n: _is_scale_campaign(n) and not _matches(n))
        ads.update(scale_ads)
        adsets = _group_totals(scale_ads, "adset_id")
        for ad_id, a in scale_ads.items():
            t = adsets[a["adset_id"]]
            adset_cpa = t["spend"] / t["atcs"] if t["atcs"] else 0
            adset_atc_to_p = t["purchases"] / t["atcs"] if t["atcs"] else 0
            why = _scale_failing_rule(a, adset_cpa, adset_atc_to_p)
            if why:
                candidates[ad_id] = why

    if not candidates:
        return []

    info = fetch_ad_statuses(config, ad_ids=set(candidates))
    actions: list[TestingAdAction] = []
    for ad_id, why in sorted(candidates.items(), key=lambda x: -ads[x[0]]["spend"]):
        a = ads[ad_id]
        ad_info = info.get(ad_id, {})
        name = ad_info.get("name", a["ad_name"])
        adset_name = ad_info.get("adset_name", a["adset_name"])
        if ad_info.get("status") != "ACTIVE":
            continue
        if any(m in name.upper() for m in ("OFF", "RUN")) or any(m in adset_name.upper() for m in ("OFF", "RUN")):
            continue

        act = TestingAdAction(
            ad_id=ad_id, ad_name=name, adset_name=adset_name, campaign_name=a["campaign_name"],
            spend_7d=a["spend"], roas_7d=a["revenue"] / a["spend"] if a["spend"] else 0,
            action="would_retire", reason=why,
        )
        if not dry_run:
            ok, err = _update_ad_status(config, ad_id, "PAUSED")
            if not ok:
                act.action, act.reason = "failed", f"{why} — pause failed: {err}"
            else:
                renamed, rerr = _rename(config, ad_id, f"{name} - OFF")
                if renamed:
                    act.action = "retired"
                    logger.info(f"Testing ad rules: retired {ad_id} ({name}) — {why}")
                else:
                    act.action, act.reason = "paused (rename failed)", f"{why} — rename failed: {rerr}"
                    today = datetime.now(AEST).date()
                    if _rename_failure_reported.get(ad_id) == today:
                        continue
                    _rename_failure_reported[ad_id] = today
        actions.append(act)
    return actions


def send_testing_ad_rules_report(actions: list[TestingAdAction], dry_run: bool, config: Config) -> bool:
    if not actions:
        return True
    mode = "DRY RUN" if dry_run else "LIVE"
    lines = [
        f"• *{a.ad_name}* — `{a.campaign_name}` / `{a.adset_name}`\n"
        f"   7d: ${a.spend_7d:,.2f} @ {a.roas_7d:.2f}x │ _{a.reason}_"
        + ("" if a.action in ("retired", "would_retire") else f" *({a.action})*")
        for a in actions[:20]
    ]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🧪 Ads retired (7-day rules) — {len(actions)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* Last {LOOKBACK_DAYS} days incl. today. *TESTING / TRYBE* (vs campaign avg): spend>${CPC_SPEND:.0f} & CPC>{CPC_MULT:g}x avg │ "
            f"spend>${NO_ATC_SPEND:.0f} & 0 ATCs │ spend>${ATC_SPEND:.0f} & (0 purchases OR (cost/ATC>{ATC_MULT:g}x avg & ROAS<{ATC_ROAS})) "
            f"→ pause + mark OFF.\n*SCALE* (vs adset avg): spend>${SCALE_SPEND:.0f} & ROAS<{SCALE_ROAS} & cost/ATC>{SCALE_CPA_MULT:g}x avg "
            f"& ATC→purchase < avg → pause + mark OFF. Remove OFF from the name to bring one back."
        )}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected testing ad rules report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send testing ad rules report: {e}")
        return False
