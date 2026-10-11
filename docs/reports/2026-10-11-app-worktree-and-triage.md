# The app's worktree toggle and sidebar triage

Branch `feat/app-worktree-and-triage`, stacked on PR #181 (`feat/suggested-conversations` at 9787fe323). Max asked for both on 2026-10-11 ("sure"); they are items 7 and 1 of `~/reviews/subfleet-2110/one-place/inventory.md`.

Status: DRAFT, written while the tests run. The sections below marked PENDING are filled in at the end.

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

PENDING: file:line list.

## Tests

PENDING: counts.

## Screenshots

PENDING.

## Not run

PENDING.
