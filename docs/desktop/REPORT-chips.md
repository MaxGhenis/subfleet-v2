# Chips implementation report

Built in the assigned detached checkout of `52ffde17`. No production daemon,
launchd configuration, caller checkout, or production state was changed.

## Design and implementation

- `docs/desktop/DESIGN-chips.md:1` records the design before implementation,
  including the installed Claude desktop host schemas, real MCP transport,
  child creation, app behavior, and Codex feasibility evidence.
- `subfleet/conversations/chip_host.py:58` builds the CLI MCP configuration;
  `:72` implements the stdio server. Writable new and resumed Claude turns
  receive it through `claude_turn.py:157` and `service.py:2111`. It exposes
  `spawn_task(title, prompt, tldr, cwd?)` and `dismiss_task(task_id, reason?)`.
- `subfleet/conversations/chips.py:28` adds persistence and scoped hashed host
  credentials; `:129` validates/deduplicates suggestions; `:174` withdraws pending
  chips; `:204` atomically creates one child and exact first message on person
  Start. Current parent defaults are inherited. The prompt retains whitespace,
  Unicode and original line endings; no brief is requested or added.
- `app/Sources/UIChips.swift:4` renders timeline cards with Start, Dismiss and
  Open session. `Chips.swift:4` handles the wire model and `:87` nests sidebar
  children. `Outbox.swift:222` journals choices, and `Timeline.swift:564` merges
  snapshots/events without reviving terminal cards.
- `tests/fake/interactive_claude.py:353` teaches the fake CLI to launch and call
  the actual MCP server. `docs/acceptance-contract.md:435` adds **C-31.1**.

## Validation

| Check | Result |
| --- | --- |
| New host/schema/launch tests (`tests/unit/test_chip_host.py`) | 28 passed |
| New daemon/store/lifecycle tests (`tests/unit/test_chips.py`) | 33 passed |
| Fake CLI with real MCP subprocess and disposable socket (`tests/unit/test_fake_claude_chips.py`) | 3 passed |
| New native app-core probes (`tests/frontend/test_core_chips.py`) | 11 passed |
| All core frontend regression cases | 121 passed; 1 skipped (includes the 11 chip cases) |
| Existing Claude turn-driver tests | 33 passed |
| Related store/service/turn/state regression suites | 205 passed; 1 environment failure |
| New daemon e2e (`tests/e2e/test_task_chips.py`) | 5 skipped by existing process-inspection fixture |
| Combined collection of all new tests | 80 collected without errors |
| `git diff --check` and Python compilation | Passed |
| `app/build.sh --dev` and strict signature verification | Passed; `build/SubfleetDev.app` (arm64) |

The existing daemon-construction regression
`test_a_request_that_reaches_its_text_after_the_service_closed_writes_nothing`
fails because this sandbox denies macOS boot-identity inspection, before chip
behavior is reached. The same restriction skips the five daemon e2e cases.
The passing socket transport tests do not replace or weaken that process check.

The initial broad frontend run was invalidated when a source comment changed
during compilation; its setup errors are not passing tests. A clean compile and
core run produced 120 passes, one skip and one failure: an existing protocol
fixture did not yet supply responses for the four added operations. That fixture
was extended with real-service chip responses, and its entire nine-test module
then passed against the newly compiled core binary. The resulting 121 distinct
core cases passed; the separate focused chip run also passed all 11 cases.

The native build used the unchanged `app/build.sh --dev` with writable temporary
compiler caches and a temporary `xcrun` wrapper passing the compiler-supported
`-Xfrontend -disable-sandbox` option to `swiftc`. This avoids the denied nested
SwiftUI macro-plugin `sandbox_apply`; the task's outer sandbox stays in effect.
The completed bundle was moved into this checkout's ignored
`build/SubfleetDev.app`. `codesign --verify --strict --verbose=2` reported it valid
on disk and satisfying its Designated Requirement. It was not launched.

## Remaining scope and handoff

Codex MCP injection and read-only Claude tools remain disabled as designed.
The Codex config surface supports a future implementation, but needs separate
launch/resume and isolation tests. No live provider or visual interaction test
was performed. Re-run the five daemon e2e cases and the blocked daemon regression
from an environment that permits normal macOS process inspection.

Changes are **uncommitted**. The attempted design commit failed creating
`/Users/maxghenis/subfleet-v2/.git/worktrees/20260928-110216-chips-astra/index.lock`
with `Operation not permitted`: Git's metadata is outside the sandbox's writable
roots. No history was rewritten and nothing was pushed.
