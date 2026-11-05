"""
Switch off "Add catalog items" (Advantage+ creative product_extensions) and
catalog browsing (product_browsing) on chosen ads.

Creatives can't be edited, so for each ad:
  1. read its creative
  2. create a copy with product_extensions (and its carousel option) and
     product_browsing OPT_OUT —
     same post / media / text / url tags / other enhancements
  3. point the ad at the new creative (ad id, stats and the post's likes and
     comments stay)
  4. re-read the ad and confirm the switch is off

Dry run by default: shows what would change and the exact creative payload.
Live runs are capped at MAX_LIVE ads per call.

Endpoint: GET /catalog-off?ads=<id,id,...>          dry run for those ads
          GET /catalog-off?limit=5                  dry run, 5 lowest-spend flagged ads
          ...&live=1                                do it
"""

import copy
import json
import logging
import time
from dataclasses import asdict, dataclass

import requests

from catalog_inventory import CREATIVE_FIELDS, _catalog_signal, build_catalog_inventory
from config import Config
from meta_api import API_BASE
from midnight_restart import _http_get_json

logger = logging.getLogger(__name__)

MAX_LIVE = 10
# Advantage+ creative features switched off on the new creative.
FEATURES_OFF = ("product_extensions", "product_browsing")
# Meta retired the single "standard_enhancements" switch; new creatives must
# set each enhancement on its own. When the old creative had the bundle OFF,
# these are written as OFF individually so nothing quietly turns on.
STANDARD_BUNDLE = ("image_brightness_and_contrast", "image_templates", "image_touchups",
                   "text_optimizations", "enhance_cta", "inline_comment", "image_animation",
                   "cv_transformation", "carousel_to_video", "image_text_translation",
                   "site_extensions", "show_destination_blurbs")
MIN_SPEND_FOR_AUTO_PICK = 1.0    # auto-picked test ads should at least be delivering

# Fields copied from the old creative onto the new one when present.
COPY_FIELDS = ("name", "object_story_id", "object_story_spec", "asset_feed_spec", "url_tags",
               "instagram_user_id", "instagram_actor_id", "degrees_of_freedom_spec",
               "contextual_multi_ads", "platform_customizations")
FETCH_FIELDS = CREATIVE_FIELDS + ",url_tags,instagram_user_id,instagram_actor_id,contextual_multi_ads,platform_customizations,effective_object_story_id"


@dataclass
class CatalogOffResult:
    ad_id: str
    ad_name: str
    campaign_name: str
    old_creative_id: str
    new_creative_id: str
    action: str      # would_change | changed | skipped | failed
    reason: str
    new_creative: dict


def _post(config: Config, path: str, payload: dict) -> tuple[bool, dict | str]:
    """POST a JSON-ish payload to Meta with small retry. Returns (ok, json or error text)."""
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in payload.items()}
    data["access_token"] = config.meta_access_token
    for attempt in range(3):
        try:
            resp = requests.post(f"{API_BASE}/{path}", data=data, timeout=60)
        except requests.RequestException as e:
            err = str(e)[:300]
        else:
            if resp.ok:
                return True, resp.json()
            try:
                e = resp.json().get("error", {})
                err = e.get("error_user_msg") or e.get("message") or resp.text[:300]
                if e.get("code") in (1, 2, 4, 17, 32) and attempt < 2:
                    time.sleep([5, 20][attempt])
                    continue
            except ValueError:
                err = resp.text[:300]
            return False, err
        if attempt < 2:
            time.sleep([5, 20][attempt])
    return False, err


def _create_creative(config: Config, spec: dict) -> tuple[bool, dict | str, str]:
    """Create the creative; if Meta rejects the promo-code metadata, retry without it."""
    ok, out = _post(config, f"{config.meta_ad_account_id}/adcreatives", spec)
    if not ok and "asset_feed_spec" in spec:
        ok2, out2 = _post(config, f"{config.meta_ad_account_id}/adcreatives",
                          {k: v for k, v in spec.items() if k != "asset_feed_spec"})
        if ok2:
            spec.pop("asset_feed_spec", None)
            return True, out2, " (promo-code metadata dropped — check the promo code still shows)"
    return ok, out, ""


def _fetch_ad(config: Config, ad_id: str) -> dict:
    return _http_get_json(config, f"{API_BASE}/{ad_id}", {
        "access_token": config.meta_access_token,
        "fields": f"id,name,campaign{{name}},creative{{{FETCH_FIELDS}}}",
    })


def build_new_creative(creative: dict, reuse_post: bool = True) -> dict:
    """Copy of the creative with product_extensions switched off.

    reuse_post: point the new creative at the ad's existing post (its own
    object_story_id, or the effective one Meta made for a spec-built ad) so
    likes and comments carry over. False rebuilds from the spec (new post).
    """
    new = {k: copy.deepcopy(creative[k]) for k in COPY_FIELDS if creative.get(k) not in (None, "", {}, [])}
    post_id = creative.get("object_story_id") or (creative.get("effective_object_story_id") if reuse_post else None)
    if post_id:
        new["object_story_id"] = post_id
        new.pop("object_story_spec", None)   # the post already carries it
    else:
        new.pop("object_story_id", None)
    dof = new.setdefault("degrees_of_freedom_spec", {})
    cfs = dof.setdefault("creative_features_spec", {})
    for feature in FEATURES_OFF:
        f = cfs.setdefault(feature, {})
        f["enroll_status"] = "OPT_OUT"
        for cust in (f.get("customizations") or {}).values():
            if isinstance(cust, dict) and "enroll_status" in cust:
                cust["enroll_status"] = "OPT_OUT"
    bundle = cfs.pop("standard_enhancements", None)
    if (bundle or {}).get("enroll_status") == "OPT_OUT":
        for feature in STANDARD_BUNDLE:
            cfs.setdefault(feature, {"enroll_status": "OPT_OUT"})
    new["name"] = (creative.get("name") or "creative")[:180] + " [no catalog items]"
    return new


