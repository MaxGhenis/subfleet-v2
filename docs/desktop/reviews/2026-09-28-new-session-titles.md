# Inline new sessions and first-message titles

`+ New` and Cmd+N open an inline draft in the main pane. Opening the draft
creates no conversation. The draft retains its text, attachments and picks
when switching away or pressing Cmd+N again, and restores them across app
restarts. Its folder picker remembers the selection and offers No folder.
No folder creates a private scratch workspace for that session.

Return journals a conversation and its first message, then opens the session.
Cmd+Return (Send and stay here) journals the same work and keeps a fresh draft
open with its picks retained. The draft editor reuses the existing composer's
paste, drop and newline handling. The title in a conversation's header offers
Rename.

## Verified Claude title path

On 2026-09-28, inspected the installed binary at exactly:

```
/Users/maxghenis/.local/share/claude/versions/2.1.280
```

The bounded inspection was:

```sh
strings -n 8 /Users/maxghenis/.local/share/claude/versions/2.1.280 \
  | rg -i -o '.{0,180}generate_session_title.{0,700}' -m 8
```

It contains the SDK call `generateSessionTitle`, a stream-json control handler,
and an asynchronous dispatch entry for `generate_session_title`. The handler
requires a string `description`, accepts an optional boolean `persist`, and
returns `{title: string | null}`. With `persist: false` it invokes the CLI's
own title generator using the running session's credentials and turn abort
signal. Subfleet sends:

```json
{"type":"control_request","request_id":"subfleet-session-title","request":{"subtype":"generate_session_title","description":"<first person message>","persist":false}}
```

The success reply's `response.response.title` supplies the title. This is a
request inside the first turn's existing process, with no additional process
or account. The verification inspected code embedded in the binary; it did
not run a real account or connect to the running daemon.

## Stability and deliberate differences

The first accepted person message immediately supplies a deterministic title:
its first clause, about six words, with leading request verbs removed. Claude
may replace that placeholder once within a persisted 10-second result budget.
Title failures never fail the turn. Missing support, errors, invalid replies,
late replies and Codex sessions retain the fallback. The fallback for Codex is
intentional: it avoids launching a separate Claude process/account just to
name another provider's conversation.

The store records `title_source` as `person`, `generated` or `fallback`.
`conversation.rename` records `person`; an atomic conditional update prevents
in-flight generation from overwriting it. Existing titles migrate as person
titles conservatively. Generation is claimed once, so another message or a
restarted runner does not regenerate it. Watch changes carry titles to the app
without waiting for its periodic list refresh.

The 10-second budget limits Subfleet's acceptance of a title and sends a scoped
`control_cancel_request`. The binary documents that cancellation stops waiting
immediately but aborts work only when the receiver supports it. The inspected
title handler uses the turn's abort signal, so cancellation of that model call
is best effort. Subfleet never interrupts a person's turn to cancel a title.

Native Claude transcript title synchronization remains separate: `persist:
false` keeps Subfleet's explicit rename authoritative in its own store.

## Validation

- New daemon title tests: 26 passed, including actual RelayServer traffic,
  missing/error replies, a held relay acknowledgement, replay, timeout and
  rename protection.
- Existing runner tests: 28 passed. Expected frame lists now include the title
  request. One existing 300 ms timing assumption uses an Event gate instead,
  preserving its pending-write assertion on a loaded machine.
- Fake Claude subprocess protocol tests: 5 passed.
- Foundation draft probes: 7 passed.
- Protocol, title watch-feed and store probes: 20 passed, including exact Ops
  parity and lossless decoding of every operation's result.
- Outbox probes: 13 passed across the main run and a targeted rerun of one
  existing lost-answer case under host load. Together with the draft probes,
  40 app-core cases passed.
- The wider 205-case daemon selection recorded 199 passes. Its five runner
  assertion/timing failures were resolved by the subsequent clean 28-case
  runner run; the remaining service case is blocked by process inspection as
  described below.
- New title e2e tests: 8 skipped; existing conversation e2e tests: 29 skipped.
  The existing harness requires macOS process/boot inspection, which this
  sandbox denies. These skips occur before a daemon is launched.
- A broader unit run encountered the same environmental restriction in
  `test_closing_the_daemon_stops_its_catalog_run_and_the_removed_root_stays_gone`
  (`/bin/ps` denied). The focused service run also cannot execute
  `test_a_request_that_reaches_its_text_after_the_service_closed_writes_nothing`
  because macOS boot identity is unavailable. Those guards were not bypassed.

The default macOS 27 SDK build failed because this environment rejects the
SwiftUI macro plugin's nested sandbox (`sandbox_apply: Operation not permitted`),
including existing `@State` properties. The installed macOS 26.5 SDK implements
`State` as a property wrapper. The unchanged build script passed with that SDK:

```sh
env SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  SWIFT_MODULECACHE_PATH="$PWD/build/swift-module-cache" \
  CLANG_MODULE_CACHE_PATH="$PWD/build/swift-module-cache" app/build.sh
```

It produced `build/Subfleet.app`, validated its Info.plist and applied its
ad-hoc signature (exit 0). Only existing Swift concurrency/unnecessary-await
warnings remained. `git diff --check` also passed.

No app was launched, and the existing daemon was never contacted. Git staging
was denied because the linked worktree's index is outside the writable sandbox:
`/Users/maxghenis/subfleet-v2/.git/worktrees/20260928-102618-app-new-session-titles/index.lock`.
Changes are therefore left uncommitted under the requested fallback; no history
was rewritten and nothing was pushed.
