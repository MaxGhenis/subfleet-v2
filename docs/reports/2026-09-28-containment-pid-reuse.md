# Containment and reused process identities — 2026-09-28

Containment now receives the guardian's launch identity and the attempt's
previously recorded `owned_identities`. Attempts, probes, cancellation,
finalization and quarantine resolution all use that evidence.

## Ownership rule

Guardian and child roots seed ancestry only when their snapshot boot/start
identity matches the recorded identity. A known mismatch also prunes a
descendant chain and excludes that pid from group membership. `reused_pids`
records these mismatches separately from writers. Matching zombie roots and
intermediate parents can connect live descendants, but zombies are not writers.

Group members remain attributable on the recorded boot after the leader exits:
POSIX reserves the group id while the group has members. A snapshot process at
the leader pid with a different identity proves that the original group emptied
and the id was reused, so the whole new group is excluded. A different boot also
invalidates group ownership. Missing or unreadable identity evidence needed to
make a decision makes the census unverifiable.

A live process carrying both `SUBFLEET_ATTEMPT` and `SUBFLEET_ROOT` always counts,
including a process whose recorded identity differs. Inspection failures prevent
release. Snapshot rows provide identities directly; missing start data fails
closed rather than being replaced by a later per-pid read. Evidence serialization
sorts pid maps and errors.

## Invariants and tests

`tests/unit/test_containment_identity.py` exercises:

| Invariant | Checks |
| --- | --- |
| I1: A different recorded boot/start identity is never a writer unless marked. | r218 a2's reused child in a foreign group; reused guardian with forks; reused group id; mixed genuine/reused pids; boot changes; descendant pruning; generated ownership cases. |
| I2: A live marked process always counts. | Marker override, state-root scoping, marked escapes, incomplete identity rows, and generated ownership/failure cases. |
| I3: Every live descendant through a verified ancestry chain counts. | Several generations, escaped groups, zombie parents, and an independent ancestry-path oracle. |
| I4: Every failed inspection keeps the census unverified. | Table, marker, boot, legacy boot, late-marker stat/identity, missing recorded identity and missing snapshot start data failures; generated failure cases. |
| I5: Fixed inspection inputs produce deterministic results. | Both property tests reverse table, marker and recorded-map order and compare serialized evidence byte for byte. |
| I6: Reused pids alone permit normal daemon finalization. | `tests/unit/test_daemon_pid_reuse.py` runs `_finalize` with fake adapters and a temporary store for reused-child and reused-guardian/forks cases; both succeed with rc 0, reach salvage and never quarantine. |

The Hypothesis tests run 250 ownership examples and 100 failure examples.
Additional daemon tests cover probe containment and quarantine release with
recorded identities. Existing process tests retain their signalling checks.

## Signalling

Signalling behavior is unchanged. `signal_group` freshly checks the recorded
leader with `same_process` and confirms that its pgid equals its pid before
`killpg`. `signal_process` checks the previously recorded identity before `kill`.
`same_process` accepts only an `alive` liveness result; mismatches, zombies,
missing processes and unknown inspections cannot authorize a signal. Synthetic
tests assert that reused identities cause no signals.

## Why r218 a2 remained finalizing

Only read-only SQLite (`mode=ro`) and targeted `grep` of `daemon.log` were used
against the live state root. The attempt's events were:

| Event | UTC time on 2026-09-28 | Event sequence |
| --- | --- | --- |
| `attempt.finalizing` | 01:42:09 | 785224 |
| `job.cancel_requested` | 14:56:37 | 839896 |
| `attempt.quarantined` | 22:11:51 | 885118 |

Log lines 693012–693016 show repeated `SalvageError` failures from finalization;
line 698964 reaches 512 consecutive failures with a 60-second retry delay.
Line 701598 still shows salvage failures beside 19:14:15Z context, and later
failures persist beside 21:56Z context. This was repeated salvage failure, not
an extended exit-settle window.

`_process_attempt` dispatches a finalizing attempt to `_finalize` before its
cancellation branch. `_finalize` calls `_salvage` before applying cancellation;
`SalvageError` escapes and `_schedule` retries indefinitely with capped backoff.
The underlying Git error is not recoverable from the permitted evidence: the
logger intentionally records exception types without their messages, and the
attempt/events contain no structured cause. Changing salvage retry or
cancellation behavior needs separate investigation; it is not changed here.
This particular job ended cancelled (rc 130), while its attempt was quarantined;
the uncancelled incidents can instead become lost (rc 125).

## Validation environment and delivery

Independent focused verification with `uv run pytest` on CPython 3.14.7
free-threaded passed: **88 passed in 159.52 seconds**, with no warnings.
The run covered `test_containment_identity.py`, `test_daemon_pid_reuse.py`
and `test_procs.py`, used `SUBFLEET_LIVE=0` and a separate short temporary root,
and saved `.uv-cache/containment-independent-focused.xml`. `git diff --check`
passed. An independent source review confirmed the census wiring, ownership
rules, and unchanged signalling guards; a separate read-only review confirmed
the stall evidence and cancellation ordering.

Full-suite collection on CPython 3.13.9 found **7,070 tests**. The earlier
free-threaded full-suite attempts did not finish and are not counted as complete
runs. A subsequent CPython 3.13.9 run with four workers and a 60-second timeout
also stopped making progress after worker terminations, around 29%, without
writing its JUnit report. The host's observed load average reached 239 during
validation. Those incomplete runs do not establish full-suite counts or prove
that any observed failure is pre-existing.

The current serial retry uses the already cached CPython 3.13.9 environment,
`uv run --offline --no-sync --with pytest-timeout pytest --timeout=60
--timeout-method=signal -v --tb=short`, a short separate `--basetemp`, and JUnit
output at `.uv-cache/containment-313-warm.xml`. Its per-test journal is
`.uv-cache/containment-313-warm.log`. No tests are excluded; live-provider tests
stay disabled (`SUBFLEET_LIVE=0`). Full-suite results and matched baseline
reproduction remain pending. The clean baseline is the exact
`c79aed840404c7ea532189ec0ac9cd0471cefb01` tree in `.uv-cache/baseline`, independently
verified to have no tracked changes; failures must reproduce there before being
called pre-existing. Native Swift tests allow up to 900 seconds themselves, so
the outer 60-second cap can also cause timeout failures.

The sandbox permits workspace files but denies writes to the shared Git
metadata outside this workspace. The assigned checkout's original HEAD and
the caller's branch are therefore unchanged. Commits are preserved on a
workspace-local `fix/containment-pid-reuse` branch in
`.uv-cache/containment-commits.git`, with an importable bundle at
`.uv-cache/containment-pid-reuse.bundle`. That bundle requires the base commit
above. No history was rewritten, no caller files were written, and nothing was
pushed or submitted as a pull request.

Implementation commits are `34771082` (identity-aware census, daemon wiring,
contract and tests), `c343b24c` (the two additional historical incidents), and
`4b6f1559` (ownership guarantees and the salvage-stall finding), and `c8cb29b6`
(incident outcome and workspace-local delivery clarification), followed by
`ff745fbd` (independent focused validation and stall verification). The change-list
entry distinguishes r218's false quarantine from the uncancelled incidents'
lost outcomes.
