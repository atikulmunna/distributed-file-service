# Benchmark Comparison: Local vs R2

## Run Context
- Date: 2026-02-20 02:02:19 +06:00
- Workload:
  - files: 6
  - file_size_bytes: 2097152
  - chunk_size_bytes: 524288
  - concurrent_files: 3
  - per_file_chunk_workers: 4

## Results
- Throughput (MB/s):
  - Local: 0.428
  - R2: 0.370
  - Delta (Local vs R2): +15.7%
- Total elapsed (s):
  - Local: 28.026
  - R2: 32.467
  - Delta: -13.7%
- Chunk latency avg (ms):
  - Local: 4971.649
  - R2: 5361.231
  - Delta: -7.3%
- Chunk latency p95 (ms):
  - Local: 7851.976
  - R2: 9872.363
  - Delta: -20.5%
- File total avg (ms):
  - Local: 11699.450
  - R2: 15254.881
  - Delta: -23.3%

## Interpretation
- Local backend is faster than R2 for this workload as expected (no external network/object-store latency).
- R2 performance is still within a usable range for a development baseline and validates real object-store flow.
- For tuning next:
  - Reduce remote round-trips per chunk where possible.
  - Re-run with varied `concurrent_files` and `per_file_chunk_workers` to locate saturation point.
