"""
Testing campaign kill rule.

Runs hourly. Timeframe: last 30 days.

PAUSE ad IF:
- Campaign name contains 'TESTING'
- Adset name does NOT contain 'OFF'
- Ad name does NOT contain 'RUN'
- Ad is currently ACTIVE
- AND either branch matches:
    A) DEAD:       30d spend > $50  AND  (0 ATCs OR 0 purchases)
    B) EFFICIENCY: 30d spend > $50  AND  CPA/ATC < $10  AND  (0 purchases OR ROAS < 1.6)
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

# Flip TESTING_KILL_ENABLED to True to re-enable the hourly 30d kill.
TESTING_KILL_ENABLED = True
DEAD_SPEND_THRESHOLD = 50.0        # spend > $50 & (0 ATCs OR 0 purchases)
EFFICIENCY_SPEND_THRESHOLD = 50.0  # spend > $50 & CPA/ATC < $10 & (0 purchases OR ROAS < 1.6)
CHEAP_ATC_THRESHOLD = 10.0         # "cheap ATCs" cutoff
MIN_ROAS_FOR_KILL = 1.6


@dataclass
class TestingKillAction:
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    action: str  # "would_pause" | "paused" | "failed"
    reason: str  # rule that triggered or failure reason
    lifetime_spend: float
    lifetime_revenue: float
    lifetime_roas: float
    lifetime_purchases: int
    lifetime_atcs: int
    cost_per_atc: float


def _fetch_lifetime_insights(config: Config) -> dict:
    """
    Fetch ad-level insights for ACTIVE ads over the last 30 days.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_name,adset_name,spend,actions,action_values,cost_per_action_type",
        "date_preset": "last_30d",
        "limit": 200,
        "filtering": '[{"field":"ad.effective_status","operator":"IN","value":["ACTIVE","IN_REVIEW","WITH_ISSUES"]}]',
    }

    ads = {}
    page_count = 0

    while url:
        page_count += 1

        resp = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params if page_count == 1 else None, timeout=120)

                # Broader retry detection: transient markers, rate limits, service messages
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
                    logger.warning(f"Lifetime fetch {resp.status_code}, retrying in {wait}s (attempt {attempt + 1}/5, body: {resp.text[:200]})")
                    time.sleep(wait)
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < 4:
                    wait = [30, 60, 120, 240][attempt]
                    logger.warning(f"Lifetime fetch network error, retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    raise

        if not resp.ok:
            body_preview = resp.text[:400]
            logger.error(f"Lifetime insights fetch error {resp.status_code}: {body_preview}")
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

            # Prefer Meta's reported CPA; fall back to spend/atcs
            cost_per_atc = cost_per_atc_reported if cost_per_atc_reported > 0 else (
                spend / atcs if atcs > 0 else 0
            )

            ads[row["ad_id"]] = {
                "ad_name": row.get("ad_name", "Unknown"),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "adset_name": row.get("adset_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "purchases": purchases,
                "atcs": atcs,
                "cost_per_atc": cost_per_atc,
                "roas": revenue / spend if spend > 0 else 0,
            }

        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched lifetime insights for {len(ads)} ads across {page_count} pages")
    return ads


def _update_ad_status(config: Config, ad_id: str, new_status: str) -> tuple[bool, str]:
    """Send status update to Meta with retry."""
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


def run_testing_kill(config: Config, dry_run: bool = False) -> list[TestingKillAction]:
    """Evaluate TESTING campaigns and pause underperformers."""
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Testing kill check [{mode}] ===")

    if not TESTING_KILL_ENABLED:
        logger.info("Testing kill: DISABLED via TESTING_KILL_ENABLED flag — skipping")
        return []

    lifetime = _fetch_lifetime_insights(config)
    if not lifetime:
        return []

    actions: list[TestingKillAction] = []
    considered = 0
    paused_count = 0
    failed_count = 0

    for ad_id, ad in lifetime.items():
        # Campaign must contain TESTING
        if "TESTING" not in ad["campaign_name"].upper():
            continue

        # Skip if adset name contains OFF
        if "OFF" in ad["adset_name"].upper():
            continue

        # Skip if ad name contains RUN
        if "RUN" in ad["ad_name"].upper():
            continue

        # Broadest spend gate to enter evaluation
        if ad["spend"] <= DEAD_SPEND_THRESHOLD:
            continue

        considered += 1

        # Rule branches (each has its own spend gate):
        # A) DEAD:       spend > $30 & (0 ATCs OR 0 purchases)
        # B) EFFICIENCY: spend > $30 & CPA/ATC < $10 & (0 purchases OR ROAS < 1.6)
        #    "cheap ATCs that don't convert" — engagement but no sales
        dead = ad["spend"] > DEAD_SPEND_THRESHOLD and (ad["atcs"] == 0 or ad["purchases"] == 0)
        efficiency = (
            ad["spend"] > EFFICIENCY_SPEND_THRESHOLD
            and ad["cost_per_atc"] > 0
            and ad["cost_per_atc"] < CHEAP_ATC_THRESHOLD
            and (ad["purchases"] == 0 or ad["roas"] < MIN_ROAS_FOR_KILL)
        )

        if dead:
            missing = []
            if ad["atcs"] == 0:
                missing.append("0 ATCs")
            if ad["purchases"] == 0:
                missing.append("0 purchases")
            reason = f"spend ${ad['spend']:.2f} & " + " & ".join(missing)
        elif efficiency:
            failing = "0 purchases" if ad["purchases"] == 0 else f"ROAS {ad['roas']:.2f} < {MIN_ROAS_FOR_KILL}"
            reason = f"spend ${ad['spend']:.2f} & CPA/ATC ${ad['cost_per_atc']:.2f} < ${CHEAP_ATC_THRESHOLD:.0f} & {failing}"
        else:
            continue  # doesn't meet either kill condition

        if dry_run:
            action = "would_pause"
            update_reason = "dry run"
        else:
            success, update_reason = _update_ad_status(config, ad_id, "PAUSED")
            if success:
                action = "paused"
                paused_count += 1
                logger.info(f"TESTING KILL: Paused {ad_id} ({ad['ad_name']}) — {reason}")
            else:
                action = "failed"
                failed_count += 1
                logger.warning(f"TESTING KILL: Failed to pause {ad_id}: {update_reason}")

        actions.append(TestingKillAction(
            ad_id=ad_id,
            ad_name=ad["ad_name"],
            campaign_name=ad["campaign_name"],
            adset_name=ad["adset_name"],
            action=action,
            reason=reason if action != "failed" else f"{reason} │ {update_reason}",
            lifetime_spend=ad["spend"],
            lifetime_revenue=ad["revenue"],
            lifetime_roas=ad["roas"],
            lifetime_purchases=ad["purchases"],
            lifetime_atcs=ad["atcs"],
            cost_per_atc=ad["cost_per_atc"],
        ))

    logger.info(
        f"Testing kill: {considered} considered │ "
        f"{paused_count} paused │ {failed_count} failed [{mode}]"
    )
    return actions


def build_testing_kill_message(actions: list[TestingKillAction], dry_run: bool) -> dict | None:
    """Build Slack message for testing kill run."""
    if not actions:
        return None

    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    paused = [a for a in actions if a.action in ("paused", "would_pause")]
    failed = [a for a in actions if a.action == "failed"]

    blocks = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🧪 Testing Kill Report — {now}"}
    })

    summary_parts = []
    if paused:
        verb = "would pause" if dry_run else "paused"
        summary_parts.append(f"🔴 {len(paused)} {verb}")
    if failed:
        summary_parts.append(f"⚠️ {len(failed)} failed")

    total_spend = sum(a.lifetime_spend for a in actions)

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(summary_parts) + "\n"
            f"_Rule: TESTING campaign │ adset not OFF │ ad not RUN │ 30d metrics_\n"
            f"_(spend>${DEAD_SPEND_THRESHOLD:.0f} & (0 ATCs OR 0 purchases)) OR "
            f"(spend>${EFFICIENCY_SPEND_THRESHOLD:.0f} & CPA/ATC<${CHEAP_ATC_THRESHOLD:.0f} & (0 purchases OR ROAS<{MIN_ROAS_FOR_KILL}))_\n"
            f"Total 30d spend on flagged ads: *${total_spend:.2f}*"
        )}
    })

    blocks.append({"type": "divider"})

    MAX_DISPLAY = 15

    def _format(a: TestingKillAction, emoji: str) -> str:
        cpa_txt = f"${a.cost_per_atc:.2f}" if a.cost_per_atc > 0 else "—"
        return (
            f"{emoji} *{a.ad_name}*\n"
            f"Campaign: `{a.campaign_name}` │ Adset: `{a.adset_name}`\n"
            f"30d: spend ${a.lifetime_spend:.2f} │ rev ${a.lifetime_revenue:.2f} │ ROAS {a.lifetime_roas:.2f}x │ {a.lifetime_purchases} purchases │ {a.lifetime_atcs} ATCs │ CPA/ATC {cpa_txt}\n"
            f"_Why: {a.reason}_"
        )

    if paused:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "🔴 *Paused*"}})
        for a in paused[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _format(a, "🔴")}})
        overflow = len(paused) - min(MAX_DISPLAY, len(paused))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    if failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⚠️ *Failed (needs manual pause)*"}})
        for a in failed[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _format(a, "⚠️")}})

    return {"blocks": blocks}


def send_testing_kill_report(actions: list[TestingKillAction], dry_run: bool, config: Config) -> bool:
    """Send testing kill report to Slack. Quiet if no actions."""
    payload = build_testing_kill_message(actions, dry_run)
    if payload is None:
        logger.info("No testing kill actions — skipping Slack")
        return True

    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected testing kill report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send testing kill report: {e}")
        return False
