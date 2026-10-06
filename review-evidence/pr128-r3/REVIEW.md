# Review of MaxGhenis/subfleet-v2#128, round three (head `85c38569`): request changes

The round-two P1 is fixed. A 120-line Write now gives a 364 pt card and a 382 pt sheet, the actions stay in view, and the file path comes first. The other fixes also mostly hold: Codex rows no longer read `/bin/zsh -lc`, questions appear once with the picker at the top, section headings can't be selected, and every suite passes.

Two new problems come from the same change to the field list, and the builder's Codex fixtures miss both because they lack fields Codex 0.159 sends:

- **P1:** the card now hides Codex's `kind`. That field separates running a new command from sending input to a terminal that is already running.
- **P2:** Codex's display-only `commandActions` rows are sorted ahead of the command itself.

## Round-two findings

| Finding | Status | Evidence (executed) |
|---|---|---|
| **P1.1** Unbounded card and sheet | **Fixed** | Light, text scale 1. Card / sheet heights in pt: 120-line Claude Write (CLI 2.1.284 key set) 364 / 382; long Codex command (0.159 key set) 364 / 382; masked Write 390 / 400; masked Codex command 286 / 400; short Bash 180 / 237. Dark mode gives the same numbers. The builder's scenes measure 364/382, 364/382, 390/400 and 164/215. `sizeThatFits` returns 300, 400, 400 and 400 for offers of 300, 400, 800 and 1,117 pt. Allow, Deny and Cancel are in every render at scale 1, and Allow is enabled after the masked confirmation. `input.file_path` comes before `input.content`. |
| **P2.1** Codex rows read `/bin/zsh -lc` | **Fixed** (P3 edge cases below) | I labelled 44 strings: the builder's 27 recorded commands, 16 constructed `/bin/zsh -lc` forms and one command bounded by the daemon's own `redact.bounded`. No label starts with `/bin/`. Examples: "nl -ba subfleet/daemon.py", "git show 02c63203", "rg -n TODO" (after a quoted `cd` path), "Run a Python script" (heredoc). The committed progress snapshot reads "Ran 1 command › nl -ba subfleet/daemon.py". |
| **P2.2** Plumbing and repeated questions | **Partly fixed** | Round two's real-shaped Claude Bash now lists only `input.command` (it had about 11 plumbing rows). A pending two-question card puts "Choose one" at 126 pt and 146 pt of 554 pt and 574 pt cards (round two: 705 pt of 1,230). History shows each question once and each answer once. The hide list hides too much (new P1) and too little (new P2). |
| `knownApprovalAnswers` | **Holds** | Answers stayed with their own approval id after reset and replay, for two messages and for a replacement request in one message. Compaction removes only delta rows (C-25.4), so approval events survive it. Answers could only cross if two approvals in one message shared a provider request id. Neither provider produces that for questions: Claude CLI 2.1.284 sets `request_id` from `crypto.randomUUID()`, and the driver refuses Codex `requestUserInput` (`codex_turn.py:693`). With a collision forced in the probe, which approval the card takes depends on dictionary order (C1 in 3 of 8 runs, C2 in 5), but the answers still follow the id the card holds. That ordering comes from the existing `knownApprovalID`, not from this change. |
| P3 Selectable headings | **Fixed** | Ten Down presses, then ten Up: the table rows go 1,2,3,5,6,7,9,10,11 and back, skipping heading rows 0, 4 and 8. The binding was never written nil. |
| P3 Doubled highlight | **Fixed, with a contrast regression** | There is one fill now, but see the new P3 on the pending count. |
| P3 `codex-file-change` fixture | **Partly fixed** | `changes[].path` is gone, but the required `startedAtMs` is missing (new P3). |
| P3 PR body | **Fixed** | `docs/reports/2026-10-03-visual-pass-pr-body.md` states that every finished turn shows "Completed" or its outcome plus a serving line, and that raw JSON stays under Details. The R2 report it links to is stale (new P3). |
| #113 / #124 after the integration merge | **No regression** | The #113 Changes-pane wording tests pass (`test_core_diff.py`, including "the pane says who else wrote" and "empty diffs still disclose other conversations"), as do 37 #124 UI tests and 13 cutover-start tests. The fix commit's edit to a #124 test (`test_pr124_fixes_ui.py:157`, `ReviewCutoverProbe.swift:163–164`) adds a wait for published refusals and loosens no assertion. |

## New findings

