Provider attempts inherited the daemon's shared TMPDIR, so a job using `$TMPDIR/venv` collided with other jobs and cleanup could delete their files. Every detached attempt, retry, resume, conversation turn, capacity probe and supervised enrollment now gets an exclusive 0700 folder under the user's Darwin temp root, recorded in the store before the launch gate opens. Only TMPDIR changes in the environment, spelled with a trailing slash as the platform's is. An attempt keeps one folder across its launch tries, and a failed allocation (`ENOSPC`, `EACCES`) ends the attempt as a launch failure with its reason.

**Operational note: Codex lanes can no longer write the rest of the user temp root.** Under `workspace-write`, Codex derives its writable roots from the child's TMPDIR, so a Codex provider may write its attempt's TMPDIR, its working directory and `/private/tmp`, and nothing else under `$(getconf DARWIN_USER_TEMP_DIR)`. That is the point of D-TMP1. Inside the sandbox, `getconf DARWIN_USER_TEMP_DIR` names the attempt's TMPDIR, so a brief's `$(getconf DARWIN_USER_TEMP_DIR)sf-<tag>` and `${TMPDIR%/}/sf-<tag>` are the same private folder on a Codex lane (observed with Codex 0.159.0, review r2). Either way that scratch lasts one attempt: a retry starts with none, so a brief that resumes from a scratch journal must keep the journal in its worktree or another durable path. On a Claude lane the `getconf` path stays in the shared user temp root. A conversation turn is its own attempt, so a file a turn leaves in `$TMPDIR` is gone once that turn finalizes.

**What holds scratch.** A TMPDIR is kept only while its holder is live or quarantined, or its census is not verified empty. Salvage state never holds it, because salvage snapshots the worktree, never TMPDIR, and nothing reads a keep flag. Quarantine resolves through `kill --confirm-dead`, after which the sweep removes the folder. Every release writes a durable receipt (`removed_at`, `outcome`):
- **absent:** the folder isn't there, whatever holds it. Retention never pins a job for a missing folder.
- **removed:** the folder was deleted.
- **replaced** or **outside:** for a terminal holder with a verified-empty census, the path holds something else, or isn't one of this temp root's allocations. Nothing is removed in either case.

A verified-empty census also records `cleared_at`, which pins nothing even while a failing removal is retried. Sweeps take no census and no measurement for a quarantined or live holder. A terminal holder whose census found members gets no new census until its state changes. A probe record is saved only when something changed.

**Removal.** Removal renames the folder to a tombstone in the same parent and checks that the renamed entry is the recorded directory (device, inode, owner, no symlink). Only then does it delete through descriptors. Symlinks inside are unlinked, never followed, and other devices are never entered. Directories inside the tree, never above it, are made owner-writable first, so a 0555 tree (Go's module cache) is removed. On any mismatch or failure the folder is renamed back, and the refusal is logged; finalization never fails because of it.

**Sweeps.** A sweep runs at startup, on a worker once the daemon is serving, and on retention's cadence. It releases the store's recorded allocations, then looks at other `subfleet-attempt-` folders. Known or unknown, and an unknown folder's age, come from the store. A folder any allocation record names follows that record. An unknown folder is considered only if it carries this store's marker. The store records when it first sees one (`tmpdir.unknown_seen`), removes it 24 hours later after the same identity check, and records `tmpdir.unknown_removed`. Neither the marker's contents nor the folder's birth time can make a folder known or old.

**Disk.** Scratch joins the job's retention bytes. Disk admission's existing provisional placement reserve (1.5 GB, for 600 s) stands for the attempt's worktree and TMPDIR growth together. It is an estimate, not a quota: measured scratch can exceed it. No policy value changes.

Codex 0.159.0's `debug prompt-input` and the review's `codex sandbox` probe confirmed that workspace-write follows the child's TMPDIR. Writable Claude launches add the folder with `--add-dir`. No real credentials or model requests were used.

Round 1 (review r1: three P2s, seven P3s) is in the report's round-1 section, which has each finding's cause and fix, the eight invariants, the mutation witnesses, and the counts per file. Release/217 at `7e2dc5a35` (#243, #245, #247, #249) is merged in; both conflicts kept both sides.

**Validation (round 1).**
- **Coverage:** every module in `tests/unit` (339) and `tests/fake` (113) ran this round, single-process, one pytest per hold slot.
  - `tests/unit`: 13,468 passed, 18 skipped, 0 failed.
  - `tests/fake`: 2,152 passed, 2 xfailed, 0 failed.
  - All 18 skips are environmental: 13 come from archive trees with no Git history, 4 from a dependency that isn't in the offline uv cache, and 1 is an explicit benchmark.
- **At the final code head (`134908a5c`):** the PR's six test files, `test_daemon_launch.py`, `test_probe_cancel.py`, the state contract and the module-name guard passed 168 of 168.
- **Regressions:** every round-1 regression fails at `a9b7c6195` (64 failed, 19 unchanged cases passed).
- **Mutations:** all nine are killed: the build's four, plus a salvage hold, an absent folder without a receipt, removal without rename-and-verify, and age taken from the marker or the birth time.
- **A defect validation found:** this round's own cleanup lock sat outside the never-raise guard. It failed 33 cases of `test_daemon_settle.py`, was fixed in `134908a5c` with a regression test, and the module then passed 61 of 61.
- **Re-timing:** `test_probe_gate_rechecks_terminal_or_stamp[failed]`, run in one hold, took 0.046 s at head, 0.723 s at release/217 and 0.403 s at `a9b7c6195`.

Per-file counts, the holds and every finding's cause and fix are in [the report's round-1 section](docs/reports/2026-10-22-attempt-tmpdir.md).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
