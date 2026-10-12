# Replay answer ordering — 2026-10-11

Base: `be7afd5992a660b68c3ccd2edf3fc16e92578e50` (`origin/release/217`, #175). Branch: `fix/replay-answer-order`, with writable Git metadata in this workspace's `.git-local`. The caller's checkout and shared Git metadata were not changed; no Git remote was used. Every commit carries `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

`subfleet/conversations/runner.py:564` now applies recorded answers synchronously during reconstruction, immediately after the request becomes pending and before the next stdout line. Written replies use `ClaudeTurn.restore_answer` (`claude_turn.py:326`) or `CodexTurn.restore_answer` (`codex_turn.py:239`), with the existing command event key. The runner snapshots already answered request ids at construction; later answers remain live commands even during startup. Missing replies use the same response construction, validation and sending guards as live responses (`runner.py:452`). The relay's frame tags and the saved terminal outcome still prevent duplicate or unauthorized writes.

Live answers keep their command queue timing, including a first runner still catching up with its initial stdout. Both drivers restore the `cancel-turn` interruption state. Claude also preserves the separate, tagged interrupt that sweeps queued steers, so a crash between the answer and that interrupt can recover the latter.

Caller audit: `_read_stdout` is the sole production caller of `_sync_answers`; it calls it after each complete nonblank line. The approval property calls it directly to model replay, and the saved-terminal replay test disables it on the first runner to model an answer not handed over. The service still records the person's answer and calls `TurnRunner.respond`, which queues the command. C-26.6 and C-27.3 now explicitly state answer ordering and preservation of the person's sole resolution event.

Regression coverage:

- `tests/unit/test_approval_resolution.py:354`: 12 cases, both providers × three decisions × written/unsent replies. A request and later cancel share one stdout read. The card remains answered, with exactly one decision event, no withdrawn event or new change-feed row, and no reconstruction of a written reply.
- `tests/unit/test_approval_resolution.py:405`: six live queue cases, covering startup, a live first runner and a caught-up adopted runner.
- `tests/unit/test_turn_replay.py:135`: both providers with a real relay, actual pipe output and a reopened durable store. Restart leaves the relay log unchanged and writes no second reply.
- The approval history property removes the known-case exemption, requires exact event equality after replay, pins the formerly exempt history as an explicit example and increases generated examples from 80 to 500.
- The Claude queued-steer cancellation test covers live and restored answers.

Fix round 1 has executed tests. The initial unchanged-head run selected all six required files under the shared lock on CPython 3.14.7 free-threaded. It completed 32 tests: **12 failed, 20 passed**, all failures at the replay change-feed assertion. It was interrupted during history generation after 912.71 seconds to keep the hold below 20 minutes. This partial run is not counted as completed validation. Its temporary tree was explicitly removed with the requested `python3`/`shutil.rmtree` command.

| Check | Executed | Result |
| --- | ---: | --- |
| Initial unchanged-head `test_approval_resolution.py` | 32 | 12 failed, 20 passed; property interrupted |
| Standard CPython 3.14.7 pre-fix run, history deferred | 182 | 14 failed, 168 passed; interrupted during Codex setup |
| Complete pre-fix comparison on standard CPython 3.14.7 | 267 | 14 failed, 253 passed in 149.51 s |
| `release/217` comparison, all six files | 267 | 16 failed, 251 passed in 67.33 s |
| Fixed head, all six files and complete history property | pending | not started |

The history property retains 500 generated examples plus the explicit formerly exempt history. The initial run began generation but did not finish; its partial example count was not instrumented and is not claimed. Complete property runs will have Hypothesis statistics and a local counter of calls to the unchanged test body. No test has been weakened, deleted or reduced in example count.

The standard pre-fix run reproduced all 14 failures, including both real-relay restart cases, in 252.99 seconds. Its completed-file counts were: approval resolution 20 passed / 12 failed (history deferred), turn replay 31 passed / 2 failed, Claude driver 61 passed, approval commit 6 passed, turn runner 50 passed. Codex setup was interrupted before its first completed test; this run is partial and the complete six-file comparison is queued. Output was buffered until input reached the terminal, so later polls send a harmless newline and complete runs will supersede these partial counts.

The complete pre-fix rerun supersedes those partial results: **14 failed, 253 passed in 149.51 seconds**. Per file: approval resolution **21 passed / 12 failed**, turn replay **31 passed / 2 failed**, Claude driver **61 passed**, approval commit **6 passed**, turn runner **76 passed**, Codex driver **58 passed**. Its four original files reproduce the supplied host count exactly: **14 failed, 119 passed**. The history property reports **500 passing generated examples**, **0 failing**, **73 invalid generation attempts**, plus the passing explicit case; the unchanged test body's instrumented call count is **501**. The generated phase took 58.55 seconds, and the complete hold stayed below 20 minutes. The identical test files are now queued against the archived `release/217` implementation.

The complete release comparison finished: **16 failed, 251 passed in 67.33 seconds**. Per file: approval resolution **20 passed / 13 failed**, turn replay **31 passed / 2 failed**, Claude driver **60 passed / 1 failed**, approval commit **6 passed**, turn runner **76 passed**, Codex driver **58 passed**. The 12 cancellation cases and two relay cases write an extra `withdrawn` event; the explicit history fails for the same reason, and the Claude written-answer steer test fails because `restore_answer` is absent. The explicit history executes once and fails before generation: **0 generated examples, 1 explicit example**, counter **1**. The four original files here collect the same 133 cases as the head: **16 failed, 117 passed**, rather than the supplied earlier baseline's 127 cases (16 failed, 111 passed). All six live-answer queue cases are included in both current comparisons. The fixed-head comparison is queued next.

Commit `cfb97426d` makes each history copy a closed, fully initialized empty database into its own private state root, avoiding repeated schema DDL. The actual per-history store, file publication, transactions, generated operations and assertions remain real and unchanged. AST comparison confirms all 53 assertions and the property's decorators are identical to `ca4a53677`: 500 generated examples plus the explicit case, with no exemption. The local validation probe also interrupts a running pytest after 18 minutes, leaving cleanup time within the required hold cap; interrupted runs are never reported as completed validation.

On `release/217`, answer synchronization queued a response and left the driver pending when a later cancel arrived. At `ca4a53677`, startup-recorded answers are already applied before the cancel, but approval reconstruction writes spurious `approval-needed` and `running` changes. Fix-round commit `02f52e131` checks for pending approvals inside `ConversationStore.add_approvals`' transaction before moving the message to `approval-needed`. New requests still publish their pending state atomically. Complete before/after runtime validation is in progress.

Rerun all required suites from the workspace once the shared lock is available:

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)sf-replay-$$/"
mkdir -p "$TMPDIR"
trap 'python3 -c '\''import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)'\'' "$TMPDIR"' EXIT
export UV_CACHE_DIR="$PWD/.uv-cache" UV_NO_SYNC=1 UV_OFFLINE=1
export HYPOTHESIS_STORAGE_DIRECTORY="$PWD/.uv-cache/hypothesis"
/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q \
  tests/unit/test_approval_resolution.py tests/unit/test_approval_commit.py \
  tests/unit/test_turn_runner.py tests/unit/test_turn_replay.py \
  tests/unit/test_claude_turn.py tests/unit/test_codex_turn.py \
  --hypothesis-seed=20261011 --hypothesis-show-statistics
