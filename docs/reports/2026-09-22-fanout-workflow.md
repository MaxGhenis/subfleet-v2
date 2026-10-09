# A native workflow that dispatches through subfleet, measured 2026-09-22

Follow-up 3 of the 2026-09-17 comparison
(`~/subfleet/docs/native-workflows-vs-subfleet.md`, "Composition"), redone
against subfleet 2.0.0a0 (release `20260922T133117Z`) on Claude Code 2.1.278,
and finished on 2026-10-04 from the records the run left behind. The v1 brief
named paths and mechanics that no longer exist: `~/chief-of-staff/subfleet` is
a symlink to the v1 public tree, `subfleet notices` is gone, and `run` gained
`--batch` and `--request-id`. The question it asked survives: can a thin
dispatcher agent ride `subfleet wait` past the Bash tool's 600 s cap, what does
that cost the login, and does the completion notice reach the launching
session?

Evidence labels follow the comparison doc: a bare `path:line` or `C-n.m` was
read in `~/subfleet-v2` on the date given; `observed` is this machine's state;
`doc` is Anthropic's documentation fetched 2026-10-04. Raw records are in
[2026-09-22-fanout-workflow/](2026-09-22-fanout-workflow/).

## Short version

- The slicing works. A Haiku dispatcher ran 13 consecutive
  `subfleet wait --timeout 540` calls of 540.3 to 541.5 s each, every one
  exiting 124, with no Bash call hitting the 600 s cap (`observed`).
- It never got to see the jobs finish. Both jobs sat queued for 3 h 42 min
  because no Opus lane was admissible, and the session's process ended after
  2 h 01 min of waiting. The jobs then ran unattended on a lane and succeeded
  (214 s and 431 s); their notices sat `pending` for eleven and a half days and
  surfaced at this session's next start (`observed`).
- Waiting is not cheap on the login. Each 540 s slice outlives the prompt
  cache's five-minute lifetime (`doc`), so every slice re-wrote the
  dispatcher's whole context: 1,320,152 five-minute cache-write tokens over 13
  slices, against 11,033 output tokens (`observed`). Whether cache writes count
  against the subscription's windows is not determined here; the desktop app's
  own usage history for the signed-in org rose from 22% to 84% of the 5-hour
  window during the wait, an account-wide upper bound.
- The jobs reviewed the workflow and mapped the daemon. The review (Opus 5)
  found 3 high, 4 medium and 1 low defects and said fix first; the map
  (Opus 5.5) verified 100 claims and listed 8 places the contract and the code
  disagree and 11 hazards for exactly this pattern. All eight findings and four
  of the hazards are fixed in the workflow shipped here, and a second review
  of that version found nine more defects, also fixed (below).

## The workflow

`.claude/workflows/subfleet-fanout.js` in this repo. One Haiku agent per batch,
not per job. Its three commands are rendered by the script with every path
quoted; the agent retypes them and nothing else.

0. `mkdir -p` the state directory, write a helper script and the manifest
   through two quoted heredocs, check both against CRC-32 values the script
   computed (a mistyped character stops here with `CHECKSUM_MISMATCH`), and
   run `setup`. State files are keyed by the request id, or by the label plus
   a digest of the manifest; a key whose stored manifest differs stops with
   `STATE_MISMATCH`. With no request id, items whose `outPath` is already
   non-empty are dropped (the `-o` export exists only after a deliverable is
   accepted, C-8.3). With a request id the full manifest is kept, because
   entry *n* is `<id>-n` and the daemon dedupes (below).
1. `subfleet run --batch <manifest> --json -d [--request-id ID]`, guarded by
   `should-submit`: it runs once, and again only for entries whose outcome
   was `not-sent` or `unknown` under a request id (C-16.3, C-17.3). Submit
   logs are numbered, never truncated; job ids are written only after a log
   is parsed. Without a request id a relaunch submits nothing and prints the
   `rm` that resets the state.
2. `subfleet wait $(cat jobs.txt) --timeout 540 --json`, then `post-wait`,
   which prints one `DONE`, `PENDING` or `UNKNOWN` line per job and
   `PENDING_COUNT`. The agent repeats while the count is above zero, up to
   `maxWaitLoops` (default 20). Exit 1 or 69 with no parseable row means the
   daemon did not answer; the helper sleeps for what is left of the slice, at
   most 60 s (`SUBFLEET_FANOUT_RETRY_S`), and the loop continues. Exit 1 with
   rows is a job that failed with rc 1.

