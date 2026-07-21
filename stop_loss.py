"""
Intra-day stop-loss.

Runs every 15 min during trading hours.

STOP-LOSS (turn OFF):
- Campaign name contains 'CC'
- Ad is currently ACTIVE
- Today's ad spend > $100
- Today's ad ROAS < 1.6
- Today's adset ROAS < 1.6

RESTART (turn ON):
- Campaign name contains 'CC'
- Ad is currently PAUSED (with " - OFF" marker)
- Today's ad spend > $1
- Today's ad ROAS > 1.6
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

STOP_SPEND_THRESHOLD = 100.0
STOP_ROAS_THRESHOLD = 1.6
STOP_CAMPAIGN_ROAS_THRESHOLD = 1.6
STOP_CPA_ATC_THRESHOLD = 10.0  # cost per ATC must exceed this

RESTART_SPEND_THRESHOLD = 1.0
RESTART_ROAS_THRESHOLD = 1.6
RESTART_CPA_ATC_THRESHOLD = 8.0  # if CPA/ATC is below this, restart even without ROAS

# Adset-level thresholds
ADSET_STOP_SPEND_THRESHOLD = 1500.0       # spend > this AND ROAS < ROAS threshold
ADSET_STOP_ROAS_THRESHOLD = 1.4
ADSET_STOP_NO_PURCHASE_SPEND = 300.0      # alt: spend > this AND 0 purchases

ADSET_RESTART_SPEND_THRESHOLD = 300.0     # spend gate for ROAS check
ADSET_RESTART_ROAS_THRESHOLD = 1.4
ADSET_RESTART_LOW_SPEND_MIN = 250.0       # alt band: spend $250-500 with purchases
ADSET_RESTART_LOW_SPEND_MAX = 500.0

# TESTING campaigns — adset-level rule (today's metrics)
TESTING_ADSET_SPEND_THRESHOLD = 50.0
TESTING_ADSET_ROAS_THRESHOLD = 1.6
TESTING_ADSET_CPA_ATC_THRESHOLD = 10.0


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


TARGET_KEYWORDS = {"CC", "VALUE", "SCALE"}


def _is_target_campaign(campaign_name: str) -> bool:
    """Match campaign name containing CC, VALUE, or SCALE as a whole word."""
    parts = [p.strip() for p in campaign_name.upper().replace("|", " ").split()]
    return any(k in parts for k in TARGET_KEYWORDS)


def _is_testing_campaign(campaign_name: str) -> bool:
    """Match campaign name containing TESTING."""
    return "TESTING" in campaign_name.upper()


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

    # Fetch statuses for CC/VALUE/SCALE adsets AND TESTING adsets
    target_adset_ids: set[str] = set()
    testing_adset_ids: set[str] = set()
    adset_meta: dict[str, dict] = {}
    for ad in today_ads.values():
        if not ad["adset_id"]:
            continue
        if _is_target_campaign(ad["campaign_name"]):
            target_adset_ids.add(ad["adset_id"])
            adset_meta[ad["adset_id"]] = {
                "adset_name": ad["adset_name"],
                "campaign_name": ad["campaign_name"],
            }
        elif _is_testing_campaign(ad["campaign_name"]):
            testing_adset_ids.add(ad["adset_id"])
            adset_meta[ad["adset_id"]] = {
                "adset_name": ad["adset_name"],
                "campaign_name": ad["campaign_name"],
            }

    try:
        adset_info = fetch_adset_statuses(config, target_adset_ids | testing_adset_ids)
    except Exception as e:
        logger.error(f"Adset status fetch failed: {e}")
        adset_info = {}

    actions: list[StopLossAction] = []
    adset_actions: list[AdsetAction] = []
    adset_stop = 0
    adset_restart = 0
    adset_fail = 0

    # === Ad-level stop-loss / restart for CC/VALUE/SCALE — DISABLED per request ===
    ad_stop = 0
    ad_restart = 0
    ad_fail = 0

    # Aggregate today's purchases per adset for adset-level rules
    adset_purchases: dict[str, int] = defaultdict(int)
    for ad in today_ads.values():
        if ad["adset_id"]:
            adset_purchases[ad["adset_id"]] += ad["purchases"]

    # Adset-level stop-loss / restart
    for adset_id in target_adset_ids:
        data = adset_roas.get(adset_id, {})
        spend = data.get("spend", 0)
        revenue = data.get("revenue", 0)
        roas = data.get("roas", 0)
        purchases = adset_purchases.get(adset_id, 0)

        info = adset_info.get(adset_id, {})
        status = info.get("status", "UNKNOWN")
        current_name = info.get("name", adset_meta[adset_id]["adset_name"])

        # STOP-LOSS: ACTIVE and any of:
        #   (spend > $300 AND 0 purchases)
        #   (spend > $600 AND ROAS < 1.6)
        primary_stop = spend > ADSET_STOP_SPEND_THRESHOLD and roas < ADSET_STOP_ROAS_THRESHOLD
        no_purchase_stop = spend > ADSET_STOP_NO_PURCHASE_SPEND and purchases == 0
        if status == "ACTIVE" and (primary_stop or no_purchase_stop):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "PAUSED")
                if success:
                    action = "paused"
                    adset_stop += 1
                    branch = "no-purchase" if no_purchase_stop and not primary_stop else "ROAS"
                    logger.info(f"ADSET STOP-LOSS ({branch}): Paused {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, {purchases} purchases")
                else:
                    action = "failed"
                    adset_fail += 1
                    logger.warning(f"ADSET STOP-LOSS: Failed to pause {adset_id}: {reason}")

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

        # RESTART: PAUSED and (
        #   (spend > $500 AND ROAS > 1.6) OR
        #   (spend between $250-$500 AND at least 1 purchase)
        # )
        primary_restart = spend > ADSET_RESTART_SPEND_THRESHOLD and roas > ADSET_RESTART_ROAS_THRESHOLD
        low_spend_restart = (
            ADSET_RESTART_LOW_SPEND_MIN < spend <= ADSET_RESTART_LOW_SPEND_MAX
            and purchases > 0
        )
        # Only restart if the adset itself is PAUSED (not blocked by parent
        # campaign being off — that shows up as CAMPAIGN_PAUSED).
        # Also skip if adset name contains "OFF" (manual do-not-reactivate marker).
        has_off = "OFF" in current_name.upper()
        if status == "PAUSED" and not has_off and (primary_restart or low_spend_restart):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "ACTIVE")
                if success:
                    action = "activated"
                    adset_restart += 1
                    branch = "low-spend" if low_spend_restart and not primary_restart else "ROAS"
                    logger.info(f"ADSET RESTART ({branch}): Activated {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, {purchases} purchases")
                else:
                    action = "failed"
                    adset_fail += 1
                    logger.warning(f"ADSET RESTART: Failed to activate {adset_id}: {reason}")

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

        # Skip adsets with OFF in name
        if "OFF" in current_name.upper():
            continue

        # STOP-LOSS: ACTIVE and spend > $30 and (
        #   (ROAS < 1.6 AND CPA/ATC > $10) OR
        #   (0 purchases AND 0 ATCs) OR
        #   (0 purchases)
        # )
        bad_efficiency = (
            roas < TESTING_ADSET_ROAS_THRESHOLD
            and cost_per_atc > TESTING_ADSET_CPA_ATC_THRESHOLD
        )
        dead = purchases == 0 and atcs == 0
        no_purchase = purchases == 0
        if (status == "ACTIVE"
            and spend > TESTING_ADSET_SPEND_THRESHOLD
            and (bad_efficiency or dead or no_purchase)):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "PAUSED")
                if success:
                    if dead:
                        branch = "dead"
                    elif no_purchase and not bad_efficiency:
                        branch = "no-purchase"
                    else:
                        branch = "efficiency"
                    action = "paused"
                    testing_stop += 1
                    logger.info(f"TESTING ADSET STOP ({branch}): Paused {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, CPA/ATC ${cost_per_atc:.2f}, {purchases}p / {atcs} ATC")
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

        # RESTART: PAUSED, spend > $50, and one of:
        #   - ROAS >= 1.6 (recovered on hard KPI), OR
        #   - has purchases AND CPA/ATC <= $10 (converting cheaply)
        # We no longer restart on "has ATCs" alone — that flip-flopped with
        # the new "0 purchases → pause" stop-loss branch.
        recovered = (
            roas >= TESTING_ADSET_ROAS_THRESHOLD
            or (
                purchases > 0
                and cost_per_atc > 0
                and cost_per_atc <= TESTING_ADSET_CPA_ATC_THRESHOLD
            )
        )
        if (status == "PAUSED"
            and spend > TESTING_ADSET_SPEND_THRESHOLD
            and recovered):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, adset_id, "ACTIVE")
                if success:
                    action = "activated"
                    testing_restart += 1
                    logger.info(f"TESTING ADSET RESTART: Activated {adset_id} ({current_name}) — spend ${spend:.2f}, ROAS {roas:.2f}, CPA/ATC ${cost_per_atc:.2f}, {purchases}p / {atcs} ATC")
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

    logger.info(
        f"Stop-loss complete: "
        f"AD (CC/VALUE/SCALE): {ad_stop} paused, {ad_restart} activated, {ad_fail} failed │ "
        f"ADSET (CC/VALUE/SCALE): {adset_stop} paused, {adset_restart} activated, {adset_fail} failed │ "
        f"ADSET (TESTING): {testing_stop} paused, {testing_restart} activated, {testing_fail} failed"
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
            f"_Rules: CC, VALUE, or SCALE campaigns │ Today's metrics_\n"
            f"_Adset stop: (spend>${ADSET_STOP_NO_PURCHASE_SPEND:.0f} & 0 purchases) OR (spend>${ADSET_STOP_SPEND_THRESHOLD:.0f} & ROAS<{ADSET_STOP_ROAS_THRESHOLD})_\n"
            f"_Adset restart: (spend>${ADSET_RESTART_SPEND_THRESHOLD:.0f} & ROAS>{ADSET_RESTART_ROAS_THRESHOLD}) OR (spend ${ADSET_RESTART_LOW_SPEND_MIN:.0f}-${ADSET_RESTART_LOW_SPEND_MAX:.0f} & has purchases)_\n"
            f"_TESTING adset stop: spend>${TESTING_ADSET_SPEND_THRESHOLD:.0f} & ((ROAS<{TESTING_ADSET_ROAS_THRESHOLD} & CPA/ATC>${TESTING_ADSET_CPA_ATC_THRESHOLD:.0f}) OR 0 purchases)_\n"
            f"_TESTING adset restart: spend>${TESTING_ADSET_SPEND_THRESHOLD:.0f} & (ROAS>={TESTING_ADSET_ROAS_THRESHOLD} OR (has purchases & CPA/ATC<=${TESTING_ADSET_CPA_ATC_THRESHOLD:.0f}))_"
        )}
    })

    blocks.append({"type": "divider"})

    MAX_DISPLAY = 10

    def _format_ad(a: StopLossAction, emoji: str) -> str:
        cpa_line = ""
        if a.purchases > 0:
            cpa = a.spend / a.purchases
            cpa_line = f" │ CPA: ${cpa:.2f}"
        return (
            f"{emoji} *{a.ad_name}*\n"
            f"Campaign: `{a.campaign_name}` │ Adset: `{a.adset_name}`\n"
            f"Ad: spend ${a.spend:.2f} │ ROAS {a.roas:.2f}x │ Rev ${a.revenue:.2f} │ {a.purchases} purchases{cpa_line}\n"
            f"Adset: spend ${a.adset_spend:.2f} │ ROAS {a.adset_roas:.2f}x"
        )

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
