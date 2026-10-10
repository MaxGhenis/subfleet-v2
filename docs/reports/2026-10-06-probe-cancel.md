# Admission probe cancellation, audit A1

Probe reservation now reads the job inside `probe.reserved` and refuses a cancellation stamp or terminal state (`subfleet/daemon.py:4137`). After guardian identity has committed, `probe.gate` reads the job again and writes `1` or the refusal byte `0` while still holding the transaction (`subfleet/daemon.py:3994`). The existing containment and completion path releases a refused probe's lease after verified containment; uncertain containment retains it in quarantine. C-7.4 and its version 3 change entry document the guarantee (`docs/acceptance-contract.md:219`, `:9`). The existing durable-identity gate test now expects the gate transaction (`tests/fake/test_probe_recovery.py:175`).

The reservation audit found one attempt insertion, shared by all work paths. Its `attempt.reserved` transaction re-reads the job and checks both cancellation and terminal state before taking leases or inserting the attempt (`subfleet/daemon.py:4740`). No additional production reservation fixes were needed.

| Path checked | Reservation boundary |
| --- | --- |
| Ordinary detached jobs and retries | Shared `attempt.reserved`; retry routing still reaches the same check. |
| Pilots | Pilot marks affect eligibility; the pilot is an ordinary attempt reserved by the shared transaction. |
| Resumes | Shared transaction, including the native-session lease at `daemon.py:4839`. |
| Revives | Shared transaction, including native-session and revive leases at `daemon.py:4839` and `:4884`. |
| Gate-review rounds | `gate/service.py:231` prepares a round and submits an ordinary job; the round lease at `daemon.py:4854` is acquired in the shared guarded transaction. |
| Conversation turns | Conversation eligibility precedes the shared guarded transaction. |
| Admission probes | Fixed reservation and gate transactions above. |
| Probe recovery | `daemon.py:4066` contains existing recorded probes and completes them; it reserves nothing and opens no gate. |
| Timer and enrollment probes | `timers.py:519` and `daemon.py:898` reserve lane work without a job row. They use their own cancellation/stopping signals; job cancellation does not apply. |

The verifier's original files were copied into this workspace temporarily. `bash .invariants-audit/run_verifier.sh probes` ran against the original daemon at prerequisite `6b0be1d471e93d82fa88dd459b8d69c877f32d68`: **4 characterization tests passed** in 45.51 s. The same cases ran against the fix, updating only the two cancellation expectations and closing unused fake pipes: **4 tests passed** in 43.22 s. No verifier evidence files are committed.

| Minimized after-routing cancellation | Before | After |
| --- | --- | --- |
| Cancellation stamp / state | Set / cancelled | Set / cancelled |
| Reserved admission probes | 1 | 0 |
| Guardian calls | 1 | 0 |
| Gate byte | `1` | No write |
| Reserved work attempts | 0 | 0 |

`tests/fake/test_probe_cancel.py:330` defines a Hypothesis `RuleBasedStateMachine` over submit, route, cancel, reserve probe, open gate, contain, restart, and reserve attempt. It uses a real Store and daemon, FakeAdapter, real pipes, a fake guardian, and production containment with synthetic process census results. Its oracle observes the job at transaction entry, newly persisted reservation records, and the job at each gate write. Cancellation can be injected after routing or immediately before the real `BEGIN IMMEDIATE`, so reading outside a transaction is distinguishable from reading inside it.

The invariant (`test_probe_cancel.py:394`) requires no `1` gate byte when the cancellation stamp was already set, and no probe or attempt reservation when cancellation or terminal state was present at reservation transaction entry. The state machine is configured for 60 histories of up to 25 operations, deterministic generation, no example database, and no deadline. Histories begin either empty or with a probe reserved through real submit, route, and reservation operations, ensuring gates and containment are reachable. The minimized verifier input has its own example at `test_probe_cancel.py:198`; positive gate opening, refusal, terminal states, cancellation stamps, quarantine, and six shared work paths have deterministic examples too.

Validation: the selected regression run passed **48 tests** in 511.90 s. After improving generated coverage and adding the running-attempt/pending-cancel regression, the final invariant-file run passed **24 tests** in 82.02 s. This covers **49 distinct targeted cases** in total. Hypothesis generated **60 passing histories, zero failures, and 11 discarded cases**, and stopped at `max_examples=60`; all eight operation events appeared, including gate opening (18.31%) and containment (19.72%). The final file can be run with `pytest -p no:cacheprovider -q --hypothesis-show-statistics tests/fake/test_probe_cancel.py` under the required temporary-directory environment. The selected existing tests covered probe recovery, cancellation/terminal work admission, pilots, native resumes/revives, round leases, and timer publication.

Mutation checks: **4/4 mutants detected**, each by one focused example and one expected pytest failure. The mutation runner restored the daemon exactly afterward.

| Mutation | Example | Result |
| --- | --- | --- |
| Remove reservation re-check | Minimized after-route cancellation (`test_probe_cancel.py:198`) | Reservation invariant failed; 1 failed in 24.08 s. |
| Remove gate re-check | Cancel before gate (`test_probe_cancel.py:260`, `before-gate`) | Gate invariant failed; 1 failed in 25.03 s. |
| Move reservation read outside transaction | Cancel at transaction entry (`test_probe_cancel.py:239`) | Reservation invariant failed; 1 failed in 24.09 s. |
| Move gate read outside transaction | Cancel at gate transaction entry (`test_probe_cancel.py:260`, `probe.gate`) | Gate invariant failed; 1 failed in 21.28 s. |

Every run used a fresh `TMPDIR` under `getconf DARWIN_USER_TEMP_DIR`, with no `tmp` path component and nothing under HOME, and removed it afterward. Pytest processes ran one at a time, without `-n`; the full suite is left to CI. No live Subfleet state, default policy file, provider process, sub-agent, history rewrite, or push was used.

Shared Git metadata rejected `index.lock` with “Operation not permitted”. Commits therefore use `.git-local` on `fix/probe-cancel-recheck`. Delivery is `docs/reports/2026-10-06-probe-cancel.bundle`, whose head is `refs/heads/fix/probe-cancel-recheck` and whose prerequisite is `6b0be1d471e93d82fa88dd459b8d69c877f32d68`. Verification checked the exact prerequisite and head, imported the bundle into a fresh bare repository using only the original repository for prerequisite objects, and compared every changed file with this workspace. Implementation commit: `8c4e491931f3d3b94bae1e42c58c676003e3d7d9`. Both new commit messages end with the required Claude Opus 5.5 co-author trailer.
