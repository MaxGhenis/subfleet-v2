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
`SUBFLEET_FANOUT_LOCK_WAIT_S`). In round 7 the line named only the last
launcher; since round 8 it names the launcher, helper and CLI child, each
with whether it is alive. After that child exits, the same
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

Round 8 adds regressions for review r7's four findings and three nits;
`FANOUT_TEST_FILTER='round 8'` selects all of them, and
`FANOUT_TEST_FILTER='round 8 finding'` selects the finding checks only.

- **Refused re-send.** The CLI re-sends after a lost answer. When HEAD or
  policy moved in between, the refused re-send names the job the first send
  created. `resend-moved-c` reproduces the reviewer's real-CLI row exactly:
  `{"outcome": "refused", "rc": 2, "error": "request id already used with a
  different payload by job job-c"}`. `resend-invalid-c` gives the
  `; job X carries request id R` form. Both must be adopted on creation-label
  proof. The same refusal naming an older state's job must stay `REFUSED`.
- **Model as written.** The fake now refuses a model outside the CLI's
  `MODEL_CHOICES`, read from the repository's `subfleet/cli.py`.
  `FAKE_POLICY_RETIRE=astra:sol61` is a daemon policy that retires `astra`;
  `astra` and `sol` must still be submitted as written.
- **`LOCKED` names holders.** The launcher and helper are killed while a CLI
  child keeps the lock. Steps 0, 1 and 2 must name all three pids, with only
  the CLI alive. Crafted records cover `reused`, a round-7 bare pid and an
  unreadable record.
- **Preview no longer gates snapshots.** Down, malformed and incomplete
  previews still leave unpinned jobs and pins the daemon recorded as written
  provable; only `sol`, which the daemon rewrote, stays uncertain. A hung
  preview (`FAKE_POLICY_PROOF=hang`) is asked once per invocation. A deleted
  snapshot file is named in `LOOKUP_UNKNOWN`. This replaces round 6's
  "unavailable policy proof leaves recovery uncertain across relaunch".
- **Nits.** An invalid `SUBFLEET_FANOUT_LOCK_WAIT_S` falls back to 5 s.
  Recoveries by an older helper are named, with "install between batches",
  rather than silently unknown. The round-7 report section must cite
  `c230d1d8e`, `f3e042953` and `c6975725`.

Round 9 keeps all 68 checks and adds seven regressions (75 total).
`FANOUT_TEST_FILTER='round 9'` selects them. The same seven checks fail
against `3ca9b038e` and pass on the round-9 workflow:

- `fake-ps.zsh` controls process age independently of the host's `ps` access.
  A hanging inspection, a nonzero exit with plausible output, empty failed
  output and malformed output must all leave a live stale PID `unknown`.
  Real `kill(pid, 0)` still establishes existence or death. The successful
  inspection check still distinguishes `alive` from `reused`.
- Held-lock CLI records with bad, missing, future, nonfinite or nonnumeric
  timestamps, and invalid PIDs, retain `LOCKED`/exit 75 and say `unknown`.
  A legacy bare PID has no spawn identity, so its liveness is `unknown` too.
- `recovery_checks.py` ports the six-owned-job slow-show probe using four
  synthetic 15-second timeouts and a controlled monotonic clock. Entries e
  and f keep their refusal, explain the unattempted lookup in that line,
  and contribute to `RECOVERY_SKIPPED=2`. A second case spends the budget
  between request lookup and show, explaining all three unknown entries.
  Both recover on a fresh invocation without submitting again.

The missing/mismatched identity checks still cover all 13 recorded fields
for all three jobs and require complete proof before recovery. They now
replay the recorded fake registry transport with a controlled clock in the
generated helper. This removes the host-scheduling race from repeated fake
CLI startups; staged files, stored prompt bytes, labels, digests, refusal
logs, empty job lists and recovered evidence are still real fixture files.
Generated shell commands still exercise initial submission, validation
refusal, nothing-to-wait and relaunch. Test children are supervised and
joined; the lock probes no longer create detached process groups.

