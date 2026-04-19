"""
Testing campaign ATC analysis.

Checks paused/off ads in TESTING campaigns against the campaign's
own ATC baseline (30-day window).

Flags ads whose ATC rate or cost per ATC was at or below the testing
campaign baseline — regardless of purchase count. These may have been
turned off too early or deserve a second look.
"""

import logging
from dataclasses import dataclass
from collections import defaultdict

from meta_api import AdDayMetrics
from config import Config

logger = logging.getLogger(__name__)

# Statuses that indicate the ad is effectively "off"
OFF_STATUSES = {
    "PAUSED",
    "CAMPAIGN_PAUSED",
    "ADSET_PAUSED",
    "ARCHIVED",
    "DELETED",
    "DISAPPROVED",
}

ACTIVE_STATUSES = {"ACTIVE", "IN_PROCESS", "PENDING_REVIEW", "WITH_ISSUES"}


@dataclass
class TestingAdReport:
    """A testing ad that was turned off but had strong ATC signals."""
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    effective_status: str
    total_spend: float
    total_impressions: int
    total_clicks: int
    total_add_to_carts: int
    total_purchases: int
    total_revenue: float
    roas: float
    ctr: float            # Weighted CTR
    cpc: float            # Weighted CPC
    atc_rate: float       # ATCs / clicks * 100 (weighted)
    cost_per_atc: float   # Spend / ATCs (weighted)
    days_active: int

    # How it compares to testing campaign baseline
    baseline_atc_rate: float
    baseline_cost_per_atc: float
    atc_rate_vs_baseline: str    # "below", "at", "above"
    cost_atc_vs_baseline: str    # "below", "at", "above"


