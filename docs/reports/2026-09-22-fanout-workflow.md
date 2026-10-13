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
  dispatcher's whole context: 1,320,152 five-minute cache-write tokens over
  30 turns, with 1,236,718 in the 13 slices and 83,434 in the four setup
  turns, against 11,033 output tokens (`observed`). Whether cache writes count
  against the subscription's windows is not determined here; the desktop app's
  own usage history for the signed-in org rose from 22% to 84% of the 5-hour
  window during the wait, an account-wide upper bound.
- The jobs reviewed the workflow and mapped the daemon. The review (Opus 5)
  found 3 high, 4 medium and 1 low defects and said fix first; the map
  (Opus 5.5) verified 100 claims and listed 8 places the contract and the code
  disagree and 11 hazards for exactly this pattern. All eight findings and four
  of the hazards are fixed in the workflow shipped here, and a second review
  of that version found nine more defects. The next review found seven,
  and each of its next two follow-ups found two; these are addressed in the
  second through fifth rounds below.

## The workflow

`.claude/workflows/subfleet-fanout.js` in this repo. One Haiku agent per batch,
not per job. Its three commands are rendered by the script with every path
quoted; the agent retypes them and nothing else.

0. `mkdir -p` the state directory, write a helper script and the manifest
   through two quoted heredocs, check both against CRC-32 values the script
   computed (a mistyped character stops here with `CHECKSUM_MISMATCH`), and
   run `setup`. All commands belong to one `&&` chain, including both
   heredocs; if step 0 lacks `SETUP_OK`, the dispatcher stops and reports
   `setup_ok: false`. State files are keyed by the request id, or by the label plus
   a digest of the manifest; a key whose stored manifest differs stops with
   `STATE_MISMATCH`. With no request id, items whose `outPath` is already
   non-empty are dropped (the `-o` export exists only after a deliverable is
   accepted, C-8.3). With a request id the full manifest is kept, because
   entry *n* is `<id>-n` and the daemon dedupes (below).
