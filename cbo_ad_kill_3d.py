"""
3-day hard-kill for CBO ads that are chronic underperformers.

Per-adset-keyword thresholds: only ads whose parent adset name contains
a known keyword (MIK, LED, …) are evaluated, and each keyword has its
own spend threshold. Ads in other adsets are ignored.

Trigger (per ad, rolling last 3 days):
- Ad in a CBO campaign (whole-word 'CBO' in campaign name)
- Parent adset name contains one of KILL_KEYWORD_SPEND_THRESHOLDS's keys
- Ad name does NOT contain 'OFF'
- Ad name does NOT contain 'RUN'
- Parent adset name does NOT contain 'OFF'
- 3d ad spend > keyword's threshold  (MIK $300, LED $500 by default)
- 3d ad ROAS < 2.0

Action:
- Pause the ad
- Rename by appending ' - OFF'

Permanent — no auto-restart. Midnight ad restart skips any ad whose name
contains OFF, so once killed the ad stays down until you rename it back.

Runs once per day (piggybacks the 1am AEST auto-pause slot).
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

from meta_api import API_BASE
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

KILL_ROAS_3D = 2.0
KILL_KEYWORD_SPEND_THRESHOLDS: dict[str, float] = {
    "MIK": 300.0,
    "LED": 500.0,
}
OFF_SUFFIX = " - OFF"


@dataclass
class CboAdKillAction:
    ad_id: str
    old_name: str
    new_name: str
    campaign_name: str
    adset_name: str
    matched_keyword: str
    action: str      # "killed" | "would_kill" | "failed" | "partial"
    reason: str
    spend_3d: float
    revenue_3d: float
    roas_3d: float
    purchases_3d: int


def _http_retry(config: Config, method: str, url: str, **kwargs) -> requests.Response:
    for attempt in range(4):
        try:
            resp = requests.request(method, url, timeout=60, **kwargs)
            retryable = False
            if resp.status_code in (500, 502, 503, 504):
                retryable = True
            elif resp.status_code == 400:
                try:
                    err = resp.json().get("error", {})
                    code = err.get("code")
                    subcode = err.get("error_subcode")
                    msg = (err.get("message", "") + " " + err.get("error_user_msg", "")).lower()
                    retryable = (
                        err.get("is_transient") is True
                        or code in (1, 2, 4, 17, 32)
                        or subcode in (1504018, 1487742, 1504044)
                        or any(k in msg for k in ("temporarily", "limit reached", "too many", "try again", "load", "unavailable"))
                    )
                except Exception:
                    pass
            if retryable and attempt < 3:
                wait = [15, 30, 60][attempt]
                logger.warning(f"CBO ad-kill {method} {resp.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 3:
                wait = [15, 30, 60][attempt]
                logger.warning(f"CBO ad-kill {method} network error, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("unreachable")


def _is_cbo_campaign(name: str) -> bool:
    parts = [p.strip() for p in name.upper().replace("|", " ").split()]
    return "CBO" in parts


def _match_keyword(adset_name: str) -> tuple[str | None, float | None]:
    """Return (keyword, threshold) for the first matching keyword in the
    adset name, or (None, None) if no keyword matches."""
    upper = adset_name.upper()
    for kw, thresh in KILL_KEYWORD_SPEND_THRESHOLDS.items():
        if kw.upper() in upper:
            return kw, thresh
    return None, None


def _fetch_3d_cbo_ad_metrics(config: Config) -> dict:
    """
    Ad-level insights, last 3 days, for CBO campaigns.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_name,adset_id,adset_name,spend,actions,action_values",
        "date_preset": "last_3d",
        "limit": 200,
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"CBO"}]'
        ),
    }

    out: dict = {}
    cur_url = url
    cur_params = params
    page_count = 0
    while cur_url:
        page_count += 1
        resp = _http_retry(config, "GET", cur_url, params=cur_params if page_count == 1 else None)
        if not resp.ok:
            raise requests.exceptions.HTTPError(
                f"Meta {resp.status_code}: {resp.text[:400]}", response=resp,
            )
        data = resp.json()
        for row in data.get("data", []):
            campaign_name = row.get("campaign_name", "")
            if not _is_cbo_campaign(campaign_name):
                continue
            spend = float(row.get("spend", 0))
            revenue = 0.0
            purchases = 0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                if a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))
            out[row["ad_id"]] = {
                "ad_name": row.get("ad_name", ""),
                "campaign_name": campaign_name,
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", ""),
                "spend": spend,
                "revenue": revenue,
                "roas": revenue / spend if spend > 0 else 0,
                "purchases": purchases,
            }
        paging = data.get("paging", {})
        cur_url = paging.get("next")
        cur_params = None
    logger.info(f"CBO ad-kill 3d fetch: {len(out)} CBO ads returned")
    return out


