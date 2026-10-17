"""
Cost cap / bid cap guidance report. Daily at 9am AEST, report only — never
changes anything in the account.

For every ACTIVE adset on a cost cap or bid cap (set on the adset, or
inherited from a campaign-budget campaign), using complete days only:
  - cap, 3-day and 7-day ROAS, cost per purchase vs cap, spend
  - budget use over 7 days (is the cap holding spend back?)
  - trend: last 3 days vs last 7

Guidance (both windows must agree):
  🟢 ROAS >= 1.2 on 3d and 7d   -> raise cap ~10% (5% if the 3d trend is
                                   slipping); if already spending its full
                                   budget, raise the budget instead
  🟡 ROAS 1.0-1.2               -> cautious +5% if under-spending and the
                                   trend is flat/up, otherwise hold
  🔴 ROAS < 1.0 on 3d and 7d    -> lower cap ~10%
  ⚪ fewer than 5 purchases in 7d, or 3d and 7d disagree -> hold
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from config import Config
from meta_api import API_BASE
from midnight_restart import _http_get_json
from testing_ad_rules import _today

logger = logging.getLogger(__name__)

RAISE_ROAS = 1.2
LOWER_ROAS = 1.0
MIN_PURCHASES_7D = 5
STEP = 0.10
SMALL_STEP = 0.05
UNDERSPEND = 0.7               # < 70% of budget used -> cap is likely limiting spend
FULL_SPEND = 0.9               # >= 90% -> budget, not the cap, is the limit
CAP_STRATEGIES = {
    "COST_CAP": "Cost cap",
    "LOWEST_COST_WITH_BID_CAP": "Bid cap",
}


@dataclass
class CapRow:
    adset_id: str
    adset_name: str
    campaign_name: str
    cap_type: str
    cap: float
    roas3: float
    roas7: float
    cpa3: float
    cpa7: float
    spend3: float
    spend7: float
    purchases7: int
    budget_use: float | None    # 7d avg daily spend / daily budget
    budget_scope: str           # "adset" | "campaign"
    verdict: str                # raise | cautious | hold | lower | low_data | mixed
    suggestion: str


def _fetch_capped_adsets(config: Config) -> list[dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/adsets"
    params = {
        "access_token": config.meta_access_token,
        "fields": "id,name,bid_strategy,bid_amount,daily_budget,"
                  "campaign{id,name,bid_strategy,daily_budget}",
        "limit": 500,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
    }
    out = []
    first = True
    while url:
        data = _http_get_json(config, url, params if first else None)
        first = False
        for row in data.get("data", []):
            camp = row.get("campaign") or {}
            strategy = row.get("bid_strategy") or camp.get("bid_strategy")
            if strategy not in CAP_STRATEGIES or not row.get("bid_amount"):
                continue
            adset_budget = float(row.get("daily_budget") or 0) / 100
            camp_budget = float(camp.get("daily_budget") or 0) / 100
            out.append({
                "id": row["id"],
                "name": row.get("name", "Unknown"),
                "campaign_id": camp.get("id", ""),
                "campaign_name": camp.get("name", "Unknown"),
                "cap_type": CAP_STRATEGIES[strategy],
                "cap": float(row["bid_amount"]) / 100,
                "budget": adset_budget or camp_budget,
                "budget_scope": "adset" if adset_budget else "campaign",
            })
        url = data.get("paging", {}).get("next")
    logger.info(f"Cap report: {len(out)} active capped adsets")
    return out


def _fetch_daily(config: Config, level: str, since: date, until: date) -> list[dict]:
    """Daily spend / purchases / revenue per adset or campaign."""
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": level,
        "fields": f"{level}_id,spend,actions,action_values",
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "time_increment": 1,
        "limit": 500,
    }
    rows = []
    first = True
    while url:
        data = _http_get_json(config, url, params if first else None)
        first = False
        for r in data.get("data", []):
            purchases = sum(int(float(a.get("value", 0))) for a in r.get("actions", []) or []
                            if a.get("action_type") == "purchase")
            revenue = sum(float(a.get("value", 0)) for a in r.get("action_values", []) or []
                          if a.get("action_type") == "purchase")
            rows.append({"id": r[f"{level}_id"], "date": date.fromisoformat(r["date_start"]),
                         "spend": float(r.get("spend", 0)), "purchases": purchases, "revenue": revenue})
        url = data.get("paging", {}).get("next")
    return rows


def _window(rows: list[dict], since: date, until: date) -> dict[str, dict]:
    out = defaultdict(lambda: {"spend": 0.0, "purchases": 0, "revenue": 0.0})
    for r in rows:
        if since <= r["date"] <= until:
            t = out[r["id"]]
            t["spend"] += r["spend"]
            t["purchases"] += r["purchases"]
            t["revenue"] += r["revenue"]
    return out


def _guidance(cap: float, roas3: float, roas7: float, purchases7: int,
              budget_use: float | None) -> tuple[str, str]:
    if purchases7 < MIN_PURCHASES_7D:
        return "low_data", f"Hold — only {purchases7} purchases in 7 days"
    slipping = roas3 < roas7 * 0.9
    under = budget_use is not None and budget_use < UNDERSPEND
    full = budget_use is not None and budget_use >= FULL_SPEND

    if roas3 >= RAISE_ROAS and roas7 >= RAISE_ROAS:
        if full:
            return "raise", "Raise budget — already spending its full budget, so the cap isn't the limit"
        step = SMALL_STEP if slipping else STEP
        note = " (smaller step: last 3 days slipping)" if slipping else ""
        return "raise", f"Raise cap ~{step:.0%} → ${cap * (1 + step):.2f}{note}"
    if roas3 < LOWER_ROAS and roas7 < LOWER_ROAS:
        return "lower", f"Lower cap ~{STEP:.0%} → ${cap * (1 - STEP):.2f}"
    if roas3 >= LOWER_ROAS and roas7 >= LOWER_ROAS:
        if under and not slipping:
            return "cautious", f"Cautious raise ~{SMALL_STEP:.0%} → ${cap * (1 + SMALL_STEP):.2f} (under-spending, trend steady)"
        return "cautious", "Hold — in the 1.0-1.2x zone" + (" and slipping" if slipping else "")
    return "mixed", "Hold — 3-day and 7-day ROAS disagree, wait for the trend to settle"


def build_cap_report(config: Config) -> list[CapRow]:
    adsets = _fetch_capped_adsets(config)
    if not adsets:
        return []
    yesterday = _today() - timedelta(days=1)
    d7, d3 = yesterday - timedelta(days=6), yesterday - timedelta(days=2)
    adset_rows = _fetch_daily(config, "adset", d7, yesterday)
    w7, w3 = _window(adset_rows, d7, yesterday), _window(adset_rows, d3, yesterday)
    # Campaign-budget adsets share one budget, so budget use is measured on
    # the campaign's total spend.
    camp_w7 = {}
    if any(a["budget_scope"] == "campaign" for a in adsets):
        camp_w7 = _window(_fetch_daily(config, "campaign", d7, yesterday), d7, yesterday)

    rows: list[CapRow] = []
    for a in adsets:
        t7, t3 = w7.get(a["id"]), w3.get(a["id"])
        if not t7 or t7["spend"] <= 0:
            continue
        t3 = t3 or {"spend": 0.0, "purchases": 0, "revenue": 0.0}
        roas7 = t7["revenue"] / t7["spend"]
        roas3 = t3["revenue"] / t3["spend"] if t3["spend"] else 0.0
        cpa7 = t7["spend"] / t7["purchases"] if t7["purchases"] else 0.0
        cpa3 = t3["spend"] / t3["purchases"] if t3["purchases"] else 0.0
        if a["budget"]:
            spent = t7["spend"] if a["budget_scope"] == "adset" else camp_w7.get(a["campaign_id"], {}).get("spend", 0.0)
            budget_use = (spent / 7) / a["budget"]
        else:
            budget_use = None
        verdict, suggestion = _guidance(a["cap"], roas3, roas7, t7["purchases"], budget_use)
        rows.append(CapRow(a["id"], a["name"], a["campaign_name"], a["cap_type"], a["cap"],
                           roas3, roas7, cpa3, cpa7, t3["spend"], t7["spend"], t7["purchases"],
                           budget_use, a["budget_scope"], verdict, suggestion))
    rows.sort(key=lambda r: -r.spend7)
    return rows


def send_cap_report(rows: list[CapRow], config: Config) -> bool:
    if not rows:
        logger.info("Cap report: no capped adsets with spend — skipping Slack")
        return True
    sections = (
        ("🟢 Raise", ("raise",)),
        ("🟡 Cautious / hold", ("cautious",)),
        ("🔴 Pull back", ("lower",)),
        ("⚪ Hold — not enough signal", ("low_data", "mixed")),
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎯 Cap report — {len(rows)} adsets"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"Complete days only (to yesterday). ROAS ≥{RAISE_ROAS} on 3d & 7d → raise │ "
            f"{LOWER_ROAS}-{RAISE_ROAS} → cautious/hold │ <{LOWER_ROAS} on 3d & 7d → pull back. "
            f"Needs ≥{MIN_PURCHASES_7D} purchases in 7d. Report only — nothing is changed."
        )}]},
    ]
    for heading, verdicts in sections:
        group = [r for r in rows if r.verdict in verdicts]
        if not group:
            continue
        lines = []
        for r in group[:12]:
            arrow = "↑" if r.roas3 > r.roas7 * 1.05 else "↓" if r.roas3 < r.roas7 * 0.95 else "→"
            use = f"{r.budget_use:.0%} of {r.budget_scope} budget" if r.budget_use is not None else "no daily budget"
            cpa7 = f"${r.cpa7:.2f}" if r.cpa7 else "—"
            cpa3 = f"${r.cpa3:.2f}" if r.cpa3 else "—"
            lines.append(
                f"• *{r.adset_name}* — `{r.campaign_name}`\n"
                f"   {r.cap_type} ${r.cap:.2f} │ 7d: ROAS {r.roas7:.2f}x, CPA {cpa7}, ${r.spend7:,.0f}, {r.purchases7}p │ "
                f"3d: ROAS {r.roas3:.2f}x {arrow}, CPA {cpa3} │ {use}\n"
                f"   ➜ *{r.suggestion}*"
            )
        if len(group) > 12:
            lines.append(f"_+{len(group) - 12} more_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{heading}*\n" + "\n".join(lines)}})
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected cap report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send cap report: {e}")
        return False