```

Fix-round commits are on `refs/heads/fix/replay-answer-order` in this assigned workspace's `.git-local`, starting from `ca4a53677`: `7841a9766` records the diagnosis and `02f52e131` adds the pending-state guard. No bundle or evidence log is committed.

Tests use one pytest process at a time under `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, with `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-replay-$$/`. Temporary roots are cleaned with the requested `python3`/`shutil.rmtree` command. Dependencies and Hypothesis storage are local to `.venv` and `.uv-cache`; pytest uses `UV_NO_SYNC=1` and `UV_OFFLINE=1` after installing the locked dependencies.

Comparisons use unchanged `git archive` source copies in `.uv-cache/release-217` and `.uv-cache/head-before`, selected with pytest's `pythonpath` override and `--import-mode=importlib`. The release runner's SHA-256 is `144692c540f4ec5f768895e21e9810dbf24fec4649ab7d3f09691f1d45b6077c`; the pre-fix runner's SHA-256 is `2aeaca567ed4287280b26c52df369159c12d43ab314ba94c5b1b978c6c71f9cc`, identical to the assigned starting source. A local pytest probe verifies the imported runner, store and both drivers belong to the selected source tree.

Fix round 1 starts from `ca4a536777508072208bbfb835c741d61a251175` in the assigned workspace. CI's 14 failures all reach the assertion that replay writes no change-feed rows: `ConversationStore.add_approvals` preserves the answered approval but unconditionally moves its running message to `approval-needed`; restoring the answer moves it back to `running`. These assertions are required by C-26.6's idempotent replay and C-27.3's durable resolutions, so they will remain intact. The missing guard belongs inside the approval transaction and must still allow a genuinely pending request to move the message to `approval-needed`.

The filesystem CI error is independent: all HFS+ identity assertions passed, and the fixture's `hdiutil detach` failed at teardown with exit 16 (`Resource busy`). `subfleet/folders.py` and `tests/unit/test_filesystem_identity_agreement.py` are byte-for-byte unchanged from `release/217`; neither imports the changed conversation code. No filesystem fix is part of this round. The head reproduction is running in the foreground under the required suite lock; measured results will replace the original validation section when complete. Commits for this round use the assigned branch in workspace-local `.git-local` metadata because the shared Git metadata is outside the writable workspace.