It returns `{setup_ok, already_submitted, submit_rc, request_id, wait_loops,
last_wait_rc, jobs[]}` through a schema; the script re-keys the jobs by name,
fills `out_path`, and marks any name the agent did not report as `unknown`.

Why this shape, against the v1 brief's one-agent-per-item design:

| v1 brief assumption | subfleet 2.0.0a0 |
|---|---|
| One `subfleet run` per job, N dispatchers | `run --batch FILE --json` submits every entry in one call and prints one JSON object per entry with `job_id`, `created`, `rc`, `error` (C-17.7; `cli.py:cmd_run_batch`). One dispatcher. |
| No result cache; skip when the out file is non-empty | `--request-id ID` names entry *n* `ID-n`; the same manifest resubmitted returns the existing jobs, `created: false` (C-6.2; `Daemon.submit`, `daemon.py:1087-1094` on main at `2441fbea`; verified live at 14:40:48Z, below). The digest covers the workdir's git head for every job with a repository, so a replay after a commit is exit 2 for those entries (same function; the mapping job read it at `daemon.py:723-768` on 2026-09-22). |
| The script pools concurrency | The daemon caps and queues in tier order (C-6.4, C-6.9). |
| Adopt a live run that holds the `-o` path | The refusal is exit 7 and names no job: "out_path is held by a live job" when a queued or running job holds the lease (`daemon.py:1440` on main), "output path is held by another job" when a finished, lost or quarantined one still does (`daemon.py:1419`). Both are reported as `refused`. |
| `subfleet notices` shows delivery | Notices are store rows with states `pending`, `offered`, `acknowledged`, `surfaced` (C-15.3). `runs show <job>` acknowledges the caller session's notice (`cli.py:_ack_notices`), and a workflow worker carries the caller's `CLAUDE_CODE_SESSION_ID`, so the dispatcher is forbidden to run it. |

Two things about the harness, `observed`:

- The Workflow tool refuses a `scriptPath` outside the working directory, and a
  file copied into `~/.claude/workflows/` mid-session is not found by name
  ("Available: deep-research"): the registry is read at session start. Passing
  the script inline worked, and the saved name resolved in the next session.
- A dispatcher prompt that merely says "you are a dispatcher" is not enough. In
  the first attempt (2026-09-17, v1) both Haiku agents ignored the computed task
  and started the user's whole request, because the harness relays the user
  request above the task and tells the agent the request wins. The prompt now
  opens by placing the task inside the request; on the measured run the agent
  made exactly the calls it was given, 15 of 15 at the 600,000 ms timeout.

## The test

Two read-only jobs on `~/subfleet-v2`, both `--tier standard` (Opus), batch
`fanout-test`, request id `fanout-test-20260922a`, launched at 14:38:42Z as
`wf_98ce7f93-aa9` from desktop session `ff11d148`:

| name | task | prompt | ran |
|---|---|---|---|
| `fanout-short-review` | review | review the workflow file, at most 8 findings as JSON | 214 s, Opus 5, 6 turns |
| `fanout-long-research` | research | read 12,300 lines (contract, cli, daemon, scheduler, store, notify_push, hooks, adapters) and return 60–100 cited claims about the completion path | 431 s, Opus 5.5, 35 turns |

Lane state at launch (`subfleet why`, `observed`): no Opus lane was admissible.
Ten Claude lanes were `reserve:fable:unmeasured`, two `no-slot`, one `desktop`,
three disabled, one closed; all six Codex lanes were closed until 2026-09-26 or
later.

