"""
Meta Ad Fatigue Agent
Runs daily: pulls ad data -> analyzes fatigue -> sends Slack digest.
"""

import sys
import logging

from config import Config
from meta_api import fetch_ad_insights
from fatigue_analyzer import analyze_fatigue
from slack_reporter import send_slack_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    logger.info("=== Meta Ad Fatigue Agent starting ===")

    # Load config from environment
    try:
        config = Config.from_env()
    except KeyError as e:
        logger.error(f"Missing required environment variable: {e}")
        sys.exit(1)

    # Step 1: Fetch ad insights from Meta
    logger.info("Fetching ad insights from Meta Marketing API...")
    try:
        metrics = fetch_ad_insights(config)
    except Exception as e:
        logger.error(f"Failed to fetch Meta data: {e}")
        sys.exit(1)

    if not metrics:
        logger.warning("No ad data returned from Meta API — nothing to analyze")
        # Still send a Slack message so you know it ran
        send_slack_report([], config)
        return

    # Step 2: Analyze fatigue
    logger.info(f"Analyzing {len(metrics)} data points...")
    reports = analyze_fatigue(metrics, config)

    # Step 3: Send Slack report
    logger.info("Sending Slack report...")
    success = send_slack_report(reports, config)

    if success:
        critical = sum(1 for r in reports if r.alert_level == "critical")
        warning = sum(1 for r in reports if r.alert_level == "warning")
        logger.info(
            f"=== Complete: {len(reports)} ads analyzed, "
            f"{critical} critical, {warning} warning ==="
        )
    else:
        logger.error("Failed to send Slack report")
        sys.exit(1)


if __name__ == "__main__":
    main()
