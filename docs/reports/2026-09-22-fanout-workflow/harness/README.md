# Offline harness for subfleet-fanout.js

Needs `bun`, `python3` and `zsh`. From the repository root, run:

```sh
env -u PYTHONPATH bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs .claude/workflows/subfleet-fanout.js
```

The validator checks arguments and result shaping, renders the commands with
a stub `agent()`, and executes every step through `zsh -c 'eval "$1"'`,
as the review did. It prepends fake Subfleet and Git to `PATH`, sets
`SUBFLEET_HOME` and `ZDOTDIR` to an empty temporary directory, sets
`SUBFLEET_RUN_DETACH=0` to verify explicit `-d`, and unsets `PYTHONPATH`.
It returns a nonzero status for any failed assertion and cleans up with
Python's `shutil.rmtree`, including on failure. No real jobs are submitted.

Round-3 regressions cover all seven findings: marker-driven retries capped
at three calls; terminal whole-manifest refusals; recovery of an earlier
unknown job from a digest-conflict answer; failed setup without stray files;
execution of a safely quoted reset command; no retries triggered by refused
entries; and citation/cache-token checks against the report and CSV.
The earlier scenarios remain covered, including wait failures, nameless rows,
checksums, state mismatch, separate state keys, skipped outputs and the wait
sleep cap. The report records the baseline and final results.

Round-4 regressions use a request-id registry to prove that jobs exist after
lost answers. They cover unknown → not-sent → digest conflict; committed
jobs hidden by a later missing-prompt validation refusal; unavailable, empty
and malformed request-id lookups; recovery from round-3 refusal state on
relaunch; and recovery of only the missing id after mixed outcomes. The
expanded harness has 28 checks, including all 23 existing checks. Against
the exact workflow at `c95d10809`, 24 pass and four fail; the round-4 workflow
passes all 28. The registry and recovery evidence stay in temporary folders
and are cleaned up after the run.

Round-5 regressions reproduce both findings at `91644430b`: an earlier
refusal followed by an unanswered invocation that commits the job, and
request-id reuse by a different manifest followed by validation refusal.
The fake registry now stores immutable prompt bytes and enforces C-6.2
payload conflicts. Recovery must verify the daemon's recorded identity
against an unanswered invocation's snapshot before adopting its id.
The expanded harness has 37 checks, retaining all 28 existing checks.
Additional checks exercise per-invocation prompt changes, same names/paths
with different prompt bytes, verified digest-conflict recovery, all ten
required metadata fields missing or mismatched, nested online and flat
offline show responses, cleared caller-session identity, and rejection of
legacy recovery records without identity proof. Evidence remains temporary.

Round-6 regressions reproduce all three findings at `cdb451ff9`: synchronized
calls overwriting one submission log, head-only and policy-only request-id
collisions, and an original prompt edited between snapshot and submission.
The four finding checks fail on that exact workflow. The expanded harness
has 46 checks, retaining all 37 earlier checks. It also kills a lock-owning
launcher while the child submits, compares snapshots with the repository's
pure `subfleet.ids.payload_digest`, tests unavailable policy proof, and
recovers an original unknown after both head and policy move. Missing and
mismatched show metadata now cover commit, policy and payload digest too.

Round-7 has 55 checks, keeping all 46 earlier checks and adding six finding
regressions and three proof/lock checks. HEAD-only and policy-only changes
at the actual submit boundary are tested with lost stdout and ordinary
refusal/unknown rows. Both `sol` and `fable` are resolved by the repository's
pure CLI/daemon model resolution before the fake hashes or stores a job.
Aliases recover after a lost answer, a deleted original prompt and a
whole-manifest validation refusal, including on relaunch.

