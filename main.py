"""
Meta Ad Fatigue Agent — Two daily reports:
1. Testing Winner Signals — paused TESTING ads with strong ATC
2. Early Fatigue Signals — non-TESTING ads showing decline (14d baseline vs 3d recent)
"""

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

    config = Config.from_env()

    logger.info("Fetching ad insights from Meta Marketing API...")
    metrics = fetch_ad_insights(config)

    if not metrics:
        logger.warning("No ad data returned from Meta API")
        return {"status": "ok", "message": "No ad data returned"}

    logger.info("Fetching ad statuses...")
    ad_ids = {m.ad_id for m in metrics}
    try:
        ad_statuses = fetch_ad_statuses(config, ad_ids=ad_ids)
    except Exception as e:
        logger.error(f"Failed to fetch ad statuses: {e}")
        ad_statuses = {}

    logger.info("--- Running Early Fatigue Signals ---")
    fatigue_alerts, log_entries = analyze_early_fatigue(metrics, ad_statuses, config)
    fatigue_ok = send_early_fatigue_report(fatigue_alerts, config)

    logger.info(f"=== Complete: {len(fatigue_alerts)} fatigue alerts ===")

    return {
        "status": "ok" if fatigue_ok else "error",
        "fatigue_alerts": len(fatigue_alerts),
        "fatigue_sent": fatigue_ok,
    }


def run_auto_pause() -> dict:
    """Run the auto-pause check. Dry-run unless AUTO_PAUSE_ENABLED=true."""
    import os
    dry_run = os.environ.get("AUTO_PAUSE_ENABLED", "").lower() != "true"
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Auto-pause check starting [{mode}] ===")

    config = Config.from_env()

    # Fetch ad statuses (all ads, not just from insights — we need created_time)
    logger.info("Fetching ad statuses...")
    try:
        ad_statuses = fetch_ad_statuses(config)
    except Exception as e:
        logger.error(f"Failed to fetch ad statuses: {e}")
        ad_statuses = {}

    # Auto-pause does its own lightweight spend fetch internally
    candidates = find_pause_candidates(ad_statuses, config)
    candidates = execute_pause(candidates, config, dry_run=dry_run)
    pause_ok = send_pause_report(candidates, dry_run, config)

    paused_count = sum(1 for c in candidates if c.action_taken == "paused")
    logger.info(f"=== Complete: {len(candidates)} candidates, {paused_count} paused [{mode}] ===")

    return {
        "status": "ok" if pause_ok else "error",
        "mode": mode,
        "candidates": len(candidates),
        "paused": paused_count,
    }


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


def run_stop_loss() -> dict:
    """Intra-day stop-loss + restart check (ads + adsets). Live unless AUTO_PAUSE_ENABLED != 'true'."""
    import os
    dry_run = os.environ.get("AUTO_PAUSE_ENABLED", "").lower() != "true"
    config = Config.from_env()
    ad_actions, adset_actions = _run_stop_loss(config, dry_run=dry_run)
    send_stop_loss_report(ad_actions, adset_actions, dry_run, config)
    return {
        "status": "ok",
        "mode": "DRY RUN" if dry_run else "LIVE",
        "ads_paused": sum(1 for a in ad_actions if a.action == "paused"),
        "ads_activated": sum(1 for a in ad_actions if a.action == "activated"),
        "ads_failed": sum(1 for a in ad_actions if a.action == "failed"),
        "adsets_paused": sum(1 for a in adset_actions if a.action == "paused"),
        "adsets_activated": sum(1 for a in adset_actions if a.action == "activated"),
        "adsets_failed": sum(1 for a in adset_actions if a.action == "failed"),
    }


def run_testing_kill() -> dict:
    """Kill underperforming TESTING campaign ads. Live unless AUTO_PAUSE_ENABLED != 'true'."""
    import os
    dry_run = os.environ.get("AUTO_PAUSE_ENABLED", "").lower() != "true"
    config = Config.from_env()
    actions = _run_testing_kill(config, dry_run=dry_run)
    send_testing_kill_report(actions, dry_run, config)
    return {
        "status": "ok",
        "mode": "DRY RUN" if dry_run else "LIVE",
        "paused": sum(1 for a in actions if a.action == "paused"),
        "would_pause": sum(1 for a in actions if a.action == "would_pause"),
        "failed": sum(1 for a in actions if a.action == "failed"),
    }


def run_midnight_restart() -> dict:
    """Midnight adset restart. Live unless AUTO_PAUSE_ENABLED != 'true'."""
    import os
    dry_run = os.environ.get("AUTO_PAUSE_ENABLED", "").lower() != "true"
    config = Config.from_env()
    actions = _run_midnight_restart(config, dry_run=dry_run)
    send_midnight_report(actions, dry_run, config)
    return {
        "status": "ok",
        "mode": "DRY RUN" if dry_run else "LIVE",
        "activated": sum(1 for a in actions if a.action == "activated"),
        "would_activate": sum(1 for a in actions if a.action == "would_activate"),
        "failed": sum(1 for a in actions if a.action == "failed"),
    }


if __name__ == "__main__":
    main()
