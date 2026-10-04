# Lane brief: sessions-review (re-apply the sessions lane's salvaged review pass)

You are finishing the sessions kit of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/sessions-review`). The sessions lane (`docs/lanes/sessions.md`) merged into `main` at its last commit, with all 232 of its tests green. It then ran a self-review pass, changed twelve files, and hit a usage limit before committing; those uncommitted edits made eight of its own end-to-end tests fail, so they were kept aside rather than merged. Your job is to take that review pass apart, keep what is right, and land it green.

## Inputs

- `~/subfleet-v2-lanes/out/sessions-review-pass.patch` (794 lines): the uncommitted edits, as `git diff` against the lane's last commit `1aaf321`. The same content is the salvage commit `refs/claude-salvage/lane-sessions-20260905-160239-80114` in this repository.
- `docs/lanes/sessions.md`: the original brief; `docs/lanes/reports/sessions-OUTPUT.md` if present, else the lane's `PROGRESS.md` history in `git log -p 1aaf321 -- PROGRESS.md`.
- `docs/acceptance-contract.md`: C-17.1, C-23.14, C-23.30 to C-23.36, C-23.39, C-23.54, C-23.55.
- The eight tests that failed with the patch applied: all in `tests/fake/test_sessions_end_to_end.py`: `test_the_reservation_refuses_a_second_recorder_for_one_interruption`, `test_retirement_is_durable_and_the_state_op_reports_it`, `test_revive_probes_lane_before_launch`, `test_revive_census_refreshed_and_skips_running_twin`, `test_a_revive_that_loses_the_lease_race_is_skipped_not_queued`, `test_the_lease_is_session_scoped_and_released_with_the_job`, `test_a_revive_resumes_the_named_session_rather_than_starting_a_new_one`, `test_a_revive_of_a_session_on_main_is_refused_like_any_writable_job`. The one traceback seen: a revive was refused with `reason='astra is a codex model ...'` while the requested model was `claude-fable-5-1`, which suggests the patch changed how a revive resolves its model or lane.

## Steps

1. `git apply --3way ~/subfleet-v2-lanes/out/sessions-review-pass.patch` on this branch (main has moved since `1aaf321`; resolve small conflicts). Do not commit yet.
2. Run `uv run pytest -q tests/unit/test_sessions_*.py tests/fake/test_sessions_end_to_end.py`. For each failure decide, from the contract and the original brief, whether the review pass or the test is right; fix the one that is wrong and say why in the commit message. Where the review pass added a check you cannot justify from a clause, drop that hunk and list it in the final message.
3. Commit in coherent pieces (one behaviour per commit), then run the full suite: `uv run pytest -q` (if `sysctl` is not found, `export PATH=/usr/sbin:/sbin:$PATH`). It must stay green: 3425 passed, 5 skipped on `main` at the time of writing.
4. Push after every commit: `git push -u origin lane/sessions-review`.

## Out of scope

Anything outside `subfleet/sessions/`, its tests, and the daemon seams the patch touches. Do not edit the contract; if a clause must change, say exactly what and why.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Never commit to `main`, never force-push, never write under `~/.claude`. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Headings: Kept (hunks landed, one line each); Dropped (hunks and why); Tests (command, count, time); Open questions for the integrator. No preamble.
