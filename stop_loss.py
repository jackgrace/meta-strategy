"""
Intra-day stop-loss. Runs every 15 min.

Rules:
- SCALE adsets (today's metrics):
    stop:    ACTIVE + spend>$1000 & ROAS<1.4
    restart: PAUSED + spend>$1000 & ROAS>=1.4 (intra-day if ROAS improves)
    (skip adsets with OFF in name; midnight is the primary recovery path)
- TESTING adsets (today's metrics, spend threshold tiered on daily budget):
    tier:    daily_budget<$60 → threshold $30; daily_budget>=$60 → threshold $60
    stop:    ACTIVE + spend>threshold & ROAS<1.6 & CPA/ATC>$8
    restart: PAUSED + spend>threshold & ROAS>=1.6
- TESTING ads (rolling 7d, cheap-ATC protected):
    stop:    ACTIVE + spend>$30 & (ROAS<1.6 OR 0p), skip if ATCs>0 & CPA/ATC<$6
             (protection expires at spend>$100 & 0p)
    restart: PAUSED + spend>$30 & ROAS>=1.6 & purchases>0
- CBO adsets (today's metrics):
    stop:    ACTIVE + spend>$100 & ROAS<1.6
    restart: PAUSED + spend>$100 & ROAS>1.6

Skips: adsets whose name contains OFF, ads whose name contains RUN.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests

from meta_api import API_BASE, fetch_ad_statuses, fetch_adset_statuses
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

STOP_SPEND_THRESHOLD = 80.0
STOP_ROAS_THRESHOLD = 1.6
STOP_CPA_ATC_THRESHOLD = 10.0  # cost per ATC above this — expensive ATCs = pause

RESTART_ROAS_THRESHOLD = 1.6

# SCALE — adset-level rule (today's metrics). Ad-level is off.
#   Pause:   ACTIVE + spend > $1000 & ROAS < 1.4
#   Restart: PAUSED + spend > $1000 & ROAS >= 1.4
# Matches campaigns whose name contains SCALE as a whole word only.
# Flip SCALE_ADSET_ENABLED to False to pause the rule without deleting it.
SCALE_ADSET_ENABLED = True
SCALE_ADSET_SPEND_THRESHOLD = 1000.0
SCALE_ADSET_ROAS_THRESHOLD = 1.4

# TESTING campaigns — adset-level rule (today's metrics)
# Flip TESTING_ADSET_ENABLED to True to re-enable.
TESTING_ADSET_ENABLED = True
# Pause spend threshold scales with adset daily budget:
#   daily_budget <  $60 → pause spend threshold = $30
#   daily_budget >= $60 → pause spend threshold = $60
# Adsets with no daily_budget (CBO or lifetime) fall back to the low tier.
TESTING_ADSET_BUDGET_TIER_CUTOFF = 60.0
TESTING_ADSET_SPEND_THRESHOLD_LOW = 30.0
TESTING_ADSET_SPEND_THRESHOLD_HIGH = 60.0
TESTING_ADSET_ROAS_THRESHOLD = 1.6
# Pause requires expensive ATCs too — cheap ATCs mean ASC is finding
# interest even if ROAS is soft; don't nuke that. Restart ignores this
# (ROAS-only, so a recovered adset comes back regardless of ATC cost).
TESTING_ADSET_CPA_ATC_PAUSE_THRESHOLD = 8.0

# TESTING campaigns — ad-level rule (rolling 7d metrics)
# Flip TESTING_AD_7D_ENABLED to True to re-enable.
TESTING_AD_7D_ENABLED = True
TESTING_AD_SPEND_THRESHOLD_7D = 30.0
TESTING_AD_ROAS_THRESHOLD_7D = 1.6
# Protect: if the audience is adding to cart cheaply, keep the ad running
# even without purchases yet. ASC is signalling engagement — don't kill it.
TESTING_AD_CHEAP_ATC_PROTECT = 6.0
# Hard ceiling: the cheap-ATC protection expires at this spend if still
# 0 purchases — engagement without conversion for this long isn't a
# funnel-warming signal, it's a broken funnel.
TESTING_AD_CHEAP_ATC_CEILING = 100.0

# CBO campaigns — adset-level rule (today's metrics)
CBO_ADSET_SPEND_THRESHOLD = 100.0
CBO_ADSET_ROAS_THRESHOLD = 1.6


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


@dataclass
class AdsetAction:
    adset_id: str
    adset_name: str
    campaign_name: str
    action: str  # "would_pause" | "paused" | "would_activate" | "activated" | "failed"
    reason: str
    spend: float
    revenue: float
    roas: float
    purchases: int


def _fetch_today_metrics(config: Config) -> dict:
    """Fetch today's ad-level metrics for all ads (in AEST timezone)."""
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        # Drop cost_per_action_type — we compute cost_per_atc from spend/atcs anyway.
        # Fewer fields = lighter query, less likely to hit Meta's 1504044 error.
        "fields": "ad_id,ad_name,campaign_id,campaign_name,adset_id,adset_name,spend,actions,action_values",
        "date_preset": "today",
        "limit": 200,
        # Only pull ads that actually had impressions today. Massively reduces
        # payload size on large accounts with many zero-spend ads.
        "filtering": '[{"field":"impressions","operator":"GREATER_THAN","value":"0"}]',
    }

    ads = {}
    page_count = 0

    while url:
        page_count += 1

        # Retry with backoff on timeouts and transient errors
        resp = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params if page_count == 1 else None, timeout=120)

                # Detect Meta's various retryable 400s:
                # - is_transient=true
                # - error codes: 1 (unknown), 2 (service unavailable), 4/17 (rate limits), 32 (page-level rate)
                # - error subcodes 1504018 (timeout), 1487742 (rate)
                # - message contains "temporarily", "limit reached", "load", "try again"
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
                    logger.warning(f"Stop-loss fetch {resp.status_code}, retrying in {wait}s (attempt {attempt + 1}/5, body: {resp.text[:200]})")
                    time.sleep(wait)
                    continue
                break
            except requests.exceptions.Timeout:
                if attempt < 4:
                    wait = [30, 60, 120, 240][attempt]
                    logger.warning(f"Stop-loss fetch timeout, retrying in {wait}s (attempt {attempt + 1}/5)")
                    time.sleep(wait)
                else:
                    raise
            except requests.exceptions.ConnectionError as e:
                if attempt < 4:
                    wait = [30, 60, 120, 240][attempt]
                    logger.warning(f"Stop-loss fetch connection error, retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    raise

        if not resp.ok:
            # Include Meta's actual error body in the exception so it shows in Slack
            body_preview = resp.text[:400]
            logger.error(f"Stop-loss metrics fetch error {resp.status_code}: {body_preview}")
            raise requests.exceptions.HTTPError(
                f"Meta {resp.status_code}: {body_preview}",
                response=resp,
            )

        data = resp.json()
        for row in data.get("data", []):
            spend = float(row.get("spend", 0))
            revenue = 0.0
            purchases = 0
            atcs = 0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                if a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))
                elif a.get("action_type") == "add_to_cart":
                    atcs = int(float(a.get("value", 0)))

            cost_per_atc = spend / atcs if atcs > 0 else 0

            ads[row["ad_id"]] = {
                "ad_name": row.get("ad_name", "Unknown"),
                "campaign_id": row.get("campaign_id", ""),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "purchases": purchases,
                "roas": revenue / spend if spend > 0 else 0,
                "atcs": atcs,
                "cost_per_atc": cost_per_atc,
            }

        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched today's metrics for {len(ads)} ads across {page_count} pages")
    return ads


def _fetch_testing_ads_7d(config: Config) -> dict:
    """
    Rolling 7-day ad-level insights for TESTING campaigns only.

    Includes ACTIVE and PAUSED ads (so a recovering PAUSED ad shows up
    for restart). Filters to impressions > 0 so ads that never delivered
    in the window are skipped.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_name,adset_name,spend,actions,action_values",
        "date_preset": "last_7d",
        "limit": 200,
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"TESTING"}]'
        ),
    }

    ads: dict = {}
    page_count = 0
    while url:
        page_count += 1
        resp = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params if page_count == 1 else None, timeout=120)
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
                    logger.warning(f"TESTING 7d fetch {resp.status_code}, retrying in {wait}s (attempt {attempt + 1}/5)")
                    time.sleep(wait)
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < 4:
                    wait = [30, 60, 120, 240][attempt]
                    logger.warning(f"TESTING 7d fetch network error, retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    raise

        if not resp.ok:
            body_preview = resp.text[:400]
            logger.error(f"TESTING 7d fetch error {resp.status_code}: {body_preview}")
            raise requests.exceptions.HTTPError(
                f"Meta {resp.status_code}: {body_preview}", response=resp,
            )

        data = resp.json()
        for row in data.get("data", []):
            spend = float(row.get("spend", 0))
            revenue = 0.0
            purchases = 0
            atcs = 0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                if a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))
                elif a.get("action_type") == "add_to_cart":
                    atcs = int(float(a.get("value", 0)))
            ads[row["ad_id"]] = {
                "ad_name": row.get("ad_name", "Unknown"),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "adset_name": row.get("adset_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "purchases": purchases,
                "atcs": atcs,
                "cost_per_atc": spend / atcs if atcs > 0 else 0,
                "roas": revenue / spend if spend > 0 else 0,
            }
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched TESTING 7d metrics for {len(ads)} ads across {page_count} pages")
    return ads


def _compute_adset_roas(ads: dict) -> dict:
    """Aggregate spend, revenue, ROAS, ATCs, and cost/ATC by adset_id."""
    adsets = defaultdict(lambda: {"spend": 0, "revenue": 0, "atcs": 0})
    for ad in ads.values():
        if ad["adset_id"]:
            adsets[ad["adset_id"]]["spend"] += ad["spend"]
            adsets[ad["adset_id"]]["revenue"] += ad["revenue"]
            adsets[ad["adset_id"]]["atcs"] += ad.get("atcs", 0)

    return {
        adset_id: {
            "spend": v["spend"],
            "revenue": v["revenue"],
            "roas": v["revenue"] / v["spend"] if v["spend"] > 0 else 0,
            "atcs": v["atcs"],
            "cost_per_atc": v["spend"] / v["atcs"] if v["atcs"] > 0 else 0,
        }
        for adset_id, v in adsets.items()
    }


def _compute_campaign_roas(ads: dict) -> dict:
    """Aggregate ROAS by campaign_id from ad-level data."""
    campaigns = defaultdict(lambda: {"spend": 0, "revenue": 0})
    for ad in ads.values():
        if ad.get("campaign_id"):
            campaigns[ad["campaign_id"]]["spend"] += ad["spend"]
            campaigns[ad["campaign_id"]]["revenue"] += ad["revenue"]

    return {
        cid: {
            "spend": v["spend"],
            "revenue": v["revenue"],
            "roas": v["revenue"] / v["spend"] if v["spend"] > 0 else 0,
        }
        for cid, v in campaigns.items()
    }


def _update_ad_status(config: Config, ad_id: str, new_status: str) -> tuple[bool, str]:
    """Send status update to Meta with retry. Returns (success, reason_or_message)."""
    url = f"{API_BASE}/{ad_id}"
    resp = None
    try:
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{url}?access_token={config.meta_access_token}",
                    data={"status": new_status},
                    timeout=60,
                )
                if resp.status_code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep([5, 15][attempt])
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < 2:
                    time.sleep([5, 15][attempt])
                else:
                    raise

        if resp is None:
            return False, "no response"
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


def _is_scale_campaign(campaign_name: str) -> bool:
    """Match campaign name containing SCALE as a whole word."""
    parts = [p.strip() for p in campaign_name.upper().replace("|", " ").split()]
    return "SCALE" in parts


def _is_testing_campaign(campaign_name: str) -> bool:
    """Match campaign name containing TESTING."""
    return "TESTING" in campaign_name.upper()


def _is_cbo_campaign(campaign_name: str) -> bool:
    """Match campaign name containing CBO as a whole word."""
    parts = [p.strip() for p in campaign_name.upper().replace("|", " ").split()]
    return "CBO" in parts


def run_stop_loss(config: Config, dry_run: bool = False) -> tuple[list[StopLossAction], list[AdsetAction]]:
    """
    Run intra-day stop-loss and restart checks at both ad and adset level.
    Returns (ad_actions, adset_actions).
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Stop-loss check [{mode}] ===")

    # Fetch today's metrics
    today_ads = _fetch_today_metrics(config)
    if not today_ads:
        logger.info("No ad data for today")
        return [], []

    # Compute adset + campaign aggregate ROAS
    adset_roas = _compute_adset_roas(today_ads)
    campaign_roas = _compute_campaign_roas(today_ads)

    # Fetch statuses for ads we have data for
    ad_ids = set(today_ads.keys())
    try:
        ad_info = fetch_ad_statuses(config, ad_ids=ad_ids)
    except Exception as e:
        logger.error(f"Ad status fetch failed: {e}")
        return [], []

    # Fetch statuses for SCALE, TESTING, and CBO adsets. CBO takes priority
    # over SCALE — a "MIK | CBO | SCALE" campaign is evaluated by CBO only.
    scale_adset_ids: set[str] = set()
    testing_adset_ids: set[str] = set()
    cbo_adset_ids: set[str] = set()
    adset_meta: dict[str, dict] = {}
    for ad in today_ads.values():
        if not ad["adset_id"]:
            continue
        campaign_name = ad["campaign_name"]
        if _is_cbo_campaign(campaign_name):
            cbo_adset_ids.add(ad["adset_id"])
            adset_meta[ad["adset_id"]] = {
                "adset_name": ad["adset_name"],
                "campaign_name": campaign_name,
            }
        elif SCALE_ADSET_ENABLED and _is_scale_campaign(campaign_name):
            scale_adset_ids.add(ad["adset_id"])
            adset_meta[ad["adset_id"]] = {
                "adset_name": ad["adset_name"],
                "campaign_name": campaign_name,
            }
        elif TESTING_ADSET_ENABLED and _is_testing_campaign(campaign_name):
            testing_adset_ids.add(ad["adset_id"])
            adset_meta[ad["adset_id"]] = {
                "adset_name": ad["adset_name"],
                "campaign_name": campaign_name,
            }

    try:
        adset_info = fetch_adset_statuses(
            config, scale_adset_ids | testing_adset_ids | cbo_adset_ids
        )
    except Exception as e:
        logger.error(f"Adset status fetch failed: {e}")
        adset_info = {}

    actions: list[StopLossAction] = []
    adset_actions: list[AdsetAction] = []

    # CVS/SCALE ad-level intra-day is OFF. ad_kill_3d handles
    # long-term ad-level cleanup daily at 1am AEST.
    ad_stop = 0
    ad_restart = 0
    ad_fail = 0

    # Aggregate today's purchases per adset for adset-level rules
    adset_purchases: dict[str, int] = defaultdict(int)
    for ad in today_ads.values():
        if ad["adset_id"]:
            adset_purchases[ad["adset_id"]] += ad["purchases"]

    # === SCALE adset-level stop-loss / restart (today's metrics) ===
    # Single rule:
    #   Pause:   ACTIVE + spend > $1000 & ROAS < 1.4
    #   Restart: PAUSED + spend > $1000 & ROAS >= 1.4
    scale_stop = 0
    scale_restart = 0
    scale_fail = 0

    for adset_id in scale_adset_ids:
        data = adset_roas.get(adset_id, {})
        spend = data.get("spend", 0)
        revenue = data.get("revenue", 0)
        roas = data.get("roas", 0)
        purchases = adset_purchases.get(adset_id, 0)

        info = adset_info.get(adset_id, {})
        status = info.get("status", "UNKNOWN")
        current_name = info.get("name", adset_meta[adset_id]["adset_name"])

        if "OFF" in current_name.upper():
            continue

        # STOP: ACTIVE + spend > $300 + ROAS < 1.8
        if (status == "ACTIVE"
            and spend > SCALE_ADSET_SPEND_THRESHOLD
            and roas < SCALE_ADSET_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "PAUSED")
                if success:
                    action = "paused"
                    scale_stop += 1
                    logger.info(f"SCALE ADSET STOP: Paused {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, {purchases}p")
                else:
                    action = "failed"
                    scale_fail += 1
                    logger.warning(f"SCALE ADSET STOP: Failed to pause {adset_id}: {reason}")

            adset_actions.append(AdsetAction(
                adset_id=adset_id,
                adset_name=current_name,
                campaign_name=adset_meta[adset_id]["campaign_name"],
                action=action,
                reason=reason,
                spend=spend,
                revenue=revenue,
                roas=roas,
                purchases=purchases,
            ))
            continue

        # RESTART: PAUSED + spend > $300 + ROAS >= 1.8
        if (status == "PAUSED"
            and spend > SCALE_ADSET_SPEND_THRESHOLD
            and roas >= SCALE_ADSET_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "ACTIVE")
                if success:
                    action = "activated"
                    scale_restart += 1
                    logger.info(f"SCALE ADSET RESTART: Activated {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, {purchases}p")
                else:
                    action = "failed"
                    scale_fail += 1
                    logger.warning(f"SCALE ADSET RESTART: Failed to activate {adset_id}: {reason}")

            adset_actions.append(AdsetAction(
                adset_id=adset_id,
                adset_name=current_name,
                campaign_name=adset_meta[adset_id]["campaign_name"],
                action=action,
                reason=reason,
                spend=spend,
                revenue=revenue,
                roas=roas,
                purchases=purchases,
            ))

    # === TESTING adset-level stop-loss / restart (today's metrics) ===
    testing_stop = 0
    testing_restart = 0
    testing_fail = 0

    for adset_id in testing_adset_ids:
        data = adset_roas.get(adset_id, {})
        spend = data.get("spend", 0)
        revenue = data.get("revenue", 0)
        roas = data.get("roas", 0)
        atcs = data.get("atcs", 0)
        cost_per_atc = data.get("cost_per_atc", 0)
        purchases = adset_purchases.get(adset_id, 0)

        info = adset_info.get(adset_id, {})
        status = info.get("status", "UNKNOWN")
        current_name = info.get("name", adset_meta[adset_id]["adset_name"])
        daily_budget = info.get("daily_budget_dollars", 0.0)

        # Skip adsets with OFF in name
        if "OFF" in current_name.upper():
            continue

        # Budget-tiered spend threshold: bigger-budget adsets get more rope
        # before they trip the pause. CBO/lifetime (no daily_budget) falls
        # back to the low tier.
        spend_threshold = (
            TESTING_ADSET_SPEND_THRESHOLD_HIGH
            if daily_budget >= TESTING_ADSET_BUDGET_TIER_CUTOFF
            else TESTING_ADSET_SPEND_THRESHOLD_LOW
        )

        # STOP: ACTIVE + spend > tier threshold + ROAS < 1.6 + CPA/ATC > $8
        # (both ROAS AND CPA/ATC must be bad — cheap-ATC adsets survive
        # even with soft ROAS, ASC is finding interest there)
        if (status == "ACTIVE"
            and spend > spend_threshold
            and roas < TESTING_ADSET_ROAS_THRESHOLD
            and cost_per_atc > TESTING_ADSET_CPA_ATC_PAUSE_THRESHOLD):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "PAUSED")
                if success:
                    action = "paused"
                    testing_stop += 1
                    logger.info(f"TESTING ADSET STOP: Paused {adset_id} ({current_name}) — spend ${spend:.2f}>${spend_threshold:.0f} (budget ${daily_budget:.0f}), ROAS {roas:.2f}, CPA/ATC ${cost_per_atc:.2f}, {purchases}p")
                else:
                    action = "failed"
                    testing_fail += 1
                    logger.warning(f"TESTING ADSET STOP: Failed to pause {adset_id}: {reason}")

            adset_actions.append(AdsetAction(
                adset_id=adset_id,
                adset_name=current_name,
                campaign_name=adset_meta[adset_id]["campaign_name"],
                action=action,
                reason=reason,
                spend=spend,
                revenue=revenue,
                roas=roas,
                purchases=purchases,
            ))
            continue

        # RESTART: PAUSED + spend > tier threshold + ROAS >= 1.6
        if (status == "PAUSED"
            and spend > spend_threshold
            and roas >= TESTING_ADSET_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "ACTIVE")
                if success:
                    action = "activated"
                    testing_restart += 1
                    logger.info(f"TESTING ADSET RESTART: Activated {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, {purchases}p")
                else:
                    action = "failed"
                    testing_fail += 1
                    logger.warning(f"TESTING ADSET RESTART: Failed to activate {adset_id}: {reason}")

            adset_actions.append(AdsetAction(
                adset_id=adset_id,
                adset_name=current_name,
                campaign_name=adset_meta[adset_id]["campaign_name"],
                action=action,
                reason=reason,
                spend=spend,
                revenue=revenue,
                roas=roas,
                purchases=purchases,
            ))

    # === CBO adset-level stop-loss / restart (today's metrics) ===
    cbo_stop = 0
    cbo_restart = 0
    cbo_fail = 0

    for adset_id in cbo_adset_ids:
        data = adset_roas.get(adset_id, {})
        spend = data.get("spend", 0)
        revenue = data.get("revenue", 0)
        roas = data.get("roas", 0)
        purchases = adset_purchases.get(adset_id, 0)

        info = adset_info.get(adset_id, {})
        status = info.get("status", "UNKNOWN")
        current_name = info.get("name", adset_meta[adset_id]["adset_name"])

        if "OFF" in current_name.upper():
            continue

        # STOP-LOSS: ACTIVE + spend > $100 + ROAS < 1.4
        if (status == "ACTIVE"
            and spend > CBO_ADSET_SPEND_THRESHOLD
            and roas < CBO_ADSET_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "PAUSED")
                if success:
                    action = "paused"
                    cbo_stop += 1
                    logger.info(f"CBO ADSET STOP: Paused {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}")
                else:
                    action = "failed"
                    cbo_fail += 1
                    logger.warning(f"CBO ADSET STOP: Failed to pause {adset_id}: {reason}")

            adset_actions.append(AdsetAction(
                adset_id=adset_id,
                adset_name=current_name,
                campaign_name=adset_meta[adset_id]["campaign_name"],
                action=action,
                reason=reason,
                spend=spend,
                revenue=revenue,
                roas=roas,
                purchases=purchases,
            ))
            continue

        # RESTART: PAUSED + spend > $100 + ROAS > 1.4
        if (status == "PAUSED"
            and spend > CBO_ADSET_SPEND_THRESHOLD
            and roas > CBO_ADSET_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "ACTIVE")
                if success:
                    action = "activated"
                    cbo_restart += 1
                    logger.info(f"CBO ADSET RESTART: Activated {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}")
                else:
                    action = "failed"
                    cbo_fail += 1
                    logger.warning(f"CBO ADSET RESTART: Failed to activate {adset_id}: {reason}")

            adset_actions.append(AdsetAction(
                adset_id=adset_id,
                adset_name=current_name,
                campaign_name=adset_meta[adset_id]["campaign_name"],
                action=action,
                reason=reason,
                spend=spend,
                revenue=revenue,
                roas=roas,
                purchases=purchases,
            ))

    # === TESTING ad-level stop-loss / restart (rolling 7d metrics) ===
    testing_ad_stop = 0
    testing_ad_restart = 0
    testing_ad_fail = 0

    if TESTING_AD_7D_ENABLED:
        try:
            testing_7d = _fetch_testing_ads_7d(config)
        except Exception as e:
            logger.error(f"TESTING 7d fetch failed: {e}")
            testing_7d = {}
    else:
        testing_7d = {}

    if testing_7d:
        try:
            testing_ad_info = fetch_ad_statuses(config, ad_ids=set(testing_7d.keys()))
        except Exception as e:
            logger.error(f"TESTING ad status fetch failed: {e}")
            testing_ad_info = {}

        for ad_id, ad in testing_7d.items():
            info = testing_ad_info.get(ad_id, {})
            status = info.get("status", "UNKNOWN")
            current_ad_name = info.get("name", ad["ad_name"])
            current_adset_name = info.get("adset_name", ad["adset_name"])

            # Skip markers
            if "OFF" in current_adset_name.upper():
                continue
            if "RUN" in current_ad_name.upper():
                continue

            spend = ad["spend"]
            roas = ad["roas"]
            purchases = ad["purchases"]
            atcs = ad["atcs"]
            cost_per_atc = ad["cost_per_atc"]

            # Spend gate for both directions
            if spend <= TESTING_AD_SPEND_THRESHOLD_7D:
                continue

            # Cheap-ATC protection: keep an ad running if the audience is
            # adding to cart cheaply, even without purchases yet. Only applies
            # when there ARE ATCs — 0 ATCs still trips the pause. And the
            # protection expires once spend > $50 with still 0 purchases.
            cheap_atc_protected = (
                atcs > 0
                and cost_per_atc < TESTING_AD_CHEAP_ATC_PROTECT
                and not (spend > TESTING_AD_CHEAP_ATC_CEILING and purchases == 0)
            )

            # STOP: ACTIVE + 7d spend > $30 + (ROAS < 1.6 OR 0 purchases)
            #       AND NOT cheap-ATC-protected
            if (status == "ACTIVE"
                and (roas < TESTING_AD_ROAS_THRESHOLD_7D or purchases == 0)
                and not cheap_atc_protected):
                if dry_run:
                    action, reason = "would_pause", "dry run"
                else:
                    success, reason = _update_ad_status(config, ad_id, "PAUSED")
                    if success:
                        action = "paused"
                        testing_ad_stop += 1
                        logger.info(f"TESTING AD STOP (7d): Paused {ad_id} ({current_ad_name}) — 7d spend ${spend:.2f}, ROAS {roas:.2f}, {atcs} ATC, CPA/ATC ${cost_per_atc:.2f}")
                    else:
                        action = "failed"
                        testing_ad_fail += 1
                        logger.warning(f"TESTING AD STOP (7d): Failed to pause {ad_id}: {reason}")

                actions.append(StopLossAction(
                    ad_id=ad_id, ad_name=current_ad_name,
                    campaign_name=ad["campaign_name"], adset_name=current_adset_name,
                    action=action, reason=f"7d: {reason}" if action != "failed" else reason,
                    spend=spend, roas=roas, revenue=ad["revenue"], purchases=ad["purchases"],
                    adset_spend=0.0, adset_roas=0.0,
                ))
                continue

            # RESTART: PAUSED + 7d spend > $30 + 7d ROAS >= 1.6 + purchases > 0
            # (mirror of the pause condition — recovers only when both the ROAS
            # bar is met AND at least one purchase landed in the 7d window)
            if status == "PAUSED" and roas >= TESTING_AD_ROAS_THRESHOLD_7D and purchases > 0:
                if dry_run:
                    action, reason = "would_activate", "dry run"
                else:
                    success, reason = _update_ad_status(config, ad_id, "ACTIVE")
                    if success:
                        action = "activated"
                        testing_ad_restart += 1
                        logger.info(f"TESTING AD RESTART (7d): Activated {ad_id} ({current_ad_name}) — 7d spend ${spend:.2f}, ROAS {roas:.2f}")
                    else:
                        action = "failed"
                        testing_ad_fail += 1
                        logger.warning(f"TESTING AD RESTART (7d): Failed to activate {ad_id}: {reason}")

                actions.append(StopLossAction(
                    ad_id=ad_id, ad_name=current_ad_name,
                    campaign_name=ad["campaign_name"], adset_name=current_adset_name,
                    action=action, reason=f"7d: {reason}" if action != "failed" else reason,
                    spend=spend, roas=roas, revenue=ad["revenue"], purchases=ad["purchases"],
                    adset_spend=0.0, adset_roas=0.0,
                ))

    logger.info(
        f"Stop-loss complete: "
        f"AD (TESTING 7d): {testing_ad_stop} paused, {testing_ad_restart} activated, {testing_ad_fail} failed │ "
        f"ADSET (SCALE): {scale_stop} paused, {scale_restart} activated, {scale_fail} failed │ "
        f"ADSET (TESTING): {testing_stop} paused, {testing_restart} activated, {testing_fail} failed │ "
        f"ADSET (CBO): {cbo_stop} paused, {cbo_restart} activated, {cbo_fail} failed"
    )
    return actions, adset_actions


