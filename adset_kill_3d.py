"""
3-day hard-kill for CC/SCALE/VALUE adsets that are chronic underperformers.

Trigger (per adset, rolling last 3 days):
- Adset in a campaign whose name contains CC, SCALE, or VALUE
- Adset name does NOT already contain 'OFF'
- 3d spend > $200
- 3d ROAS < 1.6

Action:
- Pause the adset
- Rename by appending ' - OFF'

Permanent — no auto-restart. Because the name gets ' - OFF' appended,
midnight restart will skip it (name filter). Bring it back manually
after review by removing the OFF suffix.

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

KILL_SPEND_3D = 200.0
KILL_ROAS_3D = 1.6
OFF_SUFFIX = " - OFF"
CVS_KEYWORDS = {"CC", "SCALE", "VALUE"}


@dataclass
class AdsetKillAction:
    adset_id: str
    old_name: str
    new_name: str
    campaign_name: str
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
                logger.warning(f"Adset-kill {method} {resp.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 3:
                wait = [15, 30, 60][attempt]
                logger.warning(f"Adset-kill {method} network error, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("unreachable")


def _is_cvs_campaign(name: str) -> bool:
    parts = [p.strip() for p in name.upper().replace("|", " ").split()]
    return any(k in parts for k in CVS_KEYWORDS)


def _fetch_3d_adset_metrics(config: Config) -> dict:
    """
    Adset-level insights, last 3 days, for CC/SCALE/VALUE campaigns.
    Server-side filter on campaign name; still filter client-side to be safe.
    Returns {adset_id: {adset_name, campaign_name, spend, revenue, roas, purchases}}.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "adset",
        "fields": "adset_id,adset_name,campaign_name,spend,actions,action_values",
        "date_preset": "last_3d",
        "limit": 200,
        # Any campaign whose name contains CC, SCALE or VALUE. Meta's `IN` for
        # substring isn't supported here, so we OR multiple CONTAIN clauses.
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"CC"}]'
        ),
    }
    # For SCALE and VALUE we page separately then merge — cheaper than a
    # broad fetch and easier than encoding OR groups in the filtering spec.
    variants = [
        ("CC", params.copy()),
        ("SCALE", {**params, "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"SCALE"}]'
        )}),
        ("VALUE", {**params, "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"VALUE"}]'
        )}),
    ]

    out: dict = {}
    for label, variant_params in variants:
        cur_url = url
        cur_params = variant_params
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
                if not _is_cvs_campaign(campaign_name):
                    continue
                if row["adset_id"] in out:
                    continue  # dedupe across variants
                spend = float(row.get("spend", 0))
                revenue = 0.0
                purchases = 0
                for av in row.get("action_values", []) or []:
                    if av.get("action_type") == "purchase":
                        revenue = float(av.get("value", 0))
                for a in row.get("actions", []) or []:
                    if a.get("action_type") == "purchase":
                        purchases = int(float(a.get("value", 0)))
                out[row["adset_id"]] = {
                    "adset_name": row.get("adset_name", ""),
                    "campaign_name": campaign_name,
                    "spend": spend,
                    "revenue": revenue,
                    "roas": revenue / spend if spend > 0 else 0,
                    "purchases": purchases,
                }
            paging = data.get("paging", {})
            cur_url = paging.get("next")
            cur_params = None
    logger.info(f"Adset-kill 3d fetch: {len(out)} CC/SCALE/VALUE adsets returned")
    return out


def _fetch_adset_names(config: Config, adset_ids: set[str]) -> dict:
    """Batch-fetch {adset_id: {name, effective_status}} — authoritative
    current name (so we don't rename based on stale insights data)."""
    out: dict = {}
    if not adset_ids:
        return out
    id_list = list(adset_ids)
    batch_size = 50
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i:i + batch_size]
        resp = _http_retry(
            config, "GET", f"{API_BASE}/",
            params={
                "access_token": config.meta_access_token,
                "ids": ",".join(batch),
                "fields": "id,name,effective_status",
            },
        )
        if not resp.ok:
            logger.error(f"Adset-kill status batch error {resp.status_code}: {resp.text[:200]}")
            continue
        data = resp.json()
        for aid, adata in data.items():
            out[aid] = {
                "name": adata.get("name", ""),
                "effective_status": adata.get("effective_status", "UNKNOWN"),
            }
    return out