Recovery now requires the creating invocation's unique batch label, recorded
by the daemon alongside the job. It recomputes the digest using the daemon's
recorded HEAD/policy and the staged prompt bytes/canonical model. An older
job from another state cannot settle a lost dedupe answer. The prompt-byte
fixture therefore loses this invocation's creation answer before editing
the original prompt; it retains the byte, conflict and relaunch assertions.
Offline show omits event metadata on main: the workflow reads the batch mark
from the daemon-written manifest beside its stored prompt, bound to the
shown job id, request id and digest. The fake reproduces both show shapes.

The crash probe kills the launcher and helper while a fake CLI retains the
inherited lock. Acquisition returns exit 75 and `LOCKED` within an explicit
budget (default/max 5 seconds, shortened with
`SUBFLEET_FANOUT_LOCK_WAIT_S`). `launcher_pid` names the last launcher; a
surviving child may be the actual holder. After that child exits, the same
lock inode can be acquired and the saved evidence recovered.

`replay.py` ships the reviewer's independent sequential oracle: seed 12505,
20,000 histories and 113,703 entry transitions. Its concurrent mode runs
64 histories with two actual generated step-1 commands released from a start
barrier. Seed 12506 chooses mixed success, unknown, refused, not-sent and
whole-manifest-refusal scenarios. It checks registry existence against
reported ids, one immutable log per batch CLI call, and preservation of ids
and log bytes on relaunch. Driver progress is streamed; individual processes
still have timeouts. The complete validator runs both modes automatically.
Concurrent mode also executes seed 12507: 16 histories changing HEAD/policy
after snapshot capture and eight histories using `sol`/`fable`, 48 additional
overlapping invocations. A submission barrier makes the first lose its answer
while the second waits for the lock. Registry payloads and creation metadata
are the oracle; immutable logs and settlement are checked again on relaunch.

Select the six round-7 regressions with `FANOUT_TEST_FILTER='round 7 finding'`.
Select concurrent perturbations alone with
`FANOUT_TEST_FILTER='randomized concurrent' FANOUT_REPLAY_CASE=head`
(or `policy`, `sol`, `fable`). All six finding checks and each of those four
concurrent selections reject the exact `8d074a715f29` baseline. For example:

```sh
FANOUT_TMP=$(mktemp -d)
git show 8d074a715f29:.claude/workflows/subfleet-fanout.js > "$FANOUT_TMP/baseline.js"
env -u PYTHONPATH FANOUT_TEST_FILTER='round 7 finding' bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$FANOUT_TMP/baseline.js"
env -u PYTHONPATH FANOUT_TEST_FILTER='randomized concurrent' FANOUT_REPLAY_CASE=head bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$FANOUT_TMP/baseline.js"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$FANOUT_TMP"
```

Both validator calls should exit 1 (six failures, then one failure).
`FANOUT_REPLAY_CASE=perturbations` runs all 24 added histories without the
original 64. Every added history captures actual attempted digests separately
from the helper's snapshots and compares them against registry records.

To select only the four reviewer regression checks:

```sh
env -u PYTHONPATH FANOUT_TEST_FILTER='round 6 finding' bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs .claude/workflows/subfleet-fanout.js
```

To reproduce them on the baseline, from the repository root (exit 1 is
expected, with zero passed and four failed):

```sh
TMPDIR=$(mktemp -d)
git show cdb451ff9:.claude/workflows/subfleet-fanout.js > "$TMPDIR/baseline.js"
env -u PYTHONPATH FANOUT_TEST_FILTER='round 6 finding' bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$TMPDIR/baseline.js"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMPDIR"
```

For an individual command sequence, from this harness directory:

