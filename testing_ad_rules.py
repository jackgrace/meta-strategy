"""
Ad-level rules for TESTING / TRYBE and SCALE campaigns, with three strikes.

No rule marks an ad OFF on the first day. Every 15 minutes a failing ad is
paused for the rest of the day (and restarted if it stops failing). At
12:05am AEST, before the midnight restart, an ad that failed on each of the
last 3 days is marked " - OFF"; ads that failed yesterday but haven't hit 3
strikes are switched back on (incl. TESTING/TRYBE ads, which the midnight
restart otherwise leaves paused).

A day only counts if the ad spent at least $30 that day, so a restarted ad
must earn fresh data before it can be judged again.

Strikes are recomputed from Meta's daily numbers on each run, so nothing is
lost when the server redeploys.

TESTING / TRYBE (last 7 days ending that day, vs the OTHER ads in the
campaign — the ad itself is left out of the average):
  1. spend > $30  & CPC > 3x campaign avg CPC              (0 clicks counts)
  2. spend > $60  & 0 ATCs
  3. spend > $125 & (0 purchases
                     OR (cost/ATC > 1.5x campaign avg & ROAS < 1.4))

SCALE (SCALE as a word in the name, incl. SCALE | CBO; last 7 days ending
that day, vs the OTHER ads in the same adset):
  spend > $125 & ROAS < 1.2
  & (cost/ATC > 1.3x others' avg (0 ATCs counts)
     OR ATC-to-purchase rate < 0.7x others' avg)

Budget hog, SCALE + TESTING + TRYBE (that day only):
  ad spend > $150 & >= 40% of adset spend & ad ROAS < 1.2 & adset ROAS < 1.4

Skips ads / adsets with OFF or RUN in the name.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from config import Config
from meta_api import API_BASE, fetch_ad_statuses
from scale_retire import _rename
from stop_loss import _is_scale_campaign, _update_ad_status

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))

TESTING_AD_RULES_ENABLED = True
SCALE_AD_RULE_ENABLED = True
HOG_ENABLED = True
STRIKES_ENABLED = True
# Every-15-min pause/restart. Off: ads are judged once a day (midnight
# strikes) instead of on noisy intra-day numbers.
INTRADAY_ENABLED = False

CAMPAIGN_KEYWORDS = ("TESTING", "TRYBE")
LOOKBACK_DAYS = 7
STRIKES_TO_RETIRE = 3
FRESH_SPEND = 30.0          # min spend on a day for it to count / be judged

CPC_SPEND = 30.0
CPC_MULT = 3.0
NO_ATC_SPEND = 60.0
ATC_SPEND = 125.0
ATC_MULT = 1.5
ATC_ROAS = 1.4

SCALE_SPEND = 125.0
SCALE_ROAS = 1.2
SCALE_CPA_MULT = 1.3
SCALE_ATC_TO_P_MULT = 0.7

HOG_SPEND = 150.0
HOG_SHARE = 0.4
HOG_AD_ROAS = 1.2
HOG_ADSET_ROAS = 1.4


@dataclass
class TestingAdAction:
    ad_id: str
    ad_name: str
    adset_name: str
    campaign_name: str
    spend_7d: float
    roas_7d: float
    action: str  # would_pause | paused | would_activate | activated | would_retire | retired | paused (rename failed) | failed
    reason: str


def _matches(campaign_name: str) -> bool:
    name = campaign_name.upper()
    return any(k in name for k in CAMPAIGN_KEYWORDS)


def _today() -> date:
    return datetime.now(AEST).date()


# ---------------------------------------------------------------- fetching

def _fetch_daily(config: Config, keyword: str, matcher, group: str, since: date, until: date) -> list[dict]:
    url = f"{API_BASE}/{config.meta_ad_account_id}/insights"
    params = {
        "access_token": config.meta_access_token,
        "level": "ad",
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend,actions,action_values",
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "time_increment": 1,
        "limit": 500,
        "filtering": (
            '[{"field":"impressions","operator":"GREATER_THAN","value":"0"},'
            '{"field":"campaign.name","operator":"CONTAIN","value":"' + keyword + '"}]'
        ),
    }
    rows: list[dict] = []
    first = True
    while url:
        resp = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params if first else None, timeout=120)
                transient_400 = False
                if resp.status_code == 400:
                    try:
                        err = resp.json().get("error", {})
                        transient_400 = err.get("is_transient") is True or err.get("code") in (1, 2, 4, 17, 32)
                    except ValueError:
                        pass
                if (resp.status_code in (403, 500, 502, 503, 504) or transient_400) and attempt < 4:
                    wait = [30, 60, 120, 240][attempt]
                    logger.warning(f"Ad rules fetch {resp.status_code}, retrying in {wait}s: {resp.text[:200]}")
                    time.sleep(wait)
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < 4:
                    time.sleep([30, 60, 120, 240][attempt])
                else:
                    raise
        if not resp.ok:
            raise requests.exceptions.HTTPError(f"Meta {resp.status_code}: {resp.text[:400]}", response=resp)

        data = resp.json()
        for row in data.get("data", []):
            if not matcher(row.get("campaign_name", "")):
                continue
            revenue = 0.0
            atcs = clicks = purchases = 0
            for av in row.get("action_values", []) or []:
                if av.get("action_type") == "purchase":
                    revenue = float(av.get("value", 0))
            for a in row.get("actions", []) or []:
                t = a.get("action_type")
                if t == "add_to_cart":
                    atcs = int(float(a.get("value", 0)))
                elif t == "link_click":
                    clicks = int(float(a.get("value", 0)))
                elif t == "purchase":
                    purchases = int(float(a.get("value", 0)))
            rows.append({
                "date": date.fromisoformat(row["date_start"]),
                "ad_id": row["ad_id"],
                "ad_name": row.get("ad_name", "Unknown"),
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", "Unknown"),
                "campaign_id": row.get("campaign_id", ""),
                "campaign_name": row.get("campaign_name", "Unknown"),
                "group": group,
                "spend": float(row.get("spend", 0)),
                "revenue": revenue,
                "atcs": atcs,
                "clicks": clicks,
                "purchases": purchases,
            })
        url = data.get("paging", {}).get("next")
        first = False
    return rows


def _fetch_rows(config: Config, since: date, until: date) -> list[dict]:
    """Daily ad rows for TESTING/TRYBE and SCALE campaigns, deduped by (ad, day)."""
    by_key: dict[tuple, dict] = {}
    # Each keyword is queried in upper and title case in case Meta's CONTAIN
    # filter is case-sensitive.
    if TESTING_AD_RULES_ENABLED:
        for kw in sorted({v for k in CAMPAIGN_KEYWORDS for v in (k, k.title())}):
            for r in _fetch_daily(config, kw, _matches, "testing", since, until):
                by_key[(r["ad_id"], r["date"])] = r
    if SCALE_AD_RULE_ENABLED or HOG_ENABLED:
        # Testing/Trybe campaigns keep their own rules even if they also say SCALE.
        def scale_match(n: str) -> bool:
            return _is_scale_campaign(n) and not _matches(n)
        for kw in ("SCALE", "Scale"):
            for r in _fetch_daily(config, kw, scale_match, "scale", since, until):
                by_key[(r["ad_id"], r["date"])] = r
    rows = list(by_key.values())
    logger.info(f"Ad rules: fetched {len(rows)} daily ad rows {since}..{until}")
    return rows


# -------------------------------------------------------------- evaluation

def _aggregate(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        t = out.setdefault(r[key], {"spend": 0.0, "revenue": 0.0, "atcs": 0, "clicks": 0, "purchases": 0, "meta": r})
        for f in ("spend", "revenue", "atcs", "clicks", "purchases"):
            t[f] += r[f]
    return out


def _testing_reason(ad: dict, avg_cpc: float, avg_cpa: float) -> str | None:
    spend = ad["spend"]
    roas = ad["revenue"] / spend if spend > 0 else 0
    cpc = spend / ad["clicks"] if ad["clicks"] > 0 else float("inf")
    cpa = spend / ad["atcs"] if ad["atcs"] > 0 else float("inf")
    if spend > CPC_SPEND and avg_cpc > 0 and cpc > CPC_MULT * avg_cpc:
        cpc_txt = "0 link clicks" if ad["clicks"] == 0 else f"CPC ${cpc:.2f}"
        return f"7d spend ${spend:.2f}>${CPC_SPEND:.0f} & {cpc_txt} > {CPC_MULT:g}x other ads' avg ${avg_cpc:.2f}"
    if spend > NO_ATC_SPEND and ad["atcs"] == 0:
        return f"7d spend ${spend:.2f}>${NO_ATC_SPEND:.0f} & 0 ATCs"
    if spend > ATC_SPEND and ad["purchases"] == 0:
        return f"7d spend ${spend:.2f}>${ATC_SPEND:.0f} & 0 purchases"
    if spend > ATC_SPEND and avg_cpa > 0 and cpa > ATC_MULT * avg_cpa and roas < ATC_ROAS:
        return (f"7d spend ${spend:.2f}>${ATC_SPEND:.0f} & cost/ATC ${cpa:.2f} > {ATC_MULT:g}x other ads' avg "
                f"${avg_cpa:.2f} & ROAS {roas:.2f}<{ATC_ROAS}")
    return None


def _scale_reason(ad: dict, peer_cpa: float, peer_atc_to_p: float) -> str | None:
    spend = ad["spend"]
    roas = ad["revenue"] / spend if spend > 0 else 0
    if not (spend > SCALE_SPEND and roas < SCALE_ROAS):
        return None
    cpa = spend / ad["atcs"] if ad["atcs"] > 0 else float("inf")
    atc_to_p = ad["purchases"] / ad["atcs"] if ad["atcs"] > 0 else 0.0
    base = f"7d spend ${spend:.2f}>${SCALE_SPEND:.0f} & ROAS {roas:.2f}<{SCALE_ROAS}"
    if peer_cpa > 0 and cpa > SCALE_CPA_MULT * peer_cpa:
        cpa_txt = "0 ATCs" if ad["atcs"] == 0 else f"cost/ATC ${cpa:.2f}"
        return f"{base} & {cpa_txt} > {SCALE_CPA_MULT:g}x other ads' avg ${peer_cpa:.2f}"
    if peer_atc_to_p > 0 and atc_to_p < SCALE_ATC_TO_P_MULT * peer_atc_to_p:
        return f"{base} & ATC→purchase {atc_to_p:.0%} < {SCALE_ATC_TO_P_MULT:g}x other ads' avg {peer_atc_to_p:.0%}"
    return None


def evaluate(rows: list[dict], as_of: date, for_restart: bool = False) -> dict[str, str]:
    """ad_id -> reason for every ad failing a rule on `as_of`.

    for_restart: drop the hog's 40%-share condition. A paused hog stops
    spending while the rest of its adset keeps going, so its share falls on
    its own; only real recovery (ad or adset ROAS) should bring it back.
    """
    window = [r for r in rows if as_of - timedelta(days=LOOKBACK_DAYS - 1) <= r["date"] <= as_of]
    day = [r for r in rows if r["date"] == as_of]
    ads7 = _aggregate(window, "ad_id")
    ads_day = _aggregate(day, "ad_id")
    fresh = {ad_id for ad_id, t in ads_day.items() if t["spend"] >= FRESH_SPEND}
    reasons: dict[str, str] = {}

    if TESTING_AD_RULES_ENABLED:
        camp = _aggregate([r for r in window if r["group"] == "testing"], "campaign_id")
        for ad_id, t in ads7.items():
            if t["meta"]["group"] != "testing" or ad_id not in fresh:
                continue
            # Baseline = the OTHER ads in the campaign, so a dominant ad isn't
            # compared with an average made mostly of its own numbers.
            c = camp[t["meta"]["campaign_id"]]
            peer_spend = c["spend"] - t["spend"]
            peer_clicks = c["clicks"] - t["clicks"]
            peer_atcs = c["atcs"] - t["atcs"]
            avg_cpc = peer_spend / peer_clicks if peer_clicks > 0 else 0
            avg_cpa = peer_spend / peer_atcs if peer_atcs > 0 else 0
            why = _testing_reason(t, avg_cpc, avg_cpa)
            if why:
                reasons[ad_id] = why

    if SCALE_AD_RULE_ENABLED:
        adsets = _aggregate([r for r in window if r["group"] == "scale"], "adset_id")
        for ad_id, t in ads7.items():
            if t["meta"]["group"] != "scale" or ad_id not in fresh:
                continue
            s = adsets[t["meta"]["adset_id"]]
            peer_atcs = s["atcs"] - t["atcs"]
            peer_cpa = (s["spend"] - t["spend"]) / peer_atcs if peer_atcs > 0 else 0
            peer_atc_to_p = (s["purchases"] - t["purchases"]) / peer_atcs if peer_atcs > 0 else 0
            why = _scale_reason(t, peer_cpa, peer_atc_to_p)
            if why:
                reasons[ad_id] = why

    if HOG_ENABLED:
        adsets_day = _aggregate(day, "adset_id")
        for ad_id, t in ads_day.items():
            if ad_id in reasons:
                continue
            s = adsets_day[t["meta"]["adset_id"]]
            spend = t["spend"]
            roas = t["revenue"] / spend if spend else 0
            share = spend / s["spend"] if s["spend"] else 0
            as_roas = s["revenue"] / s["spend"] if s["spend"] else 0
            share_ok = for_restart or share >= HOG_SHARE
            if spend > HOG_SPEND and share_ok and roas < HOG_AD_ROAS and as_roas < HOG_ADSET_ROAS:
                reasons[ad_id] = (f"budget hog: day spend ${spend:.2f} = {share:.0%} of adset, "
                                  f"ad ROAS {roas:.2f}<{HOG_AD_ROAS}, adset ROAS {as_roas:.2f}<{HOG_ADSET_ROAS}")
    return reasons


# ------------------------------------------------------------------ actions

def _names(info: dict, meta: dict) -> tuple[str, str, bool]:
    name = info.get("name", meta["ad_name"])
    adset_name = info.get("adset_name", meta["adset_name"])
    ok = not any(m in name.upper() for m in ("OFF", "RUN")) and not any(m in adset_name.upper() for m in ("OFF", "RUN"))
    return name, adset_name, ok


def _action(ad_id: str, totals: dict, name: str, adset_name: str, action: str, reason: str) -> TestingAdAction:
    spend = totals["spend"]
    return TestingAdAction(
        ad_id=ad_id, ad_name=name, adset_name=adset_name, campaign_name=totals["meta"]["campaign_name"],
        spend_7d=spend, roas_7d=(totals["revenue"] / spend if spend else 0),
        action=action, reason=reason,
    )


def _set_status(config: Config, ad_id: str, totals: dict, name: str, adset_name: str,
                status: str, verb: str, why: str, dry_run: bool) -> TestingAdAction:
    if dry_run:
        return _action(ad_id, totals, name, adset_name, f"would_{verb}", why)
    ok, err = _update_ad_status(config, ad_id, status)
    done = "paused" if verb == "pause" else "activated"
    return _action(ad_id, totals, name, adset_name, done if ok else "failed", why if ok else f"{why} — {verb} failed: {err}")


def run_testing_ad_rules(config: Config, dry_run: bool = False) -> list[TestingAdAction]:
    """Every 15 min: pause failing ads for the day; restart ads that recovered."""
    if not INTRADAY_ENABLED or not (TESTING_AD_RULES_ENABLED or SCALE_AD_RULE_ENABLED or HOG_ENABLED):
        return []
    today = _today()
    rows = _fetch_rows(config, today - timedelta(days=LOOKBACK_DAYS - 1), today)
    flagged = evaluate(rows, today)
    still_bad = evaluate(rows, today, for_restart=True)
    ads7 = _aggregate(rows, "ad_id")
    today_ads = _aggregate([r for r in rows if r["date"] == today], "ad_id")
    today_adsets = _aggregate([r for r in rows if r["date"] == today], "adset_id")
    recover = {a for a, t in today_ads.items() if t["spend"] >= FRESH_SPEND and a not in still_bad}
    if not (flagged or recover):
        return []

    info = fetch_ad_statuses(config, ad_ids=set(flagged) | recover)
    actions: list[TestingAdAction] = []
    for ad_id in sorted(set(flagged) | recover, key=lambda a: -ads7[a]["spend"]):
        t = ads7[ad_id]
        status = info.get(ad_id, {}).get("status")
        name, adset_name, ok = _names(info.get(ad_id, {}), t["meta"])
        if not ok:
            continue
        if ad_id in flagged and status == "ACTIVE":
            actions.append(_set_status(config, ad_id, t, name, adset_name, "PAUSED", "pause",
                                       flagged[ad_id] + " — paused for today (strike)", dry_run))
        elif ad_id in recover and status == "PAUSED":
            d = today_ads[ad_id]
            s_day = today_adsets[d["meta"]["adset_id"]]
            why = (f"no ad rule applies any more today — today: ad ${d['spend']:.2f} @ "
                   f"{(d['revenue'] / d['spend'] if d['spend'] else 0):.2f}x, adset "
                   f"{(s_day['revenue'] / s_day['spend'] if s_day['spend'] else 0):.2f}x")
            actions.append(_set_status(config, ad_id, t, name, adset_name, "ACTIVE", "activate", why, dry_run))
    return actions


def run_ad_strikes(config: Config, dry_run: bool = False) -> list[TestingAdAction]:
    """12:05am: OFF ads that failed 3 days running; restart yesterday's other strikes."""
    if not STRIKES_ENABLED:
        return []
    today = _today()
    days = [today - timedelta(days=k) for k in range(1, STRIKES_TO_RETIRE + 1)]
    rows = _fetch_rows(config, days[-1] - timedelta(days=LOOKBACK_DAYS - 1), days[0])
    flagged_by_day = [evaluate(rows, d) for d in days]
    struck = set.intersection(*(set(f) for f in flagged_by_day))
    # Restarting yesterday's strikes only makes sense when the intra-day job
    # paused them; otherwise a paused ad here was paused by hand.
    restart = (set(flagged_by_day[0]) - struck) if INTRADAY_ENABLED else set()
    if not (struck or restart):
        return []

    window_start = days[0] - timedelta(days=LOOKBACK_DAYS - 1)
    ads7 = _aggregate([r for r in rows if r["date"] >= window_start], "ad_id")
    info = fetch_ad_statuses(config, ad_ids=struck | restart)
    actions: list[TestingAdAction] = []
    for ad_id in sorted(struck | restart, key=lambda a: -ads7[a]["spend"]):
        t = ads7[ad_id]
        status = info.get(ad_id, {}).get("status")
        name, adset_name, ok = _names(info.get(ad_id, {}), t["meta"])
        if not ok or status in ("DELETED", "ARCHIVED", None):
            continue
        if ad_id in struck:
            why = f"failed {STRIKES_TO_RETIRE} days running — latest: {flagged_by_day[0][ad_id]}"
            if dry_run:
                actions.append(_action(ad_id, t, name, adset_name, "would_retire", why))
                continue
            if status == "ACTIVE":
                paused, err = _update_ad_status(config, ad_id, "PAUSED")
                if not paused:
                    actions.append(_action(ad_id, t, name, adset_name, "failed", f"{why} — pause failed: {err}"))
                    continue
            renamed, rerr = _rename(config, ad_id, f"{name} - OFF")
            if renamed:
                actions.append(_action(ad_id, t, name, adset_name, "retired", why))
            else:
                actions.append(_action(ad_id, t, name, adset_name, "paused (rename failed)",
                                       f"{why} — OFF rename failed: {rerr}"))
        elif status == "PAUSED":
            strikes = 1 + sum(1 for f in flagged_by_day[1:] if ad_id in f)
            actions.append(_set_status(config, ad_id, t, name, adset_name, "ACTIVE", "activate",
                                       f"strike {strikes}/{STRIKES_TO_RETIRE} yesterday — back on for a fresh day", dry_run))
    return actions


