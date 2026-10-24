#!/usr/bin/env python3
"""
Web server for the Auto CPU Scheduling Analyzer.

    python server.py            -> opens http://127.0.0.1:8000 in your browser
    python server.py --port 9000 --no-browser

Only the Python standard library + psutil are used (no Flask needed).
Routes:
    GET /                 the dashboard (static/index.html)
    GET /api/analyze?n=12 runs a fresh measurement + simulation, returns JSON
    GET /api/health       simple liveness check
"""
import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import os_scheduler_analyzer as core

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
LOCK = threading.Lock()          # only one measurement at a time (keeps timings clean)


class Handler(BaseHTTPRequestHandler):
    server_version = "SchedAnalyzer/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/health":
            return self._send(200, json.dumps({"status": "ok"}))
        if url.path == "/api/analyze":
            try:
                n = int(parse_qs(url.query).get("n", ["12"])[0])
                n = max(4, min(n, 30))
            except ValueError:
                n = 12
            try:
                with LOCK:
                    data = core.run_analysis(n)
                return self._send(200, json.dumps(data))
            except SystemExit as e:                    # core exits with a message if too few processes
                return self._send(500, json.dumps({"error": str(e)}))
            except Exception as e:                     # never crash the server
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):                 # quieter console
        sys.stderr.write("[server] " + fmt % args + "\n")


def main():
    ap = argparse.ArgumentParser(description="Auto CPU Scheduling Analyzer - web dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}"
    print(f"\n  Auto CPU Scheduling Analyzer is running at {url}\n  Press Ctrl+C to stop.\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
