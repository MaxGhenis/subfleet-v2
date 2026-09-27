# Census and probes under load, 2026-09-27

From 10:00 to 11:17Z on 2026-09-27, with release 2.1.7 (`324b6d7`) installed and the machine at load 160 to 225, admission placed nothing for 3,074 s. This report says why and what changed: the census's reader (C-5.5, C-5.12), what an unreadable census decides (C-4.2, C-5.5, C-5.6, C-5.9, C-5.10), and the probe deadline (C-11.4).

Every number under "Measured" comes from a command run on 2026-09-27 on the same machine, most with `tools/census_under_load.py`. The observations of the live daemon come from the brief for this work. This session did not read the live state root, signal the installed daemon, or change `~/.subfleet`.

## What was seen on the live daemon (from the brief)

- **Admission idle.** No job was placed for 3,074 s. 26 jobs were queued behind the head job `20260927-055500-cs-research-g01`.
- **`probe.quarantined` events by hour.** 7 and 4, then none for hours, then 2, 44 and 10 in the peak hours. Each recorded `{"errors": ["marker enumeration unavailable"], "unverifiable": true}` and no live pid.
- **The same census, outside the daemon.** A separate process took the census of the same stuck probe record (event 764620, pgid 37317) during the incident, and it came back verified empty with no errors. `ps -axEww` took 0.07 s and returned 1.3 MB for 1,000 processes. Over 75 s, 178 samples had no decode or parse failure.
- **Probe deadlines.** Probes took 45.6 s of their fixed 60 s deadline at load 160. At load 200 and more they were killed at it (rc 143, class `unknown`).
- **Latency after a probe.** One probe exited at 11:15:01Z, but `probe.completed` was recorded only at 11:17:15Z. Its job was placed 3 s after that.

## Why the census failed inside the daemon

### Measured

The daemon was pid 36129 at 11:25Z and 12:05Z, with 144 threads. `ps -M` showed every one at priority 20 (timeshare), the `utility` QoS's; a shell here runs at 31. The guardians it had started ran at 20, and so did their `claude` and `node` children, so the `ps` it starts inherits 20 as well. launchd starts it with `ProcessType` `Standard`. To stand a process in the daemon's place, the tool runs itself under `taskpolicy -c utility`, which put it at 20.

The census's marker source is `ps -axEww -o pid=,command=`. It was read in one process under four conditions, then with the 2.1.7 reader (`pipe`) against the new one (`socket`), interleaved:

| Scheduling | Threads hogging the lock | Load | Reader | p50 | max | Past the 10 s cap | Reads |
|---|---|---|---|---|---|---|---|
| default | 0 | ~130 | pipe | 0.075 s | 0.36 s | 0 of 105 | 81 |
| default | 2 | ~130 | pipe | 0.255 s | 0.61 s | 0 of 73 | 55 |
| utility | 0 | ~107 | pipe | 1.14 s | 2.14 s | 0 of 11 | 69 |
| utility | 2 | ~113 | pipe | 6.06 s | 6.51 s | 0 of 5 | 44 |
| utility | 2 | ~123 | pipe / socket | 6.00 / 2.87 s | 7.74 / 3.78 s | 0 of 7 / 0 of 7 | 44 / 3 |
| utility | 4 | ~122 | pipe / socket | 10.52 / 3.28 s | 11.97 / 4.24 s | **4 of 7** / 0 of 7 | 43 / 3 |
| utility | 4 | ~52 | pipe / socket | 2.64 / 0.44 s | 4.75 / 0.93 s | 0 of 19 / 0 of 19 | 46 / 3 |

`ps -axEww` alone, output discarded, took 0.14 to 0.19 s at the `utility` QoS and 0.08 to 0.10 s at the default.

### What that shows

