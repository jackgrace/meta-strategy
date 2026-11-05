"""
Catalog ad inventory. Read only — never changes anything.

For every ACTIVE ad: is its creative tied to the product catalog
(Advantage+ catalog ads / "Use a catalog"), and does it already hold its own
image or video that a rebuilt, catalog-free creative could keep?

Catalog signals on the creative:
  - product_set_id                      -> dynamic product ad
  - object_story_spec.template_data     -> catalog template creative
  - asset_feed_spec mentions a product set / catalog
Also flags adsets whose promoted_object points at a catalog or product set.

Endpoint: GET /catalog-ads (JSON) or /catalog-ads?format=csv
"""

import csv
import io
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta

import requests

from config import Config
from meta_api import API_BASE
from midnight_restart import _http_get_json
from testing_ad_rules import _aggregate, _fetch_daily, _today

logger = logging.getLogger(__name__)

CREATIVE_FIELDS = ("id,name,product_set_id,object_story_spec,asset_feed_spec,"
                   "object_story_id,image_hash,video_id,object_type")


@dataclass
class CatalogRow:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    creative_id: str
    catalog_signal: str      # "" when not catalog-linked
    adset_catalog: bool      # adset promoted_object points at a catalog / product set
    own_media: str           # "video" | "image" | "video+image" | ""
    video_ids: str
    image_hashes: str
    spend_7d: float


def _own_media(creative: dict) -> tuple[list[str], list[str]]:
    videos, images = [], []
    if creative.get("video_id"):
        videos.append(str(creative["video_id"]))
    if creative.get("image_hash"):
        images.append(creative["image_hash"])
    oss = creative.get("object_story_spec") or {}
    vd = oss.get("video_data") or {}
    if vd.get("video_id"):
        videos.append(str(vd["video_id"]))
    if vd.get("image_hash"):
        images.append(vd["image_hash"])
    ld = oss.get("link_data") or {}
    if ld.get("image_hash"):
        images.append(ld["image_hash"])
    for child in ld.get("child_attachments") or []:
        if child.get("video_id"):
            videos.append(str(child["video_id"]))
        if child.get("image_hash"):
            images.append(child["image_hash"])
    afs = creative.get("asset_feed_spec") or {}
    videos += [str(v.get("video_id")) for v in afs.get("videos") or [] if v.get("video_id")]
    images += [i.get("hash") for i in afs.get("images") or [] if i.get("hash")]
    dedupe = lambda xs: list(dict.fromkeys(xs))
    return dedupe(videos), dedupe(images)


def _catalog_signal(creative: dict) -> str:
    if creative.get("product_set_id"):
        return "product_set_id"
    oss = creative.get("object_story_spec") or {}
    if oss.get("template_data"):
        return "template_data"
    afs = creative.get("asset_feed_spec") or {}
    if afs:
        blob = json.dumps(afs).lower()
        if "product_set" in blob or "catalog" in blob:
            return "asset_feed_spec"
    return ""


def _adset_catalog(adset: dict) -> bool:
    po = adset.get("promoted_object") or {}
    return bool(po.get("product_set_id") or po.get("product_catalog_id"))


def _fetch_active_ads(config: Config) -> list[dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/ads"
    params = {
        "access_token": config.meta_access_token,
        "fields": f"id,name,campaign{{name}},adset{{name,promoted_object}},creative{{{CREATIVE_FIELDS}}}",
        "limit": 200,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
    }
    ads, first = [], True
    while url:
        data = _http_get_json(config, url, params if first else None)
        first = False
        ads.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
    logger.info(f"Catalog inventory: {len(ads)} active ads")
    return ads


def build_catalog_inventory(config: Config) -> list[CatalogRow]:
    ads = _fetch_active_ads(config)
    if not ads:
        return []
    yesterday = _today() - timedelta(days=1)
    rows7 = _fetch_daily(config, None, lambda _n: True, "all", yesterday - timedelta(days=6), yesterday)
    spend = _aggregate(rows7, "ad_id")

    out: list[CatalogRow] = []
    for ad in ads:
        creative = ad.get("creative") or {}
        videos, images = _own_media(creative)
        media = "+".join(x for x, has in (("video", videos), ("image", images)) if has)
        out.append(CatalogRow(
            ad_id=ad["id"],
            ad_name=ad.get("name", "Unknown"),
            adset_name=(ad.get("adset") or {}).get("name", "Unknown"),
            campaign_name=(ad.get("campaign") or {}).get("name", "Unknown"),
            creative_id=str(creative.get("id", "")),
            catalog_signal=_catalog_signal(creative),
            adset_catalog=_adset_catalog(ad.get("adset") or {}),
            own_media=media,
            video_ids=",".join(videos),
            image_hashes=",".join(images),
            spend_7d=round(spend.get(ad["id"], {}).get("spend", 0.0), 2),
        ))
    out.sort(key=lambda r: (not r.catalog_signal, -r.spend_7d))
    return out


def summarise(rows: list[CatalogRow]) -> dict:
    catalog = [r for r in rows if r.catalog_signal]
    by_campaign: dict[str, dict] = defaultdict(lambda: {"active_ads": 0, "catalog_ads": 0,
                                                         "catalog_with_own_media": 0, "catalog_spend_7d": 0.0})
    for r in rows:
        c = by_campaign[r.campaign_name]
        c["active_ads"] += 1
        if r.catalog_signal:
            c["catalog_ads"] += 1
            c["catalog_spend_7d"] = round(c["catalog_spend_7d"] + r.spend_7d, 2)
            if r.own_media:
                c["catalog_with_own_media"] += 1
    return {
        "active_ads": len(rows),
        "catalog_ads": len(catalog),
        "catalog_with_own_media": sum(1 for r in catalog if r.own_media),
        "catalog_without_media": sum(1 for r in catalog if not r.own_media),
        "catalog_spend_7d": round(sum(r.spend_7d for r in catalog), 2),
        "adsets_pointing_at_catalog": len({r.adset_name for r in rows if r.adset_catalog}),
        "by_campaign": {k: v for k, v in sorted(by_campaign.items(), key=lambda kv: -kv[1]["catalog_ads"])
                        if v["catalog_ads"]},
    }


def to_csv(rows: list[CatalogRow]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CatalogRow.__dataclass_fields__))
    writer.writeheader()
    for r in rows:
        writer.writerow(asdict(r))
    return buf.getvalue()


def send_catalog_summary(summary: dict, account_id: str, config: Config) -> bool:
    lines = [f"• `{name}` — {c['catalog_ads']} of {c['active_ads']} ads on catalog, "
             f"{c['catalog_with_own_media']} with own media, 7d ${c['catalog_spend_7d']:,.0f}"
             for name, c in list(summary["by_campaign"].items())[:20]]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📦 Catalog ads — {account_id}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*{summary['catalog_ads']}* of {summary['active_ads']} active ads use the catalog "
            f"(7d spend ${summary['catalog_spend_7d']:,.0f}). "
            f"{summary['catalog_with_own_media']} already hold their own image/video, "
            f"{summary['catalog_without_media']} are catalog-only. Read only — nothing changed."
        )}},
    ]
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send catalog summary: {e}")
        return False
