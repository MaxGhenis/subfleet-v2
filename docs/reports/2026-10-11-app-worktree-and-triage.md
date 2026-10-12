# The app's worktree toggle and sidebar triage

Branch `feat/app-worktree-and-triage`, stacked on PR #181 (`feat/suggested-conversations` at 9787fe323). Max asked for both on 2026-10-11 ("sure"); they are items 7 and 1 of `~/reviews/subfleet-2110/one-place/inventory.md`.

Status: review round 1 fixes implemented directly on `b3b5f56e269b`, without newer #181 commits. Verification results for this round are recorded below. Commits are on `feat/app-worktree-and-triage` in workspace-local `build/fix1-metadata`; the shared Git index is outside the writable workspace. Inspect with `git --git-dir=build/fix1-metadata log`. No Git remotes were contacted.

## Review round 1 (2026-10-12)

Read the full review at `~/reviews/subfleet-2110/one-place/p1-build/review-r1-full.md`, its probe file, and the supplied host log `~/reviews/subfleet-hub/host-runs/run-189-r1probes.log`. The four probes are copied verbatim into `tests/unit/test_review_pr189_r1.py`, with a unique test-module basename.

- **P1: archive wins over recovery.** `restore_after_handoff` checks `archived_at` inside the same `BEGIN IMMEDIATE` transaction that would requeue messages. An archived source refuses recovery with exit 7 `archived` and an instruction to unarchive and retry. Its cancelled messages stay terminal, with their rows, text, binding and history intact. This also covers older unfenced handoffs. Recovery does not silently undo an accepted archive. Contract clauses C-24.13/C-25.2 and C-30.3 state the rule; the service recovery path documents the refusal.
- **P2: remote archive state reaches existing focused rows.** `changes_after` enriches rows with current `archived` and `archived_at`, as it already does with titles. The Swift watch fold updates cached conversations before a filtered list can retain the focused row. An explicit false clears archive on unarchive; absent fields from an older daemon preserve cached archive state. Conversation-level feed changes trigger a list refresh. The timeline stays open, the active sidebar hides the archived row, and local pin/read marks survive. C-29.9/C-29.15 describe the wire and app behavior.
- **Tests:** the existing two-connection transaction test now covers archive/submit and archive/recovery in both write orders. Exhaustive enumeration adds all 40 valid orders of archive, handoff start, failure/settlement, recovery and submit, each with and without a durable fence: **80 cases, 400 operation steps defined**. Recovery may precede settlement. Every step checks terminal-only archived messages, every accepted message's text, preserved events and the accepted archive timestamp. A real handoff retry test verifies refusal followed by successful unarchive/retry and text movement. A new Swift core test uses real daemon list/watch answers and the existing `CoreProbeTriage.swift` harness to inspect focused archive state, active sidebar visibility, pins, read marks and remote unarchive, plus compatibility with an older feed.
- **Swift cleanup:** extracted the branch's existing compiler timeout cleanup into `tests/frontend/swift.py::run_swift` so the single `app/build.sh` attempt and core probes use the same task-owned process-group kill/reap behavior. `app/build.sh` itself is unchanged.

### Round 1 validation