- **Load alone does not explain it.** A process at the default QoS, as the brief's outside sample was, read the census in 0.075 s at load 130.
- **The time went to the daemon's reads, not to `ps`.** `ps` itself took 0.17 s at the daemon's QoS. The 2.1.7 reader drained 1.3 MB through a pipe that holds 16 to 64 KiB, which took 43 to 81 reads. Each read is a wait for the interpreter lock, and at the daemon's QoS with threads contending for the lock, each wait was long. `ps` could not exit until all but the last piece had been read, so its 10 s cap ran out while it waited on the daemon. That is `TimeoutExpired` inside `subprocess.run`, which the census reported as "marker enumeration unavailable".
- **The table read survived.** The table source (`ps -axo pid=,ppid=,pgid=,stat=,lstart=`, 52 KB) came back in 4 to 6 reads. That fits the incident: only the marker source failed.
- **Content is not the cause.** A child was given environment values holding a newline, a carriage return, `0x1c`, U+2028, U+0085, `0xff` and `é`. In the C locale the reader sets, `ps` printed its row as one line of ASCII (`\012`, `^M`, `^\`, `M-bM^@M-(`, `M-^?`). No parse or decode failure can come from what processes carry.

2.1.7's census kept no cause, so the incident's own records cannot say which failure it was. This reproduction shows the timeout and rules out parse and decode. Four GIL hogs is a stand-in for the daemon's busy threads, not a measurement of them.

## Why an unreadable census cost so much (release/217, read from the code)

- **A probe.** `_contain_probe` treated "not verified empty" as a quarantine. It ran the kill protocol first: a grace of `term_grace_s` (15 s) re-reading the census every 50 ms, then SIGKILL, then one more census. It then recorded `probe.quarantined` and set the job `waiting`/`uncertain`.
- **Recovery.** `_recover_probes` runs at the start of every detached admission pass. For each quarantined probe it took the census again and, while the job waited, wrote another `probe.quarantined` event. So the hourly counts above are events, not necessarily distinct probes.
- **Where probes run.** Admission probes run inside the detached pass (C-26.9). Every census there, at up to 10 s per source under this load, delayed every other detached job.
- **An attempt.** Start grace, a dead guardian, the kill protocol's settle window and C-5.9's exit window each quarantined an attempt on "not verified empty". An attempt's quarantine ends its job `lost` (rc 125).
- **What release/217 already covered.** Its C-5.11 fixes (`246926d`, merged by `d43a326`) defer only the paced liveness inspection of a running attempt. That is the shared table, its boot identity, and a guardian whose liveness is unknown. They do not reach the censuses that decide.
- **The probe deadline.** It was a fixed `after(60)`, set when the probe was reserved. The credential read, the guardian's spawn and identity, and the store's commits, all slow at this load, were taken out of the provider's 60 s.

## What changed

1. **The reader (C-5.5, C-5.12).**
   - `procs._read` gives the reader one end of a socket pair with an 8 MiB buffer (`READ_BUFFER_BYTES`), so `ps` writes an answer up to that size (six times the incident's) and exits without waiting; a larger one is read while it is written.
   - Each wake of the daemon reads everything buffered: 3 reads in place of 43.
   - The 10 s cap (`READ_TIMEOUT_S`) is on the reader. A reader that exited in time has answered however late its output is read; one still running at the cap is killed, reaped and named.
   - Output is decoded without failing on a stray byte. `close_fds=False` stays, so the reader is still started with `posix_spawn`.
2. **The cause is recorded (C-5.5).**
   - Each source that could not be read is recorded with why. Examples: `marker enumeration unavailable: ps timed out: still running after 10 s`; `ps exited 1: <first line of its error output>`; `ps could not start (EAGAIN)`; `ps printed a row that is not a process (row N)`.
   - `stderr_head` keeps only printable ASCII, at most 160 characters, and replaces every word holding `=` or `/`, so no environment entry or command path survives. A Hypothesis test checks this for arbitrary bytes.
3. **An inconclusive census decides nothing (C-4.2, C-5.5, C-5.6, C-5.9, C-5.10).** *Inconclusive* means a source could not be read and none that could shows a live process (`Containment.inconclusive`).
   - **Attempts.** The four deciding censuses raise `CensusDeferred` and leave the attempt's state and leases alone.
     - C-5.10 retries the pass at its backoff (0.5 s doubling to 60 s), and `daemon.log` says `deferred` at warning.
     - `attempt.census_deferred` records the census on the 1st, 2nd, 4th ... deferral.
     - A deferred kill resumes from its SIGKILL.
     - C-5.9's window stays spent, so the first census that then shows a writer quarantines at once.
   - **Probes.** The record stays `containing` and keeps its lease. Its job waits `uncertain`, which holds no later job back.
     - `probe.census_deferred` records the census on the same powers of two.
     - Recovery takes the census again when its backoff is due (`_probe_census_due`), not on every pass. It does the same for a quarantined probe, whose quarantine is now one event.
     - The first census that verifies the probe contained finishes it from its receipt and hands the job back to admission.
     - A census that shows a live process decides exactly as before.
4. **The probe deadline (C-11.4).**
   - An admission probe's deadline runs from when its launch gate opens.
   - It is `caps.probe_timeout_s` (60) at least, stretched to what recent probes needed: twice the wall of one that ended on its own, twice the deadline of one killed at it. Each piece of evidence is kept for 30 minutes, and the deadline never exceeds four times the cap (240 s).
   - A probe killed at its deadline is tried again once, at once, with the longer deadline. A second kill in a row waits the usual 60 s.
   - A killed probe is never `ok`, so no pair is approved without an `ok` probe.
   - At the incident's numbers, 45.6 s at load 160 gives the next probe 91.2 s, and a kill at 60 s gives the retry 120 s.

## Tests

Each of these fails on `origin/release/217` (5376718) and passes here:

- `tests/unit/test_procs.py`:
  - `ps` writes its whole answer before the reader reads a byte; on release/217 the child never finishes writing;
  - a reader that answered by its cap is not timed out;
  - a reader whose output end another process still holds is answered within a second; on release/217 it failed at the 10 s cap;
  - the error names its cause without `ps` text;
  - output that is not UTF-8 is no failure;
  - the census records why a source could not be read, and names a bad row by number only;
  - the `stderr_head` property.
- `tests/unit/test_daemon_settle.py`: start grace, a dead guardian, the kill protocol and the exit window each defer an inconclusive census where release/217 quarantined, and each still quarantines a census that shows a process.
- `tests/unit/test_daemon_inspection_load.py`: through the real `_schedule`, the incident's census backs off 0.5, 1 and 2 s, logs `deferred` on the 1st and 2nd, and decides once `ps` answers.
- `tests/fake/test_probe_recovery.py`:
  - a probe's deferral, its recovery at backoff, and one quarantine event where release/217 wrote one per pass;
  - the deadline's bounds as a Hypothesis property, with exact values at the incident's numbers;
  - the one prompt retry, and that no killed probe is approved;
  - the deadline running from the gate.
- `tests/unit/test_census_under_load_tool.py`: the reproduction still runs and prints no `ps` text.

Beside the tests, the incident's census (`marker enumeration unavailable`, nothing seen) was put through the four deciding sites on each tree:

| Site | release/217 | this change |
|---|---|---|
| C-5.9 exit window | attempt `quarantined`, job `lost` rc 125 | attempt `finalizing`, `CensusDeferred` |
| C-4.2 start grace | attempt `quarantined`, job `lost` rc 125 | attempt `starting`, `CensusDeferred` |
| C-4.2 dead guardian | attempt `quarantined`, job `lost` rc 125 | attempt `running`, `CensusDeferred` |
| C-5.6 kill protocol | attempt `quarantined`, job `lost` rc 125 | attempt `running`, `CensusDeferred` |

## Review

Two independent lane reviews read `2133efd`. The Astra review (`20260927-081407-probe-load-review-astra`, `~/reviews/probe-under-load-2026-09-27/review-astra.md`) asked for four changes, each reproduced before it was reported, and all four are fixed:

1. **High: blank output hid an error.** `_read` kept only the first 4 KiB of `ps`'s error output and read `empty_ok` from it. An exit 1 whose error followed 4 KiB of blank output read as an empty selection, and a census with a live writer could then be verified empty. The error output is now kept from its first byte that is not blank.
2. **Medium: a resumed kill started over.** The dead-guardian path marked the census concluded before the kill it started, so a kill deferred on its own census began again with SIGTERM and the whole grace on every pass, and its deferral count restarted at 1. The kill or the loss that follows now concludes it.
3. **Medium: the ownership commit still ate the deadline.** The gate's deadline was set before the commit of the probe's ownership, so a slow commit still shortened the probe. It is now set again after the commit; the committed value, which only recovery reads, is the one from before it.
4. **Low: the final wait ran past the cap.** A 1 s floor let a reader that had closed its streams run past its cap and still answer. The cap now holds.

Its mutation run also left the timer call sites of `held()` uncovered; they now have caller-level tests. The mutation script (`~/reviews/probe-under-load-2026-09-27/mutate.py`) kills all 30 of its mutants.

## Not changed, and why

- **A probe finished by recovery does not carry its approval into the next pass.** That pass may probe once more. Carrying an approval across passes would change what "a probe was run" means for C-11.4 and C-11.7a, and deferrals should now be rare.
- **The first containment of a probe still runs release/217's grace**, re-reading the census every 50 ms for up to `term_grace_s`. That grace is what lets a census that recovers within it keep the probe's approval in the same pass. Only a retry skips it.
- **The daemon's scheduling is unchanged.** It still runs at the `utility` QoS, as launchd's `ProcessType` `Standard` gives it, and the providers it starts inherit that. Raising it would also raise every provider unless the guardian lowered them, and a Mac thermal guard is known to pause processes above 20% CPU. That is an operator's choice, not this change's.
- **A state root whose path is not ASCII cannot match the census's root marker.** `ps` in the C locale escapes the path (`M-C`), so the marker source never finds that root's processes. The shipped root, `~/.subfleet`, is ASCII.
