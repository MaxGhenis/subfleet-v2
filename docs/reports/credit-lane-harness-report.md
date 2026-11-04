# API credit lanes: the harness (`subfleet.api_agent`)

Status: IN PROGRESS. A first version, written when the host fell to 3% battery
(2026-11-04 01:00 UTC); later commits replace it.

Build of the "The harness" section of `docs/decisions/2026-11-03-api-credit-lanes.md`,
to the fixed "Interface between the adapter and the harness". The adapter was not built.

## Commits so far

- `e27d936ab` build: the `api` extra with `anthropic==2.4.0`, `uv lock`, and
  `--extra api` in CI's `uv sync`.
- `d4dcab493` feat(api_agent): the harness modules (steps 2 to 6 in one commit, because
  `harness.py` wires every tool).

## Sandbox decision

A Subfleet-owned seatbelt profile through `/usr/bin/sandbox-exec`, not `codex sandbox`.
Prototype results (manual runs, 2026-11-03):

- Codex's `:workspace` profile leaves `~/Library/Keychains` readable and allows
  `com.apple.SecurityServer` with network on, and in a probe its network profile let a
  sandboxed process connect to a unix socket outside the writable roots (the daemon's
  socket is such a socket).
- The Subfleet profile with network on: `curl https://example.com` returned 200, and a
  unix socket outside the writable roots was refused (`Operation not permitted`). Unix
  sockets inside the writable roots bind and connect.

The full matrix script and its results are still to come.

## Open

Tests, the contract section, the matrix script and the full-suite run were still in
progress at this write.
