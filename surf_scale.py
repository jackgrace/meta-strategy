"""
Surf scaling for TESTING campaign adsets with WINNER in the name.

Every 60 min:
- Fetch today's spend + ROAS per adset (level=adset).
- For each ACTIVE adset in a TESTING campaign whose name contains "WINNER"
  (and does NOT contain "OFF"), get its current daily_budget.
- If today's spend > 25% of current daily_budget AND today's ROAS > 2.0:
  double the daily_budget (bid untouched).

Midnight (12:05am AEST, wired from server.py):
- Reset every TESTING WINNER adset's daily_budget to DEFAULT_BUDGET (\$200).

Meta stores budgets in the account currency's minor units — cents for AUD/USD.
So \$200 = 20000. We do all math in dollars and convert at the API boundary.
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

SPEND_PCT_THRESHOLD = 0.25       # spent > 25% of current daily budget
ROAS_THRESHOLD = 2.0             # today's ROAS > 2.0
DEFAULT_BUDGET_DOLLARS = 200.0   # midnight reset value
MAX_BUDGET_DOLLARS = 10000.0     # sanity cap so runaway doubling can't nuke us

WINNER_KEYWORD = "WINNER"
OFF_KEYWORD = "OFF"


@dataclass
class SurfAction:
    adset_id: str
    adset_name: str
    campaign_name: str
    action: str            # "would_double" | "doubled" | "skipped" | "failed" | "reset" | "would_reset"
    reason: str
    spend_today: float
    roas_today: float
    old_budget: float
    new_budget: float


# ---------- HTTP with retry ----------

def _http_json(config: Config, method: str, url: str, **kwargs) -> requests.Response:
    """POST or GET with retry on transient errors. Returns the response."""
    for attempt in range(4):
        try:
            resp = requests.request(method, url, timeout=60, **kwargs)
            is_retryable = False
            if resp.status_code in (500, 502, 503, 504):
                is_retryable = True
            elif resp.status_code == 400:
                try:
                    err = resp.json().get("error", {})
                    code = err.get("code")
                    subcode = err.get("error_subcode")
                    msg = (err.get("message", "") + " " + err.get("error_user_msg", "")).lower()
                    is_retryable = (
                        err.get("is_transient") is True
                        or code in (1, 2, 4, 17, 32)
                        or subcode in (1504018, 1487742, 1504044)
                        or any(k in msg for k in ("temporarily", "limit reached", "too many", "try again", "load", "unavailable"))
                    )
                except Exception:
                    pass
            if is_retryable and attempt < 3:
                wait = [15, 30, 60][attempt]
                logger.warning(f"Surf scale {method} {resp.status_code}, retrying in {wait}s: {resp.text[:200]}")
                time.sleep(wait)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < 3:
                wait = [15, 30, 60][attempt]
                logger.warning(f"Surf scale {method} network error, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("unreachable")


# ---------- Fetchers ----------

def _fetch_todays_adset_metrics(config: Config) -> dict:
    """
    Adset-level insights for today.
    Returns {adset_id: {campaign_name, adset_name, spend, revenue, roas, purchases}}.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "adset",
        "fields": "adset_id,adset_name,campaign_name,spend,action_values,actions",
        "date_preset": "today",
        "limit": 200,
        "filtering": '[{"field":"impressions","operator":"GREATER_THAN","value":"0"}]',
    }

    out: dict = {}
    page_count = 0
    while url:
        page_count += 1
        resp = _http_json(config, "GET", url, params=params if page_count == 1 else None)
        if not resp.ok:
            raise requests.exceptions.HTTPError(
                f"Meta {resp.status_code}: {resp.text[:400]}", response=resp,
            )
        data = resp.json()
        for row in data.get("data", []):
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
                "campaign_name": row.get("campaign_name", "Unknown"),
                "adset_name": row.get("adset_name", "Unknown"),
                "spend": spend,
                "revenue": revenue,
                "roas": revenue / spend if spend > 0 else 0,
                "purchases": purchases,
            }
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None
    logger.info(f"Surf scale fetched today's metrics for {len(out)} adsets ({page_count} pages)")
    return out


