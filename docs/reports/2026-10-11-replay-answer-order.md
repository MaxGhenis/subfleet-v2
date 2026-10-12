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

Validation is blocked by the required shared pytest lock. The baseline waited for over 60 minutes without starting pytest; its output log remained empty, and a nonblocking lock probe returned exit 75 (`already locked`). Only this job's waiting command was interrupted (exit 130), leaving the other workspace's lock holder alone. No suite lock was held by this job. Six touched Python files passed syntax compilation, `git diff --check` passed, and the working tree was clean before this report update.

| Check | Executed | Result |
| --- | ---: | --- |
| Baseline: 12 cancellation cases, six live queue cases, strict history property | 0 | blocked before pytest started |
| `test_approval_resolution.py` | 0 | blocked |
| `test_approval_commit.py` | 0 | blocked |
| `test_turn_runner.py` | 0 | blocked |
| `test_turn_replay.py` | 0 | blocked |
| `test_claude_turn.py` | 0 | blocked |
| `test_codex_turn.py` | 0 | blocked |

Property counts: the old setting was 80 generated examples; the new setting is 500 generated examples plus one explicit formerly exempt history. Executed generated examples: 0. Executed explicit examples: 0. Before/after failure and pass counts are unavailable because the baseline never started; no failing or passing pytest result is claimed.

Before, answer synchronization queued a response and left the driver pending when a later cancel arrived. The changed code applies startup-recorded answers before that cancel and preserves the person's sole decision event. This behavior is asserted by the new regressions but remains unverified at runtime until the lock becomes available.

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

Commits before this validation report: `c04c90feb` (strict regressions), `5189b6b07` (restore paths and contract), `84e8e00d7` (durable startup answer detection), `52f88ff7a` (caught-up live coverage and initial report). The final head is on `refs/heads/fix/replay-answer-order` in `.git-local`. A local delivery bundle at `docs/reports/2026-10-11-replay-answer-order.bundle` contains this job's commits and requires the base commit above.

Tests use one pytest process at a time under `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, with `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-replay-$$/`. Temporary roots are cleaned with the requested `python3`/`shutil.rmtree` command. Dependencies and Hypothesis storage are local to `.venv` and `.uv-cache`; pytest uses `UV_NO_SYNC=1` and `UV_OFFLINE=1` after installing the locked dependencies.

The baseline uses an unchanged source copy under `.uv-cache/replay-baseline`, selected with pytest's `pythonpath` override and `--import-mode=importlib`. Its runner is byte-for-byte equal to the base commit (SHA-256 `144692c540f4ec5f768895e21e9810dbf24fec4649ab7d3f09691f1d45b6077c`). This allows implementation work to continue while the baseline run waits for the shared lock.

Fix round 1 starts from `ca4a536777508072208bbfb835c741d61a251175` in the assigned workspace. CI's 14 failures all reach the assertion that replay writes no change-feed rows: `ConversationStore.add_approvals` preserves the answered approval but unconditionally moves its running message to `approval-needed`; restoring the answer moves it back to `running`. These assertions are required by C-26.6's idempotent replay and C-27.3's durable resolutions, so they will remain intact. The missing guard belongs inside the approval transaction and must still allow a genuinely pending request to move the message to `approval-needed`.

The filesystem CI error is independent: all HFS+ identity assertions passed, and the fixture's `hdiutil detach` failed at teardown with exit 16 (`Resource busy`). `subfleet/folders.py` and `tests/unit/test_filesystem_identity_agreement.py` are byte-for-byte unchanged from `release/217`; neither imports the changed conversation code. No filesystem fix is part of this round. The head reproduction is running in the foreground under the required suite lock; measured results will replace the original validation section when complete. Commits for this round use the assigned branch in workspace-local `.git-local` metadata because the shared Git metadata is outside the writable workspace.