def _fetch_ad_names(config: Config, ad_ids: set[str]) -> dict:
    """Batch-fetch authoritative current name + adset name for ads."""
    out: dict = {}
    if not ad_ids:
        return out
    id_list = list(ad_ids)
    batch_size = 50
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i:i + batch_size]
        resp = _http_retry(
            config, "GET", f"{API_BASE}/",
            params={
                "access_token": config.meta_access_token,
                "ids": ",".join(batch),
                "fields": "id,name,effective_status,adset{name}",
            },
        )
        if not resp.ok:
            logger.error(f"CBO ad-kill status batch error {resp.status_code}: {resp.text[:200]}")
            continue
        data = resp.json()
        for aid, adata in data.items():
            adset = adata.get("adset", {}) or {}
            out[aid] = {
                "name": adata.get("name", ""),
                "adset_name": adset.get("name", ""),
                "effective_status": adata.get("effective_status", "UNKNOWN"),
            }
    return out


def _pause_and_rename(config: Config, ad_id: str, new_name: str) -> tuple[str, str]:
    """Pause + rename in two calls. Returns (action, reason)."""
    resp = _http_retry(
        config, "POST",
        f"{API_BASE}/{ad_id}?access_token={config.meta_access_token}",
        data={"status": "PAUSED"},
    )
    if not resp.ok:
        try:
            err = resp.json().get("error", {})
            reason = err.get("error_user_title", err.get("message", "Unknown"))
        except Exception:
            reason = f"HTTP {resp.status_code}"
        return "failed", f"pause failed: {reason}"

    resp = _http_retry(
        config, "POST",
        f"{API_BASE}/{ad_id}?access_token={config.meta_access_token}",
        data={"name": new_name},
    )
    if not resp.ok:
        try:
            err = resp.json().get("error", {})
            reason = err.get("error_user_title", err.get("message", "Unknown"))
        except Exception:
            reason = f"HTTP {resp.status_code}"
        return "partial", f"paused OK, rename failed: {reason}"

    return "killed", "ok"


def run_cbo_ad_kill_3d(config: Config, dry_run: bool = False) -> list[CboAdKillAction]:
    """Once-daily 3d hard-kill for CBO ads (per adset-name keyword)."""
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== CBO ad kill (3d hard-kill) [{mode}] ===")

    try:
        metrics = _fetch_3d_cbo_ad_metrics(config)
    except Exception as e:
        logger.error(f"CBO ad-kill 3d fetch failed: {e}")
        return []

    if not metrics:
        return []

    status_map = _fetch_ad_names(config, set(metrics.keys()))

    actions: list[CboAdKillAction] = []
    killed_count = 0
    partial_count = 0
    failed_count = 0

    for ad_id, m in metrics.items():
        info = status_map.get(ad_id, {})
        current_ad_name = info.get("name", m["ad_name"])
        current_adset_name = info.get("adset_name", m["adset_name"])

        # Match adset keyword — skip if none applies
        matched_keyword, spend_threshold = _match_keyword(current_adset_name)
        if matched_keyword is None:
            continue

        spend = m["spend"]
        roas = m["roas"]
        purchases = m["purchases"]

        # Spend gate for this keyword
        if spend <= spend_threshold:
            continue
        # ROAS gate
        if roas >= KILL_ROAS_3D:
            continue

        # Skip standard markers
        if "OFF" in current_ad_name.upper():
            continue
        if "OFF" in current_adset_name.upper():
            continue
        if "RUN" in current_ad_name.upper():
            continue

        new_name = current_ad_name.rstrip() + OFF_SUFFIX
        reason = f"3d ad spend ${spend:.2f}>${spend_threshold:.0f} ({matched_keyword}) & ROAS {roas:.2f}<{KILL_ROAS_3D}"

        if dry_run:
            actions.append(CboAdKillAction(
                ad_id=ad_id, old_name=current_ad_name, new_name=new_name,
                campaign_name=m["campaign_name"], adset_name=current_adset_name,
                matched_keyword=matched_keyword,
                action="would_kill", reason=reason,
                spend_3d=spend, revenue_3d=m["revenue"], roas_3d=roas, purchases_3d=purchases,
            ))
            continue

        action, op_reason = _pause_and_rename(config, ad_id, new_name)
        if action == "killed":
            killed_count += 1
            logger.info(f"CBO AD KILL (3d, {matched_keyword}): {ad_id} '{current_ad_name}' → '{new_name}' — {reason}")
        elif action == "partial":
            partial_count += 1
            logger.warning(f"CBO AD KILL (3d, {matched_keyword}) partial: {ad_id} — {op_reason}")
        else:
            failed_count += 1
            logger.warning(f"CBO AD KILL (3d, {matched_keyword}) failed: {ad_id} — {op_reason}")

        actions.append(CboAdKillAction(
            ad_id=ad_id, old_name=current_ad_name, new_name=new_name,
            campaign_name=m["campaign_name"], adset_name=current_adset_name,
            matched_keyword=matched_keyword,
            action=action,
            reason=f"{reason} │ {op_reason}" if action != "killed" else reason,
            spend_3d=spend, revenue_3d=m["revenue"], roas_3d=roas, purchases_3d=purchases,
        ))

    logger.info(
        f"CBO ad kill 3d complete: {killed_count} killed, "
        f"{partial_count} partial, {failed_count} failed"
    )
    return actions


