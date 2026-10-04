# Lane briefs

One file per lane: the assignment, the contract clauses it binds, its worktree and branch, and the shape of its final message. Reports the lanes wrote back live under `reports/`. The integrator (the Fable session that owns `main`) merges lanes; lanes never commit to `main`.

## Which provider can do what

- **Astra (Codex) lanes** run inside the Codex sandbox with no network. They can build and run the unit suites, but the sandbox denies `/bin/ps`, `sysctl`, and unix-socket binds, so every test that starts a real daemon or guardian skips or fails there: `tests/fake`, `tests/e2e`, `tests/process`, and any test marked as needing process inspection. A Codex lane cannot reproduce or verify a containment, kill, quarantine, or recovery defect. Give those to a Claude lane or to the integrator, and say so in the brief (learned 2026-09-05, `quarantine-flakes`: the lane spent its run confirming it could not observe the process table).
- **Claude lanes** run unsandboxed under `--dangerously-skip-permissions` and can run the whole suite. They push their own branches. They die on the account's five-hour or weekly limit with rc 4 and an empty output file; v1 has no reading for most Claude accounts, so pick one whose last limit is more than five hours old, or check the lane's transcript for the `resets` time before retrying.
- **Codex lanes cannot push.** Briefs for them say "commit; do not push"; the integrator pushes after review.
- Both kinds must be launched with `subfleet run` from the integrator's session, never with the provider CLI directly; a directly launched lane dies with the session.

## Worktrees

Lane worktrees live under `~/subfleet-v2-lanes/<lane>/` on branch `lane/<lane>`, created from `main` (`git worktree add -b lane/<lane> ~/subfleet-v2-lanes/<lane> main`). Never under `/tmp`. Each worktree has its own `.venv` (`UV_PROJECT_ENVIRONMENT=$PWD/.venv`); after a Codex lane, run `uv sync --group dev` there before running the daemon suites, because the sandbox's offline sync leaves out the project's console scripts (`subfleetd`), which the end-to-end harness needs.

## Running the physical suites

```
export PATH=/usr/sbin:/sbin:$PATH
uv run pytest -q
```

A sandboxed shell without `/usr/sbin` makes every process-inspection test skip. Failed daemon tests keep their state root under `/tmp/sf-failed/<test name>/` (both the fake-daemon and the end-to-end harness): `daemon.log`, the store, receipts, and any quarantine census live there.
