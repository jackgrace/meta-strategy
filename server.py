"""
Lightweight HTTP server for manual triggers.
No extra dependencies — uses stdlib http.server + threading for scheduled runs.

Endpoints:
  GET  /run            — trigger fatigue check manually (browser/curl)
  POST /slack/trigger  — Slack slash command endpoint
  GET  /health         — health check for Railway
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

import requests

from main import (
    run_check,
    run_fatigue_only,
    run_auto_pause,
    run_stop_loss,
    run_testing_kill,
    run_midnight_restart,
    run_ad_kill_3d,
)

logger = logging.getLogger(__name__)

# AEST = UTC+10
AEST = timezone(timedelta(hours=10))


class TriggerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "time": datetime.now(AEST).isoformat()})
        elif self.path == "/run":
            self._run_check(source="http")
        elif self.path == "/fatigue":
            self._run_fatigue(source="http")
        elif self.path == "/pause":
            self._run_pause(source="http")
        elif self.path == "/pause/test":
            self._test_pause(source="http")
        elif self.path.startswith("/activate"):
            self._manual_activate()
        elif self.path == "/stoploss":
            self._run_stop_loss_endpoint(source="http")
        elif self.path == "/testing-kill":
            self._run_testing_kill_endpoint(source="http")
        elif self.path == "/midnight-restart":
            self._run_midnight_restart_endpoint(source="http")
        elif self.path == "/ad-kill-3d":
            self._run_ad_kill_3d_endpoint(source="http")
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/slack/trigger":
            self._handle_slack(run_check, "Running all checks now...")
        elif self.path == "/slack/fatigue":
            self._handle_slack(run_fatigue_only, "Running fatigue check now...")
        elif self.path == "/slack/pause":
            self._handle_slack(run_auto_pause, "Running auto-pause check now...")
        elif self.path == "/slack/stoploss":
            self._handle_slack(run_stop_loss, "Running stop-loss check now...")
        elif self.path == "/slack/testing-kill":
            self._handle_slack(run_testing_kill, "Running testing kill check now...")
        elif self.path == "/slack/midnight-restart":
            self._handle_slack(run_midnight_restart, "Running midnight restart now...")
        elif self.path == "/slack/ad-kill-3d":
            self._handle_slack(run_ad_kill_3d, "Running ad 3d hard-kill now...")
        elif self.path == "/run":
            self._run_check(source="http")
        elif self.path == "/fatigue":
            self._run_fatigue(source="http")
        else:
            self._respond(404, {"error": "not found"})

    def _handle_slack(self, check_fn, ack_message: str):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode()
        params = parse_qs(body)

        expected_token = os.environ.get("SLACK_SLASH_TOKEN")
        if expected_token:
            received_token = params.get("token", [""])[0]
            if received_token != expected_token:
                self._respond(403, {"error": "invalid token"})
                return

        self._respond_text(200, ack_message)
        threading.Thread(target=self._run_async, args=(check_fn, "slack"), daemon=True).start()

    def _run_check(self, source: str):
        logger.info(f"Manual trigger via {source} — all checks")
        try:
            result = run_check()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Check failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_fatigue(self, source: str):
        logger.info(f"Manual trigger via {source} — fatigue only")
        try:
            result = run_fatigue_only()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Fatigue check failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_pause(self, source: str):
        logger.info(f"Manual trigger via {source} — auto-pause")
        try:
            result = run_auto_pause()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Auto-pause check failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_stop_loss_endpoint(self, source: str):
        logger.info(f"Manual trigger via {source} — stop-loss")
        try:
            result = run_stop_loss()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Stop-loss check failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_testing_kill_endpoint(self, source: str):
        logger.info(f"Manual trigger via {source} — testing kill")
        try:
            result = run_testing_kill()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Testing kill check failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_midnight_restart_endpoint(self, source: str):
        logger.info(f"Manual trigger via {source} — midnight restart")
        try:
            result = run_midnight_restart()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Midnight restart failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_ad_kill_3d_endpoint(self, source: str):
        logger.info(f"Manual trigger via {source} — ad kill 3d")
        try:
            result = run_ad_kill_3d()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Ad kill 3d failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _manual_activate(self):
        """Manually turn ON an ad or adset by ID: /activate?id=<meta_id>"""
        from urllib.parse import urlparse, parse_qs
        from meta_api import API_BASE
        from config import Config

        qs = parse_qs(urlparse(self.path).query)
        obj_id = (qs.get("id") or [""])[0].strip()

        if not obj_id:
            self._respond(400, {"error": "missing ?id=<meta_id> query parameter"})
            return

        logger.info(f"Manual activate request for {obj_id}")
        try:
            config = Config.from_env()
            resp = requests.post(
                f"{API_BASE}/{obj_id}?access_token={config.meta_access_token}",
                data={"status": "ACTIVE"},
                timeout=60,
            )
            if resp.ok:
                logger.info(f"Manually activated {obj_id}")
                self._respond(200, {"status": "ok", "id": obj_id, "action": "activated"})
            else:
                try:
                    err = resp.json().get("error", {})
                    reason = err.get("error_user_title", err.get("message", "Unknown"))
                except Exception:
                    reason = f"HTTP {resp.status_code}"
                logger.error(f"Failed to activate {obj_id}: {reason}")
                self._respond(resp.status_code, {"status": "error", "id": obj_id, "reason": reason, "body": resp.text[:500]})
        except Exception as e:
            logger.error(f"Manual activate error: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _test_pause(self, source: str):
        """Test write permissions by pausing a single known ad."""
        import os
        from meta_api import API_BASE
        from config import Config

        logger.info(f"Testing pause write permission via {source}")
        TEST_AD_ID = "1237673985243723"

        try:
            config = Config.from_env()
            url = f"{API_BASE}/{TEST_AD_ID}"

            # Check token permissions
            perm_resp = requests.get(
                f"{API_BASE}/me/permissions?access_token={config.meta_access_token}",
                timeout=30,
            )
            permissions = {}
            if perm_resp.ok:
                for p in perm_resp.json().get("data", []):
                    permissions[p["permission"]] = p["status"]

            has_ads_management = permissions.get("ads_management") == "granted"
            has_ads_read = permissions.get("ads_read") == "granted"

            result = {
                "has_ads_read": has_ads_read,
                "has_ads_management": has_ads_management,
                "all_permissions": permissions,
                "verdict": "Token has write access — ready to go" if has_ads_management else "MISSING ads_management — regenerate token with this permission",
            }

            self._respond(200, result)
        except Exception as e:
            logger.error(f"Test pause error: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_async(self, check_fn, source: str):
        logger.info(f"Async trigger via {source}")
        try:
            check_fn()
        except Exception as e:
            logger.error(f"Async check failed: {e}")

    def _respond(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _respond_text(self, status: int, text: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")


def _send_failure_notification(error_msg: str):
    """Send a Slack message when the daily run fails."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        now = datetime.now(AEST).strftime("%a %d %b %Y %H:%M AEST")
        requests.post(webhook, json={
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"❌ Daily check failed — {now}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{error_msg[:500]}```\nWill retry in 15 minutes."}},
            ]
        }, timeout=10)
    except Exception:
        pass


