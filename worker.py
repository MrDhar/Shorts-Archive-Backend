"""
Shorts Archive queue worker.

Run this as a Render Background Worker.
It does not depend on the Android app remaining open.

Required:
  BACKEND_URL=https://shorts-archive-backend.onrender.com

Optional:
  WORKER_POLL_SECONDS=30
  UPLOAD_WORKER_SECRET=<same value configured on the backend>
"""
import json
import os
import time
import urllib.error
import urllib.request

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "https://shorts-archive-backend.onrender.com",
).rstrip("/")
POLL_SECONDS = max(10, int(os.getenv("WORKER_POLL_SECONDS", "30")))
WORKER_SECRET = os.getenv("UPLOAD_WORKER_SECRET", "").strip()


def request(method: str, path: str, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if WORKER_SECRET:
        headers["X-Worker-Secret"] = WORKER_SECRET

    req = urllib.request.Request(
        f"{BACKEND_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw) if raw else {}


def main():
    print(f"Shorts Archive worker started: {BACKEND_URL}", flush=True)

    while True:
        try:
            code, payload = request("GET", "/upload-queue")

            if code == 200:
                status = payload.get("status", {})
                queued = int(status.get("queued", 0))
                remaining = int(status.get("remaining_last_24h", 0))

                print(
                    f"queue={queued}, "
                    f"uploaded_last_24h={status.get('uploaded_last_24h', 0)}, "
                    f"remaining_24h={remaining}",
                    flush=True,
                )

                if queued > 0 and remaining > 0:
                    run_code, result = request("POST", "/upload-queue/run", {})
                    print(
                        f"queue run HTTP {run_code}; "
                        f"started={result.get('started')}",
                        flush=True,
                    )

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(f"Backend HTTP {exc.code}: {detail[:500]}", flush=True)
        except Exception as exc:
            print(f"Worker error: {exc!r}", flush=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
