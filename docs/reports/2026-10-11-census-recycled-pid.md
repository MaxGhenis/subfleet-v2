# Containment census and recycled PIDs — 2026-10-11

Draft checkpoint before queued test runs. Base: `4c6a8a9c989e`.

The census counted whichever process currently occupied the guardian or child
PID, even after the attempt's recorded start identity had died. It also counted
a new process group under a recycled leader PID. The three measured incidents
were qyp16-r1c (child 4203), pb-kit-u4-docs (leader 243), and pe-uk-2192-r2/a2
(child 16379). This could quarantine successful work, hold leases indefinitely,
and report rc 125. Signal authorization already checks identity separately.

Apple's [XNU 12377.1.9 `forkproc`, lines 895–949](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_fork.c#L895-L949)
allocates PIDs under `proc_list_lock` and rejects a candidate present in the
process hash, process-group hash, or session hash. A different process at the
recorded leader PID therefore proves that the original group no longer exists.
If the leader PID is absent, remaining group members must still be retained.

Planned implementation: pass recorded root identities and attempt start/exit
bounds through `_contain`; exclude proven recycled roots and groups; preserve
legacy identity uncertainty, marker enumeration, and independent signal checks.
Evidence will include recorded/current identities without commands or environments.

Validation pending: three before/after incident regressions, fake-daemon rc-0
finalization and confirm-dead salvage, five Hypothesis invariants, conservative
identity examples, and related existing suites. No live state or process is
part of this reproduction. Git metadata is read-only; commits use `.git-local`
on `fix/census-recycled-pid` inside the assigned workspace.
