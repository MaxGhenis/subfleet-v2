# Conversation continuation — 2026-10-04

Implemented on `feat/conversation-continuation`, from
`f832e6b812bb94ed59318344992d53437010649c`, in the assigned worktree only.
The live state root and daemon were not used. Shared Git metadata is outside
the writable workspace, so commits are in `.git-local`; the accompanying
`2026-10-04-continuation.bundle` names this branch and requires that base.

The daemon now batches completed child runs into a durable Subfleet message
and starts the next turn once the current turn and lease end. Completed run
ids and wake requests are consumed in the message transaction; notice delivery
is repaired after restart. Accepted deliverables use their attempt path;
failed runs never advertise an unexported output. Wakes wait for a running
turn to end because the existing steer path admits personal messages only.

`subfleet wake --runs ID... --pr OWNER/REPO#N... --at ISO --note TEXT`
registers against the calling turn or bound native session. A request fires
once when any of its kinds becomes ready: all named runs, any watched PR event,
or its time. PRs share one batched `gh api graphql` call, at most once a minute.
Requests and polling baselines survive restart. Person input wins at acceptance
and dispatch; unstarted wakes at capacity yield their job and remain queued.

Final-text grammar:

```text
WAKE-ME: runs=ID[,ID...] prs=OWNER/REPO#N[,OWNER/REPO#N...] at=ISO note="TEXT"
```

Only consecutive trailing lines are parsed. Fields are unique, optional,
whitespace-separated, with shell quoting; each line needs a trigger. A line
is an independent idempotent request. There is one pending request per kind
per conversation, at most 16 targets per kind, and timed requests must initially
be at least five minutes away. Eight wakes without personal input throttle the
conversation to one wake every 30 minutes. Eight allows several build/review
rounds; the cooldown bounds loops while allowing unattended progress.

Blocks record their own timestamp. A later catalog transcript mtime clears a
superseded Claude block only after the same fresh external-writer check used
by C-26.3, with no live/queued turn or held lease. The durable event is
`conversation.unblocked`, reason `continued-elsewhere`; unknown delivery is
never claimed or replayed. Native history uses catalog-resolved paths and a
bounded transcript read on the file pool. Outside turns survive binding and
event replay, are labelled “Made in another app”, and appear chronologically;
opening/switching scrolls to the newest row at the bottom.

C-24.10–12 and desktop design D-28–30 specify the behavior and limits.

Validation counts below are per slice, with overlap; they are not a summed
unique-test count. JUnit artifacts are under the ignored `build/` directory.

| Foreground slice | Result |
| --- | --- |
| Final wakes, CLI, scripted provider, native ownership, lost notice | 30 passed, 3:28 |
| Store, service, handoff dispatch | 132 passed, 1 deselected, 7:27 |
| Wakes, history, reconcile | 79 passed, 2:40 |
| Catalog, steer service, state reads | 122 passed, 1 sandbox failure, 4:42 |
| Notices and daemon end-to-end | 136 passed, 43 skipped, 2 failures, 2:45; both notice failures addressed below |
| Operator notices and notice headers, after person-hook isolation | 101 passed; final slice separately passes the remaining lost-notice case |
| Frontend timeline and protocol | 28 passed, 2:25 |
| Frontend store, client, outbox and steer | 83 passed before interruption; one refusal case failed, then passed alone in 1:34 |
| Frontend steer properties | 2 passed, 5:34 |
| Frontend approval/card examples and diff | 46 passed before interruption during a separate draft-probe compile |
| Frontend Markdown static recheck | 27 passed, 1 ten-second nesting timeout, 1:23 |
| Frontend isolated nesting cases | 4 passed, 1 ten-second nesting timeout, 0:43 |
| Frontend question cards, titles and failed-dependency handling | 12 passed, 0:43 |

The final 30 include four real-service/scripted-provider scenarios: run
completion, trailing `WAKE-ME:`, a stubbed PR merge, and a timer each create
exactly one Subfleet message and dispatch a second provider turn. Hypothesis
checks holds, at-most-once requests, throttling, personal FIFO, and the
differential moot-block/live-writer rule. Companion binary-provider end-to-end
tests are present; four new wake cases are among the 43 skipped because this
sandbox denies the required `sysctl`/`ps` identity inspection.

