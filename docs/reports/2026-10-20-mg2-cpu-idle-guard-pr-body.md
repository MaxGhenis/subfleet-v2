D-MG2 adds opt-in CPU idle and state-device service-ms/op admission, with
one-second sampling, bias-corrected 60 s EWMAs, startup priming and ten sampled
seconds of clear recovery. Null/load/memory-only policies retain frozen
473fe2941 decisions and hold records with no new CPU/IOKit reads.

Round 2 follows state-root device changes without recreating the sampler. Root
stat runs at sample cadence; ancestry is resolved only on change or failed
selected-driver reads. Stable reads select one registry entry without neighbor
enumeration. Device changes clear counters, EWMA and old disk observations: the
first successful new-device sample is baseline-only; the next positive delta is
readable. Unreadable data holds nothing and releases no latch. Baseline value and
age use one eligible set and cutoff, excluding zero-service observations; status
fetches each value/timestamp pair together during eviction.

Merged cached release/217 at **e345f0803** without fetching (merge **08ba94992**,
including 4f5983f4c and #243). The only conflict was the acceptance change-log
insertion; every clause and both histories were retained. Daemon admission
auto-merged, preserving release routing, wall/stop and finalization behavior with
the MG2 detached read point. C-6.13 is amended.

**Max's d1377 ruling is unchanged:** no live-policy edit and identical shipped
`machine_guard: null` bytes. The proposal remains background idle 0.15/memory warn,
session idle 0.05/memory critical, with **no load or disk threshold**. The earlier
capped disk term's historical 10/11 claim remains withdrawn; its true service
latency is unknown. Quiet-versus-deliberate-saturation separation remains
unverified, and no threshold is suggested.

On AC, 22 real-device samples with injected identity invalidations/failure made
22 root checks, four ancestry resolutions and zero neighbor enumerations.
Root stat median/p99: **31/103 µs**; ancestry: **3.48/5.83 ms**; complete steady sampler
read: **1.55/2.71 ms**. N is small and p99 coarse. The report distinguishes these
sampler costs from round 1's full daemon/store cost and gives exact inputs.

**Validation:** 404 distinct required cases passed, including the final status
regression; the final guard/policy/daemon gate is 259 passed. Both bounded
properties pass, and all four requested mutations fail named regressions with
positive controls. Five alternating head/release repetitions of each timing test
produced identical pass/fail counts on both versions (guard 0/5, timer 3/5), with
overlapping durations and lower head medians. No timing bounds or code changed.

The directory census is **2,343 passed, 5 failed, 77 skipped, 2 xfailed**:

| Directory | Complete files | Passed | Failed | Skipped | Xfailed | Unrun |
|---|---:|---:|---:|---:|---:|---:|
| tests/unit | 7 / 333 | 290 | 0 | 0 | 0 | 326 files |
| tests/fake | 113 / 113 | 2,053 | 5 | 77 | 2 | 0 |

This is **not a green or complete whole-suite run**. Shared-lock waits consumed
the hold-start window. Four fake failures are the review-confirmed boot-identity
base cases. The extra unknown-pin fixture expected claude-7 but seeded claude-9
with a one-second-earlier reset, which the unchanged ranking rule prefers. Its
controlled release replay remained queued at the cutoff, so it is not claimed as
an executed base reproduction. The nine review-confirmed base failures were not
modified. All 326 unrun unit files and every completed file's counts appear in
`docs/reports/2026-10-20-mg2-cpu-idle-guard.md`, together with merge resolutions,
all timing samples/load, costs, invariants, mutations and cleanup status.

No push or network remote. Commits use workspace-local `.git-local` on
`fix/mg2-cpu-idle-guard-217` because shared Git metadata and the `.git` pointer are
read-only; the hub must import that local ref.