def _fetch_winner_testing_adsets(config: Config) -> dict:
    """
    Fetch all ACTIVE adsets in TESTING campaigns whose name has WINNER
    and does NOT contain OFF. Returns {adset_id: {name, daily_budget_dollars,
    campaign_name, effective_status}}.
    """
    url = f"{API_BASE}/{config.meta_ad_account_id}/adsets"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,name,daily_budget,effective_status,campaign{name,effective_status}",
        "limit": 500,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
    }

    out: dict = {}
    page_count = 0
    while url:
        page_count += 1
        resp = _http_json(config, "GET", url, params=params if page_count == 1 else None)
        if not resp.ok:
            raise requests.exceptions.HTTPError(
                f"Meta {resp.status_code}: {resp.text[:400]}", response=resp,
            )
        data = resp.json()
        for row in data.get("data", []):
            name = row.get("name", "")
            campaign = row.get("campaign", {}) or {}
            campaign_name = campaign.get("name", "")
            if "TESTING" not in campaign_name.upper():
                continue
            if WINNER_KEYWORD not in name.upper():
                continue
            if OFF_KEYWORD in name.upper():
                continue
            if campaign.get("effective_status") != "ACTIVE":
                continue
            daily_budget_raw = row.get("daily_budget")
            if not daily_budget_raw:
                # No daily_budget = CBO or lifetime budget — can't scale
                continue
            out[row["id"]] = {
                "name": name,
                "campaign_name": campaign_name,
                "daily_budget_dollars": float(daily_budget_raw) / 100.0,
                "effective_status": row.get("effective_status", "UNKNOWN"),
            }
        paging = data.get("paging", {})
        url = paging.get("next")
        params = None
    logger.info(f"Surf scale: {len(out)} candidate WINNER adsets (TESTING/ACTIVE/no OFF)")
    return out


def _update_adset_budget(config: Config, adset_id: str, new_budget_dollars: float) -> tuple[bool, str]:
    """POST daily_budget update in cents. Returns (success, reason)."""
    url = f"{API_BASE}/{adset_id}"
    new_cents = int(round(new_budget_dollars * 100))
    resp = _http_json(
        config, "POST",
        f"{url}?access_token={config.meta_access_token}",
        data={"daily_budget": str(new_cents)},
    )
    if resp.ok:
        return True, "ok"
    try:
        err = resp.json().get("error", {})
        return False, err.get("error_user_title", err.get("message", "Unknown"))
    except Exception:
        return False, f"HTTP {resp.status_code}"


# ---------- Rules ----------

def run_surf_scale(config: Config, dry_run: bool = False) -> list[SurfAction]:
    """Every 60 min: double budget on TESTING WINNER adsets that meet criteria."""
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Surf scale check [{mode}] ===")

    adsets = _fetch_winner_testing_adsets(config)
    if not adsets:
        return []

    metrics = _fetch_todays_adset_metrics(config)

    actions: list[SurfAction] = []
    doubled = 0
    skipped = 0
    failed = 0

    for adset_id, meta in adsets.items():
        current_budget = meta["daily_budget_dollars"]
        row = metrics.get(adset_id)
        if not row:
            # No delivery today yet — nothing to scale on
            continue

        spend = row["spend"]
        roas = row["roas"]
        spend_gate = current_budget * SPEND_PCT_THRESHOLD

        if spend <= spend_gate or roas <= ROAS_THRESHOLD:
            skipped += 1
            continue

        new_budget = current_budget * 2
        if new_budget > MAX_BUDGET_DOLLARS:
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="skipped",
                reason=f"would exceed cap ${MAX_BUDGET_DOLLARS:.0f}",
                spend_today=spend, roas_today=roas,
                old_budget=current_budget, new_budget=current_budget,
            ))
            continue

        if dry_run:
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="would_double",
                reason="dry run", spend_today=spend, roas_today=roas,
                old_budget=current_budget, new_budget=new_budget,
            ))
            continue

        ok, reason = _update_adset_budget(config, adset_id, new_budget)
        if ok:
            doubled += 1
            logger.info(f"SURF SCALE: Doubled {adset_id} ({meta['name']}) ${current_budget:.2f} → ${new_budget:.2f} (spend ${spend:.2f}, ROAS {roas:.2f})")
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="doubled",
                reason=f"spend ${spend:.2f} > 25% of ${current_budget:.2f} & ROAS {roas:.2f} > {ROAS_THRESHOLD}",
                spend_today=spend, roas_today=roas,
                old_budget=current_budget, new_budget=new_budget,
            ))
        else:
            failed += 1
            logger.warning(f"SURF SCALE: Failed to double {adset_id}: {reason}")
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="failed",
                reason=reason, spend_today=spend, roas_today=roas,
                old_budget=current_budget, new_budget=current_budget,
            ))

    logger.info(f"Surf scale complete: {doubled} doubled, {skipped} skipped, {failed} failed")
    return actions