The catalog lifecycle failure is its final `/bin/ps` process-census assertion,
after daemon/catalog shutdown. One existing service-close test was deselected
after its daemon setup failed on the same denied boot-identity inspection.
Notice test isolation removes inherited Subfleet launch markers from the
simulated person's hook and supplies an explicitly absent process table for
the fake lost guardian; production liveness behavior is unchanged. The
unchanged Markdown parser's ten-second bound timed out in a broader run and
its isolated recheck. Several exploratory combined slices exceeded ten minutes
before interruption; final focused runs use an eight-minute ceiling and exact
child-process cleanup. No live application was launched.

Four mutation checks were caught, with no survivors: bypass blocked holds,
disable throttling, leave a fired request pending, and clear a block despite
a live writer. `2026-10-04-continuation-mutations.json` records the results;
the mutation runner restores every source edit in `finally`.

`app/build.sh` succeeded within the 15-minute allowance, validated its plist,
and signed `build/Subfleet.app`. The invocation used writable compiler caches
and `SUBFLEET_SWIFT_NESTED_SANDBOX=off` for Swift's optional inner compiler-plugin
sandbox, which cannot start under this managed sandbox. The mandatory outer
sandbox remained in force. Existing Sendable/unused-await warnings remain.
All foreground command sessions were reaped; no task process is left running.

Implementation commits: `644917e` (backend), `74e8650` (timeline, contract,
design and build/probes), and `8cd43c9` (replay/polling/deliverables and provider
validation). The bundle also contains the final report commit. Every commit
has the requested Claude Opus 5.5 co-author trailer. Nothing was pushed.

## REQUEST CHANGES follow-up (PR 127 at 7504f3b8)

Work is confined to the assigned review worktree. No live state, daemon,
installed application, Application Support files, routing aliases or default
policy are changed. Shared Git metadata refused a branch write, so the fixes
use `.git-local`, branch `subfleet/pr127-review-fixes`; the final bundle requires
7504f3b8. No pushes or history rewrites are made.

CI was addressed first. The eight parametrized MCP scope tests reproduced
with their exact old environment assertions; the ledger check reproduced its
three orphan clauses. A service-level reproduction of CoreProbeLive's tenth
failure returned no native history before catalog discovery. The native live
probe itself requires process inspection unavailable in this lane.

| Finding | Fix and regression | Head result | Fixed result | Mutation |
| --- | --- | --- | --- | --- |
| P1 CI environment (8 cases) | `tests/unit/test_claude_conversation_mcp_scope.py`: retain exact flags and expect the two intentional turn markers | 8 failed | 8 passed | reverting marker expectations: 8 failures |
| P1 CI ledger | `docs/desktop/ledger.json`: R-11–13 cite C-24.10–12 with implementation evidence | 1 failed | 4 ledger tests passed | removing C-24.10: 1 failure |
| P1 CI empty native history | `history.page` and `op_conversation_history`: prefer catalog paths, then recorded attempt paths or the exact Claude workspace path; no tree scan | `test_history_before_first_catalog_pass`: failed | passed | disabling the exact path fallback: 1 failure |

The first fixed CI slice was 13 passed on Python 3.14. Mutation details are
in `2026-10-04-continuation-review-mutations.json`. Final line references,
base classifications, suite counts, app build and bundle head follow below.

The two account-burning P1s are fixed in `wakes.py`: first observation only
establishes a baseline; first-poll checks, reviews, merges and closes count
only with timestamps strictly after registration. Partial GraphQL errors
(including exit 1 with useful `data`) affect only their alias. A missing or
inaccessible watch produces one explicit refusal message in its conversation,
consumes that request, and cannot suppress another conversation's wake.
`test_property_unchanged_watched_pr_never_wakes` fails on 7504f3b8 (old completed
checks, minimal case) and passes with the fix. Both partial-error exit-code
cases fail on the head and pass fixed. The focused PR/CLI slice is 9 passed;
mutations restoring first-poll events and whole-batch rejection were killed
(1 and 2 failing tests respectively). The property resets the persisted poll
clock for each generated case so shrinking cannot be hidden by previous cases.
