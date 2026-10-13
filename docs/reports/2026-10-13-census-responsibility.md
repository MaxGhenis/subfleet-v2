# D-CEN1: raw markers and responsibility lineage, 2026-10-13

Implementation and bounded validation are complete in the assigned workspace.
Host acceptance remains incomplete: this sandbox denies executing `/bin/ps` and
the boot-identity `sysctl`, and an isolated launchd bootstrap failed. The real
escaper regressions, their baseline/mutation comparisons, successful full-census
timings, and launchd responsibility check still need the hub's permitted host.

Continued checkpoint `7ac60bcede906c7a07370a62195b4559e51167df`, preserving its
regressions-first history. No remote operation, sub-agent, live-store access or
live-daemon modification. The shared Git index rejected writes with EPERM, so
new commits use workspace-local `.git-local`, branch
`fix/census-responsibility`, with read-only alternates to the shared object store.
Inspect it with `GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git log`.
Only named source, test, contract and report files were staged; no logs, JSON
evidence or bundles. All commits carry the requested co-author trailer.

The incident was attempt `20261013-015921-pr213-suite-rerun-inplace/a1` on codex-1.
Detached vitest 19935, its worker 21910, and esbuild 21192 had their own group and
ppid 1. Only esbuild's environment marker kept the leases quarantined. Node's
title rewrite left NULs over argv, which made `ps -E` stop before the environment
markers of the vitest processes. The hub's raw-buffer probe found those markers;
its Perl `$0` probe demonstrated that another process can erase them altogether.
The disclaimer probe demonstrated responsibility lineage across double-fork,
setsid, reparenting and renaming. These observations motivate both sources.

The marker source now uses the process-table snapshot plus a subsequent
`proc_listpids` listing, checks accessible live candidates with `proc_pidinfo`, and
reads `KERN_PROCARGS2` through in-process `sysctl`. A match is an exact whole
NUL-delimited attempt string anywhere after the executable path, with the exact
state-root string when supplied. An argv match can only over-include. Other-user
EPERM and gone/zombie processes are skipped; a failed live same-uid read or a
buffer filling `kern.argmax` prevents release. Refused reads are rechecked for a
process already exiting. Neither commands nor environments enter evidence.

`procs.spawn_disclaimed` uses ctypes `posix_spawn` with
`POSIX_SPAWN_CLOEXEC_DEFAULT`, `/dev/null` standard streams, `addinherit_np` for
the launch gate, `addchdir_np` for cwd, the given environment, and restored
SIGPIPE/SIGXFSZ defaults. Its handle preserves the daemon's pid/poll/wait surface
and reaps through `waitpid`. All three daemon guardian sites use it: attempts,
probes, and Claude re-enrolment. No additional daemon guardian spawn site was
found. The provider spawn inside the guardian remains undisclaimed.

An attempt records `responsible_root: {pid, unique_id}` beside its guardian
identity. The fourth source counts live same-uid processes naming that root,
including its pids, shapes and identities in census evidence. Kernel unique-id
order excludes processes born before the root in its boot without trusting a
wall clock. Unavailable ordering retains candidates. A failed responsibility
read prevents release; attempts without a recorded root keep three sources.
Missing spawn SPI uses the existing subprocess path without any metadata-read
dependency and logs once per daemon. A successful disclaimed spawn always
records a root, even when the metadata read fails.

Darwin drops descendants' responsibility links when the root exits, even before
it is reaped. A rooted guardian therefore writes its exit receipt and waits for
no other process to name it, at intervals increasing from 0.1 s to at most 2 s.
Failed self-reads and listings are retried; they cannot establish emptiness or
that the guardian is unrooted. Every discovered responsibility identity is
persisted as a later walk seed, so the kill protocol's SIGKILL cannot discard a
member already observed. Census discovery grants no additional signal authority.
This SPI requires a live root to discover a previously unseen escaper; an
externally terminated root before discovery remains a limitation. The receipt
path preserves the root and the daemon kill path censuses before killing it.

C-5.1, C-5.2, C-5.5 and C-5.12 now specify these behaviors and costs. The version
history cites D-CEN1 and this incident. Normal running-attempt inspections still
share their existing process table and do neither full kernel scan.

The TCC diagnostic started a real guardian through `Daemon._spawn_guardian` and
held its launch gate closed. `proc_pidpath` gave both the launcher and guardian:

`/Users/maxghenis/.local/share/uv/python/cpython-3.14.7+freethreaded-macos-aarch64-none/bin/python3.14t`

`codesign -dv --verbose=4` succeeded for each and their identities were equal:
`Identifier=-`, `Signature=adhoc`, no TeamIdentifier, and
`CDHash=42c5365fa3175cf94baa02422825a3d78cff7299`.
The guardian's responsible pid equaled its own pid and its root was recorded.
The diagnostic closed its gate and reaped the guardian without starting a
provider. Equal executable/signing identity supports unchanged TCC attribution
when launchd starts the daemon as its own responsible process. That last premise
was **not verified**: an isolated, uniquely labelled launchd diagnostic returned
bootstrap status 5, `Input/output error`, and created no diagnostic result.
No existing launchd service or live daemon was inspected or changed, and no TCC
grant was modified. The hub must verify the launchd daemon's self-responsibility.

