"""
Meta Ad Fatigue Agent — Two daily reports:
1. Testing Winner Signals — paused TESTING ads with strong ATC
2. Early Fatigue Signals — non-TESTING ads showing decline (14d baseline vs 3d recent)

Supports multiple ad accounts via a comma-separated META_AD_ACCOUNT_ID
env var. Each rule runs once per account.
"""

import dataclasses
import os
import sys
import logging

from config import Config
from meta_api import fetch_ad_insights, fetch_ad_statuses
from testing_analyzer import analyze_testing_missed_opportunities
from early_fatigue import analyze_early_fatigue
from auto_pause import find_pause_candidates, execute_pause, send_pause_report
from stop_loss import run_stop_loss as _run_stop_loss, send_stop_loss_report
from testing_kill import run_testing_kill as _run_testing_kill, send_testing_kill_report
from midnight_restart import run_midnight_restart as _run_midnight_restart, send_midnight_report
from slack_reporter import send_testing_missed_opps, send_early_fatigue_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _per_account_configs() -> list[Config]:
    """
    Parse META_AD_ACCOUNT_ID as a comma-separated list and return
    one Config per account. Falls back to a single-config list.
    """
    base = Config.from_env()
    ids = [i.strip() for i in base.meta_ad_account_id.split(",") if i.strip()]
    if not ids:
        return [base]
    return [dataclasses.replace(base, meta_ad_account_id=aid) for aid in ids]


def _dry_run() -> bool:
    return os.environ.get("AUTO_PAUSE_ENABLED", "").lower() != "true"


def run_check() -> dict:
    """
    Daily run: auto-pause (14d spend<\$30) is DISABLED.
    /pause and /slack/pause endpoints still call run_auto_pause() directly.
    """
    logger.info("Auto-pause disabled — daily run is a no-op.")
    return {"status": "ok", "message": "auto-pause disabled"}


def run_fatigue_only() -> dict:
    """Run ONLY the fatigue check (no testing report). For /fatigue endpoint."""
    logger.info("=== Fatigue check starting ===")
    per_account = []
    for config in _per_account_configs():
        logger.info(f"--- Fatigue: account {config.meta_ad_account_id} ---")
        metrics = fetch_ad_insights(config)
        if not metrics:
            per_account.append({"account": config.meta_ad_account_id, "fatigue_alerts": 0})
            continue

        ad_ids = {m.ad_id for m in metrics}
        try:
            ad_statuses = fetch_ad_statuses(config, ad_ids=ad_ids)
        except Exception as e:
            logger.error(f"Failed to fetch ad statuses: {e}")
            ad_statuses = {}

        fatigue_alerts, _ = analyze_early_fatigue(metrics, ad_statuses, config)
        send_early_fatigue_report(fatigue_alerts, config)
        per_account.append({
            "account": config.meta_ad_account_id,
            "fatigue_alerts": len(fatigue_alerts),
        })

    return {"status": "ok", "per_account": per_account}


def run_auto_pause() -> dict:
    """Run the auto-pause check across all configured accounts."""
    dry_run = _dry_run()
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Auto-pause [{mode}] ===")

    per_account = []
    for config in _per_account_configs():
        logger.info(f"--- Auto-pause: account {config.meta_ad_account_id} ---")
        try:
            ad_statuses = fetch_ad_statuses(config)
        except Exception as e:
            logger.error(f"Failed to fetch ad statuses: {e}")
            ad_statuses = {}
        candidates = find_pause_candidates(ad_statuses, config)
        candidates = execute_pause(candidates, config, dry_run=dry_run)
        send_pause_report(candidates, dry_run, config)
        per_account.append({
            "account": config.meta_ad_account_id,
            "candidates": len(candidates),
            "paused": sum(1 for c in candidates if c.action_taken == "paused"),
        })

    return {"status": "ok", "mode": mode, "per_account": per_account}


def run_stop_loss() -> dict:
    """Intra-day stop-loss + restart check across all accounts."""
    dry_run = _dry_run()
    mode = "DRY RUN" if dry_run else "LIVE"
    per_account = []
    for config in _per_account_configs():
        logger.info(f"--- Stop-loss: account {config.meta_ad_account_id} ---")
        ad_actions, adset_actions = _run_stop_loss(config, dry_run=dry_run)
        send_stop_loss_report(ad_actions, adset_actions, dry_run, config)
        per_account.append({
            "account": config.meta_ad_account_id,
            "ads_paused": sum(1 for a in ad_actions if a.action == "paused"),
            "ads_activated": sum(1 for a in ad_actions if a.action == "activated"),
            "ads_failed": sum(1 for a in ad_actions if a.action == "failed"),
            "adsets_paused": sum(1 for a in adset_actions if a.action == "paused"),
            "adsets_activated": sum(1 for a in adset_actions if a.action == "activated"),
            "adsets_failed": sum(1 for a in adset_actions if a.action == "failed"),
        })
    return {"status": "ok", "mode": mode, "per_account": per_account}


def run_testing_kill() -> dict:
    """Kill underperforming TESTING campaign ads across all accounts."""
    dry_run = _dry_run()
    mode = "DRY RUN" if dry_run else "LIVE"
    per_account = []
    for config in _per_account_configs():
        logger.info(f"--- Testing-kill: account {config.meta_ad_account_id} ---")
        actions = _run_testing_kill(config, dry_run=dry_run)
        send_testing_kill_report(actions, dry_run, config)
        per_account.append({
            "account": config.meta_ad_account_id,
            "paused": sum(1 for a in actions if a.action == "paused"),
            "would_pause": sum(1 for a in actions if a.action == "would_pause"),
            "failed": sum(1 for a in actions if a.action == "failed"),
        })
    return {"status": "ok", "mode": mode, "per_account": per_account}


def run_midnight_restart() -> dict:
    """Midnight adset restart across all accounts."""
    dry_run = _dry_run()
    mode = "DRY RUN" if dry_run else "LIVE"
    per_account = []
    for config in _per_account_configs():
        logger.info(f"--- Midnight restart: account {config.meta_ad_account_id} ---")
        actions = _run_midnight_restart(config, dry_run=dry_run)
        send_midnight_report(actions, dry_run, config)
        per_account.append({
            "account": config.meta_ad_account_id,
            "activated": sum(1 for a in actions if a.action == "activated"),
            "would_activate": sum(1 for a in actions if a.action == "would_activate"),
            "failed": sum(1 for a in actions if a.action == "failed"),
        })
    return {"status": "ok", "mode": mode, "per_account": per_account}


def main():
    try:
        result = run_check()
    except KeyError as e:
        logger.error(f"Missing required environment variable: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