| Validation | Executed | Result |
|---|---:|---|
| Supplied host baseline at `b3b5f56e269b`, all four reviewer probes | 4 | **2 passed, 2 failed**, in 30.49 s. Both requested probes failed: archived handoff recovery left a queued message, and the remote archive left an active Swift sidebar entry. This is supplied host evidence, not a local rerun. |
| Local baseline probes (two attempts before product edits) | 0 | Both exited 75 before pytest started: required shared lock occupied. |
| Archive unit tests and both existing 60-example properties | 0 tests / 0 examples | Not run: shared lock. |
| Archive/submit and archive/recovery transaction orders | 0 of 4 | Not run: shared lock. |
| Exhaustive handoff/archive/submit enumeration | 0 of 80 cases / 0 of 400 steps | Not run: shared lock. Defined cases cover both fence variants and all valid total orders. |
| Existing handoff, handoff briefs and dispatch-race files, including the new unarchive/retry case | 0 | Not run: shared lock. |
| Test-module basename check | 0 | Not run: shared lock. The reviewer file is copied verbatim under `tests/unit/test_review_pr189_r1.py`. |
| Head reviewer probes | 0 | Not run: shared lock; no head pass or failure count can be claimed. |
| Swift core triage/protocol tests and reviewer probe | 0 | One head core-suite attempt exited 75 before pytest or Swift compilation started. |
| Guard-removal mutation | 0 | Removed only the new `restore_after_handoff` archive guard for a bounded foreground attempt. The lock command exited 75 before pytest started. The exact original source was restored in `finally` and verified present; no mutant is committed. |
| `app/build.sh --dev build/fix1-dev` | 1 attempt, 0 completed builds | Timed out after 540 s, without diagnostics; `run_swift` killed the task-owned process group and reaped its direct child. Staging directories and its private temporary directory were removed with Python. No app bundle or compiler runner remains. No retry. |
| `uv sync --locked` | 1 | Passed; dependencies installed only in this workspace. |
| Python AST parsing of changed product/test Python files and the copied probes | 7 files | Passed. |
| `sh -n app/build.sh` | 1 | Passed. |
| `git diff --check` | 1 | Passed. |

**Local pytest total: 0 executed.** Seven foreground shared-lock attempts were made: two baseline, one P1 suite, one head Python suite, one head core suite, one mutation, and one final Python suite. Six used a 45-second acquisition timeout; the final attempt allowed 120 seconds. All exited 75 before acquiring the lock, so no hold ran and no tests ran. Each had its own `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-r189f1-<slice>-$$/`, cleaned with the requested Python `shutil.rmtree` command. No runner was detached or left running, and no lock was broken, replaced or bypassed.

The Python suites requested `tests/unit/test_conversation_archive.py`, `test_conversation_archive_properties.py`, `test_conversation_archive_transactions.py`, `test_conversation_handoff.py`, `test_conversation_handoff_briefs.py`, `test_conversation_handoff_dispatch_races.py`, `test_test_module_names.py`, and the copied reviewer file (excluding its Swift remote-archive case in the Python slices). The head core attempt requested `tests/frontend/test_core_triage.py`, `test_core_protocol.py`, and all four copied reviewer probes in one process.

The mutation attempt selected the full transaction/enumeration file, `test_archived_handoff_recovery_keeps_the_message_until_unarchive`, and `test_handoff_retry_cannot_requeue_a_message_in_an_archived_source`. Those tests are intended to detect removal of the guard, including archive-first recovery on separate connections and unfenced failure/archive/recovery orders. **Which tests actually fail under the mutation is unverified**, because pytest never started. The hub must run the head suites and mutation on the host to establish passing head probes, executed enumeration cases and observed mutant failures.

The full repository suite and visual screenshots were not run. No local baseline failure or head pass is inferred from static inspection. The build timeout is not evidence of a source-level compiler failure.


### Round 1 commits

- `ab3f926fe` — retain the reviewer probes verbatim.
- `746d491c9` — transactional archive/recovery refusal, contract, exhaustive and two-connection cases, real unarchive/retry test.
- `01726d1b8` — current archive state on the feed, Swift fold/list refresh, core regression test and contract.
- `af7e748c9` — reuse compiler cleanup for bounded app builds.

Only named source, test, contract and report files are committed. Build artifacts, dependency caches, temporary data and evidence logs are ignored and unstaged. No sub-agents, live stores, app windows, shared metadata writes, history rewrites, newer #181 merges or remote operations were used.

The sections below retain the earlier implementation and validation record through `b3b5f56e269b`; their earlier unrun counts are separate from this fix round.

## What the inventory said, checked against the code

