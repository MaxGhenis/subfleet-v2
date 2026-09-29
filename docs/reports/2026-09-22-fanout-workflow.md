# A native workflow that dispatches through subfleet, measured 2026-09-22

Follow-up 3 of the 2026-09-17 comparison
(`~/subfleet/docs/native-workflows-vs-subfleet.md`, "Composition"), redone
against subfleet 2.0.0a0 (release `20260922T133117Z`) on Claude Code 2.1.278.
The v1 brief named paths and mechanics that no longer exist: `~/chief-of-staff/subfleet`
is now a symlink to the v1 public tree, `subfleet notices` is gone, and `run`
gained `--batch` and `--request-id`. The question it asked survives: can a thin
dispatcher agent ride `subfleet wait` past the Bash tool's 600 s cap, and does the
completion notice reach the launching session?

Evidence labels follow the comparison doc: a bare `path:line` or `C-n.m` was read
that day in `~/subfleet-v2`; `observed` is this machine's state that day.

## The workflow

`.claude/workflows/subfleet-fanout.js` in this repo. One Haiku agent per batch,
not per job:

1. Writes the manifest to `stateDir`, drops every item whose `outPath` is already
   non-empty (the `-o` export exists only after a deliverable is accepted, C-8.3;
   `daemon.py:_export_locked`), and runs
   `subfleet run --batch <manifest> --json [--request-id ID]` once. Job ids go to
   `<label>.jobs.txt`, so the wait step needs no substitution by the model.
2. Loops `subfleet wait $(cat jobs.txt) --timeout 540 --json` with the Bash tool's
   `timeout` at 600 000 ms until the exit code is not 124, capped at
   `maxWaitLoops` (default 20, 3 h of queue plus run time).
3. Returns `{submit_rc, request_id, wait_loops, last_wait_rc, jobs[]}` through a
   schema; the script re-keys the jobs by name and fills `out_path` itself.

Why this shape, against the v1 brief's one-agent-per-item design:

| v1 brief assumption | subfleet 2.0.0a0 |
|---|---|
| One `subfleet run` per job, N dispatchers | `run --batch FILE --json` submits every entry in one call and prints one JSON object per entry with `job_id`, `created`, `rc`, `error` (C-17.7; `cli.py:cmd_run_batch`). One dispatcher. |
| No result cache; skip when the out file is non-empty | `--request-id ID` names entry n `ID-n`; the same manifest resubmitted returns the existing jobs, `created: false` (C-6.2). The skip check stays as a second guard. |
| The script pools concurrency | The daemon caps at 4 active attempts fleet-wide, 2 per lane, and queues in tier order (C-6.4, C-6.9). |
| Adopt a live run that holds the `-o` path (parsed from v1's rc 3 message) | The refusal is exit 7, "output path is held by another job", and names no job (`daemon.py:1065`). The dispatcher reports it as `refused`. |
| `subfleet notices` shows delivery | Notices are store rows with states `pending`, `offered`, `acknowledged`, `surfaced` (C-15.3). `runs show <job>` **acknowledges** the caller session's notice (`cli.py:_ack_notices`), and a workflow worker carries the caller's `CLAUDE_CODE_SESSION_ID`, so the dispatcher is forbidden to run it. |

Two things about the harness, `observed`:

- The Workflow tool refuses a `scriptPath` outside the working directory, and a
  file copied into `~/.claude/workflows/` mid-session is not found by name
  ("Available: deep-research"): the registry is read at session start. Passing
  the script inline worked.
- A dispatcher prompt that merely says "you are a dispatcher" is not enough. In
  the first attempt (2026-09-17, v1) both Haiku agents ignored the computed task
  and started the user's whole request, because the harness relays the user
  request above the task and tells the agent the request wins. The prompt now
  opens by placing the task inside the request; on this run the agent made
  exactly the two calls it was given.

## The test

Two read-only jobs on `~/subfleet-v2`, both `--tier standard` (Opus):

| name | task | prompt | expected |
|---|---|---|---|
| `fanout-short-review` | review | review the workflow file, ≤ 8 findings as JSON | a few minutes |
| `fanout-long-research` | research | read 12,300 lines (contract, cli, daemon, scheduler, store, notify_push, hooks, adapters) and return 60–100 cited claims about the completion path | over 600 s; research/standard jobs since 2026-09-19 average 835 s, n = 13, max 4,825 s (`jobs` table, `observed`) |

Batch `fanout-test`, request id `fanout-test-20260922a`, state dir in the
session scratchpad, launched at 14:38:42Z as `wf_98ce7f93-aa9` from session
`ff11d148`. Login gauge before launch: 5-hour 10%, weekly 3%, Fable 6%
(desktop usage card, `observed`).

Lane state at launch (`subfleet why`, `observed`): no Opus lane was admissible.
Ten Claude lanes were `reserve:fable:unmeasured`, two `no-slot`, one `desktop`,
three disabled, one closed; all six Codex lanes were closed until 2026-09-26 or
later; earliest reset 16:30Z. So both jobs queued, and the wait loop was
exercised first by queue time.

MEASUREMENTS_PLACEHOLDER

## Using it

```sh
cp ~/subfleet-v2/.claude/workflows/subfleet-fanout.js ~/.claude/workflows/   # every project, this user only
```

Start a new session (the registry is read at start), then run `/subfleet-fanout`
or `Workflow({name: "subfleet-fanout", args})` with

```json
{"label": "my-batch", "requestId": "my-batch-2026-09-22a",
 "items": [{"task": "review", "tier": "standard", "dir": "/abs/repo",
            "promptPath": "/abs/brief.md", "outPath": "/abs/out/brief.md", "name": "brief"}]}
```

`requestId` makes a relaunch from a fresh session idempotent (C-6.2); omit it
and only the non-empty-output skip and the output lease (C-6.5) stand between a
relaunch and a second submission. `dir` under `/tmp` needs `allowTmp: true`
(C-2.4). A project checkout can carry the file in its own `.claude/workflows/`;
`~/subfleet` ignores `.claude/` (`.gitignore:9`), this repo does not.
