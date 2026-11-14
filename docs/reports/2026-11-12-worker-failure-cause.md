# Worker failure causes on release/217

Implemented causes at all three sites with #26's cadence. Final targeted
validation: **1,185 passed, 2 skipped; all seven mutants killed**. The existing
underscore-scheme URL leak remains for the hub. Commits are blocked by the
workspace's shared `index.lock` permissions.

Start: `2026-11-12 05:49:38 UTC` (`date -u`, first command). Base:
`b51f08f961e1b1dcfb93347eb080f9ddd9a9260d`. Work is confined to the assigned
workspace. The checkout is detached; `fix/worker-failure-cause-217` already
exists and is checked out in the caller's worktree, so switching to it was
refused. The caller's checkout and branch have not been changed.

## Handler path, checked before adding causes

At the base, `subfleet/daemon.py:971` obtains `self.log`; lines 976–981 remove
stale handlers, open `daemon.log` in append mode, and install the sole
`daemon_log.ScrubbingStreamHandler`. `daemon_log.py:313–315` installs
`ScrubbingFormatter`; lines 317–328 format before acquiring the write lock,
and lines 333–341 write the formatted text. Propagation remains enabled, but
the repository installs no ancestor or root handler. Every daemon `self.log`
record therefore reaches this formatter.

`daemon_log.py:98` defines `RECORD_SCRUB_MAX = 16_384`. The formatter copies the
record, renders its message and traceback/stack parts, detects exception groups
before cutting, and shares that source budget among the parts (244–252,
275–297). Tracebacks are reconstructed from `exc_info`, ignoring cached
`exc_text` when exception information exists (299–307). `handoff.cut_whole`
(998–1059) keeps safe whole lines from each part's head and tail and inserts an
omission marker. It avoids cuts inside credentials, quoted values, PEM blocks,
folded matches and data URI bodies; an unsafe long line becomes only a marker.
The cut precedes the bounded whole-text scrub.

Single-line messages use `refusals.scrubbed_line` (618–625): scrub whole, fold,
scrub again, then cut without splitting redaction markers to `LOG_LINE_MAX =
2000`. Multiline messages and traceback/stack blocks use `scrubbed_block`
(`daemon_log.py:131–166`): scrub whole, fold and rescrub, keeping the folded
result if it hides more. Groups are also read without their margins (180–202).
Release and head scrub results are intersected (149–150). The 16,384-character
limit bounds pre-scrub source text, with omission-marker/join overhead; it is
not a strict final-output cap, since redaction markers can expand text.
Existing handler tests use `2 * (RECORD_SCRUB_MAX + 100)` as an emitted-size
guard for selected fixtures, not a universal cap. An untimed 16,384-character
`token=x\n` repetition emits 34,816 characters, including 2,048 redaction
markers. The new shared-budget test instruments the actual source budget.

`ScrubbingFormatter.format` (266–272) catches rendering/scrubbing exceptions
and returns `failed_line` (230–241), containing checked level/logger/class
metadata rather than exception words. Process hooks (`daemon.py:13169`,
`daemon_log.py:698–740`) and startup refusals (`daemon.py:13174,13180,13215`,
`daemon_log.write_scrubbed`, 399–405) use the same formatter. No repository
logging-record path to `daemon.log` bypasses it.

Separate non-record diagnostics do write directly: `faulthandler` (explicitly
documented at `daemon_log.py:69–73`), watchdog thread/frame metadata without
source lines or locals (`daemon.py:13029–13056`), and fixed watchdog stopping
messages/numeric/class metadata (`13079,13085–13087,13114–13117`). They never
receive the worker/control/request exceptions' words. This audit interprets
"daemon record" in the context of the requested `self.log` records; it does
not claim that every byte in the file passes the formatter.

## Exception logging idiom