def _pause_and_rename(config: Config, adset_id: str, new_name: str) -> tuple[str, str]:
    """
    Pause first, then rename. Two separate calls to avoid combined-update
    validation errors. Returns (action, reason).
    action ∈ {"killed", "partial", "failed"}.
    """
    # 1. Pause
    resp = _http_retry(
        config, "POST",
        f"{API_BASE}/{adset_id}?access_token={config.meta_access_token}",
        data={"status": "PAUSED"},
    )
    if not resp.ok:
        try:
            err = resp.json().get("error", {})
            reason = err.get("error_user_title", err.get("message", "Unknown"))
        except Exception:
            reason = f"HTTP {resp.status_code}"
        return "failed", f"pause failed: {reason}"

    # 2. Rename
    resp = _http_retry(
        config, "POST",
        f"{API_BASE}/{adset_id}?access_token={config.meta_access_token}",
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


def run_adset_kill_3d(config: Config, dry_run: bool = False) -> list[AdsetKillAction]:
    """Once-daily 3d hard-kill for CC/SCALE/VALUE adsets."""
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Adset kill (3d hard-kill) [{mode}] ===")

    try:
        metrics = _fetch_3d_adset_metrics(config)
    except Exception as e:
        logger.error(f"Adset-kill 3d fetch failed: {e}")
        return []

    if not metrics:
        return []

    status_map = _fetch_adset_names(config, set(metrics.keys()))

    actions: list[AdsetKillAction] = []
    killed_count = 0
    partial_count = 0
    failed_count = 0

    for adset_id, m in metrics.items():
        spend = m["spend"]
        roas = m["roas"]
        purchases = m["purchases"]

        if spend <= KILL_SPEND_3D:
            continue
        if roas >= KILL_ROAS_3D:
            continue

        info = status_map.get(adset_id, {})
        current_name = info.get("name", m["adset_name"])

        # Skip if already OFF (idempotent)
        if OFF_SUFFIX.strip().upper() in current_name.upper():
            continue

        new_name = current_name.rstrip() + OFF_SUFFIX
        reason = f"3d spend ${spend:.2f} & ROAS {roas:.2f} < {KILL_ROAS_3D}"

        if dry_run:
            actions.append(AdsetKillAction(
                adset_id=adset_id, old_name=current_name, new_name=new_name,
                campaign_name=m["campaign_name"], action="would_kill", reason=reason,
                spend_3d=spend, revenue_3d=m["revenue"], roas_3d=roas, purchases_3d=purchases,
            ))
            continue

        action, op_reason = _pause_and_rename(config, adset_id, new_name)
        if action == "killed":
            killed_count += 1
            logger.info(f"ADSET KILL (3d): {adset_id} '{current_name}' → '{new_name}' — {reason}")
        elif action == "partial":
            partial_count += 1
            logger.warning(f"ADSET KILL (3d) partial: {adset_id} — {op_reason}")
        else:
            failed_count += 1
            logger.warning(f"ADSET KILL (3d) failed: {adset_id} — {op_reason}")

        actions.append(AdsetKillAction(
            adset_id=adset_id, old_name=current_name, new_name=new_name,
            campaign_name=m["campaign_name"], action=action,
            reason=f"{reason} │ {op_reason}" if action != "killed" else reason,
            spend_3d=spend, revenue_3d=m["revenue"], roas_3d=roas, purchases_3d=purchases,
        ))

    logger.info(
        f"Adset kill 3d complete: {killed_count} killed, "
        f"{partial_count} partial, {failed_count} failed"
    )
    return actions


def _build_slack_message(actions: list[AdsetKillAction], dry_run: bool) -> dict | None:
    if not actions:
        return None
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    killed = [a for a in actions if a.action in ("killed", "would_kill")]
    partial = [a for a in actions if a.action == "partial"]
    failed = [a for a in actions if a.action == "failed"]

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"☠️ Adset 3d Hard-Kill — {now}"}
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

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(parts) + "\n"
            f"_Rule: CC/SCALE/VALUE adset │ 3d spend>${KILL_SPEND_3D:.0f} & 3d ROAS<{KILL_ROAS_3D}_\n"
            f"_Action: pause + append `- OFF` (permanent — manual review to revive)_\n"
            f"3d spend on killed adsets: *${total_spend:.2f}*"
        )}
    })
    blocks.append({"type": "divider"})

    MAX_DISPLAY = 15

    def _fmt(a: AdsetKillAction, emoji: str) -> str:
        return (
            f"{emoji} `{a.old_name}` → `{a.new_name}`\n"
            f"Campaign: `{a.campaign_name}`\n"
            f"3d: spend ${a.spend_3d:.2f} │ rev ${a.revenue_3d:.2f} │ ROAS {a.roas_3d:.2f}x │ {a.purchases_3d} purchases\n"
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


def send_adset_kill_report(actions: list[AdsetKillAction], dry_run: bool, config: Config) -> bool:
    payload = _build_slack_message(actions, dry_run)
    if payload is None:
        logger.info("No adset 3d kills — skipping Slack")
        return True
    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected adset-kill report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send adset-kill report: {e}")
        return False
