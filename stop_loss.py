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
ADSET_STOP_SPEND_THRESHOLD = 1000.0       # spend gate for ROAS check
ADSET_STOP_ROAS_THRESHOLD = 1.2
ADSET_STOP_NO_PURCHASE_SPEND = 250.0      # alt: pause if spend>this AND 0 purchases

ADSET_RESTART_SPEND_THRESHOLD = 500.0     # spend gate for ROAS check
ADSET_RESTART_ROAS_THRESHOLD = 1.2
ADSET_RESTART_LOW_SPEND_MIN = 250.0       # alt band: spend $250-500 with purchases
ADSET_RESTART_LOW_SPEND_MAX = 500.0


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
        "fields": "ad_id,ad_name,campaign_id,campaign_name,adset_id,adset_name,spend,actions,action_values,cost_per_action_type",
        "date_preset": "today",
        "limit": 200,
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
            cost_per_atc_reported = 0.0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                if a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))
                elif a.get("action_type") == "add_to_cart":
                    atcs = int(float(a.get("value", 0)))
            for cpa in row.get("cost_per_action_type", []) or []:
                if cpa.get("action_type") == "add_to_cart":
                    cost_per_atc_reported = float(cpa.get("value", 0))

            cost_per_atc = cost_per_atc_reported if cost_per_atc_reported > 0 else (
                spend / atcs if atcs > 0 else 0
            )

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
    """Aggregate ROAS by adset_id from ad-level data."""
    adsets = defaultdict(lambda: {"spend": 0, "revenue": 0})
    for ad in ads.values():
        if ad["adset_id"]:
            adsets[ad["adset_id"]]["spend"] += ad["spend"]
            adsets[ad["adset_id"]]["revenue"] += ad["revenue"]

    return {
        adset_id: {
            "spend": v["spend"],
            "revenue": v["revenue"],
            "roas": v["revenue"] / v["spend"] if v["spend"] > 0 else 0,
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

    # Fetch statuses for CC-campaign adsets
    target_adset_ids: set[str] = set()
    adset_meta: dict[str, dict] = {}
    for ad in today_ads.values():
        if _is_target_campaign(ad["campaign_name"]) and ad["adset_id"]:
            target_adset_ids.add(ad["adset_id"])
            adset_meta[ad["adset_id"]] = {
                "adset_name": ad["adset_name"],
                "campaign_name": ad["campaign_name"],
            }

    try:
        adset_info = fetch_adset_statuses(config, target_adset_ids)
    except Exception as e:
        logger.error(f"Adset status fetch failed: {e}")
        adset_info = {}

    actions: list[StopLossAction] = []
    adset_actions: list[AdsetAction] = []
    stop_count = 0
    restart_count = 0
    fail_count = 0
    adset_stop = 0
    adset_restart = 0
    adset_fail = 0

    for ad_id, ad in today_ads.items():
        # Only target campaigns (CC, VALUE, or SCALE)
        if not _is_target_campaign(ad["campaign_name"]):
            continue

        info = ad_info.get(ad_id, {})
        status = info.get("status", "UNKNOWN")
        current_name = info.get("name", ad["ad_name"])

        adset_data = adset_roas.get(ad["adset_id"], {})
        adset_spend = adset_data.get("spend", 0)
        adset_r = adset_data.get("roas", 0)

        campaign_data = campaign_roas.get(ad["campaign_id"], {})
        campaign_r = campaign_data.get("roas", 0)

        # STOP-LOSS: ACTIVE + all thresholds breached
        if (status == "ACTIVE"
            and ad["spend"] > STOP_SPEND_THRESHOLD
            and ad["roas"] < STOP_ROAS_THRESHOLD
            and ad["cost_per_atc"] > STOP_CPA_ATC_THRESHOLD
            and campaign_r < STOP_CAMPAIGN_ROAS_THRESHOLD):

            if dry_run:
                action, reason = "would_pause", "dry run"
            else:
                success, reason = _update_ad_status(config, ad_id, "PAUSED")
                if success:
                    action = "paused"
                    stop_count += 1
                    logger.info(f"STOP-LOSS: Paused {ad_id} ({ad['ad_name']}) — spend ${ad['spend']:.2f}, ROAS {ad['roas']:.2f}, CPA/ATC ${ad['cost_per_atc']:.2f}, campaign ROAS {campaign_r:.2f}")
                else:
                    action = "failed"
                    fail_count += 1
                    logger.warning(f"STOP-LOSS: Failed to pause {ad_id}: {reason}")

            actions.append(StopLossAction(
                ad_id=ad_id,
                ad_name=ad["ad_name"],
                campaign_name=ad["campaign_name"],
                adset_name=ad["adset_name"],
                action=action,
                reason=reason,
                spend=ad["spend"],
                roas=ad["roas"],
                revenue=ad["revenue"],
                purchases=ad["purchases"],
                adset_spend=adset_spend,
                adset_roas=adset_r,
            ))
            continue

        # RESTART: only if ad itself is PAUSED (not blocked by parent being off).
        # Statuses like ADSET_PAUSED / CAMPAIGN_PAUSED mean the ad isn't the
        # one that was turned off — reactivating it is a no-op and creates
        # false Slack notifications.
        # Also skip if EITHER the ad name OR the adset name contains "OFF".
        has_off = (
            "OFF" in current_name.upper()
            or "OFF" in ad.get("adset_name", "").upper()
        )
        good_roas = ad["roas"] > RESTART_ROAS_THRESHOLD
        good_cpa = ad["cost_per_atc"] > 0 and ad["cost_per_atc"] < RESTART_CPA_ATC_THRESHOLD
        if (status == "PAUSED" and not has_off
            and ad["spend"] > RESTART_SPEND_THRESHOLD
            and (good_roas or good_cpa)):

            if dry_run:
                action, reason = "would_activate", "dry run"
            else:
                success, reason = _update_ad_status(config, ad_id, "ACTIVE")
                if success:
                    action = "activated"
                    restart_count += 1
                    logger.info(f"RESTART: Activated {ad_id} ({ad['ad_name']}) — spend ${ad['spend']:.2f}, ROAS {ad['roas']:.2f}, CPA/ATC ${ad['cost_per_atc']:.2f}")
                else:
                    action = "failed"
                    fail_count += 1
                    logger.warning(f"RESTART: Failed to activate {ad_id}: {reason}")

            actions.append(StopLossAction(
                ad_id=ad_id,
                ad_name=ad["ad_name"],
                campaign_name=ad["campaign_name"],
                adset_name=ad["adset_name"],
                action=action,
                reason=reason,
                spend=ad["spend"],
                roas=ad["roas"],
                revenue=ad["revenue"],
                purchases=ad["purchases"],
                adset_spend=adset_spend,
                adset_roas=adset_r,
            ))

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

        # STOP-LOSS: ACTIVE and (
        #   (spend > $500 AND ROAS < 1.6) OR
        #   (spend > $250 AND 0 purchases)
        # )
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

    ad_label = f"AD: {stop_count} paused, {restart_count} activated, {fail_count} failed"
    logger.info(
        f"Stop-loss complete: {ad_label} │ "
        f"ADSET: {adset_stop} paused, {adset_restart} activated, {adset_fail} failed"
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
            f"_Ad stop: spend>${STOP_SPEND_THRESHOLD:.0f} & ROAS<{STOP_ROAS_THRESHOLD} & CPA/ATC>${STOP_CPA_ATC_THRESHOLD:.0f} & campaign ROAS<{STOP_CAMPAIGN_ROAS_THRESHOLD}_\n"
            f"_Ad restart: no 'OFF' in name & spend>${RESTART_SPEND_THRESHOLD:.0f} & (ROAS>{RESTART_ROAS_THRESHOLD} OR CPA/ATC<${RESTART_CPA_ATC_THRESHOLD:.0f})_\n"
            f"_Adset stop: (spend>${ADSET_STOP_SPEND_THRESHOLD:.0f} & ROAS<{ADSET_STOP_ROAS_THRESHOLD}) OR (spend>${ADSET_STOP_NO_PURCHASE_SPEND:.0f} & 0 purchases)_\n"
            f"_Adset restart: (spend>${ADSET_RESTART_SPEND_THRESHOLD:.0f} & ROAS>{ADSET_RESTART_ROAS_THRESHOLD}) OR (spend ${ADSET_RESTART_LOW_SPEND_MIN:.0f}-${ADSET_RESTART_LOW_SPEND_MAX:.0f} & has purchases)_"
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
