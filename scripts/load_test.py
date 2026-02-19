import argparse
import math
import os
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

PROFILE_PRESETS = {
    "fast": {"concurrent_files": 2, "per_file_chunk_workers": 2},
    "balanced": {"concurrent_files": 3, "per_file_chunk_workers": 4},
    "max-throughput": {"concurrent_files": 4, "per_file_chunk_workers": 6},
}


def _build_payload(size_bytes: int) -> bytes:
    pattern = b"dfs-load-test-"
    repeats = math.ceil(size_bytes / len(pattern))
    return (pattern * repeats)[:size_bytes]


def _chunk_bytes(data: bytes, chunk_size: int) -> list[bytes]:
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def _upload_one_file(
    client: httpx.Client,
    base_url: str,
    file_name: str,
    payload: bytes,
    chunk_size: int,
    per_file_chunk_workers: int,
) -> dict:
    started = time.perf_counter()
    init_resp = client.post(
        f"{base_url}/v1/uploads/init",
        json={"file_name": file_name, "file_size": len(payload), "chunk_size": chunk_size},
        timeout=30.0,
    )
    init_resp.raise_for_status()
    upload_id = init_resp.json()["upload_id"]

    chunks = _chunk_bytes(payload, chunk_size)
    latencies_ms: list[float] = []

    def _upload_chunk(index: int, chunk: bytes) -> None:
        t0 = time.perf_counter()
        resp = client.put(
            f"{base_url}/v1/uploads/{upload_id}/chunks/{index}",
            content=chunk,
            headers={"Content-Length": str(len(chunk))},
            timeout=60.0,
        )
        resp.raise_for_status()
        latencies_ms.append((time.perf_counter() - t0) * 1000)

    with ThreadPoolExecutor(max_workers=per_file_chunk_workers) as pool:
        futures = [pool.submit(_upload_chunk, idx, chunk) for idx, chunk in enumerate(chunks)]
        for fut in as_completed(futures):
            fut.result()

    complete = client.post(f"{base_url}/v1/uploads/{upload_id}/complete", timeout=30.0)
    complete.raise_for_status()

    total_ms = (time.perf_counter() - started) * 1000
    return {
        "upload_id": upload_id,
        "file_bytes": len(payload),
        "chunk_count": len(chunks),
        "total_ms": total_ms,
        "chunk_latencies_ms": latencies_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test for distributed-file-service upload lifecycle.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--files", type=int, default=5, help="Number of files to upload")
    parser.add_argument("--file-size-bytes", type=int, default=5 * 1024 * 1024, help="Per file size in bytes")
    parser.add_argument("--chunk-size-bytes", type=int, default=1024 * 1024, help="Chunk size in bytes")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_PRESETS.keys()),
        default="balanced",
        help="Concurrency profile preset to use.",
    )
    parser.add_argument(
        "--concurrent-files",
        type=int,
        default=None,
        help="How many files to upload in parallel (client-side). Overrides profile if set.",
    )
    parser.add_argument(
        "--per-file-chunk-workers",
        type=int,
        default=None,
        help="Parallel chunk uploads per file (client-side). Overrides profile if set.",
    )
    parser.add_argument("--output", default="", help="Optional path to write JSON summary")
    args = parser.parse_args()
    profile = PROFILE_PRESETS[args.profile]
    concurrent_files = args.concurrent_files if args.concurrent_files is not None else profile["concurrent_files"]
    per_file_chunk_workers = (
        args.per_file_chunk_workers
        if args.per_file_chunk_workers is not None
        else profile["per_file_chunk_workers"]
    )

    payload = _build_payload(args.file_size_bytes)
    upload_jobs = [f"load-file-{uuid.uuid4()}.bin" for _ in range(args.files)]

    run_started = time.perf_counter()
    results = []

    with httpx.Client() as client, ThreadPoolExecutor(max_workers=concurrent_files) as pool:
        futures = [
            pool.submit(
                _upload_one_file,
                client,
                args.base_url,
                file_name,
                payload,
                args.chunk_size_bytes,
                per_file_chunk_workers,
            )
            for file_name in upload_jobs
        ]
        for fut in as_completed(futures):
            results.append(fut.result())

    elapsed = time.perf_counter() - run_started
    total_bytes = sum(item["file_bytes"] for item in results)
    total_chunks = sum(item["chunk_count"] for item in results)
    all_chunk_latencies = [lat for item in results for lat in item["chunk_latencies_ms"]]
    mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
    p95 = statistics.quantiles(all_chunk_latencies, n=20)[-1] if len(all_chunk_latencies) >= 20 else max(
        all_chunk_latencies, default=0.0
    )

    summary = {
        "base_url": args.base_url,
        "files": args.files,
        "file_size_bytes": args.file_size_bytes,
        "chunk_size_bytes": args.chunk_size_bytes,
        "profile": args.profile,
        "concurrent_files": concurrent_files,
        "per_file_chunk_workers": per_file_chunk_workers,
        "elapsed_seconds": round(elapsed, 3),
        "total_bytes_uploaded": total_bytes,
        "total_chunks_uploaded": total_chunks,
        "throughput_mb_per_s": round(mb_per_s, 3),
        "chunk_latency_ms_avg": round(statistics.mean(all_chunk_latencies), 3) if all_chunk_latencies else 0.0,
        "chunk_latency_ms_p95": round(p95, 3),
        "file_total_ms_avg": round(statistics.mean(item["total_ms"] for item in results), 3) if results else 0.0,
    }

    print("Load test summary:")
    for key, value in summary.items():
        print(f"- {key}: {value}")

    if args.output:
        import json

        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"\nWrote summary to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
