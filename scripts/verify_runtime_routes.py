import json
import os
import sys

import httpx


def main() -> int:
    base_url = os.getenv("VERIFY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    print(f"Checking runtime at {base_url}")
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        try:
            health = client.get("/health")
        except Exception as exc:
            print(f"[FAIL] Could not connect to service: {exc}")
            return 1

        print(f"[INFO] /health status={health.status_code}")
        if health.status_code != 200:
            print("[FAIL] /health is not healthy.")
            return 1

        version = client.get("/version")
        print(f"[INFO] /version status={version.status_code}")
        if version.status_code == 200:
            print(f"[OK] version payload: {json.dumps(version.json(), sort_keys=True)}")
        else:
            print("[WARN] /version missing. You may be running an older server process.")

        ui = client.get("/ui")
        app_version_header = ui.headers.get("X-DFS-App-Version")
        print(f"[INFO] /ui status={ui.status_code} X-DFS-App-Version={app_version_header}")
        if ui.status_code == 200:
            print("[OK] /ui is available.")
            return 0

        if ui.status_code == 404:
            print("[FAIL] /ui returned 404. Likely runtime mismatch.")
            print("[HINT] Stop running servers, `git pull`, then restart uvicorn from repo root.")
            return 2

        print(f"[FAIL] /ui unexpected status: {ui.status_code}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
