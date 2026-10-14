Frozen provider stream P3 follow-ups, 2026-10-14

Base: `75597f81bb3b` (release/217, #209 merged). All three review findings are
fixed; work stayed in the assigned checkout. No network Git remotes, sub-agents
or pushes were used.

Finalization atomically copies every regular stream with `st_nlink > 1` into a
fresh inode before classification or replay. This conservative rule is smaller
than maintaining a provider-specific list of writable artifacts: it covers all
hard-linked aliases, including stdout, stderr and last.md. A raw path naming
stdout itself is live; its snapshot is captured at canonical `stream.jsonl`
and reused on replay. Under C-9.2, later writes through an alias therefore cannot
add completion proof and turn a cached rejection into an accepted interim answer.

The `raw-stream` artifact now records the authoritative regular candidate,
including the canonical file beside an absent custom launch path, with its path,
byte count and SHA-256. Non-regular entries still supply no events or artifact.
An entry swapped between inspection and open maps ENXIO, EOPNOTSUPP (102 on
Darwin), ENOTSUP, ELOOP, EISDIR and EINVAL to `NotRegularFile`. Other I/O errors
still propagate. C-4.3, C-8.2, C-9.2, C-12.6 and version history were updated.

Regressions were committed before production fixes. Against unchanged base
production code, the corrected baseline selection had **20 expected failures
and 44 passing controls**: six writable-alias replay/restart failures, four
canonical artifact omissions and ten socket/error-mapping failures. The review's
cached auth-dead, rc-0 and captured-interim input now preserves its rejection and
previous export, including a writer descriptor held across atomic publication.
The frozen generator includes hard links to actual stdout and Unix sockets.
Bounded properties cover frozen authority, fallback equivalence and refusal to
open non-regular entries; streams contain at most ten events.

Final validation: **987 passed, no failures or skips**, across all requested files.

| File under `tests/` | Passed |
| --- | ---: |
| `unit/test_frozen_provider_streams.py` | 125 |
| `fake/test_frozen_stream_nonregular.py` | 46 |
| `unit/test_codex_turn_completed.py` | 41 |
| `fake/test_codex_turn_completed_fake.py` | 105 |
| `fake/test_finalization_replay.py` | 10 |
| `fake/test_salvage_finalization.py` | 47 |
| `unit/test_claude_adapter.py` | 170 |
| `unit/test_empty_provider_results.py` | 334 |
| `unit/test_sessions_transcripts.py` | 71 |
| `fake/test_state_contract.py` | 37 |
| `unit/test_test_module_names.py` | 1 |

The five final foreground pytest runs used separate shared-lock holds, taking
9.60, 20.30, 36.73, 7.78 and 5.93 pytest seconds. Every hold stayed below 20
minutes; tests ran serially with `HYPOTHESIS_PROFILE=ci`, disposable TMPDIR and
Hypothesis storage, and the cache provider disabled. Temporary evidence was
removed with Python `shutil.rmtree`; no `.hypothesis` folder remains.
Dependencies were installed with `uv sync --locked`; Python 3.14.7 freethreaded
ran with `PYTHON_GIL=1`. The sole warning is the existing Codex `zip`
parametrization deprecation. Broader full, process, e2e, live suites and CI were
not run. `git diff --check` passes.

Shared Git metadata lies outside the writable workspace. Commits use
`.git-local/repository.git` on `fix/frozen-stream-p3`, borrowing base objects
read-only; the original `.git` pointer and detached HEAD are preserved. Only
named source, test and documentation files were staged, and every commit has
the requested co-author trailer. Delivery is the ignored, unstaged
`docs/reports/2026-10-14-frozen-stream-p3.bundle`, with prerequisite
`75597f81bb3b` and the workspace-local branch. No logs, JSON evidence or bundles
were staged.