def analyze_testing_missed_opportunities(
    all_metrics: list[AdDayMetrics],
    ad_statuses: dict[str, str],
    config: Config,
) -> list[TestingAdReport]:
    """
    Check OFF ads in TESTING campaigns against the testing campaign's
    own ATC baseline.

    1. Filter to TESTING campaigns only
    2. Split ads into ACTIVE vs OFF based on effective_status
    3. Calculate ATC baseline from ACTIVE testing ads
    4. Flag OFF ads with ATC rate or cost per ATC at or below baseline
    """

    # Filter to testing campaigns
    testing_metrics = [
        m for m in all_metrics
        if config.testing_campaign_keyword.upper() in m.campaign_name.upper()
    ]

    if not testing_metrics:
        logger.info(f"No '{config.testing_campaign_keyword}' campaign data found")
        return []

    logger.info(f"Testing campaign: {len(testing_metrics)} ad-day records found")

    # Group testing metrics by ad
    testing_by_ad: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in testing_metrics:
        testing_by_ad[m.ad_id].append(m)

    # Count ads by status
    active_ads = 0
    off_ads = 0
    unknown_status = 0
    for ad_id in testing_by_ad:
        status = ad_statuses.get(ad_id, "UNKNOWN")
        if status in OFF_STATUSES:
            off_ads += 1
        elif status in ACTIVE_STATUSES:
            active_ads += 1
        else:
            unknown_status += 1

    logger.info(
        f"Testing campaign ads: {active_ads} active, {off_ads} off, "
        f"{unknown_status} unknown status"
    )

    # Calculate ATC baseline from ACTIVE testing ads
    baseline_atc_rates = []
    baseline_cost_per_atcs = []
    active_with_atcs = 0

    for ad_id, days in testing_by_ad.items():
        status = ad_statuses.get(ad_id, "UNKNOWN")
        if status not in ACTIVE_STATUSES:
            continue

        total_clicks = sum(d.clicks for d in days)
        total_atcs = sum(d.add_to_carts for d in days)
        total_spend = sum(d.spend for d in days)

        if total_atcs > 0 and total_clicks > 0:
            active_with_atcs += 1
            baseline_atc_rates.append(total_atcs / total_clicks * 100)
            baseline_cost_per_atcs.append(total_spend / total_atcs)

    if not baseline_atc_rates:
        logger.info(
            f"No baseline possible: {active_ads} active testing ads, "
            f"none had any ATCs tracked"
        )
        return []

    # Median baseline (robust against outliers)
    baseline_atc_rates.sort()
    baseline_cost_per_atcs.sort()
    baseline_atc_rate = baseline_atc_rates[len(baseline_atc_rates) // 2]
    baseline_cost_per_atc = baseline_cost_per_atcs[len(baseline_cost_per_atcs) // 2]

    logger.info(
        f"Testing ATC baseline: rate={baseline_atc_rate:.2f}%, "
        f"cost=${baseline_cost_per_atc:.2f} "
        f"(from {active_with_atcs} active testing ads with ATCs)"
    )

    # Check OFF ads against the baseline
    reports = []
    off_checked = 0
    off_skipped_winner = 0
    off_no_atcs = 0
    off_below_baseline = 0

    for ad_id, days in testing_by_ad.items():
        status = ad_statuses.get(ad_id, "UNKNOWN")
        if status not in OFF_STATUSES:
            continue

        # Skip ads with WINNER in the name — already known winners, not missed opportunities
        if "WINNER" in days[0].ad_name.upper():
            off_skipped_winner += 1
            continue

        off_checked += 1

        total_spend = sum(d.spend for d in days)
        total_impressions = sum(d.impressions for d in days)
        total_clicks = sum(d.clicks for d in days)
        total_atcs = sum(d.add_to_carts for d in days)
        total_purchases = sum(d.purchases for d in days)
        total_revenue = sum(d.revenue for d in days)

        if total_atcs == 0:
            off_no_atcs += 1
            continue

        # Calculate weighted metrics
        atc_rate = (total_atcs / total_clicks * 100) if total_clicks > 0 else 0
        cost_per_atc = total_spend / total_atcs if total_atcs > 0 else 0
        ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
        cpc = total_spend / total_clicks if total_clicks > 0 else 0
        roas = total_revenue / total_spend if total_spend > 0 else 0

        # Compare to testing campaign baseline (within 10% = "at baseline")
        if atc_rate >= baseline_atc_rate * 0.9:
            atc_rate_vs = "at" if atc_rate <= baseline_atc_rate * 1.1 else "above"
        else:
            atc_rate_vs = "below"

        if cost_per_atc <= baseline_cost_per_atc * 1.1:
            cost_atc_vs = "at" if cost_per_atc >= baseline_cost_per_atc * 0.9 else "below"
        else:
            cost_atc_vs = "above"

        # Flag if ATC rate is at/above baseline OR cost per ATC is at/below baseline
        is_opportunity = atc_rate_vs in ("at", "above") or cost_atc_vs in ("at", "below")

        if not is_opportunity:
            off_below_baseline += 1
            continue

        reports.append(TestingAdReport(
            ad_id=ad_id,
            ad_name=days[0].ad_name,
            campaign_name=days[0].campaign_name,
            adset_name=days[0].adset_name,
            effective_status=status,
            total_spend=total_spend,
            total_impressions=total_impressions,
            total_clicks=total_clicks,
            total_add_to_carts=total_atcs,
            total_purchases=total_purchases,
            total_revenue=total_revenue,
            roas=roas,
            ctr=ctr,
            cpc=cpc,
            atc_rate=atc_rate,
            cost_per_atc=cost_per_atc,
            days_active=len(days),
            baseline_atc_rate=baseline_atc_rate,
            baseline_cost_per_atc=baseline_cost_per_atc,
            atc_rate_vs_baseline=atc_rate_vs,
            cost_atc_vs_baseline=cost_atc_vs,
        ))

    # Sort by ATC rate descending (strongest signal first)
    reports.sort(key=lambda r: r.atc_rate, reverse=True)

    logger.info(
        f"Testing OFF ads: {off_checked} checked │ "
        f"{off_skipped_winner} skipped (WINNER in name) │ "
        f"{off_no_atcs} with no ATCs │ "
        f"{off_below_baseline} below baseline │ "
        f"{len(reports)} flagged"
    )

    return reports
