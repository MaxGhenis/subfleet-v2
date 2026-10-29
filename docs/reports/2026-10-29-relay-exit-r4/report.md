VERDICT #271: APPROVE

Review r4 of MaxGhenis/subfleet-v2#271 (D-RELAY-EXIT1) at `c8904e4b2`: fix round r4's `0469bcb53` plus the hub's
merge of release/217 at `e8ecb42e7`. Lane `20261028-215711-pr271-review-r4`, 2026-10-29 01:57Z to 03:20Z; scratch
`$(getconf DARWIN_USER_TEMP_DIR)sf-r271r4`, one venv per Python synced from the tree's lock (3.14.7, 3.12.14).
No production file changed.

The kernel exit peek is correct on both CI Pythons. Every requested file passed on 3.14 and 3.12, and every named
mutant was killed with its control passing: round 4's eight, round 3's two, round 2's five and seven of mine. No
P1 or P2. Three P3s: two text corrections (the peek never reaps, but `poll()` still can, so "the guardian's wait
stays the only reaper" is false) and one residual window, measured but never hit.

## Checks

**1. The exit peek: pass.** `relay.child_exited` (`relay.py:262`) reads `returncode` with `getattr`, then
`poll()`, then peeks with `waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)`.
- **Reaping.** No peek reaps, and the PR adds no `poll()` or `wait()`: `relay.py` had `child.poll()` at three sites
  (`3769a17e5`: `:451`, `:458`, `:499`) and now has one, inside the helper (`:270`), called from the same three
  sites (`:542`, `:549`, `:590`); `guardian.py` waits only at `:295` and `:306`. But the guardian's wait is not the
  only reaper, before or after this PR: `poll()` reaps whenever Popen's lock is free, and `send_signal` calls
  `poll()` first. Executed: `tests/fake/test_turn_relay_exit_r4.py::test_the_relay_s_poll_reaps_and_stores_the_status_when_the_wait_lock_is_free`.
  A child exits 7 with nothing holding the lock; `server.status()` says `exited`, the zombie is gone from the
  process table, `returncode == 7`, and `child.wait()` returns 7. In the guardian that can happen only between
  `relay.serve()` starting its thread and `child.wait()` (`guardian.py:294-295`), and Popen stores what it reaps,
  so the exit receipt keeps the real status. The text calls this the only reaper; see P3-1.
- **Binding.** Chosen once at import: `EXIT_PEEK` and `_exit_peek` at `relay.py:257-258`, `_libc_waitid` bound at
  `:235`. Executed: `os.waitid` on 3.14, `libc waitid` on 3.12, and the libc binding bound on both.
- **Darwin's headers.** I compiled a C probe against the SDK headers on this arm64 host: `sizeof(siginfo_t)` is
  104; the offsets are `si_signo` 0, `si_errno` 4, `si_code` 8, `si_pid` 12, `si_uid` 16, `si_status` 20,
  `si_addr` 24, `si_value` 32, `si_band` 40 and `__pad` 48; `pid_t`, `uid_t`, `id_t` and `idtype_t` are 4 bytes;
  `P_PID` is 1, `WEXITED` 4, `WNOHANG` 1, `WNOWAIT` 32. `ctypes.sizeof(_SigInfo)` and every field offset match on
  both Pythons. `waitid(idtype_t, id_t, siginfo_t *, int)` (`sys/wait.h:249`) matches the argtypes (`c_int`,
  `c_uint32`, `POINTER(_SigInfo)`, `c_int`). The C compiler links `_waitid`, the symbol `CDLL(None).waitid` resolves.
  `os` exposes the same constants on 3.12, which has no `os.waitid`.
- **errno.** `CDLL(None, use_errno=True)` and `ctypes.get_errno()` read it safely. The mutant without `use_errno`
  is killed (table below).
- **ECHILD and other errors.** ECHILD reads as exited. Any other `OSError` reads as "not shown to exit" and is
  logged once per process (the module global `_peek_failure_logged`). One guardian process serves one relay and
  one child, so in production that is once per relay and once per child. That is enough: a repeated failure has
  the same cause, and the line names the errno. Only a second, different errno in the same guardian goes unlogged.

**2. Round 4's deviations: all acceptable.**
- **The ctypes binding is bound only on macOS.** A platform with neither `os.waitid` nor macOS gets
  `_libc_waitid = None`, so every peek raises ENOSYS and is logged once; the relay is back to Popen's evidence
  alone (round 3's behaviour). Executed:
  `test_without_either_binding_the_relay_has_only_popen_s_evidence`. With the lock held, an exited child reads
  `False` with one `ENOSYS` line; with the lock free, `poll()` reaps it and reads 9. No CI job or lane reaches
  that path: `tests.yml` runs `macos-15` only, and CPython on Linux has `os.waitid`. `EXIT_PEEK` would still say
  `libc waitid` there (P3-3).
- **A pid of 0 or less is skipped.** That is correct: `P_PID` with 0 or a negative id names no child, so ECHILD
  would read a fake's exit. Mutant `pid-zero-peeked` is killed by
  `test_a_child_without_a_real_pid_is_never_peeked[pid-zero]`.
- **`no-child` for an exited, unreaped child.** At `runner.py:955-972` the runner records the stop as not
  delivered (`audit.record(False)`), advances `next_seq` and pops the frame; nothing waits on delivery.
  - The stop audit's only reader is `daemon._receipt_precedes_signal` (`daemon.py:4405`). It skips a
    `signal_sent: false` stop delivery, so an exit that came before that stop can now supersede a wall cancel.
    That is right: the child had already exited.
  - With the lock free, the old code already answered `no-child` (`poll()` reaped first), so every caller already
    handled it.
  - Both wall files pass at head (55/0/0), with their pid-less fakes.
- **The property's fourth lock mode.** It still tests decision 2. Children end with random codes and signals, at
  random times, before or during the peeks, with the lock free, held, or held by a real thread in `child.wait()`.
  Each binding is checked for no exit read before the gate opens, an exit staying read, a zombie still present
  under a held lock after every peek, and the real status returned. The fourth mode adds the ECHILD window, with
  the test standing in for Popen's wait. The kill reports show that mode is where the property catches a reaping
  peek and the ctypes binding without `WNOWAIT`.
  - **The 1 ms pause hides no race the property could see.** No assertion depends on read density. With a real
    `child.wait()` thread, the exit-to-reap interval is shorter than the sampling interval: at 100 µs sampling
    (`test_peeks_across_a_real_guardian_wait`), Popen's own record answered the first exit read in 15 of 16
    children on 3.14 and 2 of 8 on 3.12. So that mode exercises the peek only by scheduling chance, with or without
    the pause. The held and reaped-unpublished modes exercise the zombie and ECHILD paths deterministically: 213
    to 228 exited peeks and 84 to 90 ECHILD peeks per binding in my runs.
  - **Property counts reproduce exactly** on both Pythons: 60 examples, 155 children (18/3/25/14 by size), lock
    free 28, held 53, reaped-unpublished 42, guardian thread 32; 67 exit codes, 88 signals; 53 never-reap checks
    (52 nonzero).

**3. Trying to break it: no defect.** Every input ran on real `/bin/sh` children, started and reaped here, one or
two live at once (at most four in the property).
- **A child that exits between a peek and the guardian's wait.** This is the exit-in-progress window, where the
  child's stdin has closed but the kernel has not yet made it waitable. It is measured in
  `docs/reports/2026-10-29-relay-exit-r4/review_exit_timing.py`:
  - **Stdin close to waitable exit.** Measured from kqueue `EV_EOF` with no reaper: 0.05 to 0.14 ms at the median,
    0.07 to 0.42 ms at p90, and 0.16 to 8.1 ms at most, per run of 40 `/bin/sh` or Python children at load 20
    to 39. Unread pipe data doesn't lengthen it.
  - **The real relay path.** A guardian-like thread waits on the child, the relay's write meets EPIPE, and the
    runner's `status()` follows at once. The refusal carried `child_exited` in **240 of 240** cases, and the status
    said `exited` in all 240, 80 of them with a frame left unread in the pipe. The relay journals the failed
    write (an fsync) before its peek, and that covers the interval here.
  - **The runner end to end.** A real child stands for `[fake:exit-after-ack]`, and its next write lands once
    stdin has closed: **32 of 32** settled `ended-without-result`.
  - **Not exercised:** an exit that outlasts the relay's journal append and the runner's status read; see P3-2.
- **A pid reused after the guardian reaps.** Executed by `test_a_reused_pid_reads_by_whose_child_holds_it`. A pid
  that is not this process's child reads `exited` (ECHILD), and a live child of this process reads `running`. The
  guardian has one child, and the helper peeks only after `returncode` and `poll()` show nothing. A reused pid
  therefore reads ECHILD, which is true, since its own child was reaped. Darwin also assigns pids in sequence, so
  reuse within that interval would need the pid space to wrap.
- **A peek during Popen's own `wait()` holding its lock.** This is the property's guardian-waiting mode (32
  children per binding) and `test_peeks_across_a_real_guardian_wait` (8 children per binding per Python). It
  never read an exit early, an exit stayed read, and the wait returned the real status.
- **`waitid` interrupted by a signal.** `test_a_peek_under_a_signal_storm_never_meets_eintr` ran 6,500 to 14,500
  peeks per binding under a 200 µs `ITIMER_PROF`, with 30 to 77 `SIGPROF`s, and met no failure. `WNOHANG` never
  sleeps, so neither binding can meet EINTR, and `os.waitid` would retry it anyway (PEP 475).
  `test_an_interrupted_libc_peek_proves_nothing_and_is_logged_once` injects EINTR into the libc call: it reads
  "not exited" and logs one `EINTR` line.

**4. Both Pythons: pass.** The six `test_turn_relay_exit*.py` files gave 90/0/0 on 3.14 and 81/0/9 on 3.12. The
nine 3.12 skips are the `os.waitid` cases of `test_turn_relay_exit_peek.py`, each with the reason "CPython 3.12
leaves os.waitid out of its macOS build: the relay peeks through libc there".

**5. Round 3's rulings at head: hold.**
- **Decision 2.** `runner.py:507-524` retries a failed stat 3 times and returns `None`, never 0, and `:501-503`
  takes the first read that ends short of a chunk as catch-up.
- **Decision 3.** `_settle_failed` (`:423`) settles unless the outcome was reported, the runner is stopping, or the
  store refused as closed.
- **Tests.** The 17 r3 cases for these rulings pass on both Pythons: the entry-stat retries, every stat failing,
  an unknown size bounding a writer, a failing runner settling, and a closing service leaving the turn.
- **No drift.** Round 4 changed only `relay.py` under `subfleet/`, and the merge's `runner.py` diff equals the
  branch's.

**6. Text: true at head except the lines below.** The live body (one `gh pr view 271 --json body`) equals
`pr-body-r4fix.md` after stripping trailing whitespace. Every commit it names exists in the history, and its four
release/217 merges are merges. `siginfo_t`, the binding, ECHILD, log-once, `no-child`, the property counts, the
mutant table and the 27 / 18 + 9 counts all reproduce. Corrections:

- **C-26.4** (`docs/acceptance-contract.md:730`): replace "The relay learns of its child's exit without reaping
  it: from Popen's own record, or else from the kernel by `waitid` with `WEXITED | WNOHANG | WNOWAIT`, since"
  with "The relay reads its child's exit from Popen's own record (`returncode`, then `poll()`, which reaps only
  while Popen's lock is free and stores what it reaps), or else from the kernel by `waitid` with
  `WEXITED | WNOHANG | WNOWAIT`, which never reaps, since". Replace "The guardian's wait stays the child's only
  reaper and records its real status." with "No peek reaps: the guardian's wait reaps the child, or Popen's
  `poll()` does if the relay reads the child before that wait takes Popen's lock, and Popen stores the real status
  either way, which the guardian's wait returns."
- **Change-log line** (`:9`): replace "The guardian's wait stays the only reaper." with "No peek reaps; whichever of
  the guardian's wait and Popen's `poll()` reaps the child, Popen stores its real status."
- **Report** (`docs/reports/2026-10-28-relay-exit-r2/report.md`, "Fix round r4"): replace "`guardian.py`'s
  `child.wait()` (`:295`, `:306`) stays the only reaper. Nothing else in `relay.py` or `guardian.py` calls `poll()`
  or `wait()`." with "No peek reaps. The helper keeps Popen's `poll()`, which reaps while Popen's lock is free
  (before `guardian.py:295`'s `child.wait()` takes it) and stores the status, as the three sites' `poll()` did;
  `send_signal` polls the same way. `relay.py` calls `child.poll()` only in the helper, and `guardian.py` calls
  `wait()` only at `:295` and `:306`." In its "Text" paragraph, replace "the guardian's wait stays the only
  reaper" with "no peek reaps, and Popen stores the real status whoever reaps".
- **PR body:**
  - Header: "**Review r4 approved fix round 4 (the kernel exit peek) on both CI Pythons; text corrections
    applied.**"
  - "The fix", first bullet: replace "carry `child_exited: true` when the guardian has reaped the child
    (`child.poll() is not None`)" with "carry `child_exited: true` when the child is shown to have exited
    (`relay.child_exited`: Popen's record, or from round 4 a non-reaping kernel peek)".
  - Runner bullet: replace "For older guardians that don't send the flag, it asks `relay.status()`" with
    "Without the flag (an older guardian, or no exit shown yet), it asks `relay.status()`" (`runner.py:981`
    asks whenever the flag is absent).
  - Round 4 bullet: replace "the guardian's `child.wait()` stays the only reaper" with "no peek reaps; Popen
    stores the child's real status whether its `poll()` or the guardian's `child.wait()` reaps it".
  - Invariant: replace "A relay exit read never reaps." with "A peek never reaps; a relay exit read reaps only
    through Popen's own `poll()` while the guardian's wait doesn't hold Popen's lock, and Popen stores that
    status." After "Timing alone never decides." add "No timer stands in for proof: a child whose exit is still
    in progress (stdin closed, not yet waitable) at both the refusal and the status read remains an unproved
    loss."
  - Evidence, e2e: replace "so it is unclassified, and this PR stays a draft until review r3 weighs it." with "so
    it is unclassified. Review r3 reproduced one ordering that yields it (the guardian's wait hiding the exit from
    `poll()`), which round 4 closes."
  - Evidence, add: "**Review r4** (`c8904e4b2`): every requested file passed on 3.14 and 3.12, and all 22 named
    mutants were killed on both. With real children that end on their own, 240 of 240 relay refusals carried
    `child_exited`, and 32 of 32 exit-after-ack runs settled `ended-without-result`. The exit-in-progress interval
    measured 0.05 to 0.14 ms at the median and 8.1 ms at most."

**7. The merge `c8904e4b2`: pass.**
- **Contract lines.** All 5 lines release added between the merge base `f1d2f116e` and `e8ecb42e7` are in the
  merged contract, its 1 removed line is gone, and all 4 lines the branch added are present.
- **No repeats.** No clause head repeats (`C-26.4` once), and the three D-RELAY-EXIT1 change-log lines (`:9`,
  `:10`, `:12`) are distinct. The one repeated line, a D-ST1 change-log entry at `:199` and `:203`, is on release
  already (twice at both `f1d2f116e` and `e8ecb42e7`).
- **Diffs.** For each of the 23 files in `git diff e8ecb42e7 c8904e4b2`, the added and removed lines equal those in
  `git diff f1d2f116e 0469bcb53`. The two lists name the same files, and only the contract changed on both sides.

## Findings

No P1 or P2.

- **P3-1, text: "the guardian's wait stays the only reaper" is false.** The claim appears at
  `docs/acceptance-contract.md:730` and `:9`, in the report's Fix round r4 section and in the PR body. The helper
  keeps Popen's `poll()`, which reaps while Popen's lock is free (`relay.py:270`).
  - **Executed input:** `test_the_relay_s_poll_reaps_and_stores_the_status_when_the_wait_lock_is_free`, where
    `status()` reaps an exited child and stores 7.
  - **Effect:** no wrong status, since Popen stores it.
  - **Fix:** the corrected text in check 6. No code change.
- **P3-2, residual window: an exit in progress at both the refusal and the status read is still `relay-failed`.**
  The child's stdin closes before the kernel makes it waitable (`relay.py:549`'s refusal flag and the runner's
  status read at `runner.py:981` both read "running" then).
  - **Executed inputs** (`review_exit_timing.py`): the interval measured 0.05 to 0.14 ms at the median and at most
    8.1 ms; 240 of 240 relay refusals were flagged and 32 of 32 runner runs settled `ended-without-result`, so no
    run hit it.
  - **Effect:** it is the contract's rule (no proof, no exit). The PR's "Timing alone never decides" could be read
    as saying outcomes never depend on timing.
  - **Fix:** the invariant sentence in check 6. Optional hardening if the hub wants the window closed: after a
    write's EPIPE with no exit shown, re-peek for a short bound (say 100 ms, checking stop's cut) before building
    the refusal. A live child that closed stdin would still read `running`, only later.
- **P3-3, label: `EXIT_PEEK` names "libc waitid" where no binding exists.** That is a platform with neither
  `os.waitid` nor macOS (`relay.py:257`), which no CI job or lane reaches.
  - **Executed input:** `test_without_either_binding_the_relay_has_only_popen_s_evidence`.
  - **Fix:** `EXIT_PEEK = "os.waitid" if hasattr(os, "waitid") else "libc waitid" if _libc_waitid is not None else
    "none"`, or keep it and say so in C-26.4.

## Mutants

Each mutant ran against its named killer on both Pythons, and each control passed unmutated. The holds were
`mut314`, starting 02:52:45Z at load 23.2, and `mut312`, starting 02:57:14Z at load 26.4. In each, `fix_r3_mutants.py`
gave 24/0/0, `named_mutants.py` 10/0/0 (its informational property report deselected, as in rounds 3 and 4), and
`review_mutants.py` 14/0/0.

| Mutant | Killer | 3.14 | 3.12 |
|---|---|---|---|
| Helper without the peek (r3) | r3's wait-lock regression (60 s wait) | killed | killed |
| A peek that reaps (r3) | `test_a_peek_never_changes_the_status_the_guardian_records` | killed | killed |
| `status` left on `poll()` | `test_every_relay_site_reads_an_exit_the_guardian_wait_hides` | killed | killed |
| Refusal flag left on `poll()` | the same | killed | killed |
| Signal delivery left on `poll()` | the same | killed | killed |
| ECHILD read as "not exited" | `test_a_child_the_guardian_reaped_reads_as_exited` | killed | killed |
| Another `OSError` read as "exited" | `test_another_peek_failure_proves_nothing_and_is_logged_once[EIO]` | killed | killed |
| ctypes binding without `WNOWAIT` | `test_a_binding_reads_an_exit_without_reaping_it[exit 5-libc-waitid]` | killed | killed |
| Decision 2's two (r3) | their named killers | killed | killed |
| Round 2's five | their named killers | killed | killed |
| **Review r4:** libc peek reads any siginfo as an exit | `test_live_children_never_read_as_exited[lock-free-libc-waitid]` | killed | killed |
| **Review r4:** flags without `WEXITED` (EINVAL) | `test_a_peek_never_changes_the_status_the_guardian_records` (30 s wait) | killed | killed |
| **Review r4:** a pid of 0 peeked | `test_a_child_without_a_real_pid_is_never_peeked[pid-zero]` | killed | killed |
| **Review r4:** every failure logged | `test_another_peek_failure_proves_nothing_and_is_logged_once[EIO]` | killed | killed |
| **Review r4:** libc failure ignored | `test_a_child_the_guardian_reaped_reads_as_exited[libc-waitid]` | killed | killed |
| **Review r4:** libc binding without `use_errno` | the same | killed | killed |
| **Review r4:** `poll()` dropped from the helper | `test_the_relay_s_poll_reaps_and_stores_the_status_when_the_wait_lock_is_free` | killed | killed |

The property also kills the helper without its peek, the reaping peek, ECHILD read as "not exited" and the ctypes
binding without `WNOWAIT`, on both Pythons.

## Files added

- `tests/fake/test_turn_relay_exit_r4.py`: eight cases (six on 3.12, two skipped with the `os.waitid` reason).
  They are deterministic and pass at `c8904e4b2`: `poll()` reaping while the lock is free, the signal storm, the
  injected EINTR, no binding, a reused pid, and a real guardian wait.
- `docs/reports/2026-10-29-relay-exit-r4/review_exit_timing.py`: the exit-in-progress measurements. They are
  timing-dependent, so they sit outside `testpaths`, and they assert only what holds whatever the timing.
- `docs/reports/2026-10-29-relay-exit-r4/review_mutants.py`: my seven mutants.
- This report and `holds.md`.