def _features_on(creative: dict) -> list[str]:
    cfs = ((creative.get("degrees_of_freedom_spec") or {}).get("creative_features_spec") or {})
    return [f for f in FEATURES_OFF if (cfs.get(f) or {}).get("enroll_status") == "OPT_IN"]


def _is_off(creative: dict) -> bool:
    return not _features_on(creative)


def _opted_in(creative: dict) -> set[str]:
    cfs = ((creative.get("degrees_of_freedom_spec") or {}).get("creative_features_spec") or {})
    return {k for k, v in cfs.items() if isinstance(v, dict) and v.get("enroll_status") == "OPT_IN"}


def run_catalog_off(config: Config, ad_ids: list[str] | None = None, limit: int = 5,
                    live: bool = False) -> list[CatalogOffResult]:
    if not ad_ids:
        rows = [r for r in build_catalog_inventory(config)
                if r.catalog_signal == "product_extensions" and r.spend_7d >= MIN_SPEND_FOR_AUTO_PICK]
        rows.sort(key=lambda r: r.spend_7d)
        ad_ids = [r.ad_id for r in rows[:limit]]
    if live and len(ad_ids) > MAX_LIVE:
        raise ValueError(f"Live run capped at {MAX_LIVE} ads per call ({len(ad_ids)} given)")

    results: list[CatalogOffResult] = []
    for ad_id in ad_ids:
        ad = _fetch_ad(config, ad_id)
        name = ad.get("name", "Unknown")
        campaign = (ad.get("campaign") or {}).get("name", "Unknown")
        creative = ad.get("creative") or {}
        old_id = str(creative.get("id", ""))
        make = lambda action, reason, new_id="", spec=None: CatalogOffResult(
            ad_id, name, campaign, old_id, new_id, action, reason, spec or {})

        if _is_off(creative):
            results.append(make("skipped", "catalog items and catalog browsing already off"))
            continue
        turning_off = ", ".join(_features_on(creative))
        if not (creative.get("object_story_id") or creative.get("effective_object_story_id")
                or creative.get("object_story_spec")):
            results.append(make("skipped", "creative has no post or story spec to copy — needs a manual look"))
            continue
        spec = build_new_creative(creative)
        post_kept = bool(spec.get("object_story_id"))
        if not live:
            results.append(make("would_change", f"{turning_off} -> OPT_OUT; "
                                + ("same post (likes/comments kept)" if post_kept else "rebuilt from spec (NEW post)"),
                                "", spec))
            continue

        ok, out, note = _create_creative(config, spec)
        if (not ok and post_kept and creative.get("object_story_spec") and not creative.get("object_story_id")
                and any(w in str(out).lower() for w in ("post", "story", "object_story"))):
            # Meta wouldn't accept the effective post id; fall back to the spec (new post).
            spec = build_new_creative(creative, reuse_post=False)
            post_kept = False
            ok, out, note2 = _create_creative(config, spec)
            note += " — post could not be reused, rebuilt from spec (NEW post, social proof not carried)" + note2
        if not ok:
            results.append(make("failed", f"creating creative: {out}{note}", "", spec))
            continue
        new_id = str(out.get("id", ""))
        ok, out = _post(config, ad_id, {"creative": {"creative_id": new_id}})
        if not ok:
            results.append(make("failed", f"creative {new_id} made but ad not switched: {out}", new_id, spec))
            continue
        after = (_fetch_ad(config, ad_id).get("creative") or {})
        verified = str(after.get("id")) == new_id and _is_off(after)
        newly_on = sorted(_opted_in(after) - _opted_in(creative))
        if newly_on:
            note += f" — NOTE enhancements now on that weren't before: {', '.join(newly_on)}"
        results.append(make("changed" if verified else "failed",
                            (f"verified: {turning_off} off, " + ("same post" if post_kept else "NEW post") + note) if verified else
                            f"ad switched but re-read shows creative {after.get('id')} with {_features_on(after)} still on",
                            new_id, spec))
        logger.info(f"Catalog off: {results[-1].action} {ad_id} ({name}) {old_id} -> {new_id}")
    return results


def send_catalog_off_report(results: list[CatalogOffResult], live: bool, config: Config) -> bool:
    if not results or not live:
        return True
    lines = [f"• *{r.ad_name}* — `{r.campaign_name}` │ {r.action}: {r.reason}" for r in results[:25]]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🏷️ Catalog items off — {len(results)} ads"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Rebuilt each ad's creative with Advantage+ *Add catalog items* and *catalog browsing* switched off, same post and media."}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    try:
        return requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10).ok
    except requests.RequestException as e:
        logger.error(f"Failed to send catalog-off report: {e}")
        return False


def to_dict(r: CatalogOffResult) -> dict:
    return asdict(r)