`reviewer_replay.py` ports the reviewer's independent 32-history driver
(seed 20261013), including 31 launcher/helper kills and its separate
completeness oracle. It mixes foreign holders, alias pins, HEAD/policy
movement, preview faults and deleted prompts. It creates no evidence files
and uses only temporary fake registries:

```sh
env -u PYTHONPATH python3 docs/reports/2026-09-22-fanout-workflow/harness/reviewer_replay.py .claude/workflows/subfleet-fanout.js
```

To run the seven regressions on the round-8 baseline (exit 1 expected):

```sh
FANOUT_TMP=$(mktemp -d)
git show 3ca9b038e:.claude/workflows/subfleet-fanout.js > "$FANOUT_TMP/baseline.js"
env -u PYTHONPATH FANOUT_TEST_FILTER='round 9' bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$FANOUT_TMP/baseline.js"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$FANOUT_TMP"
```

Round 10 retains all 75 checks and adds three (78 total).
`FANOUT_TEST_FILTER='round 10'` selects them:

- Successful fake `ps` output `00:99` and 310 nines followed by `:00` must
  leave a held lock `unknown`, print `LOCKED` and exit 75 in all three
  generated workflow steps. No submission occurs.
- `lock_checks.py` checks the complete rendered runner under a real held
  `flock`, with real PID existence checks. It replaces only `ps` transport
  and the wall clock (a fixed float), then checks **4,096 strings** of at
  most 8,192 characters, seed **12510**, across launcher/helper/CLI records:
  **12,288 holder classifications**. A separate split/range oracle checks
  format, component ranges and the total. Invalid inspection must remain
  `unknown`; valid ages matching the recorded spawn are `alive`, while a
  process born after its record is `reused`. Every string must retain
  `LOCKED`/exit 75 without raising an unexpected exception. Cases include
  day-prefixed hour boundaries, unrestricted hours without days, enormous
  numeric fields, integer-conversion guards, epoch boundaries, whitespace,
  separators, NULs and Unicode. No subprocess is needed for this property.

Elapsed seconds and minutes must each have two digits in 0–59, except
Darwin's minutes-only `60:00` at exactly one hour. A day
prefix requires hours, in 0–23; hours without days may exceed 23. Durations
older than the Unix epoch cannot match a positive recorded spawn timestamp
and report `unknown` before float subtraction. Conversion failures also
report `unknown`.

To reproduce round 10 on the exact baseline (exit 1, three failures expected):

```sh
FANOUT_TMP=$(mktemp -d)
git show 4c5e61e2316a:.claude/workflows/subfleet-fanout.js > "$FANOUT_TMP/baseline.js"
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 FANOUT_TEST_FILTER='round 10' bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$FANOUT_TMP/baseline.js"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$FANOUT_TMP"
```

The same filtered call against `.claude/workflows/subfleet-fanout.js` must
pass all three checks. The complete validator includes them alongside the
20,000 sequential and 104 shipped concurrent histories; run the separate
32-history independent oracle with the command above.

Round 11 retains **78 validator checks** and extends the existing property
check with the source-formatter regression and all three shell steps:

- `etime_formatters.py` ports the reviewer's Apple/procps-ng generators.
  Apple's strict `> 3600` transition emits `60:00`; procps-ng uses `>=`.
  The split/range property oracle derives the one-hour exception from the
  Apple formatter, independently of the production parser.
- `parser_checks.py` requires runner/oracle agreement with **4,128** source
  formatter cases (seed 125101), **20** malformed cases, **six** epoch-bound
  cases, and **4,096** comparisons across integer and float clocks. Twelve
  complete held-lock shell commands cover `60:00` (alive), `60:01`, `61:00`
  and `99:00` (unknown), with no submission. Their fixed fake age uses a
  matching fixed wall clock in the unmodified rendered runner; the real
  monotonic clock and two-second subprocess timeout remain active.
- `--mutations` reverts the minute boundary and requires both the formatter
  regression and 4,096-string property to fail. Three other mutations
  (epoch bound, component ranges, identity classification) must still fail
  the property.
- The inspection fake uses `zsh -f`, `zsh/datetime` and shell builtins;
  normal inspection starts no Python process. Its hanging mode directly
  execs sleep, so the production **two-second** timeout remains exercised.
  Inspection assertion failures show the complete step's observed duration
  and a separately measured fresh fake-ps probe versus that timeout.
  Readiness, lock-wait and step-timeout failures also show duration/budget.

