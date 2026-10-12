# Provider active-attempt cap (D-RT1)

Verdict: implemented and committed; pytest validation remains pending because
the shared test lock was occupied throughout three bounded queue attempts.

Policy: `providers.<provider>.max_active_attempts` accepts a positive integer or
null. An absent key is null (off); shipped default policy bytes remain unchanged.
The chosen operating value remains Max's decision.

Implemented invariants:

- Reservation counts the provider's `reserved`, `starting`, `running`, and
  `finalizing` attempts across all lanes inside the reserving transaction.
- Detached jobs and conversation turns both count and are held at the cap:
  D-RT1's evidence counted turns, and both use the affected provider runtime.
- Probes are exempt from this provider cap.
- Held jobs acquire no lease, preserve admission order with their peers, and
  explain the exact policy key and provider count in `why` and status.
- Other providers remain unaffected; policies without the key retain their
  exact-byte hash and prior admission decisions.
- FIFO applies across models and lane pins sharing the provider count. A mixed
  provider chain can still take an available uncapped provider; once it selects
  a capped provider it respects that provider's older peers. Each existing pool
  keeps its order, and attended turns retain priority over detached work.
- An older lease waiter holds no later job back but keeps the last provider
  slot, as the existing fleet cap does. Existing caps continue to apply.

Test counts:

- 31 new unit cases: loader validation and optional-section shapes, positive and
  null values, absent-key byte/hash preservation, and a differential against the
  independent pre-cap scheduler (300 Hypothesis examples configured).
- 24 new fake-daemon cases: N-plus-one admission and release, provider isolation,
  all four live states for turns and detached jobs, turn priority, changes between
  evaluation and reservation in both pools, probe exemption including the timer's
  actual reservation path, FIFO across models/lanes, lease waiters, mixed provider
  routing, and random arrivals/completions (50 Hypothesis examples configured).
- **0 pytest cases executed, 0 passes, 0 failures.** All 55 new cases and the
  requested regression batch remain unrun. Queue windows were 120, 120 and 180
  seconds; each `lockf -k -t ...` exited 75 (`already locked`). No pytest process
  started and no lock was acquired. No detached runner or build remains.
- Static checks passed: all nine changed Python files parse; `git diff --check`
  is clean; the shipped default policy is byte-identical to `24be965c5ce8`.
  Its SHA-256 remains
  `47e1c5111b9cef28751257667cd8aa2eb9ba0ac39ef7e55beff30879a429e49c`.

The final attempted batch, for the hub to rerun under its own shared-lock hold:

```sh
export TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-provider-cap-$$/
mkdir -p "$TMPDIR"
/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q \
  tests/unit/test_provider_active_cap.py tests/fake/test_provider_active_cap_fake.py \
  tests/unit/test_policy.py tests/unit/test_scheduler.py \
  tests/unit/test_scheduler_turns.py tests/fake/test_admission_lease_waiters.py \
  tests/fake/test_route_view.py tests/fake/test_admission_visibility.py
suite_rc=$?
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMPDIR"
exit "$suite_rc"
```

Dependencies were installed into the workspace `.venv` in the foreground.
Every task-specific temporary directory was removed with the required Python
cleanup command. The cap remains off; no operating value was chosen or enabled.

Git: the shared worktree index is read-only in this sandbox. Commits use
workspace-local metadata `.subfleet-build.git` on `feat/provider-active-cap`,
with the original object store as a read-only alternate. The initial report is
committed as `b68959f0f`, and the implementation, contract and tests as
`478848c13`. Only named source, test and Markdown files were staged. No remote
was contacted; no caller checkout, live state or live process was changed.
The local branch can be inspected with
`git --git-dir=.subfleet-build.git --work-tree=. log --oneline` from this workspace.
The original shared worktree HEAD remains unchanged; import the commits from
this local metadata when collecting the build.
