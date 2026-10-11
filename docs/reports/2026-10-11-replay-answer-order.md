# Replay answer ordering — 2026-10-11

Base: `be7afd5992a660b68c3ccd2edf3fc16e92578e50` (`origin/release/217`, #175). Branch: `fix/replay-answer-order`, with writable Git metadata in this workspace's `.git-local`. The caller's checkout and shared Git metadata were not changed; no Git remote was used. Every commit carries `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

`subfleet/conversations/runner.py:564` now applies recorded answers synchronously during reconstruction, immediately after the request becomes pending and before the next stdout line. Written replies use `ClaudeTurn.restore_answer` (`claude_turn.py:326`) or `CodexTurn.restore_answer` (`codex_turn.py:239`), with the existing command event key. The runner snapshots already answered request ids at construction; later answers remain live commands even during startup. Missing replies use the same response construction, validation and sending guards as live responses (`runner.py:452`). The relay's frame tags and the saved terminal outcome still prevent duplicate or unauthorized writes.

Live answers keep their command queue timing, including a first runner still catching up with its initial stdout. Both drivers restore the `cancel-turn` interruption state. Claude also preserves the separate, tagged interrupt that sweeps queued steers, so a crash between the answer and that interrupt can recover the latter.

Caller audit: `_read_stdout` is the sole production caller of `_sync_answers`; it calls it after each complete nonblank line. The approval property calls it directly to model replay, and the saved-terminal replay test disables it on the first runner to model an answer not handed over. The service still records the person's answer and calls `TurnRunner.respond`, which queues the command. C-26.6 and C-27.3 now explicitly state answer ordering and preservation of the person's sole resolution event.

Regression coverage:

- `tests/unit/test_approval_resolution.py:354`: 12 cases, both providers × three decisions × written/unsent replies. A request and later cancel share one stdout read. The card remains answered, with exactly one decision event, no withdrawn event or new change-feed row, and no reconstruction of a written reply.
- `tests/unit/test_approval_resolution.py:406`: six live queue cases, covering startup, a live first runner and a caught-up adopted runner.
- `tests/unit/test_turn_replay.py:135`: both providers with a real relay, actual pipe output and a reopened durable store. Restart leaves the relay log unchanged and writes no second reply.
- The approval history property removes the known-case exemption, requires exact event equality after replay, pins the formerly exempt history as an explicit example and increases generated examples from 80 to 500.
- The Claude queued-steer cancellation test covers live and restored answers.

Validation is pending: the baseline is queued behind another workspace holding the required shared pytest lock. No pytest result or generated property count is claimed yet. Syntax compilation and `git diff --check` passed. Required suites: `test_approval_resolution.py`, `test_approval_commit.py`, `test_turn_runner.py`, `test_turn_replay.py`, `test_claude_turn.py` and `test_codex_turn.py`, all under `tests/unit`. The baseline selects the 12 cancellation regressions, six live queue cases and the strict history property; the fixed run will exercise all six suites with `--hypothesis-seed=20261011 --hypothesis-show-statistics`.

Tests use one pytest process at a time under `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, with `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-replay-$$/`. Temporary roots are cleaned with the requested `python3`/`shutil.rmtree` command. Dependencies and Hypothesis storage are local to `.venv` and `.uv-cache`; pytest uses `UV_NO_SYNC=1` and `UV_OFFLINE=1` after installing the locked dependencies.

The baseline uses an unchanged source copy under `.uv-cache/replay-baseline`, selected with pytest's `pythonpath` override and `--import-mode=importlib`. Its runner is byte-for-byte equal to the base commit (SHA-256 `144692c540f4ec5f768895e21e9810dbf24fec4649ab7d3f09691f1d45b6077c`). This allows implementation work to continue while the baseline run waits for the shared lock.
