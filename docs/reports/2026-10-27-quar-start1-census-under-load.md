# D-QUAR-START1 D4: start grace's census under load

2026-10-27, on the shared host. Branch `fix/quar-start1-census-217` against `release/217` (`46ab44d72`).

## What failed

D-QUAR-START1 (a), #250 (`c9430763f`), extends an expired start grace while the
guardian is the only verified, running member of the attempt's census, up to
`START_GRACE_MAX_S` (60 s). Fix round 2b's D4 made any unverifiable census end the
extension. At load 94 to 101, `tests/fake/test_quar_start1_daemon.py` failed 5 of 6
runs, at `c9430763f` and at #270's head alike. Each failure ended the extension at a
census whose one error was "marker read unavailable for pid N", with N a process other
than the guardian. The guardian was KILLed while it was alive and still starting, which
is the loss D-QUAR-START1 (a) exists to prevent.

## Which processes refuse the marker read

Every census reads every process's argument buffer (`KERN_PROCARGS2`) for
`SUBFLEET_ATTEMPT=` and `SUBFLEET_ROOT=` (C-5.5's marker source). Three probes used the
census's own reader (`procs.kernel`) and recorded numbers and process names only: errno,
buffer sizes, ages and microseconds. No argument or environment value was printed or
stored.

| Probe | Load | What it did | Result |
|---|---|---|---|
| Whole-host passes | 18.8 to 43.5 | 900 passes over every pid (1,525 to 1,599 pids, 33 ms a pass), each refusal re-read at once and after 50 ms | 20 passes met a refusal, one each (1 in 45) |
| New pids, passive | 20.6 | 20 s watching every new pid from its first sight | 46 of 1,668 new pids refused at least once |
| New pids, with spawning | 21.2 | the same for 15 s while the probe started 1,500 children | 127 of 2,480 refused at least once |

Every refused process was:

- **This user's.** There were 20 of 20 in the whole-host passes, and the new-pid probes saw no other user's.
- **A newborn.**
  - The whole-host refusals were 0.1 to 2.0 s old by `ps` start time, which has one-second resolution.
  - In the new-pid probes' 24 recorded examples, the first refusal came 0.3 to 1.9 ms after first sight. The refusal had ended by 7.1 ms at most.
  - Every one of the 173 refused new pids then read whole, or had gone. None was still refused when its probe ended.
- **Refused with EIO or EINVAL.**
  - The whole-host passes saw 14 EIO and 6 EINVAL.
  - In the new-pid probes, an immediate raw re-read after the census's refusal gave 136 EIO and 20 EINVAL. For the other 17 that re-read already succeeded, so the window had closed within microseconds.
- **Not a full buffer.** Where the kernel reported the size (14 of 20), it was 3.3 to 7.3 KB against the census's 1 MiB buffer. None was cut short.

By name, across all three probes: bash 82, zsh 28, true 18, python3.14 17, sh 11,
claude 4, codex 3, cargo 3, sqlite3 2, awk 2, signal-cli 2, python3.13 2, and one each
of clang, sandbox-exec, gh, Python, security, sed, jq, build-script-build, grep, git,
date and env. Seven had gone before their name was read.

That is the shell and tool churn of the host's other sessions. Nothing persistent
refuses its read, and nothing this attempt starts was among them. The probes show
*when* a read is refused (in a process's first milliseconds), not the kernel's reason;
XNU's source was not read.

At 1 in 45 passes, and with the extension taking a census every second, a start
extended for 10 to 20 s meets such a census often. At load 94 to 101 it met one in 5
of 6 runs.

## A second cause: the guardian's own identity readers

Before `start.json`, the guardian learns its boot id and start time through
`procs._read`, which runs `sysctl` and `ps` as its children in its process group. Timed
30 times at load 31, they took 5.5 ms (`sysctl`) and 8.6 ms (`ps`) on average, 48.7 ms
at most.

A census that lands while one is alive finds two members in the guardian's group, walk
and responsibility set. So `live_pids != {guardian}`, and the extension ended for a
healthy guardian here as well. If the census lands in that child's first milliseconds,
its marker read is refused too.

## Decision

D4 should not end the extension for these two cases, and it still ends it for
everything else.

The extension decides nothing:

- It signals, ends and releases nothing.
- It is bounded by `START_GRACE_MAX_S`.
- It only defers the branch below it, which takes its own census when it runs.

Every verdict (release, loss, quarantine, the kill protocol's end) still takes a
verifiable census.

A census whose only errors are other processes' failed marker reads still vouches for
the guardian. It read the group, the walk, the responsibility source, every identity
and the guardian's own marker. The only unknown is whether the unread processes carry
this attempt's marker.

Before `start.json` nothing should carry it:

- **The guardian has launched nothing.** The daemon reads `start.json` again after the
  census and before it asks whether to extend (`_process_attempt`).
- **The guardian publishes `start.json` before it starts the provider.** So a provider
  alive at the census means a `start.json` that read finds.
- **Only the identity readers run before `start.json`.** The test runs the guardian in
  process with its real readers. `sysctl` and `ps` are exactly the processes it starts
  before `start.json`. Neither carries a marker: `_read` gives them four variables (LC_ALL,
  LANG, TZ, PATH). The provider, started after `start.json`, does carry them.

The guardian's environment is marked. A child is unmarked only because `_read` replaces
its environment, or because its read was refused.

The daemon also runs hook processes with the markers in `_publish_self_test`. That
happens synchronously, before the guardian is spawned, so none is alive at a start
census. The argument does not rest on it: the extension's safety comes from deciding
nothing, and the premise is supporting evidence.

### The rule (C-4.2, `Daemon._start_extends`)

An expired start grace extends, until the bound, only while:

1. **The census is verifiable, or its only errors are failed marker reads.** Those reads
   must be of processes other than the guardian, and the census must name them
   (`Containment.only_markers_unread`, `markers_unread`).
2. **No parent is held.**
3. **Every live member other than the guardian is one of its identity readers**
   (`Daemon._identity_readers`). A reader is a member that:
   - has the guardian as its parent in the table;
   - is in the attempt's recorded group, and is one of the census's group members;
   - is neither stopped (`T`) nor a zombie (`Z`);
   - was not found marked.
4. **The guardian matches its record and is running.** It is the attempt's recorded
   identity (pid, boot, start), and is neither stopped nor a zombie.

Each of these still ends the extension:

- a failed read of the guardian's own marker;
- any other error (table, identity, responsibility or enumeration);
- a census that names no unread pids;
- a marked child, a grandchild, or a child in a group of its own;
- a stopped child, or a member the table did not shape;
- a held parent;
- the bound.

The census stays unverifiable exactly as before. `markers_unread` is not part of the
evidence a census records (`to_dict` is unchanged).

The first `attempt.start_extended` event names what it admitted (`markers_unread`,
`identity_readers`). A refused extension is now logged at INFO, with pids and error
texts only. The failed base run below shows why: nothing in its state said which census
ended its extension.

## Invariants

These hold for every input. The tests below execute each one.

1. **Naming is exact.** `markers_unread` is the set of live same-user processes whose
   marker read was refused.
2. **Verifiability is unchanged.** A census is unverifiable iff it has an error.
3. **The narrowing is exact.** `only_markers_unread` holds iff the census's errors are
   exactly those reads' errors.
4. **A refused read is never treated as marked.** A process whose read was refused is
   never in `marker_pids`.
5. **The readers are exact.** `_identity_readers` is the set of live members, other than
   the guardian, that the reader rule above admits.
6. **The extension is exact.** It is granted iff every condition of the rule holds, and
   never at or past `START_GRACE_MAX_S`.
7. **Nothing extended is signalled.** A refused extension ends as before: the abandon is
   written before any signal, the group is KILLed, and the attempt ends `failed`,
   `starting-no-receipt`. An unread stranger is never signalled.
8. **The guardian's pre-start children are exactly the readers.** Before `start.json`
   the guardian starts exactly `sysctl` and `ps`, neither carrying a marker. After it,
   the provider carries them.

## Tests

`tests/unit/test_quar_start1_census.py`, simulated host (`tests/kill4_world.py`) unless noted:

- **The census names what it could not read** (invariants 1–4): a Hypothesis property
  over worlds of up to six strangers, each unread or not, marked or not, of this user or
  another, with or without the guardian's own read refused and a failed table (120
  derandomized examples).
- **The 2026-10-27 shape extends:**
  - a stranger is refused on every census;
  - four censuses send no signal;
  - the event names the stranger;
  - `start.json` then lands and the attempt runs.
- **Other unverifiable censuses still end at the grace** (invariant 7): the guardian's
  own read refused, another error, an unread non-child group member, and past the bound.
  Each takes the full path: abandon first, group KILL, `failed` `starting-no-receipt`,
  and the stranger never signalled.
- **The extension property** (invariant 6): stopped guardian, another member, elapsed
  0–120 s, any set of eight error kinds, named or not, and a bare unverifiable census
  (200 examples and 3 pinned).
- **The guardian's identity reader does not end a slow start**, read whole or refused.
  The event names it, it exits, and `start.json` lands.
- **No extension beside any other member:**
  - a marked child;
  - a stopped child;
  - an unmarked grandchild, and a marked one;
  - a child in a group of its own.

  Each ends `failed` `starting-no-receipt` with the group KILLed.
- **The reader property** (invariants 5–6): up to four members, each varying parent,
  group, state (S, R, U, T, Z, empty), marked, group membership and shape, with or
  without a held parent (300 examples).
- **The premise** (invariant 8), executed with real `sysctl` and `ps`:
  - the guardian runs in process;
  - every process start goes through a recorded `subprocess` hook;
  - any other start fails the test.
- **Mutation controls**, each a strict xfail on its named test:
  - any unverifiable census extends;
  - the guardian's own failed read is tolerated;
  - the census does not record its unread pids;
  - no readers are admitted;
  - a marked child is admitted;
  - a stopped child is admitted;
  - a grandchild is admitted;
  - a child in another group is admitted;
  - `only_markers_unread` holds whenever any read failed.

At base (`46ab44d72`), the stranger test ends `quarantined` instead of extending.

### The real daemon under load

`tests/fake/test_quar_start1_daemon.py` runs a real daemon, guardian and provider, with
the guardian held before its `start.json` by the harness's `--start-delay`. The runs
were one at a time, under `suite_hold.sh`:

| Code | Runs | Load (1 min, at each run's end) | Failed |
|---|---|---|---|
| base `46ab44d72` (the starting session's runs) | 6 | 94 to 101 | 5 |
| base `46ab44d72`, alternating with head | 6 | 45 to 78 | 1 (load 78) |
| head, D4 only (`60751b478`, some alternating) | 8 | 28 to 88 | 0 |
| head, D4 and readers (working tree, alternating) | 4 | 48 to 63 | 0 |
| head, D4 and readers (`58ea5083e`) | 10 | 53 to 71 | 0 |
| base, under churn, alternating | 4 | 66 to 74 | 1 |
| head `58ea5083e`, under churn, alternating | 4 | 59 to 71 | 0 |

"Under churn" means `churn.py` (in the evidence folder) started about 100 short-lived
`/usr/bin/true` processes a second for the length of the run. It barely moved base's
rate at this load.

Pooled at the load this host gave during this work (28 to 88):

- **Base failed 2 runs of 10.**
- **Head failed none of 26:** 18 with the readers and 8 with D4 only.

If head failed at base's rate, 26 passes in a row would happen with probability
0.8^26, about 0.003. The deterministic evidence is the unit tests: the 2026-10-27 shape
ends `quarantined` at base and extends at head.

The base failures leave no record of which census ended their extension. Head now logs
it at INFO (`start grace of … not extended: errors …, members …`), so a failure at head
under load would name its cause.

All run logs, summaries, kept roots, probes and the churn helper are in
`~/reviews/subfleet-quar-start1-census-2026-10-27/runs/`.

### The broad suite

A broad run covered 42 files: the kill4, census, quar, guardian and daemon-settle unit
files, `tests/fake/test_state_contract.py`, `tests/fake/test_quar_start1_daemon.py`,
`tests/fake/test_kill4_terminal_daemon.py`, two process tests and the module-name and
invariants-index guards. It gave 610 passed, 50 xfailed and 2 failed.

Both failures were `tests/fake/test_kill4_terminal_daemon.py::test_a_counted_loop_driver_is_contained_with_its_children`,
on its assertion that the loop driver is among the attempt's counted identities. That
test does not reach start grace: its attempt is `running` within a second. Run
alternately at load 67 to 74, it failed 5 of 6 cases at base and 3 of 6 at head, on the
same assertion. It is a failure under load that predates this change (see Residuals).

## Residuals

- **A responsibility-only member with no shape still ends the extension.** That is a
  process the responsibility source attributes but the table did not shape. The readers
  are shaped whenever the table saw them; a reader that is born after the table and seen
  only by responsibility is refused.
- **Other census consumers still meet these refusals.** The kill protocol's end, release
  and quarantine all treat a census with a failed newborn read as unverifiable, as they
  should. Under load that can turn a clean end into quarantine. A bounded re-read of a
  refused newborn, inside the census and for every consumer, would remove the transient
  at its source. That is a separate change: it alters every verdict's evidence, not only
  the extension.
- **The counted loop-driver test fails under load at base.** In
  `tests/fake/test_kill4_terminal_daemon.py::test_a_counted_loop_driver_is_contained_with_its_children`,
  the driver is recorded as owned, but only its child had been counted when the wall or
  the receipt ended the attempt. Evidence is in
  `~/reviews/subfleet-quar-start1-census-2026-10-27/counted-driver-flake/`. It is out of
  this change's scope.
