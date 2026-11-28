# D-REMOTE-JOBS1 · Remote jobs

Subfleet needs a record for cloud work started outside its local lanes. A remote
job identifies a Claude Code cloud session or a Codex cloud task. The first
slice records and displays adopted jobs. [Issue #356](https://github.com/MaxGhenis/subfleet-v2/issues/356)

## Record

`subfleet remote adopt <url> [--repo OWNER/NAME] [--branch B] [--name N] [--account LABEL]`
records a job started elsewhere. Adoption is local and runs without a provider
request. The hub confirmed Claude's `https://claude.ai/code/<session id>` shape,
where `session_` is followed by ASCII letters or digits. The Codex shape
`https://chatgpt.com/codex/tasks/<task id>`, where `task_` is followed by ASCII
letters, digits or underscores, is a hub assumption pending a real task URL.
Each suffix must be nonempty. The provider and parsed id uniquely identify an
adoption. `subfleet remote forget <id>` removes the local record for a mistaken
adoption. Each successful mutation records one event with the Subfleet id,
provider and provider id.

The `REMOTE_JOBS_MIGRATION` adds a separate `remote_jobs` table. Its row holds a
Subfleet id in the local date-time and short-name style, provider, provider ref,
URL, optional repo, branch, name and account, a job state, and the creation and
update timestamps. `last_polled_at` is initially null. Adopted jobs start as
`running` with no provider observation. The account is an operator-supplied
label and must contain no `@`.

The allowed provider data is the id, canonical task or session URL, repo,
branch and state. Credentials, including tokens, cookies and keys, email
addresses, URL query strings, provider output, transcripts and prompts must
never be stored in the row or its events. The first slice only works with the
local record and has no credential input.

The job and attempt tables remain the inputs for local admission, leases,
worktree allocation, disk reserves and retention. Remote records do not
participate in those operations. A later `subfleet run --remote` can launch
through a provider adapter and record the returned id, URL and launching
account. Launching needs provider confirmation and a recovery policy for an
ambiguous result so a retry cannot silently launch another job.

## Poll

A later poller should use provider CLIs through read-only adapters. Each
adapter needs a verified, noninteractive command with structured or reliably
parseable status output. For Codex, start by checking the behavior of
`codex cloud status` in a workspace the CLI can see. For Claude, identify a
read-only status command. Starting a session, sending an empty follow-up or
teleporting a session could change provider or local state, so those are
outside the polling interface.

The initial cadence should be on the order of minutes per nonterminal job,
with bounded command time, limited concurrency and backoff on errors. Status
reads the last stored snapshot. A successful observation updates the state
and `last_polled_at` together. An update applies to the same record the poll
started with, so a late result cannot bring back a forgotten job or update a
new adoption of the same provider id.

The mapping below describes the evidence each adapter must recognize. It
does not assert the providers' literal state names or imply that every
provider exposes every distinction.

| Subfleet state | Claude cloud evidence | Codex cloud evidence |
| --- | --- | --- |
| `queued` | Session work accepted and awaiting execution | Task accepted and queued for execution |
| `running` | Session work actively executing | Task executing |
| `waiting` | Session work explicitly blocked on input, approval or another prerequisite | Task explicitly blocked on approval or another prerequisite |
| `succeeded` | Current work completed with a successful outcome, even if the session accepts follow-ups | Task completed with a successful outcome |
| `failed` | Provider reports a terminal execution failure | Provider reports a terminal execution failure |
| `cancelled` | Provider confirms cancellation | Provider confirms cancellation |
| `lost` | Provider confirms the tracked session is gone in the correct account and workspace | Provider confirms the tracked task is gone in the correct account and workspace |

If the CLI cannot see the workspace, keep the last state and successful poll
time. The same rule applies to timeouts, rate limits, auth failures and
unrecognized responses. Report a short diagnostic without retaining raw CLI
output. Treat a lookup miss as inconclusive until the adapter can distinguish
workspace visibility from confirmed loss. In #356 the Codex CLI reported no
cloud environments while its desktop app offered a runnable Default
environment, so the CLI's visibility needs verification before polling or
launching there. [Issue #356](https://github.com/MaxGhenis/subfleet-v2/issues/356)

## Show

The first slice adds `subfleet remote list` and `--json`. `subfleet status`
shows a **Remote** section and explains that adopted states are unpolled.
`subfleet status --json` includes `remote_jobs` when there are records, with
null `last_polled_at` indicating the lack of a provider observation. With an
empty table, existing status output stays byte-identical. Status obtains the
records from the daemon when available and uses its existing offline store
path otherwise. Local `wait`, including `wait --mine`, continues to follow
local work only in this slice.

A later remote-aware wait can consume poller observations and use the existing
terminal states. A result should point at the pushed branch, PR or diff where
available. Use stored repo and branch when sufficient and resolve other
result links on demand under the provider-data limit above.

## Steer

Issue #356 reports that Claude cloud accepts a follow-up with
`claude -p "<message>" --cloud <session>`. A later `subfleet steer <id> "<message>"`
could route there using the recorded provider id. It needs a way to distinguish
provider acknowledgement from an ambiguous send before offering any retry.
Codex cloud steering is manual in the desktop thread. CLI support remains
unverified until it can see the workspace. Local lane jobs still have no
equivalent command to reach a running agent by job id. [Issue #356](https://github.com/MaxGhenis/subfleet-v2/issues/356)

## Account

Remote work draws on the provider account that launched it. A later usage view
should attribute a remote job to that account within its provider, using a
non-email label mapped by the operator. Keep an omitted or unresolved label
unattributed. An account label by itself conveys no authentication. Local lane
slot and disk reserves stay independent of this accounting. A running cloud
session stays with its launching account.

## Open questions

- Which provider CLI versions expose enough status evidence for the mappings
  above, and what is the real Codex task URL shape?
- How should operators select the launching account and workspace for queries
  when a provider CLI has several logins or its desktop sees a different workspace?
- How long should status flag stale data, and should an opted-in remote wait
  return or keep waiting when a provider is unreachable?
- Does either provider expose stable result links on demand, and how should
  multiple turns on a Claude session affect terminal state and result selection?
- Which provider observations prove steer delivery or launch duplication, and
  which usage readings can be associated reliably with the launching account?
