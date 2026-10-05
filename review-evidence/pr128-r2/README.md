# Round-two review evidence for #128 (head 3e2f7f58)

This is review-job evidence only, not for the PR branch. Nothing here executes a recorded command or contacts a daemon.

- `probes/R2ViewProbe.swift`: one offscreen probe compiled with the PR's `app/Sources` (`-D SUBFLEET_VIEW_TEST`). It has these parts:
  - `approvals`: card and sheet for every scene in `probes/scenes.json`, after `approval.get` loads.
  - `turns`: settled outcomes, serving facts, the Fast warning and Codex progress.
  - `labels`
  - `sidebar`: list focus, arrow keys, filter.
  - `controls`: Send appearance and amber contrast.
  - `sheetfit`: whether the review sheet's content can shrink.
- `probes/make_scenes.py`: builds the scenes from the shapes the daemon emits (codex_turn.py:674–688, claude_turn.py:547–563) and from every fixture in `tests/fixtures/visual/approvals.json`.
- `probes/run.py`: the foreground slice runner. It enforces a hard deadline and kills only its own process group.
- `probes/xcrun-shim.sh`: for test slices, it adds a private `-module-cache-path`. The shared module cache made one compile take 777 s instead of 102 s.
- `output/*.json`: raw probe output. `output/snapshot-pixel-compare.txt` compares `tools/app_snapshots.sh` re-renders on this Retina display against the committed `after/` images.
- `slices.jsonl`: every slice, with exit code, duration and tail.

## Codex command strings

These come from one lane's `codex exec --json` stream, Codex CLI 0.159, at `~/.subfleet/jobs/20261004-221349-desc-reconcile-r2-hard/a1/stream.jsonl` (read-only). All 52 `command_execution.command` strings begin with a shell wrapper. Under the new `ToolActivity.label` rule (first two words), they label as `/bin/zsh -c` 46 times and `/bin/zsh -lc` 6 times.

A local Codex rollout (`~/.codex/sessions/2026/10/03/…`) records the argv as `["/bin/zsh", "-lc", "<script>"]`.