1. `subfleet run --batch <manifest> --json -d [--request-id ID]`, guarded by
   `should-submit`: it runs once, and again only for entries whose outcome
   was `not-sent`, `unknown` or missing under a request id (C-16.3, C-17.3).
   `RETRY_STEP1` appears on the first unsettled call and drives at most three
   step 1 calls per run. A fresh refused entry never triggers retries; the full
   manifest is still replayed to preserve entry indexes when another entry
   needs recovery. A row-less validation refusal is recorded once as
   `MANIFEST_REFUSED`, with the CLI message, and stops new submissions across
   relaunches. Earlier unknown outcomes survive later unsent or refused
   attempts; a later unanswered invocation makes a previously refused entry
   uncertain again. Before every invocation, the helper snapshots each
   entry's identity and prompt SHA-256 beside the numbered submit log.
   After a manifest refusal, `runs --request-id <id>-<n> --json` finds each
   earlier unknown request; `runs show <job> --json` verifies its recorded
   identity and stored prompt bytes against an unanswered invocation's
   snapshot. The show subprocess clears `CLAUDE_CODE_SESSION_ID` so it
   acknowledges no notices. Verified ids are saved and waited on.
   Inconclusive lookups keep those entries `OUTCOME_UNKNOWN` and counted
   in `UNSETTLED`; only entries without earlier uncertainty become `REFUSED`.
   Submit logs are numbered, never truncated; job ids are written only after a log
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
| No result cache; skip when the out file is non-empty | `--request-id ID` names entry *n* `ID-n`; the same manifest resubmitted returns the existing jobs, `created: false` (C-6.2; `Daemon.submit`, `daemon.py:1087-1094` at this PR's base `81d58fe9`; verified live at 14:40:48Z, below). The digest includes the prompt, the workdir's git head and the policy hash of the exact `policy.json` bytes loaded at daemon start (`daemon.py:1071-1094` at `81d58fe9`). A replay after any of these changes returns exit 2 naming the earlier job. If that entry previously had an unknown or missing answer, the helper verifies the named job against an unanswered invocation's identity snapshot before waiting on it (same function; the mapping job read it at `daemon.py:723-768` on 2026-09-22). |
| The script pools concurrency | The daemon caps and queues in tier order (C-6.4, C-6.9). |
| Adopt a live run that holds the `-o` path | The refusal is exit 7 and names no job: "out_path is held by a live job" when a queued or running job holds the lease (`daemon.py:1440` at `81d58fe9`), "output path is held by another job" when a finished, lost or quarantined one still does (`daemon.py:1419` at `81d58fe9`). Both are reported as `refused`. |
| `subfleet notices` shows delivery | Notices are store rows with states `pending`, `offered`, `acknowledged`, `surfaced` (C-15.3). `runs show <job>` acknowledges the caller session's notice (`cli.py:_ack_notices`). The helper clears `CLAUDE_CODE_SESSION_ID` for recovery lookups; the dispatcher agent itself runs only the supplied steps. |

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
   && …` chains everything on the heredoc declaration line, through both
   bodies and check/setup. A failed chain prints no `SETUP_OK`, which now
   explicitly tells the dispatcher to stop (completed in the third round).
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
(`Daemon.wait`, `daemon.py:1867-1891` at this PR's base `81d58fe9`; the release line
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
   at this PR's base `81d58fe9`, with both `-o` refusal texts (the revision
   label was corrected in the third round).
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

### Third round, 2026-10-12

Review r2 (`~/reviews/fanout-workflow-2026-10-04/review-r2.md`) requested
changes on head `340726bf5`, with two medium and five low findings. The
offline harness reproduced all seven there (13 checks passed, nine failed;
finding 1 has three failing checks). The resulting changes are:

1. **Medium.** In-run recovery was unreachable because the retry condition
   required `RESUBMIT`, which only appeared after a retry had already begun.
   `post-submit` now prints `RETRY_STEP1` on the first retryable outcome with
   a request id. The prompt and marker-driven harness cap step 1 at three
   calls; persistent unavailability remains `not-sent` at that cap.
2. **Medium.** A row-less exit 2, or the CLI's "nothing was submitted"
   message, now records a terminal whole-manifest refusal with the stderr
   tail. `MANIFEST_REFUSED` explains the batch refusal and `REFUSED` labels
   each unresolved entry. Repeated step calls and relaunches submit nothing.
   A row-less exit 1 without that message remains an unknown outcome.
3. **Low.** When a previously unknown or unanswered entry returns a digest
   conflict naming the job that already owns its request id, the helper
   recovers that id as `created: false`. It waits on the earlier job, and
   supplies its name even when wait rows omit names. A fresh digest conflict
   remains refused. The digest description now includes the prompt and
   `policy.json`, as well as the workdir head.
4. **Low.** Both step 0 heredocs and check/setup now share one `&&` chain.
   A failed directory creation or `cd` cannot write a manifest in the caller
   directory. Missing `SETUP_OK` explicitly stops the dispatcher. Step 1's
   post-processing also stays inside its successful `cd` guard.
5. **Low.** The reset command quotes the absolute state directory with
   `shlex.quote`, leaving only the validated key and `.*` outside the quotes.
   The harness executes the reset for a directory with a space and a quote,
   and verifies that unrelated files survive.
6. **Low.** Only `not-sent`, `unknown` or missing answers without job ids
   trigger resubmission. Refused entries are terminal and never trigger it.
   The full manifest is replayed if another entry needs recovery, to keep
   request-id indexes stable; the header, phase detail and report say this.
7. **Low.** Daemon citations now name `81d58fe9`, this PR's base. The cache
   total is 1,320,152 over 30 turns: 1,236,718 in 13 slices and 83,434 in four
   setup turns. The harness checks these sums directly against the CSV.

Validation: **23 checks passed, zero failed**, using `bun 1.3.12` and
`python3 3.14.7`, with `PYTHONPATH` unset, fake
`subfleet` first in `PATH`, and an empty temporary `SUBFLEET_HOME`. Every
rendered command ran through `zsh -c 'eval "$1"'`. The harness covers
`submit-down` with and without a request id, persistent submit-down,
`refuse-b`, `unknown-c`, whole-manifest refusals, row-less unknown outcomes,
digest conflicts, blocked setup, safe reset, `failed-a`, `wait-down-first`,
`lost-c`, `unknown-id`, `no-name`, a full run in a state directory with a
space and a quote, checksum tampering, manifest mismatch, distinct batches,
skipped outputs on relaunch, and the wait sleep cap near the slice deadline.
The standalone `render.mjs` CLI also passed the README sequence: setup,
submit, a pending wait, then successful completion. No `tests/` file references this workflow
or harness, so no pytest run was needed. The live workflow and daemon were
untouched; only the hub should install the reviewed workflow after merge.

### Fourth round (round 4), 2026-10-12

Review r3 (`~/reviews/fanout-workflow-2026-10-04/review-r3.md`) requested
changes on head `c95d10809`, with two medium findings. Both reviewer
scenarios are now registry-backed harness regressions:

1. **Medium, resolved.** `unknown-c → submit-down-always → digest-conflict-c`
   no longer loses c's earlier unknown outcome. Uncertainty survives later
   unsent or refused attempts until a job id is learned. The third call
   recovers `job-c` from the conflict, reports `JOBS=3`, `UNSETTLED=0`, and
   waits on all three jobs. The registry verifies c existed throughout and
   no duplicate request was created.
2. **Medium, resolved.** A submission that committed three jobs but lost
   stdout, followed by removal of b's prompt, still records the latest
   manifest validation refusal. That refusal stops further submissions and
   preserves the earlier unknowns. The helper looks up their exact original
   request ids with `subfleet runs --request-id <id>-<n> --json`, persists
   recovered ids in an append-only recovery file, and waits on all three.
   Restoring the prompt and relaunching reuses those ids without submitting.

Unavailable, empty and malformed lookups keep `UNSETTLED=3` and remain
recoverable on relaunch, including state written by the round-3 helper
without a refusal log pointer. An empty ledger result cannot prove an
earlier request never ran: `cmd_runs` can fall back to the offline store.
Lookups require exactly one row carrying the requested id and a job id;
they are bounded to 15 seconds per request and 60 seconds overall per call.
A mixed history of unknown, then not-sent, then validation refusal recovers
only the missing c id. Fresh validation refusals, including after only
not-sent attempts, remain terminal and do not query the ledger.

Validation used the shipped fake first in `PATH`, an empty temporary
`SUBFLEET_HOME`, `PYTHONPATH` unset, `bun 1.3.12`, `python3 3.14.7`, and
`zsh -c 'eval "$1"'` for every rendered command:

- Original harness at `c95d10809`: **23 passed, zero failed**.
- Expanded harness against the exact `c95d10809` workflow: **24 passed,
  four failed**, including both reviewer reproductions.
- Expanded harness against the round-4 workflow: **28 passed, zero failed**,
  retaining all 23 existing checks.
- `python3 -m subfleet.cli runs --help` confirmed the request-id filter
  without contacting the daemon; `git diff --check` passed.

Only named workflow, harness and report files were staged; no logs, JSON
evidence or bundles were committed. Commits use workspace-local Git metadata
in `.git-local` on `docs/fanout-workflow-measured`, because the shared Git
metadata is outside this job's writable sandbox. Temporary fixtures are
removed with the prescribed Python `shutil.rmtree` command. No real jobs,
network remotes, live workflow edits or daemon changes were used.

### Fifth round (round 5), 2026-10-12

Review r4 (`~/reviews/fanout-workflow-2026-10-04/review-r4.md`) requested
changes on `91644430b`, with two medium findings. Both exact reviewer
sequences are registry-backed harness regressions:

1. **Medium, resolved.** `refuse-b,unknown-c` followed by a full-manifest
   retry that commits b and loses stdout now makes b uncertain. Deleting
   b's prompt before the third call causes a manifest validation refusal,
   then recovery verifies `rq-test-2 = job-b`, reports b as submitted with
   `JOBS=3`, `UNSETTLED=0`, and includes b in wait. An invocation's refusal
   cannot settle a later unanswered invocation.
2. **Medium, resolved.** An older batch and a new state directory reusing
   its request id and label with different names/output paths no longer
   share recovered jobs. The fake retains the old registry entries under
   C-6.2, loses the new invocation's stdout, and then refuses validation
   after prompt deletion. Recovery reports `JOBS=0`, `UNSETTLED=3`, waits
   on nothing, and leaves the old registry unchanged.

Before each submission, a sidecar to its numbered log records the entry's
name, resolved workdir/output path, task, tier, sandbox, model pin and
SHA-256 of its exact prompt bytes. Only snapshots from unanswered
invocations are eligible recovery evidence. `Daemon`'s show operation
returns these job fields and a `prompt_path` pointing to its stored copy;
the online response wraps them in `job`, while `Offline.show_job` returns
them flat. The helper handles both and hashes the stored prompt, so deleting
or editing the caller's prompt does not destroy the original proof. It
requires matching request/job ids and every identity field before adopting
or replaying recovered evidence. A C-6.2 conflict supplies a candidate id,
which undergoes the same verification; the id alone is insufficient.
`cli.py:session_id` reads `CLAUDE_CODE_SESSION_ID`, and `_ack_notices` returns
without it. The helper removes that variable only from lookup subprocesses.

Missing/mismatched fields, unreadable stored prompts, unavailable/empty/
malformed responses, or missing invocation snapshots leave entries unknown.
Legacy recovery rows without proof are ignored. Direct created/existing
submission answers remain usable; legacy unknown state cannot be adopted
from the ledger without its original snapshot. A daemon-resolved model pin
that differs from the requested spelling also keeps recovery uncertain.
Verified evidence persists across relaunches, with no extra submission or
lookup for already known jobs. Each lookup is bounded to 15 seconds and
the whole recovery pass to 60 seconds.

**Entry state machine.** Submission replay and wait reporting have distinct
states; an unverified candidate id is only an annotation on an unknown entry.

| State | What enters it and what can move it onward |
|---|---|
| Unattempted (`outcome=""`, internal) | Setup keeps the entry. Its first invocation yields a known id, refusal, not-sent or unknown. |
| `skipped` | With no request id, setup omits an entry whose output is non-empty. It stays skipped across relaunches; explicitly resetting state allows setup to reconsider it. |
| `refused` | An explicit refusal, or whole-manifest validation refusal with no earlier uncertainty. It triggers no retry. A full-manifest retry driven by another entry can move it to known, not-sent, or unknown, including when its answer is missing. A recorded whole-manifest refusal stops all later submissions. |
| `not-sent` | A daemon connection refusal with an explicit not-sent row and no earlier unknown. Under a request id it triggers a full-manifest retry. A later answer can move it to known, refused or unknown; a validation refusal settles it as refused. Without a request id, relaunch prints a reset command. |
| Submission unknown (`outcome="unknown"`, public `submit-unknown`) | An explicit unknown or missing row for an entry with no known id, even after a refusal. Later not-sent/refused rows and validation refusals preserve it. Only a direct job-id answer or verified recovery moves it to known. Failed identity/ledger checks preserve it. |
| Known (`created`/`existing`, internal; `SUBMITTED` marker) | A direct submission answer or verified recovered id. Full-manifest retries and relaunches preserve the first known id. Wait moves its reported state to running, terminal, or unknown without forgetting the id. |
| `running` | A wait timeout, an unanswered daemon wait, or a killed wait without markers keeps the job pending. Wait repeats through the configured cap; cap exhaustion returns running with rc 124. A later wait/relaunch can yield a terminal result or unknown. |
| `succeeded`, `failed`, `cancelled`, `lost` | A terminal wait row reports one of these states with its rc/output evidence. Waiting stops when no ids remain pending. Submission state retains the known id for later inspection/relaunch. |
| Report unknown (`unknown`) | Failed setup, an omitted dispatcher result, a missing/invalid wait answer, or missing markers. Setup failures stop submission. A later successful setup or wait can resolve the report; an already known id remains in submission state. If the dispatcher dies entirely, the workflow returns `dispatcher: died` with no entry results. |

Validation used only the shipped fake in `PATH`, an empty temporary
`SUBFLEET_HOME`, `PYTHONPATH` unset, `bun 1.3.12`, `python3 3.14.7`, and
`zsh -c 'eval "$1"'` for every rendered command:

- Original harness against `91644430b`: **28 passed, zero failed**.
- Both added reviewer regressions against `91644430b`: **28 passed, two
  failed**, preserving all existing checks.
- Expanded harness against the exact exported `91644430b` workflow:
  **28 passed, nine failed**.
- Expanded harness against this fix: **37 passed, zero failed**, including
  all 28 retained checks, ten missing and ten mismatched metadata variants,
  per-invocation prompt identity, prompt-only collisions, verified C-6.2
  recovery, both show shapes, caller-session isolation and legacy evidence.
- `git diff --check` passed.

Commits stage only named workflow, harness and report sources and carry the
requested coauthor trailer. Shared Git metadata and the worktree's `.git`
pointer are outside the writable sandbox, so commits use workspace-local
`.git-local` metadata on `docs/fanout-workflow-measured`; the original
detached checkout remains at `91644430b`. No logs, JSON evidence or bundles
are staged. Fixtures are cleaned with the prescribed Python `shutil.rmtree`
command. No real jobs, network remotes, sub-agents, live workflow edits or
daemon changes were used.

### Sixth round (round 6), 2026-10-13

Review r5 (`~/reviews/fanout-workflow-2026-10-04/review-r5.md` and
`review-r5-full.md`) requested changes on `cdb451ff9`. All three medium
findings are covered by fake registry regressions:

1. **Overlapping relaunches, resolved.** An advisory `fcntl.flock` lock for
   each `(stateDir, state key)` spans setup before either heredoc is written,
   all of step 1, and each wait slice. Submission selection, log reservation,
   CLI execution, refusal processing, recovery and jobs-file updates occur
   under that same lock. Logs are reserved with exclusive creation and opened
   for append. Replay merges every numbered log and verified recovery row.
   The lock inode remains in place, and the descriptor is inherited by the
   helper and submitting/waiting CLI. Killing the launcher does not release
   exclusivity while a surviving child still writes evidence; the kernel
   releases the lock when its final holder exits. The synchronized creator /
   validation-refuser regression now registers three jobs, makes only one
   batch submission, retains `JOBS=3`, `UNSETTLED=0` across relaunch, and waits
   on all three. It also kills the lock-owning launcher while its child runs.
2. **Incomplete recovery identity, resolved.** Each invocation captures the
   workdir's Git head and the loaded daemon policy hash returned by a fresh,
   non-creating `run --dry-run --json` placement preview. Its sidecar computes
   the exact canonical C-6.2 payload digest, including staged prompt bytes,
   resolved paths, head, task, tier, model pin, sandbox and the workflow's
   implicit lane/exclusions/desktop flags. Recovery requires the recorded
   `payload_digest`, commit and policy, plus every earlier identity field and
   request/job id. Missing proof stays uncertain. Separate head-only and
   policy-only collisions with old jobs now retain `JOBS=0`, `UNSETTLED=3`
   through digest conflicts and relaunch, without waiting on old jobs. A
   legitimate earlier unknown still recovers against its original snapshot
   after both the current head and policy move. Unavailable, malformed or
   incomplete policy previews cannot retroactively supply missing proof.
3. **Mutable prompt snapshots, resolved.** Each invocation writes exclusive,
   read-only prompt copies and a separate staged manifest. The batch CLI
   reads those staged paths. The regression edits b's original prompt after
   snapshot capture and independently records the bytes read by the fake;
   their SHA-256 matches the sidecar. Recovery of the older same-byte job is
   then valid because that payload was actually attempted. A missing source
   prompt becomes a missing staged path, preserving CLI validation refusals
   and every earlier unknown. Staged files stay beside their numbered log,
   so the existing reset command also removes them.

**Concurrency state machine.** The entry transitions in round 5 remain the
replay rules. Concurrent calls add a serialized invocation layer:

| Invocation state | Transition and evidence rule |
|---|---|
| Waiting for the per-key lock | Changes no state. After acquisition, rereads the complete history before deciding whether to submit. |
| Setup holds the lock | Checksums and binds the manifest, then reconstructs known ids. A state mismatch or missing `SETUP_OK` stops dispatch. |
| Step 1 holds the lock | May reserve one new log, stage its bytes and identity, submit the full manifest, merge outcomes, verify recovery and write ids. Known ids remain known; later refusals/not-sent answers preserve earlier uncertainty. Releases only after processing. |
| Wait holds the lock | Reads a stable ids file, collects one wait slice and reports it before another caller can replace wait files or submission state. |
| Interrupted invocation | Before log reservation, supplies no new attempt. A reserved unanswered log remains unknown. Surviving children retain the lock; after the last holder exits, relaunch replays all evidence. Lock files are never unlinked during normal operation. |

The harness isolates shell startup with an empty temporary `ZDOTDIR`, puts
fake Subfleet and fake Git first in `PATH`, sets an empty temporary
`SUBFLEET_HOME`, and unsets `PYTHONPATH`. This prevents shell configuration
from moving real Git ahead of the fixture. The digest check also calls the
repository's pure `subfleet.ids.payload_digest` function to compare the
snapshot with the daemon implementation; it invokes no daemon.

Validation ran only fake commands through `zsh -c 'eval "$1"'`:

- Four reviewer regressions against the exact exported `cdb451ff9` workflow:
  **zero passed, four failed**, as expected. The prompt regression also runs
  through conflict recovery and relaunch before checking its byte proof;
  the baseline persists the wrong old job, and this stronger replay fails.
- Expanded harness against the fixed workflow: **46 passed, zero failed**,
  including all 37 existing checks and the four reviewer regressions. The
  stronger prompt replay was rerun separately: **one passed, zero failed**.
- Sequential driver, seed 12505: **20,000 histories and 113,703 entry
  transitions passed**, with and without request ids.
- Concurrent driver, seed 12506: **64 histories, 128 overlapping invocations
  and 121 immutable submission logs passed** in the final full run. An
  isolated run also passed, with 116 logs. The seed fixes scenario choices;
  the OS determines lock acquisition order. Each history verifies the
  registry, counts batch calls against distinct logs, and checks that replay
  preserves ids and log bytes. The driver streams progress with per-process
  timeouts; an earlier short overall wrapper timeout was removed.
- Replay-driver Python syntax validation and `git diff --check` passed.

Run the full suite from the repository root with
`env -u PYTHONPATH bun docs/reports/2026-09-22-fanout-workflow/harness/validate.mjs .claude/workflows/subfleet-fanout.js`.
The harness README gives the selected-regression and baseline commands.

Commits use workspace-local `.git-local` metadata on
`docs/fanout-workflow-measured`: writing the shared Git index was refused by
the sandbox. Only named workflow, harness and report source files are staged,
with the requested coauthor trailer. No logs, JSON evidence or bundles are
staged. Fixtures are removed with the prescribed Python `shutil.rmtree`
command. No real jobs, network remotes, sub-agents, live workflow edits or
daemon changes were used.

## Round 7: bind recovery to the daemon-recorded invocation, 2026-10-13

Read `review-r6.md` and `review-r6-full.txt` from the review archive. The two
medium findings reproduce at the exact requested `8d074a715f29` checkpoint.
Local `main` (`eef8d3d9b`) and the checkpoint both record the canonical payload
digest, its HEAD/policy inputs, and creation-time batch metadata. Online
`runs show --json` returns that metadata beside `job`; offline show omits it
but the daemon-written `manifest.json` beside `prompt.md` retains it.

1. **Recovery identity, resolved with option (b).** Each numbered invocation
   stages an exclusive manifest with a fresh UUID in its batch label. The
   daemon records that label only when it creates the job; C-6.2 deduplication
   returns an existing job without replacing its creation metadata. Recovery
   first requires the exact batch id, unique label, entry index and batch
   size of an unanswered invocation. It then compares the stable manifest
   fields and stored prompt bytes, and recomputes the full payload digest
   with the daemon's recorded HEAD and policy hash. The earlier preview's
   HEAD/policy/digest remain diagnostic observations, not submission proof.
   A legitimate creation after HEAD/policy changes during submission can
   therefore recover, while a lost conflicting attempt against an older
   job cannot. The request ids and prompt bytes retain their existing
   semantics, and no daemon or CLI source was changed.
2. **Model aliases, resolved.** Explicit pins are canonicalized before
   staging the submitted manifest and building identity. The read-only
   daemon placement preview exposes `decision.chain`; a pinned preview has
   one canonical model even when `chosen_model` is null because no lane is
   available. The helper uses that surface rather than a copied alias map.
   Preview failure or absent canonical-pin proof leaves recovery uncertain.
   The fake uses the repository's pure CLI retirement mapping and
   `resolve_model` before hashing/storing jobs: `sol` becomes `astra`, and
   `fable` becomes `opus` under this checkpoint's policy.
3. **Lock note, addressed.** Acquisition uses nonblocking flock with an
   explicit default/max five-second budget. Exhaustion prints `LOCKED`,
   the lock path and last launcher's pid, then exits 75 without writing
   workflow state. The dispatcher preserves its last per-name evidence and
   stops on that marker. A surviving inherited child may be the actual
   holder, so the pid is accurately called `launcher_pid`. Files are never
   unlinked to reclaim a held lock. Five seconds of contention plus the
   590-second helper slice budget fit within the 600-second Bash timeout.
   Submission/CLI execution still relies on that outer timeout.

**Updated state machine.** Round-5 outcome transitions remain: explicit
successful ids stay known, later refused/not-sent answers cannot erase an
earlier unknown, and a whole-manifest validation refusal sends nothing.
The stronger invocation/recovery layer is:

| State or evidence | Transition and proof rule |
|---|---|
| Waiting for the per-key lock | Reads no mutable workflow state. Acquires within five seconds or reports `LOCKED`/exit 75; no stale-file reclamation. |
| Setup holds the lock | Checksums/binds the manifest and replays known ids. Mismatch or missing `SETUP_OK` stops dispatch. |
| Reserved invocation | Numbered log, exclusive prompt snapshots, canonical explicit pins and a unique creation label precede the batch call. Request ids keep full-manifest entry indices. |
| Successful response | Keeps the explicitly returned id across retries and relaunch, as before. |
| Unknown or missing response | Keeps the invocation snapshot and uncertainty; later refusals/not-sent attempts cannot settle it. |
| Candidate with a different/missing creation mark | `LOOKUP_UNKNOWN` and `OUTCOME_UNKNOWN`; no id adoption or wait on that candidate. An older equal preview is insufficient. |
| Candidate with this invocation's creation mark | Requires exact request id/job metadata, stable manifest fields, matching stored prompt SHA-256 and a recomputed digest using daemon-recorded HEAD/policy. Only then writes recovery evidence and adopts the id. |
| Offline candidate | Reads the daemon-written manifest beside its recorded prompt. Requires its job id, request id and digest to match the shown row before using the creation mark. Missing/corrupt proof remains uncertain. |
| Relaunch | Replays the same creation mark and digest proof from saved recovery records. Legacy snapshots without a mark cannot retroactively prove an unknown; explicit successful rows remain known. |
| Interrupted holder | Surviving children retain flock. A later launcher reports `LOCKED`; after the last holder exits, the same inode permits replay/recovery. |

A lost deduplication answer for a job created outside the saved history
remains uncertain, even if its preview looks equal. Neither the daemon's
creation record nor a pre-submit preview proves the refused/lost attempt's
payload. A later explicit successful dedupe response can still settle it.
This supersedes round 6's assumption that an equal pre-submit snapshot could
prove that older job. The prompt-byte fixture now loses this invocation's
creation answer, edits the original prompt after staging, then exercises
conflict recovery and relaunch. Its byte/digest assertions are retained.

Validation is fake only, with fake Subfleet/Git first in PATH, empty temporary
`SUBFLEET_HOME`/`ZDOTDIR`, and PYTHONPATH unset:

- Exact `8d074a715f29` export: **zero passed, six finding regressions failed**
  as expected. HEAD-only and policy-only races each cover lost stdout and
  ordinary refusal/unknown rows. Attempted payload digests are captured
  independently and differ from the older registry jobs on every call.
- The same baseline independently rejects concurrent `head`, `policy`,
  `sol` and `fable` selections: **zero passed, one failed per selection**.
- Expanded harness: **55 checks passed, zero failed**, keeping the 46 earlier
  checks and adding six finding checks plus three proof/lock checks.
- Sequential oracle, seed 12505: **20,000 histories / 113,703 transitions**.
- Original concurrent oracle, seed 12506: **64 histories / 128 overlapping
  invocations / 123 immutable submission logs** in the full run. Acquisition
  order changes the log count; an isolated run had 126 logs.
- Added concurrent driver, seed 12507: **16 HEAD/policy and eight alias
  histories / 48 overlapping invocations**, including validation refusals,
  deleted prompts, immutable-log checks and repeated relaunch settlement.
  Each history separately captures actual attempted payload digests and inputs
  and compares them against registry records; all 24 were rerun after adding
  those independent digest assertions.
- Targeted checks cover actual HEAD/policy changes during a successful lost
  creation, missing/mismatched creation marks, alias digest equality, nested
  online/flat offline proof, and a surviving fake CLI after launcher/helper
  death. The lock probe reports `LOCKED` within a shortened 0.1-second budget,
  then recovers after the surviving CLI exits.
- Python AST parsing, fake CLI Bash syntax and `git diff --check` pass.

Run the complete validator with the command in the harness README. Select
finding regressions with `FANOUT_TEST_FILTER='round 7 finding'`; select a
concurrent baseline perturbation with
`FANOUT_TEST_FILTER='randomized concurrent' FANOUT_REPLAY_CASE=head`
(or `policy`, `sol`, `fable`). The README documents export and cleanup.

The sandbox refused writes to the shared Git index and the workspace's Git
pointer. Commits therefore use workspace-local `.git-local` metadata on
`docs/fanout-workflow-measured`, with the requested coauthor trailer. Use
`git --git-dir=.git-local` to inspect them. Only named workflow, harness and
report sources are staged; no logs, JSON evidence or bundles. Temporary
fixtures use the prescribed Python `shutil.rmtree` cleanup. No real jobs,
network remotes, sub-agents, live workflow edits or caller-checkout writes.

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