def run_midnight_budget_reset(config: Config, dry_run: bool = False) -> list[SurfAction]:
    """
    At 12:05am AEST: reset every TESTING WINNER adset's daily_budget back
    to DEFAULT_BUDGET_DOLLARS. Skips if the adset is already at that value
    (avoids no-op API calls).
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"=== Midnight budget reset [{mode}] — target ${DEFAULT_BUDGET_DOLLARS:.0f} ===")

    adsets = _fetch_winner_testing_adsets(config)
    if not adsets:
        return []

    actions: list[SurfAction] = []
    reset = 0
    unchanged = 0
    failed = 0

    for adset_id, meta in adsets.items():
        current_budget = meta["daily_budget_dollars"]
        if abs(current_budget - DEFAULT_BUDGET_DOLLARS) < 0.01:
            unchanged += 1
            continue

        if dry_run:
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="would_reset",
                reason="dry run", spend_today=0, roas_today=0,
                old_budget=current_budget, new_budget=DEFAULT_BUDGET_DOLLARS,
            ))
            continue

        ok, reason = _update_adset_budget(config, adset_id, DEFAULT_BUDGET_DOLLARS)
        if ok:
            reset += 1
            logger.info(f"MIDNIGHT RESET: {adset_id} ({meta['name']}) ${current_budget:.2f} → ${DEFAULT_BUDGET_DOLLARS:.2f}")
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="reset",
                reason=reason, spend_today=0, roas_today=0,
                old_budget=current_budget, new_budget=DEFAULT_BUDGET_DOLLARS,
            ))
        else:
            failed += 1
            logger.warning(f"MIDNIGHT RESET: Failed for {adset_id}: {reason}")
            actions.append(SurfAction(
                adset_id=adset_id, adset_name=meta["name"],
                campaign_name=meta["campaign_name"], action="failed",
                reason=reason, spend_today=0, roas_today=0,
                old_budget=current_budget, new_budget=current_budget,
            ))

    logger.info(f"Midnight budget reset complete: {reset} reset, {unchanged} already at default, {failed} failed")
    return actions


# ---------- Slack ----------

def _build_slack_message(actions: list[SurfAction], title: str, dry_run: bool) -> dict | None:
    if not actions:
        return None
    now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
    mode = "DRY RUN" if dry_run else "LIVE"

    doubled = [a for a in actions if a.action in ("doubled", "would_double")]
    reset = [a for a in actions if a.action in ("reset", "would_reset")]
    failed = [a for a in actions if a.action == "failed"]
    skipped = [a for a in actions if a.action == "skipped"]

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"🏄 {title} — {now}"}
    }]

    parts = []
    if doubled:
        parts.append(f"🟢 {len(doubled)} doubled")
    if reset:
        parts.append(f"🔄 {len(reset)} reset")
    if failed:
        parts.append(f"⚠️ {len(failed)} failed")
    if skipped:
        parts.append(f"⏭️ {len(skipped)} skipped")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"*[{mode}]* " + " │ ".join(parts) + "\n"
            f"_TESTING adsets with WINNER in name (no OFF), ACTIVE, ABO only._"
        )}
    })
    blocks.append({"type": "divider"})

    MAX_DISPLAY = 20

    def _fmt(a: SurfAction, emoji: str) -> str:
        lines = [
            f"{emoji} *{a.adset_name}*",
            f"Campaign: `{a.campaign_name}`",
            f"Budget: ${a.old_budget:.2f} → ${a.new_budget:.2f}",
        ]
        if a.spend_today or a.roas_today:
            lines.append(f"Today spend: ${a.spend_today:.2f} │ ROAS: {a.roas_today:.2f}x")
        if a.reason and a.action in ("failed", "skipped"):
            lines.append(f"_{a.reason}_")
        return "\n".join(lines)

    for group, emoji, label in (
        (doubled, "🟢", "Doubled"),
        (reset, "🔄", "Reset to default"),
        (failed, "⚠️", "Failed"),
        (skipped, "⏭️", "Skipped"),
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


def send_surf_report(actions: list[SurfAction], dry_run: bool, config: Config, title: str = "Surf Scale") -> bool:
    payload = _build_slack_message(actions, title, dry_run)
    if payload is None:
        return True
    try:
        resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected surf report: {resp.status_code} — {resp.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send surf report: {e}")
        return False
