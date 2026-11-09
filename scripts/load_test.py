"""Load test for the upload lifecycle (and optionally download).

Every run writes a self-describing JSON summary: git SHA, timestamp, platform, server
version, all arguments, throughput, chunk latency percentiles, retries, and errors.

429 and 503 responses are retried after their Retry-After delay (capped). Any other
failure is counted, marks that file failed, and the run continues.

Fixed workloads (use --workload so runs stay comparable over time):
  W1 throughput:        20 files of 64 MiB, 8 MiB chunks, 8 files x 4 chunk workers, download
  W2 spec concurrency: 100 files of 16 MiB, 5 MiB chunks, 100 files x 2 chunk workers
  W3 large file:         1 file of 1 GiB, 8 MiB chunks, 8 chunk workers, download

Example:
  python scripts/load_test.py --base-url http://127.0.0.1:8001 --workload W1 --label baseline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

MIB = 1024 * 1024
REQUEST_TIMEOUT_S = 120.0
RETRYABLE_STATUSES = {429, 503}

PROFILE_PRESETS = {
    "fast": {"concurrent_files": 2, "per_file_chunk_workers": 2},
    "balanced": {"concurrent_files": 3, "per_file_chunk_workers": 4},
    "max-throughput": {"concurrent_files": 4, "per_file_chunk_workers": 6},
}

WORKLOADS = {
    "W1": {
        "files": 20,
        "file_size_bytes": 64 * MIB,
        "chunk_size_bytes": 8 * MIB,
        "concurrent_files": 8,
        "per_file_chunk_workers": 4,
        "download": True,
    },
    "W2": {
        "files": 100,
        "file_size_bytes": 16 * MIB,
        "chunk_size_bytes": 5 * MIB,
        "concurrent_files": 100,
        "per_file_chunk_workers": 2,
        "download": False,
    },
    "W3": {
        "files": 1,
        "file_size_bytes": 1024 * MIB,
        "chunk_size_bytes": 8 * MIB,
        "concurrent_files": 1,
        "per_file_chunk_workers": 8,
        "download": True,
    },
}


@dataclass
class RunStats:
    retries: int = 0
    errors_by_status: Counter = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def add_error(self, key: str) -> None:
        with self._lock:
            self.errors_by_status[key] += 1


class FileFailed(Exception):
    pass


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _retry_after_seconds(response: httpx.Response, cap_s: float) -> float:
    try:
        value = float(response.headers.get("Retry-After", "1"))
    except ValueError:
        value = 1.0
    return min(max(value, 0.0), cap_s)


def send_with_retry(
    send: Callable[[], httpx.Response],
    stats: RunStats,
    max_attempts: int = 10,
    max_wait_s: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Send, retrying 429/503 after Retry-After. Counts retries and final error statuses."""
    attempt = 1
    while True:
        response = send()
        if response.status_code not in RETRYABLE_STATUSES or attempt >= max_attempts:
            break
        stats.add_retry()
        sleep(_retry_after_seconds(response, max_wait_s))
        attempt += 1
    if response.status_code >= 400:
        stats.add_error(str(response.status_code))
    return response