| Claim | Verdict | Where |
|---|---|---|
| New conversation always sends `workspace_kind: "in-place"` | True. `UIModel.swift` passed `recovery?.workspace_kind ?? "in-place"` to the folder check and the create; only a refused create's retry carried another kind. | `UIModel.swift:687,739` at 9787fe323 |
| The daemon already cuts and records a worktree | True. `_cut_worktree` adds `worktrees/conversation-<id>` on branch `subfleet/<id>` and records `{path, branch, source, repository, base, created_at}`; `test_a_worktree_conversation_records_and_returns_its_path_and_branch` covers it. | `service.py` `_cut_worktree` |
| `workspace.check` accepts the kind | True, and with `worktree` it skips C-26.10's in-place checks, so a checkout on `main` can start a worktree conversation. | `service.py` `_validate_workspace` |
| `store.update_conversation` accepts `archived_at`; the list hides archived rows | True. | `store.py:877`, `store.py:988` at 9787fe323 |
| No op sets `archived_at` | True. | `protocol.py` `CONVERSATION_OPS` |
| The CLI lists conversations | False: no CLI verb calls `conversation.list`, so no CLI verb was added. | `cli.py` |

What the inventory missed, found while verifying:

- `archived_at` already has teeth. `message.submit` refuses an archived conversation, the dispatcher (`_dispatch_one`, `next_dispatchable`) and the wake engine (`wakes.eligible`) skip one, and `status.json` counts leave it out. A queued message in an archived conversation would never run, so archive must refuse while any message is open, and the refusal must be checked in the same transaction as a submit, or the two can cross.
- `submit_message` did not re-check `archived_at` in its transaction: `message.submit` read the conversation, then accepted the message in a later transaction.
- `conversation.list` excluded from the catalog only the sessions of the conversations it listed. An archived conversation's native session would have come back as a native row, and opening it reopens the archived conversation.
- `include_archived` already existed on `conversation.list`, passed only to the catalog (Codex's own archived threads). It now covers conversations too.

## Decisions

- **Archive is person-only (C-25.6).** An agent's own conversation would be low-risk to archive, but it always holds the agent's running message while the agent acts, and archive refuses that. So every conversation an agent could archive is another's, and archiving it would hide it from the person (a block waiting on them included) and hold its wakes. Unarchive is the same choice undone, so it is a person's too.
- **Refused while any message is open, not only a running one.** Queued, waiting, starting, running, approval-needed, steering and delivery-unknown all refuse (exit 7 `conversation-active`), since nothing dispatches in an archived conversation. Also refused while a handoff of it runs or its fence stands (`handoff-running`) and while a suggestion's Start is making it (`conversation-starting`). A blocked conversation may be archived and keeps its block.
- **Archive does not move `updated_at`**, so unarchiving does not jump a month-old conversation to Today. It writes one feed row so a watching client learns of it.
- **Unread uses the watch feed's `seq`.** The task said "event sequence"; `conversation.events` is read only for the focused conversation (C-29.9), so the app has no event sequence for the others. The watch feed's change `seq` is the one sequence the app has for every conversation. A conversation is unread when a change to one of its messages arrived after the newest one the person saw it at, and it is not focused. On first launch everything already there counts as read (the floor), and a feed that restarts below the floor (a new store) resets the marks.
- **Unread first is a section.** With it on, unread conversations (other than pinned ones) are listed in an Unread section after Pinned and before the date groups; each conversation appears once.
- **Pins are ordered newest pin first**, so a new pin appears where the person is looking. Only conversations can be pinned or archived; native sessions cannot until opened.
- **The repository key for the Worktree toggle is the common git directory**, found with `realpath(3)`, so a linked worktree shares its repository's remembered choice. Foundation's `resolvingSymlinksInPath` strips `/private` and would disagree with git and the daemon.
- **The toggle defaults to off** for a repository with no remembered choice: the behaviour before this change.

## Changes

The original implementation is retained; the inventory verification and decisions above still stand.

| File:line | Change |
|---|---|
| `subfleet/protocol.py:32`, `subfleet/conversations/service.py:70`, `:89`, `:756` | Archive op, `archive.v1`, person-only authorization and control-loop notification on unarchive. |
| `subfleet/conversations/store.py:987`, `:1104` | Archive's open-message check and submit's archived check are inside their own write transactions; archive preserves activity timestamps and writes one feed row. |
| `subfleet/conversations/service.py:435`, `subfleet/conversations/store.py:1029` | List filtering, `include_archived` and suppression of archived conversations' native catalog rows. |
| `app/Sources/Protocol.swift:128`, `:289`, `:295`, `:615` | Archive wire fields/capability and worktree branch/source accessors. |
| `app/Sources/SidebarTriage.swift:13`, `:91` | Persisted read/pin marks, feed sequence floor and Pinned/Unread section ordering. |
| `app/Sources/ConversationStore.swift:483`, `:699`, `:792`, `:826`, `:953` | Feed-based unread state, archived-only sidebar view, sections and search outside Archived. |
| `app/Sources/UIModel.swift:194`, `:216`, `:228`, `:665`, `:867` | Archive/list actions, mark/pin persistence and worktree selection/remembering. |
| `app/Sources/UIWindow.swift:197`, `:303`, `:476`, `:584`, `:595` | Sidebar filters/actions/unread dots, archived composer notice and header branch/actions. |
| `app/Sources/NewConversationDraft.swift:28`, `:73`, `app/Sources/NewConversationWorkspace.swift:62`, `:97`, `app/Sources/UINewConversation.swift:33` | Journaled worktree choice, checks/create kind, discovery by common git directory and toggle. |
| `tests/frontend/test_core_triage.py:102`, `tests/frontend/CoreProbeTriage.swift:102` | Nine core scenarios for archive, unread, pins, draft persistence, common-directory defaults and real daemon requests. |
| `tests/frontend/test_visual_triage.py:36`, `tests/frontend/TriageSnapshotProbe.swift:152` | Offscreen light/dark rendering of sidebar sections, Archived and New conversation with Worktree. |
| `tests/unit/test_conversation_archive.py:38`, `:151`, `:235` | Applied the saved test patch: explicit 1,000-row list window, required waiting reason and honest wake-notification test name. Queued/waiting dispatch claims are checked separately. |
| `tests/unit/test_conversation_archive_properties.py:13` | Added 60-example Hypothesis property with real messages/events, full row round-trip, one feed row per transition and idempotent repeats. |
| `tests/unit/test_conversation_archive_transactions.py:18`, `:98` | Added two-connection submit/archive interleavings in both orders and queued/waiting archived dispatch checks. |
| `tests/frontend/swift.py:55`, `app/build.sh:75` | Kill/reap compiler children on interrupted probes; use Python directory cleanup for development builds. |

Contract clauses `docs/acceptance-contract.md:460` (C-24.13), `:465` (C-25.2), `:469` (C-25.6), `:519` (C-29.15) and `:520` (C-29.16) and the desktop op ledger were added by the previous lane and remain binding.

## Tests

**Pytest: 0 executed.** The requested invocation below was started in the foreground, queued for about an hour, then interrupted before acquiring the lock. Its temporary directory was removed with Python. No pytest runner or build was left running.

```sh
TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-p1f-$$/
/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q tests/unit/test_conversation_archive.py
```

A final, genuinely nonblocking diagnostic, `/usr/bin/lockf -t 0 -k /private/tmp/claude-501/subfleet-suites.lock /usr/bin/true`, returned **75**, `already locked`. The same diagnostic against a workspace file returned **0**. No lock was broken or replaced. Permission to use a workspace-local suite lock was requested and was not received; no suite was run under one. Earlier diagnostics incorrectly treated `-n` as nonblocking; the local macOS manual says it prevents file creation, and `-t 0` is the nonblocking option. Those earlier commands were interrupted or killed and reaped.

Counts for the intended validation files are all zero; **Not run** does not mean passed:

| File | Executed | Result |
|---|---:|---|
| `tests/unit/test_conversation_archive.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_archive_properties.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_archive_transactions.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_service.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_store.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_catalog.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_wakes.py` | 0 | Not run: shared lock |
| `tests/unit/test_conversation_status.py` | 0 | Not run: shared lock |
| `tests/unit/test_review_r5_repro.py` | 0 | Not run: shared lock |
| `tests/unit/test_descriptors_desktop.py` | 0 | Not run: shared lock |
| `tests/unit/test_test_module_names.py` | 0 | Not run: shared lock |
| `tests/frontend/test_core_triage.py` | 0 | Not run: shared lock; core compilation also timed out |
| `tests/frontend/test_core_protocol.py` | 0 | Not run: shared lock; core compilation also timed out |
| `tests/frontend/test_visual_triage.py` | 0 | Not run: shared lock and unresponsive Swift tools |

The saved patch was checked against the code and applied in full:

- `listed()` now explicitly asks for the supported 1,000-row window, avoiding accidental truncation of accumulated property fixtures.
- The waiting fixture supplies `readmit:capacity`; `store._set_state` requires a reason for every waiting message (C-24.4). This fixes a fixture error, not daemon archive behavior. Before/after pytest counts are unavailable. The original file is preserved, ignored, at `build/archive-baseline/test_archive_baseline.py` for a focused run of `test_archive_is_refused_while_a_message_is_open[waiting]` once the lock is available.
- The original test is honestly named `test_unarchive_wakes_the_control_loop`. Its empty dispatch had not proved exclusion of pending work. The separate new dispatch test uses queued and waiting archived legacy rows and checks both `next_dispatchable` and a stale `_dispatch_one` call; neither case has run yet.

**Property:** the existing 60-example operation-sequence property is retained. The added 60-example property seeds one to four terminal messages and two real events per message. It checks the full conversation row, message/event rows, one feed row per archive/unarchive transition and no extra feed rows for idempotent repeats. **0 examples executed; result unverified.**

**Interleaving:** two parameterized cases use independent SQLite connections to the same database, pause after archive's open-message read or submit's archived read, assert that the first write transaction stays open, and observe the second writer attempting `BEGIN IMMEDIATE`. Archive-first must refuse submit without a message row; submit-first must refuse archive with the row remaining unarchived. **0 cases executed; result unverified.**

Checks that did complete:

| Check | Result |
|---|---|
| `uv sync --locked` | Passed; dependencies installed in this workspace |
| `python3 -m py_compile` on both added unit modules, the patched archive module and `tests/frontend/swift.py` | Passed |
| `sh -n app/build.sh` | Passed |
| `app/build.sh --dev build/triage-dev` | Did not finish within 900 s; process group terminated/reaped, staging and temporary directories cleaned; no bundle |
| Core probe compile via `tests/frontend/swift.py`, `SUBFLEET_MODEL_TEST`, all six core probe sources | Timed out after 540 s without diagnostics; compiler group killed/reaped, temporary directory cleaned |
| `xcrun swiftc --version`, `xcrun --no-cache swiftc --version`, direct Command Line Tools `swiftc --version` | Each timed out after 45 s; task-owned process groups killed/reaped |

The development build used `--dev` and `SUBFLEET_SWIFT_NESTED_SANDBOX=off`, with a private module cache. Compiler failures provide no evidence of a source-level failure or a successful build.

## Screenshots

**None produced.** `tests/frontend/test_visual_triage.py` could not run under the required shared lock. Its Swift view probe also cannot be considered runnable when even the toolchain's version commands time out. The harness and its six intended 2880 × 1800 light/dark scenes remain committed: sidebar Pinned/Unread sections, Archived, and New conversation with Worktree on. No native window was launched.

## Not run

All pytest files in the table, including the property examples, both interleavings, Swift core behavior and the screenshot harness. The full repository test suite was not run. There are no before/after pytest counts and no validated property or interleaving result. Resume verification when the required shared lock is available (or a different lock is explicitly authorized) and the Swift toolchain can start.

Workspace-local commits are on `feat/app-worktree-and-triage` under `.git-local`; use `git --git-dir=.git-local` to inspect them. The shared checkout metadata remains at the supplied base commit. Only named source/test/report files were staged; logs, evidence JSON, bundles and build artifacts were not committed.
