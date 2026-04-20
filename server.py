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

from main import run_check

logger = logging.getLogger(__name__)

# AEST = UTC+10
AEST = timezone(timedelta(hours=10))


class TriggerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "time": datetime.now(AEST).isoformat()})
        elif self.path == "/run":
            self._run_check(source="http")
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/slack/trigger":
            self._handle_slack()
        elif self.path == "/run":
            self._run_check(source="http")
        else:
            self._respond(404, {"error": "not found"})

    def _handle_slack(self):
        # Slack slash commands send application/x-www-form-urlencoded
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode()
        params = parse_qs(body)

        # Verify token if configured (optional but recommended)
        expected_token = os.environ.get("SLACK_SLASH_TOKEN")
        if expected_token:
            received_token = params.get("token", [""])[0]
            if received_token != expected_token:
                self._respond(403, {"error": "invalid token"})
                return

        # Respond immediately (Slack requires <3s response)
        self._respond_text(200, "Running checks now... results will be posted shortly.")

        # Run the check in a background thread
        threading.Thread(target=self._run_check_async, args=("slack",), daemon=True).start()

    def _run_check(self, source: str):
        logger.info(f"Manual trigger via {source}")
        try:
            result = run_check()
            self._respond(200, result)
        except Exception as e:
            logger.error(f"Check failed: {e}")
            self._respond(500, {"status": "error", "message": str(e)})

    def _run_check_async(self, source: str):
        logger.info(f"Async trigger via {source}")
        try:
            run_check()
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


def _run_daily_scheduler():
    """Background thread: runs both reports daily at 1am AEST."""
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


def start_server():
    port = int(os.environ.get("PORT", 8080))

    # Start daily scheduler in background
    scheduler = threading.Thread(target=_run_daily_scheduler, daemon=True)
    scheduler.start()
    logger.info("Daily scheduler started (midnight AEST)")

    server = HTTPServer(("0.0.0.0", port), TriggerHandler)
    logger.info(f"Server listening on port {port}")
    logger.info(f"  GET  /run           — manual trigger")
    logger.info(f"  POST /slack/trigger  — Slack slash command")
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
