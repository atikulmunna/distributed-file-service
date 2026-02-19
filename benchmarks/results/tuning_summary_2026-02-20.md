# R2 Tuning Sweep (2026-02-20)

Workload:
- files: 6
- file_size_bytes: 2097152
- chunk_size_bytes: 524288

## Results (sorted by throughput)
- `cf3_pcw4`: throughput=`0.475 MB/s`, p95=`9298.913 ms`, file_avg=`11783.977 ms`, elapsed=`25.255 s`
- `cf4_pcw4`: throughput=`0.433 MB/s`, p95=`14778.79 ms`, file_avg=`17078.045 ms`, elapsed=`27.702 s`
- `cf4_pcw6`: throughput=`0.416 MB/s`, p95=`13624.796 ms`, file_avg=`17194.826 ms`, elapsed=`28.864 s`
- `cf2_pcw2`: throughput=`0.384 MB/s`, p95=`7452.314 ms`, file_avg=`10039.62 ms`, elapsed=`31.262 s`

## Recommendation
- Use `concurrent_files=3` and `per_file_chunk_workers=4` for this environment.
- Reason: highest measured throughput in this sweep (`0.475 MB/s`).
- Note: `cf2_pcw2` gives lower latency, so prefer it if response-time consistency is more important than throughput.