def _run_daily_scheduler():
    """Background thread: runs both reports daily at 1am AEST. Retries once after 15 min on failure."""
    while True:
        now = datetime.now(AEST)
        # Next 1am AEST
        next_run = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        logger.info(f"Scheduler: next run at {next_run.isoformat()} ({wait_seconds:.0f}s from now)")
        time.sleep(wait_seconds)

        logger.info("Scheduler: running daily checks")

        # Auto-pause (with a single 15-min retry on failure)
        try:
            run_check()
        except Exception as e:
            logger.error(f"Scheduled auto-pause failed: {e}")
            _send_failure_notification(str(e))
            logger.info("Scheduler: retrying auto-pause in 15 minutes...")
            time.sleep(900)
            try:
                run_check()
            except Exception as e2:
                logger.error(f"Auto-pause retry also failed: {e2}")

        # 3d hard-kill for CC/SCALE/VALUE ads — DISABLED.
        # Module + /ad-kill-3d endpoint remain for manual triggering, but
        # nothing fires automatically. Uncomment to re-enable.
        # try:
        #     logger.info("Scheduler: running daily ad 3d hard-kill")
        #     run_ad_kill_3d()
        # except Exception as e:
        #     logger.error(f"Scheduled ad-kill-3d failed: {e}")
        #     _send_failure_notification(f"ad-kill-3d: {e}")


