# Failed tests' state roots kept and uploaded: the parts of PR #68 that release/217 lacked, 2026-10-11

Base: `origin/release/217` at `be7afd599`. Source: PR #68's branch
`fix/question-card-race-217` at `00b8144f9` (commits `b72626b76` and
`00b8144f9` for these parts; `27f460867` and `db90edff3` for the probe).
PR #68's race fix is superseded on release/217 by `4a97e16b3` (an approval,
its event and approval-needed commit together, C-27.1) and #175, so
`subfleet/conversations/runner.py`, `store.py`, `tests/frontend/daemon_harness.py`
and `tests/unit/test_approval_visibility.py` were not ported.

## Changes

### 1. CI uploads `/tmp/sf-failed` (`.github/workflows/tests.yml:60-71`)

A step after the test step, `Upload failed tests' state roots`, runs when
`failure() || cancelled()`. It uses `actions/upload-artifact`, pinned by commit
as the workflow pins checkout, setup-python and setup-uv:
`actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1`.
The artifact is `sf-failed-python-<version>-attempt-<n>`, one name per matrix
job and re-run attempt, with `if-no-files-found: ignore`,
`include-hidden-files: true` (so dot-files in a kept root, such as the e2e
workdir's `work/.git`, go too) and `retention-days: 14`. These inputs are #68's.
No secrets, no `permissions` change; nothing else in the workflow changed.

The lane that ported it had no network and checked the pin offline: a 2026-07-09
session's tag table (`~/logpile/.../rollout-2026-07-09T17-07-49-...jsonl`) maps
`actions/upload-artifact@v7.0.1` to `043fb46d...`, and a GitHub Actions log
from 2026-09-22 (`~/reviews/thesis-pr288/wt2/drafts/bill-pipeline-failures/github-35689483342-1/generate.log`)
shows `actions/upload-artifact@v7` resolving to the same SHA.

### 2. The live app test keeps a failed run's root (`tests/frontend/conftest.py:32-39`, `tests/frontend/test_core_live.py:11-12, 20, 39, 67-111`)

**Why it never fired on release/217.** `dev_daemon` set `failed = True`,
yielded, then set `failed = False` after the `yield`, and deleted the root when
`failed` was false. pytest tears a yield fixture down by calling `next()` on its
generator (`_pytest/fixtures.py`, `_teardown_yield_fixture`, read in pytest
9.1.1 this session); it never throws the test's exception into the generator.
So the line after the `yield` ran on every run that got past setup, failed or
not, and every failed run's root was deleted. Only a daemon that failed to
start kept its root, and then at `/tmp/sf-app-*`, which nothing uploaded. No
`pytest_runtest_makereport` hook applied to `tests/frontend` either: the e2e
and fake harnesses' hooks are in their own directories' conftests.

**Fix.** `tests/frontend/conftest.py` gains the same hook the e2e and fake
conftests have (each phase's report stored as `item.rep_<phase>`).
`dev_daemon` keeps the root when the setup or call report failed, when the
daemon did not start, or when `harness.close()` raised (the C-15.1 notice
check); otherwise it deletes the root as before. `keep_failed_root` copies the
root to `/tmp/sf-failed/<test>` (the name sanitised as the other harnesses
sanitise it, sockets left out with the e2e harness's `_sockets`) and adds the
probe's record under `probe/`: `live-checks.json`, `live-state.json` (release/217
writes it when a required step stops the run), `exchanges.jsonl`, and `app/`
(the app's outbox and staged attachments). It then deletes the original root.
A copy that raises `OSError` leaves the original in place and prints where.

### 3. The probe records what the first list returned (`tests/frontend/CoreProbeLive.swift:134-139, 240-247, 289-309`)

Release/217's probe already stops at a failed required step
(`LiveRun.require`, `cb739b4d7`), which covers #68's `endRun`. Still missing:

- `listing(_:)` (`:134-139`) projects one `approval.list` answer (approval id,
  message, request id, kind, state). The checks "the card joins its approval
  id" (`:247`) and "the question card joins its approval id" (`:295`) record it
  beside the card. A card left without an id now shows whether the daemon listed
  nothing when the app read it (C-27.1's race) or listed something the card did
  not join (a C-27.5 join fault).
- Step 6 answers the question inside its own `do`/`catch` and requires "the
  question is answered as the app" (`:303`, `:307`), as step 5 requires
  "approval.respond accepted". Before, an `approval.get` or `approval.respond`
  error left step 6 through the generic "the run ends without an error".

### 4. A deterministic e2e regression for C-27.1 (`tests/e2e/conftest.py:54-65`, `tests/e2e/test_conversations.py:421-445`)

The e2e observers gain `SUBFLEET_E2E_APPROVAL_PUBLISH_DELAY_S`: inside the
daemon, `ConversationStore._publish` sleeps that long before writing a file
whose parent directory is `approvals` (an approval's exact request), and
nothing else changes. With the variable unset the observers are unchanged.

`test_an_approval_is_listed_as_soon_as_its_event_is_read[[fake:approval]-tool]`
and `[...[fake:question]-question]` start the daemon with a one-second delay,
follow `conversation.events` (long poll, `wait_s=5`) until `approval.requested`,
then call `approval.list` and `message.status` at once. They expect exactly one
approval: this message, the event's `request_id` (C-27.5, an addition for
release/217, whose list carries `request_id`), the kind, `pending`, and the
event's display; and the message `approval-needed`.

### 5. Docs (`docs/lanes/README.md:23`)

The paragraph on kept state roots names the live app test, its `probe/`
record, and the CI artifact.

## Verification

One pytest process at a time, under the shared suite lock, 21:00Z to 22:43Z on a
machine whose one-minute load ran from 16 to 232. Logs are outside the repository
(`~/reviews/subfleet-2110/pr68-split/verify2/`).

| Check | Result |
| --- | --- |
| Core probe build (`compile_probe`) | built in 328 s |
| New e2e test, release/217's store, run 1 | 1 passed; 1 stopped in `E2E.start` (the harness's 10 s wait for the daemon) |
| Run 2 | both cases stopped in `E2E.start` |
| Run 3 | 2 passed |
| New e2e test, mutated store, run 1 | both cases stopped in `E2E.start` |
| Mutated store, runs 2 and 3 | both cases failed at the test's own assertion: `approval.list` returned `[]` (`tests/e2e/test_conversations.py:443`) |
| `tests/frontend/test_core_live_failure.py` | 1 passed |
| A forced failure through the new `dev_daemon` | root kept at `/tmp/sf-failed/<test>` with `probe/live-checks.json`, `live-state.json`, `exchanges.jsonl` and `app/support/outbox.json`; the original root removed |
| The same forced failure through be7afd599's `dev_daemon` | nothing under `/tmp/sf-failed`; the original root removed |
| `tests/frontend/test_core_live.py`, run inside a Subfleet lane | stopped at step 5: the daemon refused `approval.get` as person-only, because the caller carried a lane's attempt markers. Root kept, 22 checks recorded, and "the card joins its approval id" passed with its listing recorded |
| `tests/e2e/test_conversations.py`, whole file | 25 passed, 14 skipped, 3 failed, 2 errors in 1,010 s |

The mutation makes the event batch commit alone before the publish, as it did before
`4a97e16b3`: six lines at the top of the store method that commits an approval with its
events. On the unmutated store no run of the new test reached its body and failed; every
failure there was the harness's start wait.

The whole-file failures are in tests this branch does not change:
`test_a_person_who_resolves_a_claude_message_delivered_then_chooses_how_to_go_on` (a
30 s `until_state` wait), `test_turns_that_cannot_show_changes_say_why` (`failed` where
`complete` was expected, then `ENOTEMPTY` removing the root at teardown),
`test_a_handoff_is_refused_while_the_source_has_a_live_turn` (a 30 s `until_state` wait)
and `test_a_claude_conversation_hands_off_to_codex_with_its_pending_messages` (the
harness's 10 s start wait, in setup). The load was 41 when the file started and 178 when it ended. They were not rerun
on a quiet machine, so their cause is not established here.

The `actions/upload-artifact` pin was confirmed against GitHub on 2026-10-11:
`git/ref/tags/v7.0.1` is commit `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`. v7.0.2
(2026-10-07, download retries on HTTP 429) exists; this keeps #68's version.

## Not run, or not shown here

- The upload step itself: it runs only in GitHub Actions, on a failed or cancelled job.
- `tests/frontend/test_core_live.py` to its end: a lane's caller is refused at step 5, so
  steps 5 and 6 (and the new "the question is answered as the app") ran nowhere here. CI
  runs it outside a lane.
- A collection of `tests/e2e` and `tests/frontend` as its own step; CI collects both.
- The whole e2e file on a quiet machine (see above).