def build_stop_loss_slack_message(
    ad_actions: list[StopLossAction],
    adset_actions: list[AdsetAction],
    dry_run: bool,
) -> dict | None:
    """
    Build intra-day Slack message combining ad + adset actions.
    Returns None when nothing happened (keeps the channel quiet).
    """
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    ad_paused = [a for a in ad_actions if a.action in ("paused", "would_pause")]
    ad_activated = [a for a in ad_actions if a.action in ("activated", "would_activate")]
    ad_failed = [a for a in ad_actions if a.action == "failed"]

    as_paused = [a for a in adset_actions if a.action in ("paused", "would_pause")]
    as_activated = [a for a in adset_actions if a.action in ("activated", "would_activate")]
    as_failed = [a for a in adset_actions if a.action == "failed"]

    if not (ad_paused or ad_activated or ad_failed or as_paused or as_activated or as_failed):
        return None

    blocks = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"⛔ Stop-Loss Report — {now}"}
    })

    summary_parts = []
    ad_total_p = len(ad_paused) + len(as_paused)
    ad_total_a = len(ad_activated) + len(as_activated)
    ad_total_f = len(ad_failed) + len(as_failed)
    if ad_total_p:
        summary_parts.append(f"🔴 {ad_total_p} paused")
    if ad_total_a:
        summary_parts.append(f"🟢 {ad_total_a} activated")
    if ad_total_f:
        summary_parts.append(f"⚠️ {ad_total_f} failed")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(summary_parts) + "\n"
            f"_SCALE adset: {'ON — stop spend>$'+str(int(SCALE_ADSET_SPEND_THRESHOLD))+' & ROAS<'+str(SCALE_ADSET_ROAS_THRESHOLD)+', restart mirror' if SCALE_ADSET_ENABLED else 'PAUSED (flag off)'}_\n"
            f"_TESTING adset: {('ON — stop spend>$'+str(int(TESTING_ADSET_SPEND_THRESHOLD_LOW))+' (budget<$'+str(int(TESTING_ADSET_BUDGET_TIER_CUTOFF))+') / >$'+str(int(TESTING_ADSET_SPEND_THRESHOLD_HIGH))+' (budget>=$'+str(int(TESTING_ADSET_BUDGET_TIER_CUTOFF))+') & ROAS<'+str(TESTING_ADSET_ROAS_THRESHOLD)+' & CPA/ATC>$'+str(int(TESTING_ADSET_CPA_ATC_PAUSE_THRESHOLD))) if TESTING_ADSET_ENABLED else 'PAUSED (flag off)'}_\n"
            f"_TESTING ad (7d): {'ON' if TESTING_AD_7D_ENABLED else 'PAUSED (flag off)'}_\n"
            f"_CBO adset stop: spend>${CBO_ADSET_SPEND_THRESHOLD:.0f} & ROAS<{CBO_ADSET_ROAS_THRESHOLD}_\n"
            f"_CBO adset restart: spend>${CBO_ADSET_SPEND_THRESHOLD:.0f} & ROAS>{CBO_ADSET_ROAS_THRESHOLD}_"
        )}
    })

    blocks.append({"type": "divider"})

    MAX_DISPLAY = 10

    def _format_ad(a: StopLossAction, emoji: str) -> str:
        cpa_line = ""
        if a.purchases > 0:
            cpa = a.spend / a.purchases
            cpa_line = f" │ CPA: ${cpa:.2f}"
        # TESTING 7d ad actions carry adset_spend=0 (no adset aggregate). Label
        # the window and skip the misleading "Adset: spend $0" line for those.
        is_7d = "7d" in (a.reason or "")
        window = "7d" if is_7d else "Today"
        lines = [
            f"{emoji} *{a.ad_name}*",
            f"Campaign: `{a.campaign_name}` │ Adset: `{a.adset_name}`",
            f"Ad ({window}): spend ${a.spend:.2f} │ ROAS {a.roas:.2f}x │ Rev ${a.revenue:.2f} │ {a.purchases} purchases{cpa_line}",
        ]
        if a.adset_spend > 0 or a.adset_roas > 0:
            lines.append(f"Adset: spend ${a.adset_spend:.2f} │ ROAS {a.adset_roas:.2f}x")
        return "\n".join(lines)

    def _format_adset(a: AdsetAction, emoji: str) -> str:
        cpa_line = ""
        if a.purchases > 0:
            cpa = a.spend / a.purchases
            cpa_line = f" │ CPA: ${cpa:.2f}"
        return (
            f"{emoji} *Adset: {a.adset_name}*\n"
            f"Campaign: `{a.campaign_name}`\n"
            f"Spend ${a.spend:.2f} │ ROAS {a.roas:.2f}x │ Rev ${a.revenue:.2f} │ {a.purchases} purchases{cpa_line}"
        )

    def _add_section(title: str, items: list, formatter):
        if not items:
            return
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": title}})
        for a in items[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": formatter(a)}})
        overflow = len(items) - min(MAX_DISPLAY, len(items))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    _add_section("🔴 *Adsets paused (stop-loss)*", as_paused, lambda a: _format_adset(a, "🔴"))
    _add_section("🟢 *Adsets activated (restart)*", as_activated, lambda a: _format_adset(a, "🟢"))
    _add_section("🔴 *Ads paused (stop-loss)*", ad_paused, lambda a: _format_ad(a, "🔴"))
    _add_section("🟢 *Ads activated (restart)*", ad_activated, lambda a: _format_ad(a, "🟢"))

    if ad_failed or as_failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⚠️ *Failed (needs manual action)*"}})
        for a in as_failed[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _format_adset(a, "⚠️") + f"\n_{a.reason}_"}})
        for a in ad_failed[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _format_ad(a, "⚠️") + f"\n_{a.reason}_"}})

    return {"blocks": blocks}


def send_stop_loss_report(
    ad_actions: list[StopLossAction],
    adset_actions: list[AdsetAction],
    dry_run: bool,
    config: Config,
) -> bool:
    """Send stop-loss report to Slack. Quiet if nothing happened."""
    payload = build_stop_loss_slack_message(ad_actions, adset_actions, dry_run)
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
