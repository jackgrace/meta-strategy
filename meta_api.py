"""
Meta Marketing API client.
Pulls ad-level insights with daily breakdowns for fatigue analysis.
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

import requests

from config import Config

logger = logging.getLogger(__name__)

API_BASE = "https://graph.facebook.com/v21.0"

# Fields we need for fatigue analysis
INSIGHT_FIELDS = [
    "ad_id",
    "ad_name",
    "campaign_name",
    "adset_name",
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
    "frequency",
    "actions",
    "action_values",
    "cost_per_action_type",
]


@dataclass
class AdDayMetrics:
    """Single day of metrics for one ad."""
    ad_id: str
    ad_name: str
    campaign_name: str
    adset_name: str
    date: str
    spend: float
    impressions: int
    clicks: int
    ctr: float
    cpc: float
    cpm: float
    frequency: float
    purchases: int
    revenue: float
    roas: float


def _extract_purchases(actions: list | None) -> int:
    if not actions:
        return 0
    for action in actions:
        if action.get("action_type") == "purchase":
            return int(float(action.get("value", 0)))
    return 0


def _extract_revenue(action_values: list | None) -> float:
    if not action_values:
        return 0.0
    for av in action_values:
        if av.get("action_type") == "purchase":
            return float(av.get("value", 0))
    return 0.0


def fetch_ad_insights(config: Config) -> list[AdDayMetrics]:
    """
    Fetch daily ad-level insights for the lookback window.
    Returns a flat list of AdDayMetrics (one per ad per day).
    """
    end_date = datetime.now().date() - timedelta(days=1)  # Yesterday
    # Fetch enough data for the long window (21d) — short window analysis
    # will use only the last 7 days from this same dataset
    start_date = end_date - timedelta(days=config.long_lookback_days - 1)

    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "time_increment": 1,  # Daily breakdown
        "limit": 500,
        "filtering": '[{"field":"ad.effective_status","operator":"IN","value":["ACTIVE"]}]',
    }

    all_metrics = []
    page_count = 0

    while url:
        page_count += 1
        logger.info(f"Fetching page {page_count} from Meta API...")

        resp = requests.get(url, params=params if page_count == 1 else None)
        resp.raise_for_status()
        data = resp.json()

        for row in data.get("data", []):
            spend = float(row.get("spend", 0))
            impressions = int(row.get("impressions", 0))

            if impressions == 0:
                continue

            clicks = int(row.get("clicks", 0))
            purchases = _extract_purchases(row.get("actions"))
            revenue = _extract_revenue(row.get("action_values"))

            metric = AdDayMetrics(
                ad_id=row["ad_id"],
                ad_name=row.get("ad_name", "Unknown"),
                campaign_name=row.get("campaign_name", "Unknown"),
                adset_name=row.get("adset_name", "Unknown"),
                date=row["date_start"],
                spend=spend,
                impressions=impressions,
                clicks=clicks,
                ctr=float(row.get("ctr", 0)),
                cpc=float(row.get("cpc", 0)) if clicks > 0 else 0,
                cpm=float(row.get("cpm", 0)),
                frequency=float(row.get("frequency", 1)),
                purchases=purchases,
                revenue=revenue,
                roas=revenue / spend if spend > 0 else 0,
            )
            all_metrics.append(metric)

        # Handle pagination
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None  # Next URL has params baked in

    logger.info(f"Fetched {len(all_metrics)} ad-day records across {page_count} pages")
    return all_metrics
