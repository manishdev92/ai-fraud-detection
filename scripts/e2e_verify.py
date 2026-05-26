#!/usr/bin/env python3
"""Verify Option 1 end-to-end flow (run while API is on :8000)."""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    try:
        print("1. Health:", call("GET", "/health"))
        print("2. Generate:", call("POST", "/generate-transactions", {"count": 300, "seed": 42}))
        inv = call("POST", "/run-fraud-investigation", {"lookback_hours": 336})
        print("3. Investigate:", {k: inv[k] for k in ("investigation_id", "findings_count", "orchestration")})
        fid = inv["investigation_id"]
        findings = call("GET", f"/findings?investigation_id={fid}&limit=3")
        print("4. Findings sample:", findings["findings"][0]["rule_name"] if findings["findings"] else "none")
        report = call("GET", "/reports/latest")
        print("5. Report:", report["title"][:60], "...")
        print("\nE2E PASSED")
        return 0
    except urllib.error.URLError as exc:
        print(f"ERROR: Is the API running? uvicorn app.main:app --port 8000\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