The requested base lines were rechecked: `daemon.py:5676–5677` logs ownership
failure type only; 9563–9564 logs the route record's type; 10874–10875 logs
structured `_workspace_error` fields. Nearby `_workspace_failed` (9647–9650)
renders words with `refusals.scrubbed_line` and a fallback;
`refusals.scrubbed_words` (628–635) likewise catches rendering failure before
returning a scrubbed bounded line. Exception information fits the proposed
sites: the handler owns whole-record rendering, scrubbing and bounding,
including notes, chains, source lines and groups. However, an executed probe
showed `exc_info=exc` raises before the handler when an exception overrides
`__traceback__` to raise: `logging.Logger._log` reads that attribute.
`exc_info=True`, inside the active `except`, returns normally with the same
exception information. The final calls use `exc_info=True`, or the
cadence/deduplication boolean. Hostile metadata has tests at all three sites.
No `worker_failure_cause` or `worker_failure_location` helper was ported.
The residual URL scrub-list gap below would survive #26's helper too; that
helper calls the same scrubber.

## Work and validation

Production changes, with final daemon line numbers:

- `daemon.py:618–646`: `WORKER_CAUSE_EVERY = 32`, `TRACED_CAUSES_MAX = 256`,
  and message-independent, cycle-terminating `raise_signature`; unreadable
  metadata gets a fixed fallback, without a second risky metadata lookup.
  Base exception descriptors bypass subclass metadata hooks and preserve
  distinct raise locations even for a hostile `__traceback__`.
- `963–965`: control/request signature sets and the request lock.
- `5508–5528`: every one-shot failure carries its cause; paced causes are
  exactly failures 1, 33, 65, ...; unchanged summary text appears on powers of
  two and cause-bearing failures. The retention timer remains type-only (5524).
- `5820–5835`: successful control iterations clear remembered signatures;
  `_first_trace` caps sets at 256 and restarts on the next new signature.
- `12433–12437`: request causes are remembered per operation/signature under
  the lock. The generic wire error remains fixed.
- `done()`'s `DEFERRED` count rule, generation wake-up (5538–5539), and key
  release in `finally` (5540–5541) remain intact.
- Manually amended only C-5.10 (`docs/acceptance-contract.md:478`), without
  renumbering clauses. Updated the inspection log mock to accept logging
  keywords, the bare control-loop fixture to initialize its set, and the
  admission regression to assert a visible cause with a synthetic password
  hidden. Existing exact summary compares remain unchanged.

`subfleet/sessions/handoff.py` is unchanged. Commits are blocked: both
named-file `git add` and `git commit` were denied creation of the shared
worktree's `index.lock`. No history or caller branch was rewritten; the hub
must collect the workspace files.

## C-23.14 handler matrix

Each input was a real `LogRecord` passed to `ScrubbingStreamHandler`, writing
to memory. Timings are single full `handle(record)` elapsed measurements,
with initialization before the clock, no traceback subtraction and no
selection of minima. Evidence:
`docs/reports/2026-11-12-worker-failure-handler-data.json`.

| Input row | Result on release/217 | Seconds |
|---|---|---:|
| Header after `RuntimeError: refused:` | Credential and all 12-character fragments hidden | 0.012836 |
| `curl -H 'Authorization: Basic <cred>'` | Hidden | 0.000396 |
| Command repr containing the header | Hidden | 0.000213 |
| Header after escaped `\\n` | Hidden | 0.000197 |
| `"x_" * 3000` assignment | Hidden; line form bounded | 0.128488 |
| 64 KiB newlines | Cut/bounded | 0.418768 |
| 256 KiB PEM openings | Cut/bounded | 0.510079 |
| `my_postgres://app:<pw>@db` | **Existing leak: password survives; route to hub** | 0.000132 |
| Exactly 16,384 newlines | Bounded | 0.204650 |
| Exactly 16,384 PEM-opening characters | Bounded; slowest bound-sized probe | 0.905218 |
| Exactly 16,384 assignment characters | Hidden | 0.083761 |

The requested one-second gate passed before causes were added; no measured
record exceeded one second. An initial assignment probe was 16,424 characters,
not 16,384, and was replaced by the corrected measurement (both recorded in
the JSON). The URL gap is explicitly pinned by
`test_c2314_underscore_url_password_still_reaches_handler_output_documented_limitation`.
Other rows are covered by `test_c2314_inline_headers_are_hidden_by_the_handler`,
`test_c2314_compound_assignment_is_hidden_by_the_handler` and
`test_c2314_adversarial_records_are_bounded`.

