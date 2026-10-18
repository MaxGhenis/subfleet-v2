# Replay answer ordering — 2026-10-11

Fix round 1 completed on 2026-10-12. All **267 required tests pass** at the fixed implementation. The same six files give **14 failed / 253 passed** on the PR's starting code and **16 failed / 251 passed** on `release/217`.

Base: `be7afd5992a660b68c3ccd2edf3fc16e92578e50` (`release/217`). Assigned starting head: `ca4a536777508072208bbfb835c741d61a251175`. Work stayed in the assigned workspace. Commits are on `refs/heads/fix/replay-answer-order` in workspace-local `.git-local`, because the shared Git metadata is outside the writable workspace. No files were written in the caller's checkout, no network remote was used, and no sub-agents were spawned. Every commit carries `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

At `release/217`, replay queued the recorded answer. A later provider cancellation reached a still-pending driver and wrote an extra `approval.resolved {withdrawn}` after the person's decision. At `ca4a53677`, the runner already restored answers synchronously and both drivers had their restore paths, fixing the history and Claude steer regressions. The remaining 14 failures were the 12 cancellation matrix cases and two real-relay restart cases, all at the assertion that replay writes no new change-feed rows: `ConversationStore.add_approvals` kept the answered approval but unconditionally moved its running message to `approval-needed`; applying the restored answer moved it back to `running`. Each replay wrote two spurious state changes with zero pending approvals.

These tests state the required behavior. C-26.6 requires recorded answers to reach the driver before later replayed stdout and forbids resending a written frame; C-27.3 keeps answered approvals answered, with only the person's resolution and the change feed belonging to the actual resolution. Reconstructing a resolved request creates no new approval or resolution, so it must not publish a pending-approval state. The assertions remain intact; no acceptance-contract wording change is needed.

Commit `02f52e131` changes `subfleet/conversations/store.py:1374`: after inserting requests, the same transaction checks whether the message has a pending approval before moving it to `approval-needed`. Genuinely pending requests still publish their approval, event and message state atomically. The existing synchronous restoration in `runner.py:564`, `ClaudeTurn.restore_answer` at `claude_turn.py:326`, and `CodexTurn.restore_answer` at `codex_turn.py:239` remains intact, including relay frame deduplication, terminal-outcome guards and live command-queue timing.

The passing regressions verify both providers, allow/deny/cancel-turn, written and unwritten replies, the sole person-decision event, no replay change-feed rows, and no reconstruction of a written reply. The two real-relay tests reopen the durable store and assert unchanged relay-log bytes and no second line reaching the provider pipe. All six live-answer queue cases pass on all three implementations.

Commit `cfb97426d` changes only the history property's setup (`tests/unit/test_approval_resolution.py:461`): each history copies a closed, fully initialized empty database into its own private state directory. This avoids repeating durable schema DDL hundreds of times. Each history still uses a fresh real store, real transactions, file publication, and the same generated operations and replay. AST comparison confirms all 53 assertions and the property decorators are identical to the starting head: 500 generated examples plus the explicit formerly exempt history, with no exemption or reduced count.

All comparisons used the identical current test files, locked dependencies, standard CPython 3.14.7 with the GIL enabled, and Hypothesis seed `20261011`. The release and starting implementations were extracted with `git archive` into `.uv-cache/release-217` and `.uv-cache/head-before`, containing only `subfleet`, and selected with pytest's `pythonpath` override and `--import-mode=importlib`. A local probe checked the imported runner, store and both drivers against the selected source tree. The release runner SHA-256 is `144692c540f4ec5f768895e21e9810dbf24fec4649ab7d3f09691f1d45b6077c`; the starting runner SHA-256 is `2aeaca567ed4287280b26c52df369159c12d43ab314ba94c5b1b978c6c71f9cc`.

Counts below are **passed / failed**, with no skips or errors in these completed runs.