def _call(stats: RunStats, send: Callable[[], httpx.Response]) -> httpx.Response:
    try:
        response = send_with_retry(send, stats)
    except httpx.TransportError as exc:
        stats.add_error(f"transport:{type(exc).__name__}")
        raise FileFailed(f"{type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise FileFailed(f"HTTP {response.status_code}: {response.text[:200]}")
    return response


def _build_payload(size_bytes: int) -> bytes:
    pattern = b"dfs-load-test-"
    repeats = math.ceil(size_bytes / len(pattern))
    return (pattern * repeats)[:size_bytes]


def upload_one_file(
    client: httpx.Client,
    base_url: str,
    file_name: str,
    payload: bytes,
    chunk_size: int,
    per_file_chunk_workers: int,
    headers: dict[str, str],
    stats: RunStats,
) -> dict:
    started = time.perf_counter()
    latencies_ms: list[float] = []
    result: dict = {"file_name": file_name, "file_bytes": len(payload), "upload_id": None, "failed": False, "error": None}
    view = memoryview(payload)
    try:
        init = _call(
            stats,
            lambda: client.post(
                f"{base_url}/v1/uploads/init",
                json={"file_name": file_name, "file_size": len(payload), "chunk_size": chunk_size},
                headers=headers,
            ),
        )
        upload_id = init.json()["upload_id"]
        result["upload_id"] = upload_id

        def put_chunk(index: int, offset: int) -> None:
            body = bytes(view[offset : offset + chunk_size])
            t0 = time.perf_counter()
            _call(stats, lambda: client.put(f"{base_url}/v1/uploads/{upload_id}/chunks/{index}", content=body, headers=headers))
            latencies_ms.append((time.perf_counter() - t0) * 1000)

        with ThreadPoolExecutor(max_workers=per_file_chunk_workers) as pool:
            futures = [pool.submit(put_chunk, i, off) for i, off in enumerate(range(0, len(payload), chunk_size))]
            for future in as_completed(futures):
                future.result()

        _call(stats, lambda: client.post(f"{base_url}/v1/uploads/{upload_id}/complete", headers=headers))
    except FileFailed as exc:
        result["failed"] = True
        result["error"] = str(exc)
    result["chunk_count"] = math.ceil(len(payload) / chunk_size)
    result["chunk_latencies_ms"] = latencies_ms
    result["total_ms"] = (time.perf_counter() - started) * 1000
    return result


def download_one_file(
    client: httpx.Client, base_url: str, upload_id: str, headers: dict[str, str], expected_sha256: str, stats: RunStats
) -> dict:
    started = time.perf_counter()
    digest = hashlib.sha256()
    size = 0
    failed = False
    try:
        with client.stream("GET", f"{base_url}/v1/uploads/{upload_id}/download", headers=headers) as response:
            if response.status_code >= 400:
                stats.add_error(str(response.status_code))
                failed = True
            else:
                for block in response.iter_bytes(MIB):
                    digest.update(block)
                    size += len(block)
    except httpx.TransportError as exc:
        stats.add_error(f"transport:{type(exc).__name__}")
        failed = True
    verified = not failed and digest.hexdigest() == expected_sha256
    if not failed and not verified:
        stats.add_error("download_mismatch")
    return {"bytes": size, "total_ms": (time.perf_counter() - started) * 1000, "failed": failed, "verified": verified}


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True, cwd=Path(__file__).parent)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _server_version(base_url: str) -> dict:
    try:
        return httpx.get(f"{base_url}/version", timeout=10.0).json()
    except Exception as exc:  # recorded, not fatal
        return {"error": f"{type(exc).__name__}: {exc}"}


def _latency_summary(values: list[float]) -> dict:
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 3) if values else 0.0,
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3) if values else 0.0,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load test for distributed-file-service.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--workload", choices=sorted(WORKLOADS), help="Fixed workload; overrides size and concurrency flags")
    parser.add_argument("--label", default="", help="Run label, e.g. baseline or after-task4")
    parser.add_argument("--files", type=int, default=5, help="Number of files to upload")
    parser.add_argument("--file-size-bytes", type=int, default=5 * MIB, help="Per file size in bytes")
    parser.add_argument("--chunk-size-bytes", type=int, default=MIB, help="Chunk size in bytes")
    parser.add_argument("--profile", choices=sorted(PROFILE_PRESETS), default="balanced", help="Concurrency preset")
    parser.add_argument("--concurrent-files", type=int, default=None, help="Files uploaded in parallel")
    parser.add_argument("--per-file-chunk-workers", type=int, default=None, help="Parallel chunk uploads per file")
    parser.add_argument("--download", action="store_true", help="Download every completed file and verify its SHA-256")
    parser.add_argument("--output", default="", help="JSON summary path (default with --workload and --label: benchmarks/results/<date>_<workload>_<label>.json)")
    parser.add_argument("--api-key", default=os.getenv("LOAD_TEST_API_KEY", "dev-key"), help="Sent as X-API-Key")
    return parser.parse_args(argv)