# ------------------------------------------------------------------ report

def send_testing_ad_rules_report(actions: list[TestingAdAction], dry_run: bool, config: Config, title: str = "Ad rules") -> bool:
    if not actions:
        return True
    mode = "DRY RUN" if dry_run else "LIVE"
    groups = (
        ("🪦 Marked OFF (3 strikes)", ("retired", "would_retire")),
        ("⏸️ Paused for today (strike)", ("paused", "would_pause")),
        ("▶️ Back on", ("activated", "would_activate")),
        ("⚠️ Needs attention", ("paused (rename failed)", "failed")),
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🧪 {title} — {len(actions)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*[{mode}]* TESTING/TRYBE + SCALE ad rules. Failing ads are paused for the day; "
            f"{STRIKES_TO_RETIRE} days failing in a row (≥${FRESH_SPEND:.0f} spend each day) → marked OFF. "
            f"Remove OFF from the name to bring one back."
        )}]},
    ]
    for heading, kinds in groups:
        group = [a for a in actions if a.action in kinds]
        if not group:
            continue
        lines = [
            f"• *{a.ad_name}* — `{a.campaign_name}` / `{a.adset_name}`\n"
            f"   7d: ${a.spend_7d:,.2f} @ {a.roas_7d:.2f}x │ _{a.reason}_"
            for a in group[:15]
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{heading}*\n" + "\n".join(lines)}})
    try:
        resp = requests.post(config.slack_webhook_url, json={"blocks": blocks}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected ad rules report: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send ad rules report: {e}")
        return False
