# PR #179 fix round 2

Base: `5c11d18e62fd`. Work is confined to the assigned workspace. The live switch remains off.

Status: work in progress. The full review and its probe have been read. Both
defects are confirmed by source tracing. The HTTP classifier and contract table
are implemented; executions are pending below.

Required work:

- Classify every consume HTTP outcome conservatively; document the status table in C-23.16 and cover each row.
- Exclude intents proved unsubmitted from the spend oracle; reproduce the 61-second durable-claim delay and control fake-provider transport deadlines in generated worlds.
- Show both regressions failing at the base and passing after the fixes.
- Execute the review's property example counts, including at least one FULL seed of each property, under separate shared-lock holds shorter than 20 minutes.

## Consume outcome table

| Answer | Action class | Basis |
|---|---|---|
| 2xx, `code=reset`, positive integer `windows_reset` (excluding booleans) | `confirmed` | Recognized reset confirmation |
| Other 2xx, including missing/malformed JSON or unfamiliar fields | `unknown` | May have spent |
| Any 3xx, including a reset-shaped body | `unknown` | A redirect proves no refusal |
| 409 with `error.code=already_redeemed` or `already_consumed`; 429 with `error.code=rate_limited`; neither top-level `code` nor `windows_reset` | `failed` (`refused`) | Explicit refusal of this consume |
| Every other 4xx, including empty/unrecognized bodies, 408, conflicting reset fields, or a refusal code on the wrong status | `unknown` | No proof of an unspent credit |
| Any 5xx, regardless of body | `unknown` | May have spent before answering |
| Any other HTTP status outside 2xx–5xx | `unknown` | Unrecognized response |
| No credential, inhibit marker, or deadline/cancel/occupied slot before the call starts | `failed` (`no-auth`, `inhibited`, `aborted-before-submission`) | Nothing submitted |
| Timeout, connection failure, or unexpected exception after the call starts | `unknown` | Submission may have occurred |

Unknown actions block every later consume until a fresh successful account usage
read reconciles them open. Only 2xx responses can directly confirm a reset.

## Execution

The first base regression command waited 300 seconds and exited 75 without
executing any tests. A second command is queued from a workspace-local
`git archive` of `5c11d18e62fd`, with the same six regression assertions copied
in. The run-only budget plugin checks and prints the imported production and
checker paths, ensuring the base reproduction cannot import the edited head.
It selects all five redirect cases and the exact delayed-claim world with FULL.
Each pytest hold has an 18-minute interrupt and 19-minute termination watchdog,
and its own TMPDIR with the prescribed Python cleanup.

The shared Git directory is outside the writable sandbox. Commits use
workspace-local `.git-local` metadata, branch `fix/reset-credit-demand-only-217`,
with the supplied base as its parent and read-only alternate objects. No remote
or caller checkout is changed.

Results, exact counts, FULL/OFF modes, commit heads, and unrun work will be
recorded here as execution completes.

## HTTP fix

Consume responses use a distinct `refused` outcome only for the recognized
409/429 refusals above. Generic read endpoints retain their existing diagnostic
statuses. `http-error` is no longer a no-spend exemption in the action classifier.
The outcome property includes the five redirect statuses, empty 403, 408,
reset-shaped 409, and conflicting 429. The round-2 file checks each table row
through real intent/claim/publication and the next evaluation at +60 seconds;
its adapter checks exercise every integer status 100–699 through both returned
responses and raised `HTTPError`, with six body shapes (7,200 requests).