def resolve_config(args: argparse.Namespace) -> dict:
    if args.workload:
        return {"workload": args.workload, **WORKLOADS[args.workload]}
    profile = PROFILE_PRESETS[args.profile]
    return {
        "workload": None,
        "files": args.files,
        "file_size_bytes": args.file_size_bytes,
        "chunk_size_bytes": args.chunk_size_bytes,
        "concurrent_files": args.concurrent_files or profile["concurrent_files"],
        "per_file_chunk_workers": args.per_file_chunk_workers or profile["per_file_chunk_workers"],
        "download": args.download,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = resolve_config(args)
    headers = {"X-API-Key": args.api_key}
    payload = _build_payload(config["file_size_bytes"])
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    stats = RunStats()
    connections = config["concurrent_files"] * config["per_file_chunk_workers"] + 8
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)

    meta = {
        "label": args.label,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": _git("status", "--porcelain") not in ("", "unknown"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "server_version": _server_version(args.base_url),
    }

    with httpx.Client(timeout=REQUEST_TIMEOUT_S, limits=limits) as client:
        upload_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=config["concurrent_files"]) as pool:
            futures = [
                pool.submit(
                    upload_one_file,
                    client,
                    args.base_url,
                    f"load-{uuid.uuid4()}.bin",
                    payload,
                    config["chunk_size_bytes"],
                    config["per_file_chunk_workers"],
                    headers,
                    stats,
                )
                for _ in range(config["files"])
            ]
            uploads = [future.result() for future in as_completed(futures)]
        upload_elapsed = time.perf_counter() - upload_started

        downloads: list[dict] = []
        download_elapsed = 0.0
        if config["download"]:
            done = [u["upload_id"] for u in uploads if not u["failed"]]
            download_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=config["concurrent_files"]) as pool:
                futures = [
                    pool.submit(download_one_file, client, args.base_url, upload_id, headers, expected_sha256, stats)
                    for upload_id in done
                ]
                downloads = [future.result() for future in as_completed(futures)]
            download_elapsed = time.perf_counter() - download_started

    ok_uploads = [u for u in uploads if not u["failed"]]
    uploaded_bytes = sum(u["file_bytes"] for u in ok_uploads)
    latencies = [lat for u in uploads for lat in u["chunk_latencies_ms"]]
    summary = {
        "meta": meta,
        "config": config,
        "args": vars(args) | {"api_key": bool(args.api_key)},
        "upload": {
            "files": len(uploads),
            "failed_files": len(uploads) - len(ok_uploads),
            "first_errors": [u["error"] for u in uploads if u["failed"]][:5],
            "elapsed_seconds": round(upload_elapsed, 3),
            "bytes": uploaded_bytes,
            "throughput_mib_per_s": round(uploaded_bytes / MIB / upload_elapsed, 3) if upload_elapsed else 0.0,
            "chunk_latency_ms": _latency_summary(latencies),
            "file_total_ms_avg": round(sum(u["total_ms"] for u in uploads) / len(uploads), 3) if uploads else 0.0,
        },
        "download": None,
        "retries": stats.retries,
        "errors_by_status": dict(stats.errors_by_status),
    }
    if config["download"]:
        ok_downloads = [d for d in downloads if d["verified"]]
        downloaded_bytes = sum(d["bytes"] for d in ok_downloads)
        summary["download"] = {
            "files": len(downloads),
            "verified_files": len(ok_downloads),
            "elapsed_seconds": round(download_elapsed, 3),
            "bytes": downloaded_bytes,
            "throughput_mib_per_s": round(downloaded_bytes / MIB / download_elapsed, 3) if download_elapsed else 0.0,
            "file_total_ms": _latency_summary([d["total_ms"] for d in downloads]),
        }

    print(json.dumps(summary, indent=2))
    output = args.output
    if not output and args.workload and args.label:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output = str(Path(__file__).resolve().parents[1] / "benchmarks" / "results" / f"{date}_{args.workload}_{args.label}.json")
    if output:
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        print(f"\nWrote summary to {output}")
    return 1 if summary["upload"]["failed_files"] else 0


if __name__ == "__main__":
    sys.exit(main())
