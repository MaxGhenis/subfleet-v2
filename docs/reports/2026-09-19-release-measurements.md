# Numeric release measurements, 2026-09-19

C-20.4 / plan amendment 10. Measured on this Mac at 08:26 EDT using an isolated temporary state root and fake providers. No production daemon, account, or provider request was used.

Command: `uv run python tools/measure_release_gates.py --jobs 300 --calls 200`.

Result: **PASS, exit 0**. The command checks CLI return codes, strict latency thresholds, and one succeeded attempt after recovery.

| Measurement | Target | Result |
|---|---|---|
| Store fill | 300 terminal jobs, 14 lanes | 77.2 s |
| Cached status p95, end-to-end CLI | below 100 ms | 83 ms |
| Cached status p95, socket round trip | diagnostic | 24 ms |
| Interpreter start plus CLI import p95 | diagnostic | 59 ms |
| Submit p95, excluding probes | below 250 ms | 119 ms |
| Daemon SIGKILL recovery | below 30 s | 3.4 s |
| Recovery result | one succeeded attempt | one succeeded attempt |

Local raw log: `/tmp/subfleet-plan-release-measurements.log`. Temporary state root: `/tmp/sf-gates-kso8tez8`. These temporary paths are audit aids and may be removed later; the measurements above are the durable record.

This run does not satisfy the real 100-job canary, seven-day soak, or reviewed shadow decision comparisons. Those gates remain pending in [release-gates.md](../release-gates.md).
