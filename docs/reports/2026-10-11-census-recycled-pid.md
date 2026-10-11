# Containment census and recycled PIDs — 2026-10-11

Implementation checkpoint before queued test runs. Base: `4c6a8a9c989e`.

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

`_contain` now reads the attempt's recorded guardian identity, child identity
from `owned_identities`, and start/exit receipts. Every existing consumer receives
the same keyword arguments; probe censuses delegate to this path too. The child
PID can come from the exit receipt before a caller's attempt row is refreshed.

`procs.containment` checks root/leader identities in its fresh process snapshot.
Different starts or confirmed different boot UUIDs prune a root and its walk;
a recycled leader prunes its group. A missing leader preserves group survivors.
Shifted legacy boot timestamps and failed identity inspections hold containment
and make it unverifiable. A cached UUID mismatch needs a fresh boot read.

When a root has no complete recorded identity, readable bounds exclude starts
before the attempt began or after the exit receipt. The lower bound is rounded
down to a whole second because `ps lstart` drops fractional seconds. Missing or
unparseable bounds supply no evidence for exclusion. Matching identities take precedence over
time bounds. Commands and environments stay transient; `recycled` evidence has
only recorded/current identity or bounds and a reason. `_contain` also persists
excluded identities in the attempt's evidence, retaining them through later kill
and finalization writes, so `runs show` explains a verified-empty success as well
as a quarantine release. Marker enumeration and
signal authorization are unchanged. Production edits are limited to `procs.py`,
`_contain`, and `_probe_census`'s delegation, to keep the merge with PR #131 small.

The before run uses a frozen copy of the unfixed source inside this workspace,
so the shared test-lock wait does not delay preparing and committing the fix.
The copied source was checked to contain neither new identity argument.

Five Hypothesis properties are configured for 200 examples each (actual counts
pending): (a) recycled roots/groups exclude strangers and their walks; (b)
matching roots and group members remain; (c) markers are unchanged; (d) every
source set and captured identity equals the original census when all identities
match; (e) `_kill_attempt` retains the old recorded identity and never adds or
individually signals the recycled root as a new target.

Validation pending: three before/after incident regressions, fake-daemon rc-0
finalization and confirm-dead salvage, five Hypothesis invariants, conservative
identity examples, and related existing suites. No live state or process is
part of this reproduction. Git metadata is read-only; commits use `.git-local`
on `fix/census-recycled-pid` inside the assigned workspace.