def _build_slack_message(actions: list[CboAdKillAction], dry_run: bool) -> dict | None:
    if not actions:
        return None
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    killed = [a for a in actions if a.action in ("killed", "would_kill")]
    partial = [a for a in actions if a.action == "partial"]
    failed = [a for a in actions if a.action == "failed"]

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"☠️ CBO Ad 3d Hard-Kill — {now}"}
    }]

    parts = []
    if killed:
        verb = "would kill" if dry_run else "killed"
        parts.append(f"⚫ {len(killed)} {verb}")
    if partial:
        parts.append(f"⚠️ {len(partial)} partial")
    if failed:
        parts.append(f"❌ {len(failed)} failed")

    total_spend = sum(a.spend_3d for a in killed)
    thresholds_txt = ", ".join(
        f"{kw} ${int(t)}" for kw, t in KILL_KEYWORD_SPEND_THRESHOLDS.items()
    )

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(parts) + "\n"
            f"_Rule: CBO ad │ 3d spend > per-keyword ({thresholds_txt}) & 3d ROAS<{KILL_ROAS_3D}_\n"
            f"_Action: pause + append `- OFF` (permanent — manual review to revive)_\n"
            f"3d spend on killed ads: *${total_spend:.2f}*"
        )}
    })
    blocks.append({"type": "divider"})

    MAX_DISPLAY = 15

    def _fmt(a: CboAdKillAction, emoji: str) -> str:
        return (
            f"{emoji} `{a.old_name}` → `{a.new_name}`\n"
            f"Campaign: `{a.campaign_name}` │ Adset: `{a.adset_name}` │ Keyword: `{a.matched_keyword}`\n"
            f"3d ad: spend ${a.spend_3d:.2f} │ rev ${a.revenue_3d:.2f} │ ROAS {a.roas_3d:.2f}x │ {a.purchases_3d} purchases\n"
            f"_Why: {a.reason}_"
        )

    for group, emoji, label in (
        (killed, "⚫", "Killed (renamed + paused)"),
        (partial, "⚠️", "Partial (paused, rename failed)"),
        (failed, "❌", "Failed (needs manual pause)"),
    ):
        if not group:
            continue
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{label}*"}})
        for a in group[:MAX_DISPLAY]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fmt(a, emoji)}})
        overflow = len(group) - min(MAX_DISPLAY, len(group))
        if overflow > 0:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{overflow} more_"}]})
        blocks.append({"type": "divider"})

    return {"blocks": blocks}


def send_cbo_ad_kill_report(actions: list[CboAdKillAction], dry_run: bool, config: Config) -> bool:
    payload = _build_slack_message(actions, dry_run)
    if payload is None:
        logger.info("No CBO ad 3d kills — skipping Slack")
        return True
    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected CBO ad-kill report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send CBO ad-kill report: {e}")
        return False