Timing used CPython 3.14.7 with free threading on Darwin 25.6 arm64. Both raw and
responsibility SPIs resolved and node was present. Wall time used
`perf_counter`; CPU was user plus system time from `getrusage(SELF)` and reaped
children. Pid counts came from `proc_listpids`, excluding pid 0. An earlier probe
listed 1,308 processes; timing runs saw 1,266–1,268, rather than the incident's
approximate 1,500. A separate access check returned 955 same-uid records, 338
EPERM skips and seven processes gone during inspection.

Five full-census attempts per revision were measured with an absent random
attempt marker. Base code was loaded directly from `5bd0a3a67`; head was the
fixed implementation. **Every full-census result was unverifiable**, so these
are failure-path timings and do not establish a successful before/after cost.
Head's full attempts below used the three-source legacy path. Successful
supplemental kernel scans include the listing and per-candidate reads; the
responsibility scans used one owned, disclaimed child, always found exactly that
root and reported zero read errors. That child was killed and reaped afterwards.

| Measurement | Five wall samples, ms | Five CPU samples, ms | Median wall / CPU, ms |
|---|---|---|---|
| Base full census, denied | 0.563, 0.241, 0.174, 0.150, 0.297 | 0.584, 0.250, 0.178, 0.152, 0.180 | 0.241 / 0.180 |
| Head full census, denied | 16.961, 14.707, 15.811, 16.078, 16.959 | 16.205, 14.634, 15.387, 15.462, 16.715 | 16.078 / 15.462 |
| Head raw kernel scan only | 14.988, 13.846, 19.114, 16.373, 15.112 | 14.561, 13.732, 18.922, 16.285, 15.041 | 15.112 / 15.041 |
| Head raw + responsibility kernel scan only | 19.155, 17.739, 24.162, 23.299, 21.927 | 18.558, 16.664, 22.677, 22.158, 20.788 | 21.927 / 20.788 |

Latest distinct-item validation totals are **265 passed, 34 skipped, no remaining
assertion failures or setup errors**, across 22 files. Repeated runs and mutation
failures are excluded. Initially 18 inspection-budget cases errored because their
fake daemon constructor read the host before the test installed its fake reader.
Its construction now uses a scoped synthetic identity, restored immediately
afterwards; all 22 cases passed without relaxing their assertions. The raw-marker
fixture now reaps children on readiness failure and cleans up if neighbor startup
fails. Syntax compilation and Git whitespace checks passed.

| File under `tests/` | Passed | Skipped |
|---|---:|---:|
| unit/test_procs.py | 47 | 0 |
| unit/test_census_procargs.py | 41 | 0 |
| unit/test_census_identity_uncertainty.py | 8 | 0 |
| unit/test_census_recycled_pid.py | 25 | 0 |
| unit/test_census_survivors_review.py | 14 | 0 |
| unit/test_census_responsibility_failures.py | 8 | 0 |
| unit/test_daemon_inspection_load.py | 22 | 0 |
| unit/test_test_module_names.py | 1 | 0 |
| fake/test_census_recycled_pid_daemon.py | 8 | 0 |
| fake/test_review_r185_adversarial.py | 1 | 0 |
| fake/test_state_contract.py | 37 | 0 |
| fake/test_census_escapers_daemon.py | 0 | 4 |
| fake/test_idle_cost.py | 0 | 2 |
| fake/test_launch_gates_above_stdio.py | 0 | 9 |
| process/test_census_raw_markers_process.py | 0 | 3 |
| process/test_census_responsibility_process.py | 0 | 5 |
| process/test_claude_isolation.py | 15 | 0 |
| process/test_codex_isolation.py | 30 | 0 |
| process/test_guardian_process.py | 0 | 4 |
| process/test_guardian_qos_process.py | 0 | 3 |
| process/test_guardian_relay.py | 0 | 4 |
| process/test_pipe_above_stdio.py | 8 | 0 |

The parser's two Hypothesis properties are bounded at 300 examples each, covering
arbitrary argv/environment layouts, padding, titles, cuts and arbitrary bytes.
Every pytest process ran serially in the foreground under its own shared
`lockf -k` hold, with a 60 s acquisition timeout. Completed holds lasted under
24 s of pytest time, well under 20 minutes. `uv run --no-sync` used the existing
workspace environment and a temporary uv cache because the default cache is
outside the writable sandbox. Each run set its own Darwin TMPDIR and
`HYPOTHESIS_STORAGE_DIRECTORY` and removed TMPDIR with `shutil.rmtree`. Disposable
archive copies and the checkpoint's leftover `.hypothesis` cache were removed.

