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
from slack_reporter import send_testing_missed_opps, send_early_fatigue_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_check() -> dict:
    """
    Daily run: both reports.
    1. Testing Winner Signals
    2. Early Fatigue Signals
    """
    logger.info("=== Daily check starting ===")

    config = Config.from_env()

    # Fetch data (shared by both reports)
    logger.info("Fetching ad insights from Meta Marketing API...")
    metrics = fetch_ad_insights(config)

    if not metrics:
        logger.warning("No ad data returned from Meta API")
        return {"status": "ok", "message": "No ad data returned"}

    # Fetch ad statuses (only for ads we have insights data for)
    logger.info("Fetching ad statuses...")
    ad_ids = {m.ad_id for m in metrics}
    try:
        ad_statuses = fetch_ad_statuses(config, ad_ids=ad_ids)
    except Exception as e:
        logger.error(f"Failed to fetch ad statuses: {e}")
        ad_statuses = {}

    results = {}

    # Report 1: Testing Winner Signals
    logger.info("--- Running Testing Winner Signals ---")
    testing_reports = analyze_testing_missed_opportunities(metrics, ad_statuses, config)
    testing_ok = send_testing_missed_opps(testing_reports, config)
    results["testing_flagged"] = len(testing_reports)
    results["testing_sent"] = testing_ok

    # Report 2: Early Fatigue Signals
    logger.info("--- Running Early Fatigue Signals ---")
    fatigue_alerts, log_entries = analyze_early_fatigue(metrics, ad_statuses, config)
    fatigue_ok = send_early_fatigue_report(fatigue_alerts, config)
    results["fatigue_alerts"] = len(fatigue_alerts)
    results["fatigue_sent"] = fatigue_ok
    results["fatigue_log_entries"] = len(log_entries)

    logger.info(
        f"=== Complete: {results['testing_flagged']} testing signals, "
        f"{results['fatigue_alerts']} fatigue alerts ==="
    )

    results["status"] = "ok" if (testing_ok and fatigue_ok) else "error"
    return results


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
