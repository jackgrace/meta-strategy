"""
TESTING surf reset. Runs daily at 12:05am AEST, before the midnight restart.

Intra-day surf scaling (stop_loss.py) doubles budgets on winning testing
adsets. This puts every TESTING adset whose daily budget is above the $250
base back to $250 so each day starts from the same baseline.
"""

import logging
from dataclasses import dataclass

import requests

from config import Config
from meta_api import API_BASE
from midnight_restart import _http_get_json
from stop_loss import TESTING_SURF_BASE_BUDGET, _update_adset_budget

logger = logging.getLogger(__name__)

TESTING_SURF_RESET_ENABLED = False


@dataclass
class SurfResetAction:
    adset_id: str
    adset_name: str
    campaign_name: str
    old_budget: float
    new_budget: float
    action: str  # "would_reset" | "reset" | "failed"
    reason: str = ""


def _fetch_testing_adsets_over_base(config: Config) -> list[dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/adsets"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,name,daily_budget,effective_status,campaign{name}",
        "limit": 500,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE","PAUSED","CAMPAIGN_PAUSED"]}]',
    }
    result = []
    first = True
    while url:
        data = _http_get_json(config, url, params if first else None)
        first = False
        for row in data.get("data", []):
            campaign_name = (row.get("campaign") or {}).get("name", "")
            if "TESTING" not in campaign_name.upper():
                continue
            try:
                budget = float(row.get("daily_budget") or 0) / 100.0
            except (TypeError, ValueError):
                continue
            if budget > TESTING_SURF_BASE_BUDGET:
                result.append({
                    "id": row["id"],
                    "name": row.get("name", "Unknown"),
                    "campaign_name": campaign_name,
                    "budget": budget,
                })
        url = data.get("paging", {}).get("next")
    logger.info(f"Surf reset: {len(result)} TESTING adsets above ${TESTING_SURF_BASE_BUDGET:.0f}")
    return result


def run_surf_reset(config: Config, dry_run: bool = False) -> list[SurfResetAction]:
    if not TESTING_SURF_RESET_ENABLED:
        logger.info("Surf reset: DISABLED via TESTING_SURF_RESET_ENABLED flag — skipping")
        return []

    actions = []
    for a in _fetch_testing_adsets_over_base(config):
        act = SurfResetAction(
            adset_id=a["id"], adset_name=a["name"], campaign_name=a["campaign_name"],
            old_budget=a["budget"], new_budget=TESTING_SURF_BASE_BUDGET, action="would_reset",
        )
        if not dry_run:
            ok, err = _update_adset_budget(config, a["id"], TESTING_SURF_BASE_BUDGET)
            if ok:
                act.action = "reset"
                logger.info(f"Surf reset: {a['id']} ({a['name']}) ${a['budget']:.0f} → ${TESTING_SURF_BASE_BUDGET:.0f}")
            else:
                act.action, act.reason = "failed", err
                logger.warning(f"Surf reset: failed on {a['id']}: {err}")
        actions.append(act)
    return actions


def send_surf_reset_report(actions: list[SurfResetAction], dry_run: bool, config: Config) -> bool:
    if not actions:
        return True
    mode = "DRY RUN" if dry_run else "LIVE"
    lines = [
        f"• *{a.adset_name}* ${a.old_budget:.0f} → ${a.new_budget:.0f}"
        + ("" if a.action != "failed" else f" _(failed: {a.reason})_")
        for a in actions[:30]
    ]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🌊 TESTING surf reset — {len(actions)} adsets"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* Budgets above ${TESTING_SURF_BASE_BUDGET:.0f} reset for the new day."
        )}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send surf reset report: {e}")
        return False
