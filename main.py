"""
Meta Ad Fatigue Agent
Runs daily: pulls ad data -> analyzes fatigue -> sends Slack digest.
Can also be triggered manually via HTTP endpoint or Slack slash command.
"""

import sys
import logging

from config import Config
from meta_api import fetch_ad_insights
from fatigue_analyzer import analyze_fatigue
from testing_analyzer import analyze_testing_missed_opportunities
from slack_reporter import send_slack_report, send_roas_warning, send_testing_missed_opps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_check() -> dict:
    """
    Run the full fatigue check pipeline.
    Returns a summary dict (useful for HTTP responses).
    """
    logger.info("=== Meta Ad Fatigue Agent starting ===")

    config = Config.from_env()

    # Step 1: Fetch ad insights from Meta
    logger.info("Fetching ad insights from Meta Marketing API...")
    metrics = fetch_ad_insights(config)

    if not metrics:
        logger.warning("No ad data returned from Meta API — nothing to analyze")
        send_slack_report([], config)
        return {"status": "ok", "ads_analyzed": 0, "message": "No ad data returned"}

    # Step 2: Analyze fatigue
    logger.info(f"Analyzing {len(metrics)} data points...")
    reports = analyze_fatigue(metrics, config)

    # Step 3: Send Slack report
    logger.info("Sending Slack report...")
    success = send_slack_report(reports, config)

    # Step 4: Send separate ROAS warning (7d spend > $200, ROAS < 1.6)
    logger.info("Checking ROAS warnings...")
    roas_success = send_roas_warning(reports, config)

    # Step 5: Check testing campaigns for missed opportunities
    logger.info("Checking testing campaign missed opportunities...")
    testing_reports = analyze_testing_missed_opportunities(metrics, config)
    testing_success = send_testing_missed_opps(testing_reports, config)

    critical = sum(1 for r in reports if r.alert_level == "critical")
    warning = sum(1 for r in reports if r.alert_level == "warning")

    if success:
        logger.info(
            f"=== Complete: {len(reports)} ads analyzed, "
            f"{critical} critical, {warning} warning ==="
        )
    else:
        logger.error("Failed to send Slack report")

    return {
        "status": "ok" if success else "error",
        "ads_analyzed": len(reports),
        "critical": critical,
        "warning": warning,
        "slack_sent": success,
        "roas_warning_sent": roas_success,
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
