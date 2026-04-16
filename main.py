"""
Meta Ad Fatigue Agent
Runs daily: pulls ad data -> analyzes fatigue -> sends Slack digest.
Can also be triggered manually via HTTP endpoint or Slack slash command.
"""

import sys
import logging

from config import Config
from meta_api import fetch_ad_insights, fetch_ad_statuses
from fatigue_analyzer import analyze_fatigue
from testing_analyzer import analyze_testing_missed_opportunities
from slack_reporter import send_slack_report, send_roas_warning, send_testing_missed_opps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_fatigue_and_roas() -> dict:
    """
    Run fatigue analysis + ROAS warning (the daily digest).
    Does NOT run testing campaign analysis.
    """
    logger.info("=== Fatigue + ROAS check starting ===")

    config = Config.from_env()

    logger.info("Fetching ad insights from Meta Marketing API...")
    metrics = fetch_ad_insights(config)

    if not metrics:
        logger.warning("No ad data returned from Meta API — nothing to analyze")
        send_slack_report([], config)
        return {"status": "ok", "ads_analyzed": 0, "message": "No ad data returned"}

    logger.info(f"Analyzing {len(metrics)} data points...")
    reports = analyze_fatigue(metrics, config)

    logger.info("Sending Slack report...")
    success = send_slack_report(reports, config)

    logger.info("Checking ROAS warnings...")
    roas_success = send_roas_warning(reports, config)

    critical = sum(1 for r in reports if r.alert_level == "critical")
    warning = sum(1 for r in reports if r.alert_level == "warning")

    logger.info(
        f"=== Complete: {len(reports)} ads analyzed, "
        f"{critical} critical, {warning} warning ==="
    )

    return {
        "status": "ok" if success else "error",
        "ads_analyzed": len(reports),
        "critical": critical,
        "warning": warning,
        "slack_sent": success,
        "roas_warning_sent": roas_success,
    }


def run_testing_check() -> dict:
    """
    Run ONLY the testing campaign missed opportunities check.
    Independent trigger — does not run fatigue or ROAS warning.
    """
    logger.info("=== Testing campaign check starting ===")

    config = Config.from_env()

    logger.info("Fetching ad insights from Meta Marketing API...")
    metrics = fetch_ad_insights(config)

    if not metrics:
        logger.warning("No ad data returned from Meta API")
        return {"status": "ok", "ads_analyzed": 0, "message": "No ad data returned"}

    logger.info("Fetching ad statuses from /ads endpoint...")
    try:
        ad_statuses = fetch_ad_statuses(config)
    except Exception as e:
        logger.error(f"Failed to fetch ad statuses: {e}")
        ad_statuses = {}

    logger.info("Analyzing testing campaign missed opportunities...")
    testing_reports = analyze_testing_missed_opportunities(metrics, ad_statuses, config)
    testing_success = send_testing_missed_opps(testing_reports, config)

    logger.info(f"=== Complete: {len(testing_reports)} testing opportunities flagged ===")

    return {
        "status": "ok" if testing_success else "error",
        "testing_flagged": len(testing_reports),
        "slack_sent": testing_success,
    }


# The daily scheduler and /run endpoint both call run_check.
# Currently set to ONLY run the testing ATC protocol.
# The fatigue + ROAS warning pipeline is preserved in run_fatigue_and_roas()
# but no longer wired to the daily run — can be re-enabled by swapping below.
def run_check() -> dict:
    """Default daily run: testing campaign ATC check only."""
    return run_testing_check()


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