To demonstrate the regression at `39bf83c8e` and run head mutations:

```sh
FANOUT_TMP=$(mktemp -d)
git show 39bf83c8e:.claude/workflows/subfleet-fanout.js > "$FANOUT_TMP/baseline.js"
bun docs/reports/2026-09-22-fanout-workflow/harness/render.mjs "$FANOUT_TMP/baseline.js" "$FANOUT_TMP" '{"items":[{"task":"review","tier":"standard","dir":"/Users/x","promptPath":"/p.md","outPath":"/o/a.md","name":"a"}]}'
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python3 docs/reports/2026-09-22-fanout-workflow/harness/parser_checks.py "$FANOUT_TMP/step1.sh"
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python3 docs/reports/2026-09-22-fanout-workflow/harness/lock_checks.py "$FANOUT_TMP/step1.sh"
# Both preceding checks must exit 1 on 60:00.
bun docs/reports/2026-09-22-fanout-workflow/harness/render.mjs .claude/workflows/subfleet-fanout.js "$FANOUT_TMP" '{"items":[{"task":"review","tier":"standard","dir":"/Users/x","promptPath":"/p.md","outPath":"/o/a.md","name":"a"}]}'
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python3 docs/reports/2026-09-22-fanout-workflow/harness/parser_checks.py "$FANOUT_TMP/step1.sh" --mutations
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$FANOUT_TMP"
```

**Upgrading:** install a new helper between batches. A state folder started
by the `8d074a715` (round 6) helper has snapshots and recovery records with
no creation mark. This helper cannot re-prove them, and reports them
`OUTCOME_UNKNOWN` with the reason. Finish such a batch with the helper that
started it.

`FANOUT_REPLAY_CASE=resend` runs only round 8's 16 concurrent re-send
histories (seed 12508). Eight are adopted (this state created the job) and
eight stay refused (an older state did). The registry's creation labels are
the oracle, as for round 7.

On a shared host at load 50 to 100, the harness allows 240 s per step and up
to 120 s per barrier. An overlapping invocation can find the lock held past
its 5 s budget; it then prints `LOCKED` and exits 75 having written nothing.
The drivers check that, relaunch it as a caller would, and print how many
there were.

To run round 8's checks against the round-7 workflow (exit 1 is expected):

```sh
FANOUT_TMP=$(mktemp -d)
git show 2911cef62:.claude/workflows/subfleet-fanout.js > "$FANOUT_TMP/baseline.js"
env -u PYTHONPATH FANOUT_TEST_FILTER='round 8' bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$FANOUT_TMP/baseline.js"
env -u PYTHONPATH FANOUT_TEST_FILTER='randomized concurrent' FANOUT_REPLAY_CASE=resend bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs "$FANOUT_TMP/baseline.js"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$FANOUT_TMP"
```

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
- `unknown-a`: a's answer is lost on the first run (the job is committed).
- `resend-moved`, `resend-moved-c`: every entry, or only c, commits on the
  first send, loses the answer, and is re-sent with HEAD or policy moved
  (`FAKE_RESEND_MOVES=head|policy|none`, default `head`). A different payload
  is refused naming the job that holds the request id; an equal one comes
  back `existing` with `requeried`.
- `resend-invalid-c`: c's re-send fails validation, so the CLI appends
  `; job job-c carries request id rq-…-3` to the refusal.

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
`FAKE_POLICY_PROOF=down|bad|missing|hang` tests unavailable, malformed,
incomplete or hung (30 s) policy previews. `FAKE_POLICY_RETIRE=old:new`
retires a model in the daemon policy that both the preview and the batch
resolve with. `FAKE_CAPTURE_ATTEMPT=<path>` records prompt hashes read by
the submitting fake, independently of helper snapshots. All these files stay
in the validator's temporary fixture directory.
`FAKE_CAPTURE_PAYLOADS=<path>` captures each actual attempted payload's digest
and inputs, including refused attempts. `FANOUT_FAKE_REPOSITORY` locates the
pure policy module; the validator supplies it automatically.