## Invariants and tests

Tests below are in `tests/unit/test_worker_failure_cause.py` unless qualified.
Exactly four new Hypothesis tests cover cadence and signatures (24 examples
each), and handler credential surfaces and cut windows (48 examples each).

| Invariant | Evidence |
|---|---|
| 1. Cause rendering never raises into the three sites; `done()` releases its key | `test_hostile_exception_cause_never_escapes_or_skips_handler`: string/repr failures, hostile traceback metadata, cyclic cause/context, huge messages, groups, nonstring args and notes across all three sites; inline callbacks propagate failures. `test_done_notifies_only_for_store_generation_and_always_releases_key`; handler fixed-fallback test. |
| 2. No credential or 12-character fragment reaches daemon.log | `test_exception_message_notes_chain_and_source_are_scrubbed` at all three sites; handler properties `test_cause_credentials_in_messages_notes_chains_and_source_never_reach_handler` and `test_cause_credentials_at_cut_windows_never_leave_twelve_character_fragments`. **The universal invariant does not hold** on unchanged release/217: the underscore-scheme URL row above remains for the hub. The tests prove the listed recognized scrub forms. |
| 3. Cadence, exact summaries, one-shot causes, deduplication, lock and 256-entry cap | `test_paced_cause_and_exact_summary_cadence`, `test_one_shot_worker_always_logs_a_cause`, `test_control_cause_deduplication_resets_after_completed_iteration`, `test_first_trace_holds_at_most_256_and_starts_again_when_full`, `test_control_and_request_use_the_bounded_signature_sets`, and request lock/concurrency tests. Retention and DEFERRED: `test_retention_error_is_type_only_and_deferred_keeps_count`. |
| 4. Signature ignores messages, distinguishes raise locations and never renders hostile exceptions | `test_raise_signature_ignores_message_and_distinguishes_raise_site`; `test_raise_signature_never_renders_hostile_exception`; `test_raise_signature_preserves_distinct_sites_with_hostile_metadata`. |
| 5. Records respect the handler's own bound | Handler `test_handler_shares_its_source_budget_between_message_traceback_and_stack`, adversarial records and cut-window properties. Three oversized parts share exactly 16,384 source characters plus cut markers. The handler has no strict 16,384-character final-output promise. |

## Mutation verification

Every mutation ran alone. Each test exited 1 for the intended failure; the
runner exited 0. Before the next mutation, the source was restored from its
pristine copy and checked byte-for-byte and by SHA-256. The final source hash is
`c691dcc0837fdcb2dfda0491098e82b203f638e9e50655b62e5437b7e88635b1`.

All selectors below are in `tests/unit/test_worker_failure_cause.py`.

| Mutant | Test that killed it | Pytest result |
|---|---|---|
| 1. Cause on every paced failure | `test_paced_cause_and_exact_summary_cadence` | 1 failed; counterexamples n=2 and n=3 |
| 2. Render with `str(exc)` before protected logging, escaping `done()` | `test_hostile_exception_cause_never_escapes_or_skips_handler` | 2 failed, 28 passed; string/argument-repr errors escaped |
| 3. `str(exc)` in retention timer error | `test_retention_error_is_type_only_and_deferred_keeps_count` | 1 failed; message differed from `RuntimeError` |
| 4. Never clear control signatures on completion | `test_control_cause_deduplication_resets_after_completed_iteration` | 1 failed; final cause missing |
| 5. Remember more than 256 | `test_first_trace_holds_at_most_256_and_starts_again_when_full` | 1 failed at 257 |
| 6. Change summary wording | `test_paced_cause_and_exact_summary_cadence` | 1 failed at n=1 |
| 7. Print raw cause to stderr alongside handler logging | `test_exception_message_notes_chain_and_source_are_scrubbed` | 1 failed, 2 passed; 12-character credential fragment exposed |

Replayable runner: `docs/reports/2026-11-12-worker-failure-mutants.py`.
Exact mutations, commands, full failure outputs and restoration hashes:
`docs/reports/2026-11-12-worker-failure-mutants.json`.

## Test commands and counts

Every run set `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and
`UV_CACHE_DIR=/private/tmp/sf-wcp-b1/uv-cache`. Let `S` denote
`/private/tmp/sf-wcp-b1`. The common command prefix was
`uv run --no-sync --with 'pytest-xdist>=3.6' pytest`; mutation runs also passed
`uv --cache-dir "$UV_CACHE_DIR"`. Runs were serial by file first, then the
combined `-n 4` runs. Each used a fresh basetemp.

| Run, in order | Arguments after the prefix | Result |
|---|---|---|
| Cause draft | `tests/unit/test_worker_failure_cause.py --basetemp=S/tests-new-cause-1` | 50 passed, 48.04 s |
| Hostile metadata sites | Same file, `--basetemp=S/tests-new-cause-2` | 53 passed, 22.48 s |
| Handler draft | `tests/unit/test_worker_failure_handler.py --basetemp=S/tests-new-handler-1 -q` | 21 passed, 30.74 s |
| Handler shared budget | Same file, `--basetemp=S/tests-new-handler-2 -q` | 22 passed, 31.47 s |
| Initial matched run | All matched files below plus `tests/unit/test_daemon_log_sites.py -n 4 --basetemp=S/tests-matches-1 -q` | 759 passed, 2 skipped, 378.71 s |
| Final signature regression | `tests/unit/test_worker_failure_cause.py --basetemp=S/tests-new-cause-3 -q` | 54 passed, 16.07 s |
| Final matched run | Same combined files, `-n 4 --basetemp=S/tests-matches-2 -q -rs` | 760 passed, 2 skipped, 274.70 s |
| Required contract/refusal run | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py -n 4 --basetemp=S/tests-contract-1 -q -rs` | 425 passed, 86.55 s |
| Seven isolated mutants | `.venv/bin/python docs/reports/2026-11-12-worker-failure-mutants.py`; each selector `-n 4 --basetemp=S/mutants-1794464172319997000/pytest-N -q` | 7/7 killed; runner exit 0 |

The matched files came from this exact search, expanded as pytest arguments:

```sh
rg -l 'in a row, next try|worker %s failed|control iteration failed|request %s failed|_worker_failures|ScrubbingFormatter' tests/
```

It found these 11 files after the additions: `test_daemon_log_cut_windows.py`,
`test_daemon_inspection_load.py`, `test_daemon_settle.py`,
`test_daemon_log_handler.py`, `test_kill4_properties.py`,
`test_tick_prefilter.py`, `test_admission_visibility.py`,
`test_admission_route_isolation.py`, `test_native_maintenance_startup.py`,
`test_worker_failure_cause.py`, `test_worker_failure_handler.py`.
The logging-site file was added for its handler/stream-path lints.

The two skipped tests are the optional daemon-start/launchd logging-site
regressions at `test_daemon_log_sites.py:368,390`: permitted sysctl/ps inspection
is unavailable, specifically the macOS boot identity. All four required final
files ran, including `test_invariants_index.py`. The whole suite was not run,
as instructed. Full standard-run commands/counts and available output captures
are in `docs/reports/2026-11-12-worker-failure-validation.json`.

## Commands and deviations

- Initial `date -u`, `pwd`, `git status --short`, `git branch --show-current`,
  and `rg --files -g AGENTS.md -g pyproject.toml -g '*lock*' -g SKILL.md`:
  clean detached checkout, no AGENTS.md found.
- Read the GitNexus debugging skill and searched available tool metadata:
  no GitNexus tools; used direct source tracing.
- Read #26 through `git diff $(git merge-base origin/main 07ad535e50)
  07ad535e50 -- subfleet/daemon.py`, also `git diff origin/main...07ad535e50
  -- subfleet/daemon.py`, `git show 07ad535e50 --stat`, and its contract clause:
  design available in local objects.
- `git switch -c fix/worker-failure-cause-217`: refused, branch exists.
  `git rev-parse fix/worker-failure-cause-217` confirms the base;
  `git switch fix/worker-failure-cause-217`: refused, caller owns its checkout.
- `getconf DARWIN_USER_TEMP_DIR`: `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`.
  Created `/private/tmp/sf-wcp-b1`; all explicit cache, interpreter and test
  scratch paths were below it. `TMPDIR` is set from `getconf` on sync/test
  commands; `UV_CACHE_DIR` and `--cache-dir` use its permitted scratch child
  fallback `/private/tmp/sf-wcp-b1/uv-cache`.
- `uv sync --group dev --extra api`: failed, this checkout defines no `api`
  extra. `uv sync --group dev`: passed, 9 packages installed, using the
  preinstalled CPython 3.14.7 freethreaded interpreter and workspace `.venv`.
- Direct `sed`, `nl` and `rg` reads traced the named daemon sites, formatter,
  refusal helpers, cut implementation, contract and existing test fixtures.
  Two exploratory globs had no matches; searches named nonexistent
  `subfleet/stop_watch.py` and `tests/unit/test_daemon_log.py` (exit 2), then
  used the actual files. No live state was read.
- Repeated `rg` searches checked log literals, `_worker_failures`,
  `ScrubbingFormatter`, direct `_control`/`_answer` calls, logging error mocks,
  handler installation, root logging configuration, direct stream writes,
  skip conditions, and the exact changed-line compares. They found no extra
  repository logging-record bypass. Existing exact summary assertions stayed
  unchanged; the new property pins all summary counts and all three strings.
- `uv run --no-sync python` inline probes: 11 accepted matrix timings plus one
  oversized assignment probe; the corrected assignment replaced it. One
  untimed expansion probe established 16,384 input to 34,816 output characters.
  The exception-information probe reproduced the pre-handler traceback-attribute
  raise with `exc_info=exc` and returned normally with `exc_info=True`.
- Python syntax checks passed for the handler file and mutation runner; a
  draft report patch with mismatched context changed nothing and was retried.
- The initial dev sync installed 9 packages; the first `--with` test command
  installed 7 overlay packages. No dependency or lockfile was changed.
- Mutation aggregation checked all seven intended failures and final pristine
  bytes/hash, and copied full evidence into the worktree. Standard test output
  captures were copied there before scratch cleanup. `git diff --check` passed;
  `git diff -- subfleet/sessions/handoff.py` was empty.
- Final named-file `git add` and `git commit -m 'fix: log scrubbed worker control
  and request causes on release 217'`: both exit 128, the same denied
  `index.lock`. The earlier audit-document commit attempt was denied too.
- Python parsed all three evidence JSON files, verified the pristine daemon
  hash, then removed `/private/tmp/sf-wcp-b1` with `shutil.rmtree`, guarded by
  assertions that the path contains `sf-wcp-b1`, equals that exact scratch
  directory, and is not a symlink. Cleanup passed; no scratch folder remains.
- Final `git diff --check`: passed. `git diff --stat`: five tracked files,
  72 insertions/12 deletions; `git status --short --untracked-files=normal`:
  those five modified files and seven new test/report/evidence files.
  `git rev-parse HEAD` returned the unchanged base. Final validation/cleanup
  time from `date -u`: `2026-11-12 06:24:38 UTC`, 35 minutes after the start.

Deviations: the missing `api` extra required dev-only sync; the assigned branch
belongs to the caller, so this checkout stayed detached; shared-index
permissions prevent commits; ordinary pytest runs used the explicit
`UV_CACHE_DIR` environment setting rather than repeating `--cache-dir`; the
exception boolean and base metadata descriptors address the demonstrated
pre-formatter raise/signature collision. The audit's interpretation of a
"daemon record" and the existing URL leak/output-bound distinction are stated
above. No scrub-list fix was folded into this work.

No interpreter/cache/test folder outside the workspace and
`/private/tmp/sf-wcp-b1` has been created intentionally. No live process,
credential store, live daemon log, or caller file was read or changed.
Repository pytest hooks also create their own `sf-test-homes-*` directories
under system temp and remove them at session teardown (`tests/conftest.py:60,133`);
their generated names were not retained. Explicit basetemps and uv cache
paths were all under `S`.

Head built: `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d`
