"""
Meta Marketing API client.
Pulls ad-level insights with daily breakdowns for fatigue analysis.
"""

import logging
import time
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
    cpa: float  # Cost per purchase (spend / purchases)
    add_to_carts: int
    cost_per_atc: float  # Cost per add-to-cart
    atc_rate: float  # Add-to-cart rate (ATCs / clicks * 100)


def _extract_add_to_carts(actions: list | None) -> int:
    if not actions:
        return 0
    for action in actions:
        if action.get("action_type") == "add_to_cart":
            return int(float(action.get("value", 0)))
    return 0


def _extract_cost_per_atc(cost_per_action_type: list | None) -> float:
    if not cost_per_action_type:
        return 0.0
    for cpa in cost_per_action_type:
        if cpa.get("action_type") == "add_to_cart":
            return float(cpa.get("value", 0))
    return 0.0


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
    # Fetch enough data for the longest window needed
    # (max of 21d fatigue analysis or 30d testing analysis)
    max_lookback = max(config.long_lookback_days, config.testing_lookback_days)
    start_date = end_date - timedelta(days=max_lookback - 1)

    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "time_increment": 1,  # Daily breakdown
        "limit": 500,
        "filtering": '[{"field":"ad.effective_status","operator":"IN","value":["ACTIVE","PAUSED","IN_REVIEW","WITH_ISSUES"]}]',
    }

    all_metrics = []
    page_count = 0

    while url:
        page_count += 1
        logger.info(f"Fetching page {page_count} from Meta API...")

        # Retry with backoff for transient errors and rate limits
        resp = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params if page_count == 1 else None, timeout=60)

                # Check if this is a retryable error
                is_server_error = resp.status_code in (500, 502, 503, 504)
                is_rate_limit = resp.status_code == 403
                is_transient_400 = False

                if resp.status_code == 400:
                    try:
                        err = resp.json().get("error", {})
                        is_transient_400 = err.get("code") in (1, 2) or err.get("is_transient", False)
                    except ValueError:
                        pass

                if (is_server_error or is_rate_limit or is_transient_400) and attempt < 4:
                    if is_rate_limit:
                        wait = [120, 180, 300, 300][attempt]
                    else:
                        wait = [15, 30, 60, 120][attempt]
                    logger.warning(
                        f"Meta API returned {resp.status_code} "
                        f"{'(rate limit)' if is_rate_limit else '(transient)'}, "
                        f"retrying in {wait}s (attempt {attempt + 1}/5)"
                    )
                    time.sleep(wait)
                    continue

                if not resp.ok:
                    logger.error(f"Meta API error {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < 4:
                    wait = [15, 30, 60, 120][attempt]
                    logger.warning(f"Meta API timeout, retrying in {wait}s (attempt {attempt + 1}/5)")
                    time.sleep(wait)
                else:
                    raise

        data = resp.json()

        for row in data.get("data", []):
            spend = float(row.get("spend", 0))
            impressions = int(row.get("impressions", 0))

            if impressions == 0:
                continue

            clicks = int(row.get("clicks", 0))
            purchases = _extract_purchases(row.get("actions"))
            revenue = _extract_revenue(row.get("action_values"))
            add_to_carts = _extract_add_to_carts(row.get("actions"))
            cost_per_atc = _extract_cost_per_atc(row.get("cost_per_action_type"))

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
                cpa=spend / purchases if purchases > 0 else 0,
                add_to_carts=add_to_carts,
                cost_per_atc=cost_per_atc if cost_per_atc > 0 else (spend / add_to_carts if add_to_carts > 0 else 0),
                atc_rate=(add_to_carts / clicks * 100) if clicks > 0 else 0,
            )
            all_metrics.append(metric)

        # Handle pagination
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None  # Next URL has params baked in

    logger.info(f"Fetched {len(all_metrics)} ad-day records across {page_count} pages")
    return all_metrics


def fetch_ad_statuses(config: Config, ad_ids: set[str] | None = None) -> dict[str, dict]:
    """
    Fetch effective_status and created_time for ads. If ad_ids is provided,
    fetches only those specific ads.
    Returns dict mapping ad_id -> {"status": str, "created_time": str}.
    """
    ad_info: dict[str, dict] = {}

    if ad_ids:
        id_list = list(ad_ids)
        batch_size = 50
        for i in range(0, len(id_list), batch_size):
            batch = id_list[i:i + batch_size]
            url = f"{API_BASE}/"
            params = {
                "access_token": config.meta_access_token,
                "ids": ",".join(batch),
                "fields": "effective_status,created_time,name,campaign{name},adset{name}",
            }

            resp = None
            for attempt in range(4):
                try:
                    resp = requests.get(url, params=params, timeout=60)
                    if resp.status_code in (403, 500, 502, 503, 504) and attempt < 3:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    if not resp.ok:
                        logger.error(f"Batch status error {resp.status_code}: {resp.text[:300]}")
                    resp.raise_for_status()
                    break
                except requests.exceptions.Timeout:
                    if attempt < 3:
                        time.sleep(2 ** (attempt + 1))
                    else:
                        raise

            data = resp.json()
            for ad_id_key, ad_data in data.items():
                ad_info[ad_id_key] = {
                    "status": ad_data.get("effective_status", "UNKNOWN"),
                    "created_time": ad_data.get("created_time", ""),
                    "name": ad_data.get("name", "Unknown"),
                    "campaign_name": ad_data.get("campaign", {}).get("name", "Unknown"),
                    "adset_name": ad_data.get("adset", {}).get("name", "Unknown"),
                }

        logger.info(f"Fetched ad info for {len(ad_info)} ads ({len(id_list)} requested)")
        return ad_info

    # Fallback: fetch all ads in account (slow but complete)
    url = f"{API_BASE}/{config.meta_ad_account_id}/ads"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,effective_status,created_time,name,campaign{name},adset{name}",
        "limit": 500,
    }

    page_count = 0

    while url:
        page_count += 1

        resp = None
        for attempt in range(4):
            try:
                resp = requests.get(url, params=params if page_count == 1 else None, timeout=60)
                if resp.status_code in (500, 502, 503, 504) and attempt < 3:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"/ads endpoint returned {resp.status_code}, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < 3:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                else:
                    raise

        data = resp.json()

        for row in data.get("data", []):
            ad_info[row["id"]] = {
                "status": row.get("effective_status", "UNKNOWN"),
                "created_time": row.get("created_time", ""),
                "name": row.get("name", "Unknown"),
                "campaign_name": row.get("campaign", {}).get("name", "Unknown"),
                "adset_name": row.get("adset", {}).get("name", "Unknown"),
            }

        paging = data.get("paging", {})
        url = paging.get("next")
        params = None

    logger.info(f"Fetched ad info for {len(ad_info)} ads across {page_count} pages")
    return ad_info