Disposable copies disabled each source independently inside `_kernel_sources`.
Removing the raw-read branch failed
`test_d_cen1_a_marker_born_after_the_snapshot_is_asked_about_singly` because pid
96 disappeared from `marker_pids`. Removing the responsibility branch failed
`test_d_cen1_responsibility_members_are_counted_and_recorded_without_commands`
because `{42, 43, 60, 61}` disappeared. Each produced exactly one expected unit
failure. The matching real tests skipped: three raw-process items, and one Perl
daemon plus one rooted-guardian item. Their real-process mutation proof therefore
remains unverified. The baseline archive contained exact `5bd0a3a67` production
code plus the first regression commit's four test-support files; its six real
items (three daemon, three raw-process) were **unrun**, because lock acquisition
timed out. The checkpoint's commit message reports baseline failures, but this
continuation does not claim to have reproduced them.

Preserved commits: `b53b20185` regressions first, `8d9fe7e42` implementation,
`7ac60bced` parser/source/guardian tests. Continuation commits: `b5f203cf8` failed
read hardening and contract, `a0d226f79` legacy fallback isolation,
`a1e3811c8` fixture isolation and cleanup. Report commit `a24bfcadd` adds this report; the following documentation-only
commit names `.git-local`, the repository's existing ignored metadata convention. The hub's remaining acceptance work is the skipped files above,
baseline and real mutation comparisons, five successful full-census runs per
revision at host permissions, and the launchd daemon responsibility check.

## Round 2 (2026-10-13)

Started at merged head `8d4bfebd4b8b95bfb399cbf5603afd89cc6b9675`; read
`~/reviews/subfleet-2110/census-responsibility/review-r1.md` in full and inspected
the supplied Python 3.14 CI failures. Work is confined to this assigned checkout;
no remote operation, sub-agent, live store or live daemon access. Commits use
workspace-local `.git-local`, branch `fix/census-responsibility`, because the
shared Git metadata is outside the writable sandbox. Only named files are staged.

P2-1 disables the responsibility source for a root the census proves recycled,
including an unknown root unique id. A different kernel unique id at the root pid
bounds members below that new holder and at or above the recorded root. The
daemon also refuses to persist responsibility identities from a recycled root.
Regressions include the reviewer's self-responsible S/child C input, both with an
older unique id and with None, and same-second reuse distinguishable only by the
kernel counter. Discovery still grants no signal authority.

P2-2 re-lists after reading every new pid until a listing adds none. A forked
child of a member read as gone was born before that read and appears on the next
listing. A nonempty read retains the guardian immediately; eight rounds of
continuous births raise InspectionError and cannot release it. Synthetic races
and a real, barrier-controlled four-generation fork-and-exit chain cover this.

P3-1 polls at 0.1 s through the configured exit settle window, then backs off to
2 s. P3-2 drops terminal quarantined guardians from `_children`; the next spawn
reaps the abandoned handle. P3-3 encloses the provider section and exit receipt
in `finally: linger()`, including relay serving errors, and stops the relay on
serve errors. Each has a regression. P3-4 adds the dead-root and self-disclaiming
member limits to C-5.5. C-5.2 now says the first look is immediate.

P3-5 checks the daemon's own responsible pid at startup. Only a successful
self-responsible result enables guardian disclaiming; terminal-started daemons
and unreadable startup attribution preserve the Popen responsibility chain.
Responsibility integration tests explicitly start their own daemon as a root to
model launchd. One injectable `daemon.spawn_guardian` boundary receives the
production command and Popen options; a shared fixture passes those exact values
to existing Popen stand-ins. Credentials, launch gates and preflight assertions
are retained.

P3-6 retries full/ENOMEM argument reads up to an in-process KERN_ARGMAX read,
with conservative failure at that ceiling. On this host both SC_ARG_MAX and
KERN_ARGMAX are 1,048,576 bytes. A buffer still full there remains unverifiable;
C-5.5 now states the residual oversized-area limitation rather than allowing a
partial marker read to release an attempt.

A task-owned Homebrew framework Python 3.14.7 probe reproduced the initial
`bin/python3.14` path changing to `Resources/Python.app/Contents/MacOS/Python`.
Both pid (83314) and self-responsibility stayed unchanged across re-exec; there
was no extra stub process. The launcher and runtime have different ad-hoc code
identities: launcher CDHash `5e9d2eb24407f340216ed7adde0a42ddc448add5`, runtime
CDHash `c61b2c8b7af337db8b442c65919ccd6abab9297c`. The spawn test now waits until
Python code runs before comparing the running executable identity and retains
exact argv, gate, cwd, environment and descriptor checks. Providers only start
after that re-exec, when guardian and self-responsible daemon run the same
interpreter. This supports the TCC argument for framework builds; no privacy
prompt or grant was exercised. Terminal attribution is preserved by P3-5.

The supplied CI's responsibility-only failures actually show Perl in
`marker_pids`; descendant inclusion follows earlier responsibility seed records,
which the existing assertions already allow. Platform argument visibility differs
between kernels. Both Perl fixtures now explicitly remove the attempt/root/job
markers before exec, retaining renaming and session escape, so their assertions
still require responsibility discovery and no marker/group match on either host.

Validation is in progress. The first two lock acquisitions timed out at 60 s;
no pytest ran in those holds. Each subsequent suite uses its own foreground
lockf hold with a 1,190 s outer timeout and cleans its Darwin TMPDIR with
shutil.rmtree. Final counts and skipped/unrun files will be recorded below.
