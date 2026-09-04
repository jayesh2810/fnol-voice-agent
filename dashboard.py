"""Tiny local web server for the FNOL review dashboard.

No dependencies beyond the Python standard library. Serves:

  /                    the dashboard page (dashboard.html)
  /api/live            the call in progress (claims/live.json), updated by the agent
  /api/claims          list of saved calls, newest first
  /api/claims/<file>   one saved call

Run:  python dashboard.py            then open http://localhost:8787
      python dashboard.py --port 9000
"""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAIMS_DIR = HERE / "claims"
PAGE = HERE / "dashboard.html"


def list_claims() -> list[dict]:
    rows = []
    for path in sorted(CLAIMS_DIR.glob("*.json"), reverse=True):
        if path.name == "live.json":
            continue
        try:
            d = json.loads(path.read_text())
        except Exception:
            continue
        rows.append({
            "file": path.name,
            "started_at": d.get("started_at"),
            "status": d.get("status"),
            "claim_number": d.get("claim_number"),
            "holder": (d.get("policy") or {}).get("holder_name"),
            "score": (d.get("risk") or {}).get("score"),
            "level": (d.get("risk") or {}).get("level"),
        })
    return rows


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/live":
            live = CLAIMS_DIR / "live.json"
            body = live.read_bytes() if live.exists() else b"{}"
            return self._send(body, "application/json")
        if self.path == "/api/claims":
            return self._send(json.dumps(list_claims()).encode(), "application/json")
        if self.path.startswith("/api/claims/"):
            name = self.path.split("/api/claims/", 1)[1]
            target = (CLAIMS_DIR / name).resolve()
            if target.parent != CLAIMS_DIR.resolve() or not target.exists() or name == "live.json":
                return self.send_error(404)
            return self._send(target.read_bytes(), "application/json")
        return self.send_error(404)

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # keep the terminal quiet; polling is noisy
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    CLAIMS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"FNOL dashboard: http://localhost:{args.port}   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
