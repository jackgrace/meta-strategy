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
from slack_reporter import send_testing_missed_opps, send_early_fatigue_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_check() -> dict:
    """
    Daily run: disabled.
    Auto-pause disabled due to Meta API blocking ASC ad updates.
    """
    logger.info("=== No checks configured — skipping ===")
    return {"status": "ok", "message": "No checks configured"}


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


if __name__ == "__main__":
    main()