**P1 — Codex's `kind` is hidden, so stdin to a running terminal reads as "Run a command?"** (`app/Sources/ApprovalPresentation.swift:12`, `:14`, applied at `:90`)
- **The field.** Codex 0.159's `CommandExecutionRequestApprovalParams` (from `codex app-server generate-json-schema`) and the repo's vendored 0.153.3 schema both define `kind: command | writeStdin`. The schema describes it as "Distinguishes a command approval from input sent to an existing terminal".
- **The contract.** C-27.1 says "the display includes every field that changes what is granted". The daemon copies `kind` into the summary as `input_kind` for that reason (`codex_turn.py:677–679`).
- **The change.** `hiddenParameterKeys` now lists `"kind"` and `hiddenDisplayKeys` lists `"input_kind"`.
- **Executed.** For a `writeStdin` request:
  - The 3e2f7f58 base listed `params.kind: writeStdin` when loaded and `input_kind: writeStdin` before loading.
  - The head lists neither (`output/kind.txt`). Its card and sheet read "Run a command?" plus the command block, and OCR finds no "stdin" or "kind" (`renders/card-codex-write-stdin.jpg`).
- **Fix.** Stop hiding `kind` and `input_kind`, or show them whenever the value isn't `command`.
- **Not verified:** how often Codex raises `writeStdin` approvals under Subfleet's `on-request` policy.

**P2 — Codex's parsed `commandActions` lead the list, and arrays sort as strings** (`ApprovalPresentation.swift:102–114`)
- **The cause.** Any leaf under a key named `path` gets priority 0. In Codex 0.159, `commandActions` is optional, described as "Best-effort parsed command actions for friendly display", and its entries carry `path`.
- **Executed.**
  - **12-step chain.** The 12 `params.commandActions[i].path` rows come first, in the order [0], [10], [11], [1] … [9], then cwd. `params.command` is row 13 of 50, so the card's first 240 pt show no part of the command (`renders/card-codex-12-actions.jpg`).
  - **Base comparison.** The base drew the command in its own block above the list and kept index order (`output/order.txt`).
  - **Permissions.** Twelve write roots also list as [0], [10], [11], [1] ….
  - **Single command.** The command appears twice: the block, then `params.commandActions[0].command`. It is followed by `params.commandActions[0].type: unknown`, plus `params.approvalId` when set. This repeated text is what round two's P2.2 objected to, now on 0.159-shaped requests.
- **Why tests pass.** The builder's Codex fixtures carry none of `kind`, `commandActions` or `approvalId`. The hide list was written from those fixtures, not from the schema. Claude CLI 2.1.284's `decision_reason_code` also renders on every Claude card in my CLI-shaped scenes.
- **Fix.**
  - Hide `commandActions` and `approvalId`, or place them after the command.
  - Key the scope priority on known paths: `cwd`, `grantRoot`, `permissions.*`, `blocked_path`, `input.file_path`.
  - Sort array indices numerically.
- **Not verified:** whether 0.159 fills `commandActions` on approval requests. I found no recorded approval request outside the live `~/.subfleet`, which I did not read.

**P3**
- **The pending count on a focused selection.** Removing the custom fill (which sat between `UIWindow.swift:222` and `:223`) leaves the count's explicit amber (`Theme.state.attention`, `UIWindow.swift:254–255`) on the native accent highlight.
  - **Measured** with the list focused: 1.23:1 in light mode and 3.40:1 in dark. The base measured 5.24:1 and 6.86:1 in both focus states, and the head measures 4.82:1 and 5.12:1 unfocused (`renders/sidebar-*-focused.jpg`, `probes/SidebarProbe.swift`).
  - **Also seen:** the row title rendered near-black on blue (3.24:1). That may be an artifact of the unshown window; it was not checked in a real key window.
- **Opening Details on an answered card shows a false error banner.**
  - **Path.** `UIApprovals.swift:130` calls `load()` when Details opens, for any card that has an approval id. `load()` asks `model.approvalID(for:)`, which returns nil for a settled card and sets `problem = "That approval is no longer pending."` (`UIModel.swift:1033–1034`). `problem` is the window banner (`UIWindow.swift:39–46`).
  - **Executed:** the `approvalID(for:)` call on an answered card returned nil and set that text (`output/details.json`).
  - **Not executed:** the click itself. A synthesized click doesn't reach the disclosure in an unshown window.
  - **When it fires:** for any request answered earlier in the same app run, since those cards keep their approval id. Details then shows only the summary, not the exact request.