Login gauges before launch, two sources that disagree: the desktop app's usage
card (read through the app's session tool at about 14:36Z) said 5-hour 10%,
weekly 3%, Fable 6%; the app's own per-org history file
(`plan-usage-history.json`, org `69982a27`, the org it had switched to at
14:25Z) said 5-hour 22% at 14:33Z. Which identity each one measures is the
open question in the 2026-09-06 three-identities note; both are reported as
read.

## What happened, UTC

| When | Event (`observed`) |
|---|---|
| 14:38:42 | Step 1, 0.4 s: both entries `created: true`, exit 0. |
| 14:38:51 to 16:37:05 | 13 `wait` slices of 540.3 to 541.5 s. Every one exited 124 with two `PENDING` rows: the jobs were still queued. |
| 14:40:48 | Idempotency check from the main session: the same manifest with the same request id returned both ids with `created: false`, exit 0, no new rows (C-6.2). |
| 16:27:58.967 and 16:37:05.047 | The PostToolUse hook (delivery layer 2, C-15.2) armed a waiter for each job after a dispatcher Bash call: the `since` stamps in `~/.subfleet/hooks/waiters/*.lock` match the result timestamps of the dispatcher's last two completed calls to within 15 ms. So the session's hooks do run for a workflow subagent's Bash calls, under the parent's session id, although the subagent transcript records only the `PreToolUse` approvals. Each waiter polls at most 595 s. |
| 16:37:10 | 14th slice started. |
| 16:39:37 | The session's process ended; the call died with exit 137 after 147 s. The transcript records no cause. Had the session lived, the 20-slice cap would have been reached at about 17:41Z (slices started 546.0 s apart on average), 40 minutes before the first job started. |
| 18:02:46 to 18:20:48 | Admission for the short review: 7 `job.probe_deferred`, then 67 `attempt.reserved` events and 8 decision rows over 16 minutes, a probe, and the attempt on lane `claude-11` (max@axiom.org) at 18:20:48, 3 h 42 min after submission. |
| 18:24:22 | Short review accepted and exported: rc 0, 8,529 bytes, model `claude-opus-5`, attested. Notice row 564 written `pending`. |
| 18:24:46 | Long research attempt on the same lane. Its decision carried a different policy hash (`a09458c6…` against `5502398f…` four minutes earlier) and `opus` now meant `claude-opus-5-5`. |
| 18:31:57 | Long research accepted and exported: rc 0, 41,305 bytes, attested. Notice row 567 written `pending`. |
| 2026-10-04 04:36:10 | Both notices marked `surfaced`, transport `hook:SessionStart`, when this session restarted. No socket push was attempted (`transport` stayed null until then; the mapping job found no caller of `notify_push.offer` in the tree). |

Lane cost, from each attempt's `stream.jsonl` result event, on max@axiom.org:
the review read 109,184 cached and 24,747 new input tokens and wrote 16,616;
the research read 5,004,955 cached and 272,667 new and wrote 38,928. The CLI
priced them at $0.72 and $3.96 at API rates; the lane runs on a subscription.

## What the wait cost the login

From the dispatcher's transcript (`dispatcher-usage.csv`), 30 API turns on
`claude-haiku-4-5-20251001`:

| | Tokens |
|---|---|
| Input, uncached | 244 |
| Cache reads | 75,720 (two reads, 13 s and 20 s after the first turn) |
| Cache writes, five-minute | 1,320,152 |
| Output | 11,033 |

Each slice is two API turns that each write the whole context to the cache
and read nothing: 42,791 tokens after the first slice, growing by 792 per
slice to 52,320 at the thirteenth. The cache lifetime is five minutes,
refreshed on each use (`doc`, prompt caching page), and a slice is nine, so
no read ever hits. Why the second turn of a pair writes again four seconds
after the first is not determined.

The 2026-09-17 figure of 40,600 tokens per dispatcher, and the 43,000 to
66,000 per batch in the v2 rewrite of the comparison doc (MaxGhenis/subfleet
PR #8), come from runs that waited for at most one slice. The cost scales with
slices actually waited: about 90,000 cache-write tokens per 540 s, so a
20-slice cap is about 1.8M on a batch that never starts. The workflow's
`whenToUse` now says to check `subfleet status` first, and the script logs the
per-slice cost at launch. Whether a 270 s slice would keep the cache warm, and
whether cache writes count against the plan's windows at all, are the two
measurements that would change this.

## What the reviewers found and what changed

The review job's eight findings (`short-review.deliverable.json`), each with
the change made:

1. **High.** Dropping delivered items from the manifest shifted every later
   entry's request id onto another entry's digest, so the idempotent relaunch
   was the one path that could never recover unfinished work. Now the full
   manifest is submitted whenever a request id is set; the skip applies only
   without one.
2. **High.** Step 1 truncated the jobs file and overwrote the submit log before
   the ids were safe. Now `should-submit` refuses when either exists, and the
   jobs file is written only after parsing.
3. **High.** The loop keyed on exit 124, and `wait` returns the maximum over
   jobs. On this daemon the scenario the reviewer gave (a lost job outranking
   124 while others are still pending) cannot occur, because a multi-job wait
   is answered only when every job is terminal (`Daemon.wait`, below); the
   loop now runs on `PENDING_COUNT` anyway, so it no longer depends on that
   property.
4. **Medium.** `cd DIR && cat` guarded only the `cat`; a missing state
   directory produced a clean-looking empty batch. Now `mkdir -p DIR && cd DIR
   && …` chains everything, and a failed chain prints no `SETUP_OK`.
5. **Medium.** Names with no marker line had no state. The schema gained
   `unknown`, and the prompt names the rule.
6. **Medium.** State files were keyed by `label` alone, so two batches in one
   directory shared them. Now the key is the request id when there is one.
7. **Medium.** Three heredocs per step for a small model to retype. Now one
   heredoc, in step 0, writes a helper; steps 1 and 2 are single lines.
8. **Low.** `out_bytes` trusted the wait row's `out_path`. Now it falls back
   to the manifest's path.

From the mapping job's eleven hazards (`long-research.deliverable.json`, in
its order): hazard 1 (loop on the rows, not the exit code), 3 (a daemon
restart during `wait` is exit 1 or 69, not 124; a 2026-09-24 run burnt 19
slices in seconds that way, PR #8's table), 4 (never `runs show`) and 7 (a
request id derived from a stable key) are built in. Hazard 8, that a non-empty
`-o` file does not prove this job succeeded, still stands for the skip
heuristic, which is why it applies only without a request id. The eight contract-against-code disagreements it recorded are for
the daemon's maintainers, not this workflow; two matter here: the socket push
(C-15.2 layer 4) has no caller, and exit 75 is never produced because `submit`
returns no state.

Not changed: the dispatcher still cannot see partial completion, because the
daemon answers a multi-job `wait` only when every job is terminal and exported
(`Daemon.wait`, `daemon.py:1867-1891` on main at `2441fbea`; the release line
factors the same check into `_wait_answer`). A caller who wants early results
should launch one batch per job.

### Second round

An Opus 5.5 review of the first version of this PR (`subfleet run --task
review --tier standard`, job `20261004-005708-fanout-pr-review`, 2026-10-04;
it ran the harness and reproduced three findings against the fake and the
real CLI) said request changes, with 2 high, 3 medium and 4 low findings.
Each with the change made:

1. **High.** Without a request id the state was keyed by the label, which
   defaults to `fanout`, so a second batch in the same output directory
   adopted the first one's job ids and reported them as its own. Now the key
   carries a digest of the manifest, a stored manifest that differs stops
   with `STATE_MISMATCH`, and the default state directory is a
   `.subfleet-fanout` subdirectory, not the deliverables folder.
2. **High.** `should-submit` refused whenever a submit log held any row, so a
   batch whose first submission got `not-sent` rows could never be
   resubmitted, although C-16.3's recovery is exactly that. Now, under a
   request id, entries with no job id are resubmitted (the whole manifest;
   the daemon returns the rest as `existing`), each attempt in its own
   numbered log.
3. **Medium.** Every row without a job id was printed as `REFUSED`, which
   C-17.3 forbids for an outcome the CLI could not learn. Now `REFUSED`,
   `NOT_SENT` and `OUTCOME_UNKNOWN` are distinct lines and states.
4. **Medium.** `wait` exit 1 was read as "daemon unreachable" and slept 60 s,
   but a job that failed with rc 1 also exits 1; late in a slice the sleep
   crossed the 600 s cap and the buffered markers were lost. Now the daemon
   counts as unreachable only with no parseable row, the sleep is capped at
   the time left in the slice, and the helper flushes every line.
5. **Medium.** Nothing checked that the agent retyped the heredocs exactly;
   the manifest was double-encoded JSON on one line. Now both files are
   checksummed before anything runs and the manifest is plain JSON, one job
   per line. The per-slice cost statement says it grows with the manifest.
6. **Low.** Step 1 relied on the inherited environment to detach; with
   `SUBFLEET_RUN_DETACH=0` it would have blocked. Now it passes `-d`.
7. **Low.** Code citations named a function that exists only on the release
   line and line numbers from 2026-09-22. Corrected above to function names
   on `main` at `2441fbea`, with both `-o` refusal texts.
8. **Low.** Three numbers and one rationale did not match the evidence files.
   Corrected above: the 20-slice cap at 17:41Z, 40 minutes before the first
   attempt; eleven and a half days; the two cache reads at 13 s and 20 s;
   and the exit-code scenario behind finding 3 of the first round is
   unreachable on this daemon.
9. **Low.** On a relaunch without a request id, items skipped on the first
   launch had no marker. Now step 1 prints `SKIP` for every item outside the
   submission manifest.

Offline checks before shipping, against a fake `subfleet` that replays the
real row shapes (`observed` 2026-10-04): first run, relaunch without
resubmission, a refused entry, every entry `not-sent` then resubmitted under
the request id, the same without a request id (reset command printed), an
`unknown` entry settled by resubmission, a changed manifest under a reused key
(`STATE_MISMATCH`), one character changed in the helper
(`CHECKSUM_MISMATCH`), two batches with one label in one directory (separate
keys, both submitted), the daemon down at the first wait (sleep, then
continue), a lost job, a job failed with rc 1 (no false "unreachable"), an
unknown id, rows without names, a state directory containing a space and a
quote, and every step through `zsh -c eval`, which is how the Bash tool runs
commands here.

## Live check of the shipped commands, 2026-10-04

The three rendered commands, run by hand against the daemon (release
`20261004T020418Z`) with two trivial-tier jobs that reply "OK"
(`fanout-live-20261004a`, caller session `ff11d148`): submission 0.4 s, both
placed on `codex-4` as `gpt-6-luna`, attested, finished and exported 11 s
after submission; the first `wait` slice returned two `DONE` rows and
`PENDING_COUNT=0`. A second step 1 printed `ALREADY_SUBMITTED` and ran no
`subfleet run`; step 1 with the local state cleared returned both ids with
`created: false` and added no job, which is C-6.2 after success. Both notices
were written `pending` for the caller session (`observed`).

The second version, 10:17Z the same day (`fanout-live-20261004b`): step 0
printed `CHECKSUM_OK`; step 1 run with `SUBFLEET_HOME` pointing at an empty
directory got the real CLI's two `not-sent` rows ("no daemon at …" and "the
daemon went away before jobs[1]", exit 69) and reported `UNSETTLED=2`; step 1
again with the real home printed `RESUBMIT`, slept 60 s, and both entries
came back `created: true`; one wait slice returned two `DONE` rows 22 s after
the resubmission (`observed`).

## Using it

```sh
cp ~/subfleet-v2/.claude/workflows/subfleet-fanout.js ~/.claude/workflows/   # every project, this user only
```

Start a new session (the registry is read at start), then run `/subfleet-fanout`
or `Workflow({name: "subfleet-fanout", args})` with

```json
{"label": "my-batch", "requestId": "my-batch-2026-10-04a",
 "items": [{"task": "review", "tier": "standard", "dir": "/abs/repo",
            "promptPath": "/abs/brief.md", "outPath": "/abs/out/brief.md", "name": "brief"}]}
```

Optional: `model` and `sandbox` per item or at the top level, passed through
to the manifest as `run -m` and `run -s` take them; `maxWaitLoops`; `stateDir`
(default: a `.subfleet-fanout` directory beside the first item's output);
`allowTmp` for a workdir under `/tmp` (C-2.4). Check `subfleet status` first: a batch with no admissible lane
waits in the queue and the dispatcher pays for every slice. A project
checkout can carry the file in its own `.claude/workflows/`; `~/subfleet`
ignores `.claude/` (`.gitignore:9`), this repo does not.

## Files

- `2026-09-22-fanout-workflow/dispatcher-bash-calls.csv`: the 15 Bash calls
  with durations and marker lines.
- `2026-09-22-fanout-workflow/dispatcher-usage.csv`: the 30 API turns with
  cache reads and writes.
- `2026-09-22-fanout-workflow/short-review.deliverable.json`: the review.
- `2026-09-22-fanout-workflow/long-research.deliverable.json`: the 100-claim
  map of submission, admission, `wait`, notices, export and replay.
- `2026-09-22-fanout-workflow/harness/`: the fake `subfleet`, the renderer
  and the validation script behind the offline checks, with a README.
- Store rows: `subfleet runs show 20260922-103842-fanout-short-review` and
  `…-long-research` while retention keeps them.
