"""
Google Ads change log: who changed what in the last N days (max 30, Google's
limit), grouped by person, posted to Slack. Read only.

Env: GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN,
GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CUSTOMER_ID, optional
GOOGLE_ADS_LOGIN_CUSTOMER_ID (defaults to the customer ID).
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

ADS_API_VERSION = "v23"
AEST = timezone(timedelta(hours=10))
MAX_PER_PERSON = 25

CLIENT_LABELS = {
    "GOOGLE_ADS_WEB_CLIENT": "web",
    "GOOGLE_ADS_MOBILE_APP": "app",
    "GOOGLE_ADS_EDITOR": "Ads Editor",
    "GOOGLE_ADS_API": "API",
    "GOOGLE_ADS_SCRIPTS": "script",
    "GOOGLE_ADS_AUTOMATED_RULE": "automated rule",
    "GOOGLE_ADS_BULK_UPLOAD": "bulk upload",
    "GOOGLE_ADS_RECOMMENDATIONS": "recommendation",
    "GOOGLE_ADS_RECOMMENDATIONS_SUBSCRIPTION": "auto-applied recommendation",
    "SEARCH_ADS_360_SYNC": "SA360",
    "SEARCH_ADS_360_POST": "SA360",
}


FIELD_LABELS = {
    "amountMicros": "budget",
    "targetRoas": "target ROAS",
    "targetCpaMicros": "target CPA",
    "cpcBidMicros": "max CPC",
    "status": "status",
    "name": "name",
    "biddingStrategyType": "bid strategy",
    "finalUrls": "final URL",
}


class GoogleAdsError(RuntimeError):
    pass


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise GoogleAdsError(f"Missing Railway variable {name}")
    return v


def _customer_id() -> str:
    return _env("GOOGLE_ADS_CUSTOMER_ID").replace("-", "")


def _access_token() -> str:
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": _env("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": _env("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": _env("GOOGLE_ADS_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    }, timeout=30)
    if not resp.ok:
        raise GoogleAdsError(f"Google sign-in failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]


def search(query: str, token: str | None = None) -> list[dict]:
    """Run a GAQL query via searchStream; returns the flattened result rows."""
    cid = _customer_id()
    headers = {
        "Authorization": f"Bearer {token or _access_token()}",
        "developer-token": _env("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "login-customer-id": os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", cid).replace("-", "").strip() or cid,
        "Content-Type": "application/json",
    }
    url = f"https://googleads.googleapis.com/{ADS_API_VERSION}/customers/{cid}/googleAds:searchStream"
    resp = requests.post(url, headers=headers, json={"query": query}, timeout=120)
    if not resp.ok:
        raise GoogleAdsError(f"Google Ads API {resp.status_code}: {resp.text[:500]}")
    rows: list[dict] = []
    for batch in resp.json():
        rows.extend(batch.get("results", []))
    return rows


def _get(obj: dict, path: str):
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _fmt(field: str, value) -> str:
    if value is None:
        return "—"
    leaf = field.split(".")[-1]
    if leaf.endswith("Micros"):
        try:
            return f"${int(value) / 1_000_000:,.2f}"
        except (TypeError, ValueError):
            pass
    if leaf == "targetRoas":
        try:
            return f"{float(value) * 100:.0f}%"
        except (TypeError, ValueError):
            pass
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _describe(ev: dict) -> str:
    rtype = ev.get("changeResourceType", "").replace("_", " ").lower()
    op = ev.get("resourceChangeOperation", "")
    old_r = ev.get("oldResource") or {}
    new_r = ev.get("newResource") or {}
    key = next(iter(new_r or old_r), None)
    old_v, new_v = old_r.get(key, {}) if key else {}, new_r.get(key, {}) if key else {}
    if op == "CREATE":
        name = (_get(new_v, "name") or _get(new_v, "ad.name") or _get(new_v, "keyword.text")
                or _get(new_v, "asset.name") or "")
        return f"created {rtype}" + (f" “{name}”" if name else "")
    if op == "REMOVE":
        return f"removed {rtype}"
    fields = [f for f in (ev.get("changedFields") or "").split(",") if f]
    parts = [f"{FIELD_LABELS.get(f.split('.')[-1], f)} {_fmt(f, _get(old_v, f))} → {_fmt(f, _get(new_v, f))}"
             for f in fields[:4]]
    if len(fields) > 4:
        parts.append(f"+{len(fields) - 4} more fields")
    return f"{rtype}: " + ("; ".join(parts) if parts else "updated")


def fetch_changes(days: int = 7) -> list[dict]:
    days = max(1, min(days, 30))
    token = _access_token()
    today = datetime.now(AEST).date()
    since = today - timedelta(days=days - 1)
    events = search(
        "SELECT change_event.change_date_time, change_event.user_email, change_event.client_type, "
        "change_event.change_resource_type, change_event.resource_change_operation, "
        "change_event.changed_fields, change_event.old_resource, change_event.new_resource, "
        "change_event.campaign, change_event.ad_group "
        "FROM change_event "
        f"WHERE change_event.change_date_time >= '{since} 00:00:00' "
        f"AND change_event.change_date_time <= '{today} 23:59:59' "
        "ORDER BY change_event.change_date_time DESC LIMIT 10000",
        token,
    )
    campaigns = {r["campaign"]["resourceName"]: r["campaign"].get("name", "")
                 for r in search("SELECT campaign.resource_name, campaign.name FROM campaign", token)}
    ad_groups = {r["adGroup"]["resourceName"]: r["adGroup"].get("name", "")
                 for r in search("SELECT ad_group.resource_name, ad_group.name FROM ad_group", token)}

    changes = []
    for row in events:
        ev = row.get("changeEvent", {})
        changes.append({
            "time": ev.get("changeDateTime", ""),
            "user": ev.get("userEmail") or "(unknown)",
            "via": CLIENT_LABELS.get(ev.get("clientType", ""), ev.get("clientType", "").lower()),
            "campaign": campaigns.get(ev.get("campaign", ""), ""),
            "ad_group": ad_groups.get(ev.get("adGroup", ""), ""),
            "change": _describe(ev),
        })
    logger.info(f"Google change log: {len(changes)} changes in the last {days} days")
    return changes


def send_change_log(changes: list[dict], days: int, slack_webhook_url: str) -> bool:
    by_user: dict[str, list[dict]] = defaultdict(list)
    for c in changes:
        by_user[c["user"]].append(c)
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"📝 Google Ads change log — last {days} days ({len(changes)} changes)"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Grouped by person, newest first. Times are the account's time zone. Read only."}]},
    ]
    if not changes:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No changes in this period._"}})
    for user, items in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
        lines = []
        for c in items[:MAX_PER_PERSON]:
            where = " / ".join(x for x in (c["campaign"], c["ad_group"]) if x)
            lines.append(f"• `{c['time'][:16]}` {('*' + where + '* — ') if where else ''}{c['change']} _({c['via']})_")
        if len(items) > MAX_PER_PERSON:
            lines.append(f"_+{len(items) - MAX_PER_PERSON} more_")
        text = f"*{user}* — {len(items)} changes\n"
        chunk = text
        for line in lines:  # Slack sections are capped at 3000 chars
            if len(chunk) + len(line) + 1 > 2900:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
                chunk = ""
            chunk += line + "\n"
        if chunk:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    try:
        resp = requests.post(slack_webhook_url, json={"blocks": blocks[:50]}, timeout=10)
        if not resp.ok:
            logger.error(f"Slack rejected change log: {resp.status_code} — {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.error(f"Failed to send change log: {e}")
        return False
