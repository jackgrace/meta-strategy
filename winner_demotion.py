"""
WINNER demotion — daily cleanup for stale WINNER-tagged adsets.

Trigger (per adset, rolling last 3 days):
- Adset name contains "WINNER"
- 3d spend > $50
- 3d purchases == 0 OR 3d ROAS < 1.6

Action:
- Strip "WINNER" from the adset name and append " - OFF"
- Pause the adset

Runs once per day (piggybacks the 1am AEST daily scheduler). Keeps
surf-scale from doubling budgets on WINNER-tagged adsets that no longer
deserve the tag, and keeps the account clean.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

from meta_api import API_BASE
from config import Config

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

DEMOTE_SPEND_3D = 50.0
DEMOTE_ROAS_3D = 1.6
WINNER_KEYWORD = "WINNER"
OFF_SUFFIX = " - OFF"


@dataclass
class DemotionAction:
    adset_id: str
    old_name: str
    new_name: str
    campaign_name: str
    action: str      # "demoted" | "would_demote" | "failed" | "partial"
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
                logger.warning(f"Winner-demotion {method} {resp.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 3:
                wait = [15, 30, 60][attempt]
                logger.warning(f"Winner-demotion {method} network error, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("unreachable")


def _fetch_3d_adset_metrics(config: Config) -> dict:
    """
    Adset-level insights, last 3 days, for adsets whose name contains WINNER.
    Server-side filter on adset name; still filter client-side to be safe.
    Returns {adset_id: {adset_name, campaign_name, spend, revenue, roas, purchases}}.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "adset",
        "fields": "adset_id,adset_name,campaign_name,spend,actions,action_values",
        "date_preset": "last_3d",
        "limit": 200,
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"adset.name","operator":"CONTAIN","value":"WINNER"}]'
        ),
    }
    out: dict = {}
    page_count = 0
    while url:
        page_count += 1
        resp = _http_retry(config, "GET", url, params=params if page_count == 1 else None)
        if not resp.ok:
            raise requests.exceptions.HTTPError(
                f"Meta {resp.status_code}: {resp.text[:400]}", response=resp,
            )
        data = resp.json()
        for row in data.get("data", []):
            name = row.get("adset_name", "")
            if WINNER_KEYWORD not in name.upper():
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
            out[row["adset_id"]] = {
                "adset_name": name,
                "campaign_name": row.get("campaign_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "roas": revenue / spend if spend > 0 else 0,
                "purchases": purchases,
            }
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None
    logger.info(f"Winner-demotion 3d fetch: {len(out)} WINNER adsets returned ({page_count} pages)")
    return out


def _fetch_adset_names_and_status(config: Config, adset_ids: set[str]) -> dict:
    """Batch-fetch {adset_id: {name, effective_status}} — needed to know we're not
    already-paused / already-OFF, and to get the authoritative current name."""
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
            logger.error(f"Winner-demotion status batch error {resp.status_code}: {resp.text[:200]}")
            continue
        data = resp.json()
        for aid, adata in data.items():
            out[aid] = {
                "name": adata.get("name", ""),
                "effective_status": adata.get("effective_status", "UNKNOWN"),
            }
    return out


def _strip_winner_add_off(name: str) -> str:
    """Strip a trailing/embedded ' WINNER' (case-insensitive) and append ' - OFF'."""
    # Remove standalone WINNER token, collapse whitespace, then append OFF.
    stripped = re.sub(r"\s*\bWINNER\b\s*", " ", name, flags=re.IGNORECASE).strip()
    # Normalize double spaces
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped + OFF_SUFFIX


def _pause_and_rename(config: Config, adset_id: str, new_name: str) -> tuple[str, str]:
    """
    Pause first, then rename. Two separate calls (matches auto_pause pattern —
    combined status+name in one call has triggered creative-validation errors
    on ASC-adjacent objects before). Returns (action, reason).
    action ∈ {"demoted", "partial", "failed"}.
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

    return "demoted", "ok"


def run_winner_demotion(config: Config, dry_run: bool = False) -> list[DemotionAction]:
    """Once-daily WINNER cleanup. Returns list of demotion actions."""
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== WINNER demotion [{mode}] ===")

    try:
        metrics = _fetch_3d_adset_metrics(config)
    except Exception as e:
        logger.error(f"Winner-demotion 3d fetch failed: {e}")
        return []

    if not metrics:
        return []

    status_map = _fetch_adset_names_and_status(config, set(metrics.keys()))

    actions: list[DemotionAction] = []
    demoted_count = 0
    partial_count = 0
    failed_count = 0

    for adset_id, m in metrics.items():
        spend = m["spend"]
        roas = m["roas"]
        purchases = m["purchases"]

        # Trigger: 3d spend > $50 AND (0 purchases OR ROAS < 1.6)
        if spend <= DEMOTE_SPEND_3D:
            continue
        if not (purchases == 0 or roas < DEMOTE_ROAS_3D):
            continue

        info = status_map.get(adset_id, {})
        current_name = info.get("name", m["adset_name"])
        current_status = info.get("effective_status", "UNKNOWN")

        # If already renamed to OFF, or WINNER already stripped, nothing to do.
        if WINNER_KEYWORD not in current_name.upper():
            continue
        if OFF_SUFFIX.strip().upper() in current_name.upper():
            continue

        new_name = _strip_winner_add_off(current_name)
        reason = (
            f"3d spend ${spend:.2f} & "
            + ("0 purchases" if purchases == 0 else f"ROAS {roas:.2f} < {DEMOTE_ROAS_3D}")
        )

        if dry_run:
            actions.append(DemotionAction(
                adset_id=adset_id, old_name=current_name, new_name=new_name,
                campaign_name=m["campaign_name"], action="would_demote", reason=reason,
                spend_3d=spend, revenue_3d=m["revenue"], roas_3d=roas, purchases_3d=purchases,
            ))
            continue

        action, op_reason = _pause_and_rename(config, adset_id, new_name)
        if action == "demoted":
            demoted_count += 1
            logger.info(f"WINNER DEMOTED: {adset_id} '{current_name}' → '{new_name}' — {reason}")
        elif action == "partial":
            partial_count += 1
            logger.warning(f"WINNER DEMOTE partial: {adset_id} — {op_reason}")
        else:
            failed_count += 1
            logger.warning(f"WINNER DEMOTE failed: {adset_id} — {op_reason}")

        actions.append(DemotionAction(
            adset_id=adset_id, old_name=current_name, new_name=new_name,
            campaign_name=m["campaign_name"], action=action,
            reason=f"{reason} │ {op_reason}" if action != "demoted" else reason,
            spend_3d=spend, revenue_3d=m["revenue"], roas_3d=roas, purchases_3d=purchases,
        ))

    logger.info(
        f"Winner demotion complete: {demoted_count} demoted, "
        f"{partial_count} partial, {failed_count} failed"
    )
    return actions


def _build_slack_message(actions: list[DemotionAction], dry_run: bool) -> dict | None:
    if not actions:
        return None
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    demoted = [a for a in actions if a.action in ("demoted", "would_demote")]
    partial = [a for a in actions if a.action == "partial"]
    failed = [a for a in actions if a.action == "failed"]

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"🏷️ WINNER Demotion — {now}"}
    }]

    parts = []
    if demoted:
        verb = "would demote" if dry_run else "demoted"
        parts.append(f"⚫ {len(demoted)} {verb}")
    if partial:
        parts.append(f"⚠️ {len(partial)} partial")
    if failed:
        parts.append(f"❌ {len(failed)} failed")

    total_wasted = sum(a.spend_3d for a in demoted)

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(parts) + "\n"
            f"_Rule: WINNER adset │ 3d spend>${DEMOTE_SPEND_3D:.0f} & (0 purchases OR ROAS<{DEMOTE_ROAS_3D})_\n"
            f"_Action: strip WINNER, append `- OFF`, pause_\n"
            f"3d spend on demoted adsets: *${total_wasted:.2f}*"
        )}
    })
    blocks.append({"type": "divider"})

    MAX_DISPLAY = 15

    def _fmt(a: DemotionAction, emoji: str) -> str:
        return (
            f"{emoji} `{a.old_name}` → `{a.new_name}`\n"
            f"Campaign: `{a.campaign_name}`\n"
            f"3d: spend ${a.spend_3d:.2f} │ rev ${a.revenue_3d:.2f} │ ROAS {a.roas_3d:.2f}x │ {a.purchases_3d} purchases\n"
            f"_Why: {a.reason}_"
        )

    for group, emoji, label in (
        (demoted, "⚫", "Demoted"),
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


def send_winner_demotion_report(actions: list[DemotionAction], dry_run: bool, config: Config) -> bool:
    payload = _build_slack_message(actions, dry_run)
    if payload is None:
        logger.info("No WINNER demotions — skipping Slack")
        return True
    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected winner-demotion report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send winner-demotion report: {e}")
        return False