def _send_stop_loss_failure(error_msg: str):
    """Notify Slack when a stop-loss run fails."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        now = datetime.now(AEST).strftime("%d %b %H:%M AEST")
        requests.post(webhook, json={
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"❌ Stop-loss run failed — {now}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{error_msg[:500]}```\nWill retry in 15 minutes."}},
            ]
        }, timeout=10)
    except Exception:
        pass


def _run_stop_loss_scheduler():
    """Background thread: runs the stop-loss check every 15 minutes. Forever."""
    time.sleep(30)  # let server come up first
    consecutive_failures = 0
    while True:
        try:
            logger.info("Scheduler: running 15-min stop-loss check")
            run_stop_loss()
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Stop-loss check failed (consecutive: {consecutive_failures}): {e}")
            # Notify on every failure so you know immediately
            _send_stop_loss_failure(f"{type(e).__name__}: {e}")
        time.sleep(15 * 60)


def _run_testing_kill_scheduler():
    """Background thread: runs the testing kill check every 1 hour. Forever."""
    time.sleep(60)  # let server come up first, stagger from stop-loss
    consecutive_failures = 0
    while True:
        try:
            logger.info("Scheduler: running hourly testing-kill check")
            run_testing_kill()
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Testing-kill check failed (consecutive: {consecutive_failures}): {e}")
            _send_stop_loss_failure(f"testing-kill: {type(e).__name__}: {e}")
        time.sleep(60 * 60)


def _run_midnight_restart_scheduler():
    """Background thread: runs the midnight adset restart at 12:05am AEST."""
    while True:
        now = datetime.now(AEST)
        # Next 12:05am AEST
        next_run = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        logger.info(f"Midnight-restart scheduler: next run at {next_run.isoformat()} ({wait_seconds:.0f}s from now)")
        time.sleep(wait_seconds)

        try:
            logger.info("Scheduler: running 12:05am AEST midnight restart")
            run_midnight_restart()
        except Exception as e:
            logger.error(f"Midnight restart failed: {e}")
            _send_stop_loss_failure(f"midnight-restart: {type(e).__name__}: {e}")


def start_server():
    port = int(os.environ.get("PORT", 8080))

    # Start daily scheduler in background
    scheduler = threading.Thread(target=_run_daily_scheduler, daemon=True)
    scheduler.start()
    logger.info("Daily scheduler started (1am AEST)")

    # Start 15-min stop-loss scheduler
    stop_loss_thread = threading.Thread(target=_run_stop_loss_scheduler, daemon=True)
    stop_loss_thread.start()
    logger.info("Stop-loss scheduler started (every 15 min)")

    # Start hourly testing-kill scheduler
    testing_kill_thread = threading.Thread(target=_run_testing_kill_scheduler, daemon=True)
    testing_kill_thread.start()
    logger.info("Testing-kill scheduler started (every 1 hour)")

    # Start 12:05am midnight adset restart scheduler
    midnight_thread = threading.Thread(target=_run_midnight_restart_scheduler, daemon=True)
    midnight_thread.start()
    logger.info("Midnight-restart scheduler started (12:05am AEST daily)")

    server = HTTPServer(("0.0.0.0", port), TriggerHandler)
    logger.info(f"Server listening on port {port}")
    logger.info(f"  GET  /run            — run all checks")
    logger.info(f"  GET  /fatigue        — fatigue check only")
    logger.info(f"  GET  /pause          — auto-pause dry run")
    logger.info(f"  GET  /stoploss       — intra-day stop-loss")
    logger.info(f"  GET  /testing-kill   — testing campaign kill")
    logger.info(f"  GET  /midnight-restart — reactivate qualifying adsets")
    logger.info(f"  GET  /ad-kill-3d     — 3d hard-kill for CC/SCALE/VALUE ads")
    logger.info(f"  GET  /activate?id=X  — manually reactivate an ad/adset")
    logger.info(f"  POST /slack/trigger  — Slack: all checks")
    logger.info(f"  POST /slack/fatigue  — Slack: fatigue only")
    logger.info(f"  POST /slack/pause    — Slack: auto-pause")
    logger.info(f"  POST /slack/stoploss — Slack: stop-loss")
    logger.info(f"  POST /slack/testing-kill — Slack: testing kill")
    logger.info(f"  POST /slack/midnight-restart — Slack: midnight restart")
    logger.info(f"  POST /slack/ad-kill-3d — Slack: 3d hard-kill")
    logger.info(f"  GET  /health         — health check")
    server.serve_forever()


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    start_server()
