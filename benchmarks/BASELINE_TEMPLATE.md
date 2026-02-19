# Baseline Benchmark Report

## Environment
- Date:
- Commit SHA:
- Host machine:
- Python version:
- Backend (`local|s3|r2`):
- Database:

## Test Command
```bash
python scripts/load_test.py --base-url http://127.0.0.1:8000 --files 10 --file-size-bytes 5242880 --chunk-size-bytes 1048576 --concurrent-files 4 --per-file-chunk-workers 4 --output benchmarks/results/baseline.json
```

## Results
- elapsed_seconds:
- total_bytes_uploaded:
- total_chunks_uploaded:
- throughput_mb_per_s:
- chunk_latency_ms_avg:
- chunk_latency_ms_p95:
- file_total_ms_avg:

## Notes
- Any errors / retries observed:
- Throttling behavior (`429`) observed:
- CPU / memory observations:
