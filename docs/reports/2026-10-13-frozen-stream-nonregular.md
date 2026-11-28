Frozen provider streams and cached deliverables, 2026-10-13

Base: `69b119f94007` from `release/217`. Work stayed in the assigned workspace;
the caller checkout, live stores and processes were not changed. No network Git
remotes or sub-agents were used.

Codex and Claude now select the first existing frozen directory entry, including
an empty file, directory, FIFO or symlink. A non-regular entry supplies no events.
The reader checks its type before opening it, uses `O_NOFOLLOW` for regular
streams and retains `O_NONBLOCK` protection for an entry replaced during open.
Only absent frozen candidates allow stdout fallback. The freeze preserves both
the launch's raw-stream candidate and the canonical `stream.jsonl`, so an absent
custom path cannot shadow an existing canonical entry. Raw-stream artifact
capture also refuses non-regular entries without opening them.

Claude's bounded diagnostic reader and complete summary reader honor that same
authority, including empty, whitespace-only and malformed regular files. An
interruption before `finalization.json` or `deliverable.md` cannot extract a
later stdout result through an existing frozen entry.

Finalization decides captured deliverable presence by content on every replay
and restart: Codex uses `bytes.strip()` (ASCII whitespace), while Claude decodes
UTF-8 with replacement and uses `str.strip()` (Unicode whitespace). Accepted
payload bytes remain unchanged. A cached blank `ok` follows the existing empty
result rule: `unknown`, `empty deliverable with rc 0`, no acceptance or export;
the one-attempt regression jobs become `failed`. The original provider verdict
and captured files remain intact. C-4.3, C-9.2, C-12.6 and version history were
updated with these rules.

Regressions were committed before their fixes. On the base production code,
the final locked selection produced 86 expected failures and 14 passing
controls in 56.71 seconds. Its production modules were checked byte for byte
against `69b119f94007`:

| File | Passed | Expected failures |
| --- | ---: | ---: |
| `tests/unit/test_frozen_provider_streams.py` | 6 | 42 |
| `tests/fake/test_frozen_stream_nonregular.py` | 6 | 26 |
| `tests/fake/test_codex_turn_completed_fake.py` (selected cases) | 2 | 18 |

The executed review inputs include cached `auth-dead`, rc 0, captured interim
text and a completion appended only to stdout, for directories and FIFOs, both
immediate replay and restart. Additional cases cover links, empty Claude streams,
interruptions before both capture checkpoints and cached whitespace deliverables.
The unit regressions guard `os.open` to prove non-regular frozen entries are never
opened. The custom-launch follow-up adds ten replay/restart cases.

Four bounded Hypothesis cases (two providers times two properties) use 120
generated examples and two explicit examples each, with deterministic generation
and streams of at most ten events. They check that rewriting and deleting stdout
cannot change class, deliverable or stream readings once a frozen entry exists,
and that absent-entry fallback matches freezing the same stdout bytes. Frozen
types include empty/regular files, directories, FIFOs and live/dangling links.

Final validation at the delivered code is complete for every requested
compatibility file. The continuation from the checkpoint preserved the existing
workspace-local commits and ran four additional foreground pytest processes,
each with its own shared-lock hold. All **836 tests passed**, with no failures:

| File | Passed | Coverage / unrun |
| --- | ---: | --- |
| `tests/unit/test_frozen_provider_streams.py` | 48 | Complete, including all four bounded properties |
| `tests/fake/test_frozen_stream_nonregular.py` | 32 | Complete |
| `tests/fake/test_codex_turn_completed_fake.py` | 105 | Complete |
| `tests/unit/test_codex_turn_completed.py` | 41 | Complete |
| `tests/unit/test_test_module_names.py` | 1 | Complete |
| `tests/fake/test_finalization_replay.py` | 10 | Complete |
| `tests/unit/test_claude_adapter.py` | 170 | Complete |
| `tests/unit/test_empty_provider_results.py` | 334 | Complete |
| `tests/fake/test_salvage_finalization.py` | 47 | Complete, split into 22 and 25 cases under separate holds |
| `tests/fake/test_state_contract.py` | 37 | Complete |
| `tests/unit/test_sessions_transcripts.py` | 11 | All requested opener cases; remaining cases unrun |

The four requests passed 594, 195, 22 and 25 cases in 39.46, 176.04, 15.09
and 24.37 pytest seconds respectively. The only warning is the existing Codex
`zip` parametrization deprecation. This supersedes the checkpoint report's
incomplete head compatibility counts. Earlier validation also passed the
first-fix selection (154 cases); the malformed-stream control was corrected in
`14c4e488f` to retain the parser's existing `transient` class. No production
code changed during the continuation.

Nine requests in all, including the continuation's initial collect-only request,
were refused after a 240-second lock wait, before pytest started. All requested
compatibility checks subsequently completed. CI, the full unit/fake suites and
other e2e/process/live suites were not run locally. Every pytest process used
its own shared-lock hold, a fresh `sf-frozen-<pid>` TMPDIR, `-p no:cacheprovider`,
and Hypothesis storage inside that TMPDIR. Every hold stayed below 20 minutes.
Cleanup used Python `shutil.rmtree`, including manual cleanup of the checkpoint's
interrupted request. Dependencies were synced with `uv sync --locked`; Python
3.14.7 freethreaded ran with `PYTHON_GIL=1`. The uv cache was disposable and later
requests used the installed environment with `UV_NO_SYNC=1`. Mutations borrowed
that environment with `UV_PROJECT_ENVIRONMENT`, without syncing it to a
different checkout. The continuation parsed per-file counts from temporary
JUnit XML and removed that evidence with each request's TMPDIR.

Disposable mutation results, completed before the checkpoint and preserved:

| Reverted behavior | Unit failures / controls | Fake failures / controls | Total / pytest time |
| --- | --- | --- | --- |
| Base Codex adapter, freeze and raw-artifact rules | Frozen-stream: 17 / 1 | Codex completion: 18 / 2 | 35 failures, 3 controls; 140.04 s |
| Base Claude readers | Frozen-stream: 25 / 5 | New replay cases: 20 / 0 | 45 failures, 5 controls; 32.74 s |
| Cached acceptance by artifact byte count | — | Cached-result cases: 6 / 6 | 6 failures, 6 controls; 40.72 s |

All mutations reached regression assertions. FIFOs were either rejected before
open or opened only with the existing `O_NONBLOCK` guard in base/mutant readers;
none could block. The baseline selection used all final regression tests
against unchanged base production code. Mutation and baseline copies were
removed after validation. `git diff --check` passes.

Commits, in order:

- `c62c15906`: initial regression cases and bounded properties.
- `b1e96095b`: Codex authority, safe stream reads, freeze and raw-artifact handling.
- `9538e151d`: both Claude readers and syscall guards.
- `2e398e5d9`: cached deliverable content checks and empty-result contract.
- `aaa1690a5`: custom launch-path replay/restart regression cases.
- `547fa5449`: preserve canonical entries beside custom launch paths.
- `14c4e488f`: preserve existing malformed-stream classification in the control.

- `f9e3e2f7c`: checkpoint report with initial validation and mutation results.

The continuation report commit follows these steps. Delivery is the ignored, unstaged
`docs/reports/2026-10-13-frozen-stream-nonregular.bundle`, with prerequisite
`69b119f94007`, head `refs/heads/fix/frozen-stream-nonregular`. Bundle verification
and import into separate local metadata compare the delivered commit and tree
with the workspace-local head.

The shared Git index rejected writes (`index.lock: Operation not permitted`).
Commits use ignored workspace-local `.git-local/repository.git` on
`fix/frozen-stream-nonregular`, rooted at the assigned base and borrowing existing
objects read-only. The original `.git` pointer and detached HEAD are untouched.
Only named source, test and documentation files were staged; every commit has
the requested co-author trailer. No execution evidence or bundles were staged.