```sh
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/bin" "$TMPDIR/empty-home"
cp fake-subfleet.sh "$TMPDIR/bin/subfleet"
chmod +x "$TMPDIR/bin/subfleet"
export PATH="$TMPDIR/bin:$PATH" FAKE_STATE="$TMPDIR" SUBFLEET_HOME="$TMPDIR/empty-home" SUBFLEET_FANOUT_RETRY_S=0
export FANOUT_FAKE_REPOSITORY="$(git rev-parse --show-toplevel)"
unset PYTHONPATH
bun render.mjs ../../../../.claude/workflows/subfleet-fanout.js "$TMPDIR" '{"label":"t","requestId":"rq-1","stateDir":"'"$TMPDIR"'/s","items":[{"task":"review","tier":"standard","dir":"/Users/me/repo","promptPath":"/p.md","outPath":"'"$TMPDIR"'/out-a.md","name":"a"}]}'
zsh -c 'eval "$1"' fanout-harness "$(cat "$TMPDIR/step0.sh")"
zsh -c 'eval "$1"' fanout-harness "$(cat "$TMPDIR/step1.sh")"
zsh -c 'eval "$1"' fanout-harness "$(cat "$TMPDIR/step2.sh")"
zsh -c 'eval "$1"' fanout-harness "$(cat "$TMPDIR/step2.sh")"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMPDIR"
```

`SCENARIO` is a comma-separated list:

- `refuse-b`: entry b refused, exit 7.
- `submit-down`: all entries not-sent on the first run, exit 69.
- `submit-down-always`: all entries not-sent on every run.
- `unknown-c`: entry c unknown on the first run, existing on the next.
- `digest-conflict-c`: c refused with a message naming the earlier job.
  Combine with `unknown-c` or `rowless-unknown` to test lost-answer recovery.
- `manifest-refusal`: no rows, exit 2, CLI validation message saying
  "nothing was submitted".
- `manifest-refusal-marker`: the same message with exit 1; still a refusal.
- `rowless-unknown`: commits all jobs to the fake registry, then loses all
  stdout and exits 1 on the first run.
- `lose-stdout`: commits any new jobs (refusing payload conflicts under
  existing request ids), then loses all stdout and exits 1 on every run.
- `check-prompts`: validates that every prompt exists before committing any
  jobs; deleting one prompt produces a whole-manifest refusal.
- `lookup-down-always`: request-id lookups fail with exit 69.
- `lookup-empty`: lookups return no rows with exit 0, as an offline store can.
- `lookup-bad-json`: lookups return malformed stdout with exit 0.
- `wait-down-first`: first wait cannot reach the daemon, exit 69.
- `lost-c`: c finishes lost, exit 125.
- `failed-a`: a finishes failed with rc 1, exit 1.
- `unknown-id`: wait returns no rows, exit 2.
- `no-name`: wait rows omit names.

The fake replays the CLI's row shapes at this PR's base `81d58fe9`.
It rejects a run without `-d`. Rendered commands and the dispatcher prompt
are written only to the selected output directory; do not commit them.
Its `runs show` response is nested under `job` by default, as the daemon
records it. `FAKE_SHOW_FLAT=1` selects the offline shape;
`FAKE_SHOW_MISSING=<field>` and `FAKE_SHOW_MISMATCH=<field>` test absent and
different recorded metadata. The fake rejects `show` if it inherits
`CLAUDE_CODE_SESSION_ID`, so the harness proves recovery cannot acknowledge
the caller's notices.

The fake's `run --dry-run --json` returns a non-creating placement preview;
`calls.preview` counts these separately from `calls.run` batch submissions.
`FAKE_WORKDIR_HEAD` and `FAKE_POLICY_HASH` default to `head-one` and
`policy-one`. The helper's fake Git and the registry use the same commit.
`FAKE_SUBMIT_HEAD` and `FAKE_SUBMIT_POLICY` override only daemon-observed
submission values, leaving the snapshot/preview untouched.
`FAKE_POLICY_PROOF=down|bad|missing` tests unavailable, malformed or incomplete
policy previews. `FAKE_CAPTURE_ATTEMPT=<path>` records prompt hashes read by
the submitting fake, independently of helper snapshots. All these files stay
in the validator's temporary fixture directory.
`FAKE_CAPTURE_PAYLOADS=<path>` captures each actual attempted payload's digest
and inputs, including refused attempts. `FANOUT_FAKE_REPOSITORY` locates the
pure policy module; the validator supplies it automatically.