- **Shell labels.** The `cd` prelude loop (`ShellCommandPresentation.swift:16`) skips to the first `&&` even across `;`, `||` or a newline:
  - `cd app; rm -rf build && make` → "make" (hides the `rm -rf`);
  - `cd app || exit 1⏎bun test && bun run build` → "bun run build".

  Also `(cd app && swift build)` → "(cd app", `set -euo pipefail⏎…` → "set -euo pipefail", and `# check⏎git status` → "# check".
- **Fixture still not Codex's schema.** `tests/fixtures/visual/approvals.json:45` omits `startedAtMs`, which both the 0.159 schema and the vendored `tests/fixtures/codex/app-server-0.153.3/ServerRequest.json` require. `test_visual_round_two.py:93–95` asserts that incomplete set as "the request schema".
- **Stale R2 report in the PR.** The PR body points readers to `docs/reports/2026-10-03-visual-pass/R2.md`.
  - 13 of its 15 links name files left out of the commit (`r2-measurements.json`, `r2-tests.xml`, `r2-before/*`, and others).
  - It cites `bb762633`, `761a62f2` and `d6110252`, none of which is in the PR's history.
  - Line 49 says "a corrected full run is underway", with "final full counts … added after completion". That never happened.
- **Text scale.** The sheet's 400 pt cap (`UIApprovals.swift:397`) is fixed in points. At 207% the masked sheet still shows every action. At 249% the field area drops to its 40 pt minimum (one line) and the action row touches the bottom edge (`renders/sheet-masked-write-scale-249.jpg`). This assumes the sheet inherits the window's text scale, which I haven't verified.

## Proposed tests (`proposed-tests.patch`, not committed to the PR)
The patch adds four tests to `test_visual_round_two.py`, with probe outputs in `R2PresentationProbe.swift`:
- `writeStdin` is visible when loaded and in the summary;
- `params.command` comes before every `commandActions` row, and arrays keep numeric order;
- a `cd …;` prelude doesn't skip to a later `&&`;
- the file-change fixture validates against the vendored Codex schema.

On this head, in a scratch export, **all 4 fail** for the reasons above; the 28 existing tests selected alongside them pass (244 s).

## Not verified
- How often Codex 0.159 sends `writeStdin` approvals, or `commandActions` on approval requests, under Subfleet's policy.
- The row title colour on a focused selection in a real key window.
- A real click on Details.
- Whether sheets inherit the text scale.
- Full Keyboard Access or VoiceOver.
- `test_core_live.py`: not run. A lane's process ancestry carries Subfleet's attempt markers, so the daemon refuses it after a 900 s wait.

## Tests and build
- **Frontend:** 501 collected; 500 passed, 0 failed, 0 skipped, in 10 foreground slices of 72–447 s. `test_core_live.py` was not run.

  | Slice | Passed |
  |---|---:|
  | Round two | 81 |
  | Review fixes and progress | 48 |
  | Snapshots | 3 |
  | Approval reach | 40 |
  | Steer and Markdown | 87 |
  | Core rest | 147 |
  | Conversation and reading views | 11 |
  | #124 | 37 |
  | Cutover start | 13 |
  | Menu and status | 33 |
- **App protocol:** 32 passed (`test_app_cutover_daemon` 13, `test_pr124_fixes` 9, `test_pr124_review_r2_fixes` 6, `test_desktop_ledger` 4). Round two reported 34 from a file set it didn't record.
- **App build:** `app/build.sh` passed in 343 s.
  - `codesign --verify --deep --strict` passed; bundle ID `org.maxghenis.subfleet`.
  - 0 errors and 5 distinct warnings, at `UIModel.swift:193`, `:262`, `:474`, `:478` and `UISearchPalette.swift:284`. None is on a line this PR changed.
  - The app was not installed or launched.
- **Compile setup.** Compiles ran through a PATH shim that adds a private module cache; test compile flags are otherwise unchanged. My own probes compiled with `-j 12`.
- **Run conditions.** Load average was 10–88 during the run, and every probe reported 0 visible windows.

## Evidence
Everything is in `review-evidence/pr128-r3/`:
- `probes/`: `R3Probe.swift`, `SidebarProbe.swift`, `KindProbe.swift`, `OrderProbe.swift`, the scene and command generators, the slice runner and the PNG contrast script;
- `output/`: raw JSON and text;
- `renders/`;
- `slices.jsonl`: every slice, with exit code, seconds and load.

All temporary files were under `.review-tmp/`. I killed nothing; the only related processes running were the caller's waiter and this job's own guardian.