| File | release/217 | PR ca4a53677 | Fixed implementation |
| --- | ---: | ---: | ---: |
| `test_approval_resolution.py` | 20 / 13 | 21 / 12 | 33 / 0 |
| `test_turn_replay.py` | 31 / 2 | 31 / 2 | 33 / 0 |
| `test_claude_turn.py` | 60 / 1 | 61 / 0 | 61 / 0 |
| `test_approval_commit.py` | 6 / 0 | 6 / 0 | 6 / 0 |
| `test_turn_runner.py` | 76 / 0 | 76 / 0 | 76 / 0 |
| `test_codex_turn.py` | 58 / 0 | 58 / 0 | 58 / 0 |
| **All six files** | **251 / 16** | **253 / 14** | **267 / 0** |
| **Original four files** | **117 / 16** | **119 / 14** | **133 / 0** |

The starting-code result reproduces the supplied four-file host count exactly. This release comparison collects the same 133 original-file cases as the head, including all six live queue cases; the supplied earlier release result collected 127 cases (111 passed / 16 failed). Its smaller pass count is not used for the identical-file comparison above.

Completed runtimes: release **67.33 s**, starting PR **149.51 s**, fixed implementation **204.57 s**. Each run was foreground, under its own `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock` hold and its own Darwin-user `sf-r187f-$$/` temporary root. A local 18-minute interrupt timer left cleanup time within the 20-minute cap; it did not fire in any completed comparison. Temporary roots were cleaned with the requested `python3`/`shutil.rmtree` command and verified absent. No runner was left behind. `git diff --check` and syntax compilation passed. Logs, XML evidence and validation helpers remain ignored and are not committed.

The history property executed at its written count:

| Implementation | Generated histories | Explicit case | Test-body calls |
| --- | ---: | --- | ---: |
| release/217 | 0 | 1 failed; generation never starts | 1 |
| PR ca4a53677 | 500 passed | 1 passed | 501 |
| Fixed implementation | 500 passed | 1 passed | 501 |

Hypothesis reports 73 additional invalid generation attempts in each passing history run; these do not enter the test body and do not replace the 500 passing histories. Generated-phase runtimes were **58.55 s** before and **77.66 s** after. The other properties completed their written 60, 60 and 40 generated examples in all three comparisons.

Two earlier exploratory runs were interrupted and are superseded by the complete comparisons: the initial free-threaded run completed 12 failures / 20 passes before stopping during history generation at 912.71 s; a standard run with history deferred completed 14 failures / 168 passes before interruption during Codex setup at 252.99 s. Neither partial run is counted as complete validation. Long waits occurred outside this job's lock holds; output buffering was handled by harmless newline polls.

The filesystem CI error is unrelated to this branch. The supplied trace shows all HFS+ identity assertions passing, followed by the volume fixture's single `hdiutil detach` failing at teardown with exit 16, `Resource busy` on disk5 (`tests/unit/test_filesystem_identity_agreement.py:70`). The log does not identify the process occupying the volume. `subfleet/folders.py` and that test are byte-for-byte unchanged from the base, and the test imports none of the changed conversation code. They remain unchanged. The local disk-image reproduction, full repository suite, and a second local Python-version matrix were not run; all six required files and the complete fixed-head history property were run.

A fixed-head rerun, after preparing the standard Python environment outside the suite lock:

```sh
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PROJECT_ENVIRONMENT="$PWD/.uv-cache/venv314"
uv sync --locked --python /opt/homebrew/bin/python3.14
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)sf-r187f-$$/"
mkdir -p "$TMPDIR"
export UV_NO_SYNC=1 UV_OFFLINE=1
export HYPOTHESIS_STORAGE_DIRECTORY="$PWD/.uv-cache/hypothesis"
/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q \
  tests/unit/test_approval_resolution.py tests/unit/test_turn_replay.py \
  tests/unit/test_claude_turn.py tests/unit/test_approval_commit.py \
  tests/unit/test_turn_runner.py tests/unit/test_codex_turn.py \
  --hypothesis-seed=20261011 --hypothesis-show-statistics
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMPDIR"
```

The validated implementation and tests are the source changes in `02f52e131` and `cfb97426d`; the final report commit changes documentation only. The final head is the assigned local branch ref named above and is reported in the delivery.
