"""
Testing campaign missed opportunity analyzer.

Finds ads in TESTING campaigns that were turned off (have "OFF" in name)
with 0 purchases, but whose ATC rate or cost per ATC was at or below
the account baseline — these may have been killed too early.
"""

import logging
from dataclasses import dataclass
from collections import defaultdict

from meta_api import AdDayMetrics
from config import Config

logger = logging.getLogger(__name__)


@dataclass
class TestingAdReport:
    """A testing ad that was turned off but had strong ATC signals."""
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    total_spend: float
    total_impressions: int
    total_clicks: int
    total_add_to_carts: int
    total_purchases: int
    ctr: float            # Weighted CTR
    cpc: float            # Weighted CPC
    atc_rate: float       # ATCs / clicks * 100 (weighted)
    cost_per_atc: float   # Spend / ATCs (weighted)
    days_active: int

    # How it compares to baseline
    baseline_atc_rate: float
    baseline_cost_per_atc: float
    atc_rate_vs_baseline: str    # "below", "at", "above"
    cost_atc_vs_baseline: str    # "below", "at", "above"


def analyze_testing_missed_opportunities(
    all_metrics: list[AdDayMetrics],
    config: Config,
) -> list[TestingAdReport]:
    """
    Find ads in TESTING campaigns that were killed too early.

    1. Filter to TESTING campaigns only
    2. Calculate account-wide ATC baseline from all active ads
    3. Find ads with "OFF" in name + 0 purchases
    4. Flag those with ATC rate or cost per ATC at or below baseline
    """

    # Split into testing vs all ads
    testing_metrics = [
        m for m in all_metrics
        if config.testing_campaign_keyword.upper() in m.campaign_name.upper()
    ]

    if not testing_metrics:
        logger.info("No TESTING campaign data found")
        return []

    # Calculate account-wide ATC baseline from ALL ads (not just testing)
    # This gives us the benchmark to compare against
    all_by_ad: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in all_metrics:
        all_by_ad[m.ad_id].append(m)

    # Baseline: median ATC rate and cost per ATC across all ads with ATCs
    all_atc_rates = []
    all_cost_per_atcs = []

    for ad_id, days in all_by_ad.items():
        total_clicks = sum(d.clicks for d in days)
        total_atcs = sum(d.add_to_carts for d in days)
        total_spend = sum(d.spend for d in days)

        if total_atcs > 0 and total_clicks > 0:
            all_atc_rates.append(total_atcs / total_clicks * 100)
            all_cost_per_atcs.append(total_spend / total_atcs)

    if not all_atc_rates:
        logger.info("No ATC data available for baseline calculation")
        return []

    # Use median as baseline (more robust than mean against outliers)
    all_atc_rates.sort()
    all_cost_per_atcs.sort()
    baseline_atc_rate = all_atc_rates[len(all_atc_rates) // 2]
    baseline_cost_per_atc = all_cost_per_atcs[len(all_cost_per_atcs) // 2]

    logger.info(
        f"ATC baseline: rate={baseline_atc_rate:.2f}%, "
        f"cost=${baseline_cost_per_atc:.2f} "
        f"(from {len(all_atc_rates)} ads with ATCs)"
    )

    # Group testing metrics by ad
    testing_by_ad: dict[str, list[AdDayMetrics]] = defaultdict(list)
    for m in testing_metrics:
        testing_by_ad[m.ad_id].append(m)

    reports = []

    for ad_id, days in testing_by_ad.items():
        ad_name = days[0].ad_name
        campaign_name = days[0].campaign_name

        # Must have "OFF" in name (turned off)
        if "OFF" not in ad_name.upper():
            continue

        total_spend = sum(d.spend for d in days)
        total_impressions = sum(d.impressions for d in days)
        total_clicks = sum(d.clicks for d in days)
        total_atcs = sum(d.add_to_carts for d in days)
        total_purchases = sum(d.purchases for d in days)

        # Must have 0 purchases
        if total_purchases > 0:
            continue

        # Must have some ATCs to be a missed opportunity
        if total_atcs == 0:
            continue

        # Calculate weighted metrics
        atc_rate = (total_atcs / total_clicks * 100) if total_clicks > 0 else 0
        cost_per_atc = total_spend / total_atcs if total_atcs > 0 else 0
        ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
        cpc = total_spend / total_clicks if total_clicks > 0 else 0

        # Compare to baseline (within 10% = "at baseline")
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
            continue

        reports.append(TestingAdReport(
            ad_id=ad_id,
            ad_name=ad_name,
            campaign_name=campaign_name,
            adset_name=days[0].adset_name,
            total_spend=total_spend,
            total_impressions=total_impressions,
            total_clicks=total_clicks,
            total_add_to_carts=total_atcs,
            total_purchases=total_purchases,
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
        f"Testing campaign: {len(testing_by_ad)} ads total, "
        f"{len(reports)} missed opportunities flagged"
    )

    return reports
