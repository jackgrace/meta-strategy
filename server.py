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

from main import run_check, run_fatigue_only, run_auto_pause

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
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/slack/trigger":
            self._handle_slack(run_check, "Running all checks now...")
        elif self.path == "/slack/fatigue":
            self._handle_slack(run_fatigue_only, "Running fatigue check now...")
        elif self.path == "/slack/pause":
            self._handle_slack(run_auto_pause, "Running auto-pause check now...")
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

            # Step 1: try to pause
            resp = requests.post(
                f"{url}?access_token={config.meta_access_token}",
                json={"status": "PAUSED", "name": "1237673985243723 29-MAR - OFF"},
                timeout=30,
            )

            if resp.ok:
                result = {"status": "ok", "message": f"Ad {TEST_AD_ID} paused successfully", "response": resp.json()}
                logger.info(f"Test pause succeeded: {resp.json()}")
            else:
                result = {"status": "error", "code": resp.status_code, "message": resp.text[:500]}
                logger.error(f"Test pause failed: {resp.status_code} {resp.text[:500]}")

            self._respond(resp.status_code if not resp.ok else 200, result)
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
        try:
            run_check()
        except Exception as e:
            logger.error(f"Scheduled check failed: {e}")
            _send_failure_notification(str(e))

            # Retry once after 15 minutes
            logger.info("Scheduler: retrying in 15 minutes...")
            time.sleep(900)
            logger.info("Scheduler: retry attempt")
            try:
                run_check()
            except Exception as e2:
                logger.error(f"Retry also failed: {e2}")


def start_server():
    port = int(os.environ.get("PORT", 8080))

    # Start daily scheduler in background
    scheduler = threading.Thread(target=_run_daily_scheduler, daemon=True)
    scheduler.start()
    logger.info("Daily scheduler started (midnight AEST)")

    server = HTTPServer(("0.0.0.0", port), TriggerHandler)
    logger.info(f"Server listening on port {port}")
    logger.info(f"  GET  /run            — run all checks")
    logger.info(f"  GET  /fatigue        — fatigue check only")
    logger.info(f"  GET  /pause          — auto-pause dry run")
    logger.info(f"  POST /slack/trigger  — Slack: all checks")
    logger.info(f"  POST /slack/fatigue  — Slack: fatigue only")
    logger.info(f"  POST /slack/pause    — Slack: auto-pause")
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
