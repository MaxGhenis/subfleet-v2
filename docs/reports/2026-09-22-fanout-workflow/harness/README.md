# Offline harness for subfleet-fanout.js

Needs `bun`, `python3` and `zsh`. From the repository root, run:

```sh
env -u PYTHONPATH bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs .claude/workflows/subfleet-fanout.js
```

The validator checks arguments and result shaping, renders the commands with
a stub `agent()`, and executes every step through `zsh -c 'eval "$1"'`,
as the review did. It prepends only the fake to `PATH`, sets
`SUBFLEET_HOME` to an empty temporary directory, sets
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

For an individual command sequence, from this harness directory:

```sh
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/bin" "$TMPDIR/empty-home"
cp fake-subfleet.sh "$TMPDIR/bin/subfleet"
chmod +x "$TMPDIR/bin/subfleet"
export PATH="$TMPDIR/bin:$PATH" FAKE_STATE="$TMPDIR" SUBFLEET_HOME="$TMPDIR/empty-home" SUBFLEET_FANOUT_RETRY_S=0
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
