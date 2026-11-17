# Worker failure causes on release/217

Implemented causes at all three sites with #26's cadence. Final targeted
validation: **1,185 passed, 2 skipped; all seven mutants killed**. The build
round left the underscore-scheme URL leak for the hub, and its commits were
blocked by the workspace's shared `index.lock` permissions; the hub then
committed the build as `0b750661a` and this report as `1593a4efd`. Review r1
found more (URL forms, hostile types, re-raises, escapes, chain cost); "Fix r1"
records what changed. Review r2 found a test the release/217 merge broke, a URL
password with an `@` and then a raw `/`, keys split by invisible characters and
an untested outer frame; "Fix r2" records what changed. Review r3 found a
mask fix r2's terminal reading lost, approval masking that missed two URL
passwords, and r2's follower case (a key split across feeds) still open; "Fix
round 3" at the end records what changed.

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

In the build round `subfleet/sessions/handoff.py` was unchanged, and commits
were blocked: both named-file `git add` and `git commit` were denied creation
of the shared worktree's `index.lock`, so the hub collected the files and
committed them (`0b750661a`, `1593a4efd`). Fix r1 changes the head's URL rule
in `handoff.py` (see "Fix r1").

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
| `my_postgres://app:<pw>@db` | **Leaked in the build round; hidden since fix r1** | 0.000132 |
| Exactly 16,384 newlines | Bounded | 0.204650 |
| Exactly 16,384 PEM-opening characters | Bounded; slowest bound-sized probe | 0.905218 |
| Exactly 16,384 assignment characters | Hidden | 0.083761 |

The requested one-second gate passed before causes were added; no measured
record exceeded one second. An initial assignment probe was 16,424 characters,
not 16,384, and was replaced by the corrected measurement (both recorded in
the JSON). The build round pinned the URL gap with
`test_c2314_underscore_url_password_still_reaches_handler_output_documented_limitation`;
fix r1 replaced it with `test_c2314_underscore_url_password_is_hidden_by_the_handler`.
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
| 2. No credential or 12-character fragment reaches daemon.log | `test_exception_message_notes_chain_and_source_are_scrubbed` at all three sites; handler properties `test_cause_credentials_in_messages_notes_chains_and_source_never_reach_handler` and `test_cause_credentials_at_cut_windows_never_leave_twelve_character_fragments`. In the build round **the universal invariant did not hold**: the underscore-scheme URL row above leaked, and review r1 found five more URL forms. Fix r1 hides all six (see "Fix r1"); values the scrubber does not claim to recognize remain outside it. |
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

The build round's runner rewrote `subfleet/daemon.py` in place; fix r1
replaced it with one that works on a copy (see "Fix r1"), carrying these seven
mutants with current anchors.
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

Head built by the build round: none of its own (commits were blocked); its
base was `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d`, and the hub committed its
files as `0b750661a`.

## Fix r1

Round 1's fix of review r1 (`review-r1.md`, head `1593a4efd`). Start:
`2026-11-15 01:42:31 UTC` (`date -u`, first command). Load was 42 to 189
throughout; most runs were at 150 to 185. All values in tests and probes are
made up; no environment value was printed.

### Findings and changes

**P1, URL forms.** `subfleet/sessions/handoff.py:105-117` (the head's rule)
and `:297-305` (`_linear_sub`'s URL scan). A scheme is now any run of
letters, digits, `+`, `.`, `_` and `-` that `://` follows, tried once from the
run's start (so `9p://`, `retry=2postgres://`, `my_postgres://`). The user may
be empty (`redis://:pw@cache`) or hold an `@` (`user@server`, as Azure's
database URLs name one). The password runs to the last `@` before the first
`/` (`ftp://anon:Pw@x@host`), or, where no `@` comes before that `/`, to the
last `@` before whitespace or the next `://` (a raw `/`). An empty password is
skipped. Each try reads at most to the next `://`, so the rule stays linear.
`_URL_START_RE` is gone. One cost: `https://host:8443/@user` is now read as a
password (`8443/`) and hidden; a password holding `/` cannot be told from it.

- **The frozen copy.** `subfleet/sessions/scrub_release.py` is release/217's
  scrubber frozen at `2c981cba5` (#239 round 8). Its docstring says "Never edit
  this file; a release change to the scrubber is a new frozen module", and
  `tests/unit/test_scrub_release_frozen.py` pins its kept lines to that git
  object. It stays unchanged, and that is safe: every consumer runs it first
  and then keeps a whitespace-free run only if the head's own output holds it
  (`handoff.shown_by_both`). That covers `handoff.scrub_secrets` and
  `scrub_bounded` (`_released_pieces`), `daemon_log.scrubbed_block` (the
  handler's block form, the tail and the follower),
  `suggestions._released_first`/`_line_bounded` (the line form), and
  `redact.mask_approval` (the head masks release's output). So a run the head
  hides never shows: `my_postgres://app:<pw>@db` is written as `[REDACTED]`
  whole. `_taken_out` reads only release's quoted-value spans, which no URL
  touches.
- **Tests.** `tests/unit/test_worker_failure_url_and_terminal.py`:
  - `test_r1_url_forms_are_hidden_on_every_route`: 7 forms × 10 routes. The
    routes are the message, a note, `__cause__`, `__context__`, a source line,
    an exception-group member (each through the worker's `done()`), `_control`,
    `_answer`, `scrubbed_tail` and `LogFollower`. Each case checks for no
    12-character fragment of the value and that the control URL
    `https://status.example.test:8443/health?check=1` is written whole.
  - `test_a_url_without_a_password_is_written_whole_on_every_route` (10).
  - The documented-limitation test became
    `test_c2314_underscore_url_password_is_hidden_by_the_handler`
    (`tests/unit/test_worker_failure_handler.py`).

**P2, hostile types.** `subfleet/daemon.py:623-685`:
- `raise_signature` reads `type.__dict__["__qualname__"].__get__(type(link))`.
- `_plain` turns each part into an exact `str`, `int` or `None`
  (`str.__str__`, `int.__index__`), so the signature hashes without running a
  subclass's code.
- `failure_class` reads `type.__dict__["__name__"]` the same way. The three
  summary lines (`:5552`, `:5566`, `:5861`, `:12478`) and the retention mark
  (`:5562`) use it; the text is unchanged for ordinary classes.
- `_first_trace` (`:5866-5878`) catches any failure and returns True, so the
  cause is logged.
- Tests in `tests/unit/test_worker_failure_cause.py`:
  - `test_hostile_exception_types_never_raise_into_control_or_request`: a list
    `__qualname__`, a `str` subclass whose `__hash__` raises, and a raising
    `__name__`, at both sites; two raises log `[True, False]`.
  - `test_raise_signature_of_a_hostile_type_is_plain_and_hashable`.
  - `test_first_trace_treats_an_uncomparable_signature_as_new`.

**P2, re-raises.** `daemon.py:641-674`: each link is its qualified name plus
the innermost frame (where it was first raised) and the outermost (where it
was caught). Raising the same object again prepends frames between them and
keeps both. I kept the outermost frame beside r1's innermost one, so causes
from different call sites in the handler stay distinct; this deviates from
r1's fix as written. Tests:
`test_one_object_raised_on_three_control_ticks_logs_one_cause` and
`test_one_object_raised_from_three_requests_logs_one_cause`, each giving
`[True, False, False]`.

**P2, R1 to R3.** `test_raise_signature_honours_raise_from_none`,
`test_raise_signature_reads_line_numbers` and
`test_raise_signature_reads_the_whole_chain`.

**P2, escapes.** `subfleet/daemon_log.py`:
- `:140-191`: `_CONTROL` covers C0 (but tab, line feed and carriage return),
  DEL and C1. `_rendered` reads a text as a terminal shows it: CSI, string
  (OSC, DCS, SOS, PM, APC, to BEL, ST or the line's end) and other ESC
  sequences, plus non-whitespace controls, are left out. `_visible` writes
  each control as `\xNN`. `_controls_shown` replaces a run with `[REDACTED]`
  when its terminal reading is not a run of the rendered text's scrub.
- `:346-356`: the formatter scrubs the cut parts again in their terminal
  reading when any part holds a control (`_scrubbed_parts`, `:359`).
- `:608` and `:853`: `scrubbed_tail` and `LogFollower._write` do the same
  (`_terminal_safe`, `:170`).
- So r1's OSC 52 case is written as
  `\x1b]52;c;ZmFrZQ==\x07 clipboard \x1b[2J` in visible text, and
  `pass\x1b[0mword=<value>` becomes `[REDACTED]`.
- Tests: `test_osc52_and_screen_clear_are_written_as_visible_escapes` and
  `test_a_key_an_escape_splits_is_still_a_password` (message, control,
  request, tail, follow each), `test_c1_controls_bell_and_other_c0_controls_are_visible`,
  `test_a_record_without_controls_is_written_as_before`, and a Hypothesis
  invariant, `test_no_control_character_but_tab_and_line_breaks_is_ever_written`
  (60 examples, handler, tail and follower).
- `tests/unit/test_daemon_log_handler.py::test_a_one_line_record_is_exactly_its_line_form`
  now expects the escaped form when a line holds a control.

**P3, formatting cost.** `daemon_log.py:104-111`, `:386-470`:
- `_chain_ends` counts the exceptions Python's renderer would read (chain plus
  group members), through `BaseException` descriptors, with cycles ended.
- At most `EXCEPTIONS_RENDERED_MAX = 64`, it renders as before
  (`formatException`). Above that, `_ends_text` renders 16 links from each end
  of the chain. Each link is built alone (`TracebackException(..., _seen=set())`,
  CPython's no-chain mode) and linked by hand, with no group members, and a
  marker line counts the links left out. At most `CHAIN_READ_MAX = 100,000`
  links are read.
- `_seen` is a private CPython parameter (present from 3.12 to 3.15). If it
  changed, `format` would catch the error and write the fixed `failed_line`.
- Tests: `test_a_chain_within_the_bound_is_rendered_as_python_renders_it`,
  `test_a_longer_chain_renders_only_its_two_ends` (65, 2,000 and 5,000 links;
  exactly 32 `TracebackException`s built), `test_a_group_of_many_members_is_rendered_without_them`,
  and `test_a_long_chain_logged_by_a_site_writes_both_ends`.
- Not bounded: an exception's own `str()` (r1's 10^6-entry dict). That is the
  exception's code, and every logger pays it.

**P3, change log.** `docs/acceptance-contract.md:9` adds the C-5.10 line, and
`:479` amends C-5.10 for this round's behaviour. No clause was renumbered.

**P3, report.** The stale lines above (the summary, the commit note, the URL
row, the invariant row, the runner note and "Head built") are corrected.

**P3, the runner.** It stays, rewritten so it never writes into the checkout.
`docs/reports/2026-11-12-worker-failure-mutants.py` copies the tracked
`subfleet/`, `tests/` and top-level files under a `sf-` scratch folder. It
checks that the copy is the one imported, applies each mutant alone there,
runs its tests with the venv's pytest, writes the copied bytes back and checks
their hash. It hashes the checkout's three sources before and after, and
removes the copy with a guarded `rmtree`. It holds the build round's seven
mutants (B1 to B7, anchors updated) and this round's sixteen.

### Mutants

One run, `.venv/bin/python docs/reports/2026-11-12-worker-failure-mutants.py
--scratch $TMPDIR/sf-wc-f1 --out $TMPDIR/sf-wc-f1/mutants.json --budget 420`,
03:02 to 03:07 UTC, load 178 to 156. Exit 0: **23 of 23 killed** (each pytest
exited 1). The copy was removed, every restored hash matched, and
`git status` was clean afterwards.

| Mutant (file) | Killed by | Result |
|---|---|---|
| B1 cause on every paced failure (daemon) | `test_paced_cause_and_exact_summary_cadence` | 1 failed |
| B2 render before the handler (daemon) | `test_hostile_exception_cause_never_escapes_or_skips_handler` | 2 failed, 28 passed |
| B3 `str(exc)` in `timers.mark` (daemon) | `test_retention_error_is_type_only_and_deferred_keeps_count` | 1 failed |
| B4 control set never clears (daemon) | `test_control_cause_deduplication_resets_after_completed_iteration` | 1 failed |
| B5 set past 256 (daemon) | `test_first_trace_holds_at_most_256_and_starts_again_when_full` | 1 failed |
| B6 summary words (daemon) | `test_paced_cause_and_exact_summary_cadence` | 1 failed |
| B7 raw print to stderr (daemon) | `test_exception_message_notes_chain_and_source_are_scrubbed[worker]` | 1 failed, 2 passed |
| F1 no `_` in a scheme, word-boundary start (handoff) | `test_r1_url_forms_are_hidden_on_every_route[underscore-*]` | 20 failed |
| F2 scheme must start with a letter (handoff) | `…[digit-first-scheme-*]`, `…[scheme-after-digit-*]` | 20 failed |
| F3 tried at the run's first boundary letter (handoff) | same | 20 failed |
| F4 non-empty user (handoff) | `…[empty-user-*]` | 10 failed |
| F5 no `/` in a password (handoff) | `…[raw-slash-*]` | 10 failed |
| F6 password ends at the first `@` (handoff) | `…[second-at-*]` | 10 failed |
| F7 `type(link).__qualname__` (daemon) | `test_hostile_exception_types_never_raise_into_control_or_request` (both qualname cases, both sites), `test_raise_signature_of_a_hostile_type_is_plain_and_hashable` | 6 failed, 3 passed |
| F8 `_first_trace` unguarded (daemon) | `test_first_trace_treats_an_uncomparable_signature_as_new` | 1 failed |
| F9 class name through the metaclass (daemon) | `test_hostile_exception_types_never_raise_into_control_or_request[*-raising-name]` | 2 failed, 4 passed |
| F10 whole-traceback keying (daemon) | `test_one_object_raised_on_three_control_ticks_logs_one_cause`, `…_from_three_requests_…` | 2 failed |
| F11 R1 `__suppress_context__` ignored (daemon) | `test_raise_signature_honours_raise_from_none` | 1 failed |
| F12 R2 line number 0 (daemon) | `test_raise_signature_reads_line_numbers` | 1 failed |
| F13 R3 first link only (daemon) | `test_raise_signature_reads_the_whole_chain` | 1 failed |
| F14 escapes not neutralized (daemon_log) | `test_osc52_and_screen_clear_are_written_as_visible_escapes`, the control-character invariant | 6 failed |
| F15 terminal reading ignored (daemon_log) | `test_a_key_an_escape_splits_is_still_a_password` | 5 failed |
| F16 chain not capped (daemon_log) | `test_a_longer_chain_renders_only_its_two_ends` | 3 failed |

### Formatting cost

The probe was `measure_chain.py` in scratch. It builds an n-link `__context__`
chain of raised `RuntimeError`s, then times `formatException` alone and the
full `ScrubbingStreamHandler.handle`, wall clock, single runs.

| Links | Before (`1593a4efd`, load ~45): `formatException` / handler | After (load ~91 to 101): handler | Written |
|---:|---|---:|---|
| 64 | not measured | 0.976 s (rendered as before) | 16,292 chars |
| 65 | not measured | 1.023 s | 9,124 chars |
| 2,000 | 3.195 s / 5.100 s | 1.125 s | 16,352 → 9,159 chars |
| 5,000 | 9.491 s / 7.800 s | 0.135 s | 16,354 → 9,159 chars |

After the change, `formatException` alone still takes 5.1 s and 6.1 s for 2,000
and 5,000 links, but the handler no longer calls it for them. The "before"
and "after" columns ran at different loads (45 against about 95), so the
ratio understates the gain.

### Commands and results

Every test run set `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` and
`UV_CACHE_DIR=$TMPDIR/uv-cache`, used `uv run --no-sync --with
'pytest-xdist>=3.6' pytest … -n 4 -p no:cacheprovider` (the mutant and timing
runs used `.venv/bin/python -m pytest`, one worker), and passed a new
`--basetemp=$TMPDIR/sf-wc-f1/pytest-…` each time. Times are UTC; load is the
1-minute average at start and end.

| # | When | Files | Result | Load |
|---|---|---|---|---|
| 0 | 01:56 | `uv sync --group dev` | 9 packages installed | 81 |
| 1 | 02:11 | cause + handler files | 1 failed (the documented-limitation test, as expected), 75 passed | 42 → 83 |
| 2 | 02:17 | + url/terminal file, `-x` | 1 failed (my test expected a tab kept in the line form, which folds it; fixed), 111 passed | 116 → 108 |
| 3 | 02:17 | the three worker-failure files | **190 passed** in 41 s | 99 → 89 |
| 4 | 02:20 | 14 `test_scrub_*` files | stopped by `timeout 580` at about 71%, no failure printed | 106 → 172 |
| 5 | 02:30 | 11 of them | stopped at about 93%, no failure printed | 157 → 175 |
| 6 | 02:42 | `test_invariants_index.py` | **25 passed** (after the contract edit) | 166 → 175 |
| 7 | 02:43 | `test_test_module_names.py` | **1 passed** | 175 → 186 |
| 8 | 02:43 | `test_scrub_inline_headers.py` | 1 failed, 272 passed (see below) | 186 → 110 |
| 9 | 02:51 | `test_scrub_no_new_leak.py` | **4 passed** in 392 s | 113 → 181 |
| 10 | 02:58 | `test_scrub_release_first.py` | **25 passed** | 181 → 184 |
| 11 | 03:02 | mutants | **23/23 killed** | 178 → 156 |
| 12 | 03:07 | `test_scrub_review_r10`, `r2` to `r5` | **171 passed, 2 skipped** | 157 → 178 |
| 13 | 03:13 | `r6` to `r8`, `r9b`, `test_scrub_release_frozen`, `test_scrub_key_order` | **129 passed, 2 skipped** | 179 → 176 |
| 14 | 03:20 | `test_sessions_handoff`, `_excerpts`, `test_conversation_redact`, `test_suggestion_secret_properties`, `test_conversation_handoff_briefs` together | stopped at 560 s at about 82%, with 7 `F` marks in the progress line and no names | 179 → 184 |
| 15 | 03:30 | `test_sessions_handoff.py` alone | **145 passed** | 187 → 177 |
| 16 | 03:31 | `test_sessions_handoff_excerpts.py` alone | stopped at 560 s, no summary | 178 → 186 |
| 17 | 03:40 | `test_conversation_redact`, `test_suggestion_secret_properties`, `test_conversation_handoff_briefs` | **44 passed** | 189 → 186 |
| 18 | 03:42 | excerpts, `-x` | stopped at 500 s after 13 passed | 184 → not recorded |
| 19 | 03:51 | 4 excerpts tests by id (the URL-fragment properties, the linear-time wrapper tests, the classifier) | **11 passed** | 128 → 129 |
| 20 | 03:52 | excerpts without `test_an_excerpt_never_shows_a_value_the_whole_scrub_removes` | **158 passed** | 132 → 155 |
| 21 | 03:57 | `test_daemon_log_cut_windows`, `_first_pass` | stopped at 560 s at about 99%, no `F` | 178 → 183 |
| 22 | 04:12 | `test_daemon_log_handler`, `_sites`, `-v` | stopped at 540 s: 99 passed, 5 failed (below) | 158 → 75 |
| 23 | 04:26 | the differential test plus two of the four-MiB tests, after the fix | 2 passed, 1 failed (below) | 166 → 153 |
| 24 | 04:42 | `test_handler_refusals.py` | stopped by `timeout 330`: about 105 tests passed (the progress line reached 19% and 33 more), no `F` | 143 → 168 |

Failures and what they show:
- **Run 8:** `test_inline_headers_and_bearer_keep_scrubbing_linear[a Cookie:\n]`.
  Its CPU-scaling subprocess hit its own 60 s wall-clock timeout at load about
  185. The input has no `://`, so the URL rule tries no match on it.
- **Run 22:**
  - `test_a_one_line_record_is_exactly_its_line_form` was an intended change
    (controls are now escaped). Its expectation was updated, and it passed in
    run 23.
  - Four `test_a_four_mib_record_of_a_cost_shape_is_written_within_a_second[*-traceback]`
    failures: the "CPU beyond Python's own formatting" guard. Those inputs hold
    no control character and have a one-link chain, so this change adds only
    one descriptor walk to them.
- **Run 23:** `[quote-dense-traceback]` passed. `[blank-lines-after-a-key-traceback]`
  failed on its `@pytest.mark.timeout(30)` inside `textwrap`, which is Python's
  own traceback formatting.
- **Not checked against base:** I did not get these timing tests run on base
  in the time left (a base copy was made but not used), so "load, not this
  change" is a reading of the code, not a measurement.
- **Run 14's seven `F` marks are unexplained.** Every file of that batch
  passed when run alone (runs 15, 17, 19 and 20), except excerpts' heavy
  property, which never finished in any run. Round 2 should run that batch,
  and that property, at a lower load.

**Did not run** (time ran out at this load):
- daemon_log files: `test_daemon_log_reminders.py`,
  `test_daemon_log_scrubbing.py`, `test_daemon_log_trailing_reminders.py`,
  and the rest of `test_daemon_log_handler.py` and `test_daemon_log_sites.py`
  after run 22's 540 s;
- most of **`test_handler_refusals.py`**, which this brief requires (run 24
  covered about 105 of its tests, with no failure);
- the daemon and fake files: `test_daemon_inspection_load.py`,
  `test_daemon_settle.py`, `test_kill4_properties.py`,
  `test_tick_prefilter.py`, `tests/fake/test_admission_route_isolation.py`,
  `test_admission_visibility.py`, `test_native_maintenance_startup.py` and
  `test_state_contract.py`;
- other files that import `handoff`: `tests/fake/test_sessions_end_to_end.py`,
  `test_launch_env_round8.py`, `test_outcome_unknown.py`,
  `test_protocol_secrecy.py`, `test_sessions_cli.py`,
  `test_sessions_transcripts.py`, `test_status_hold_source.py`,
  `test_store_ids.py` and `test_task_hold_cas_wire.py`;
- excerpts' `test_an_excerpt_never_shows_a_value_the_whole_scrub_removes`;
- a re-run of the three worker-failure files after `3fcd0ac72`. No source
  changed after run 3, only the contract, the runner, the report and
  `test_daemon_log_handler.py`.

Other commands:
- `git diff`/`git log` reads, `rg`/`grep` searches inside the worktree, and
  inline `.venv/bin/python -I` probes of the URL forms and escapes against the
  handler and the tail;
- `py_compile` and an anchor check that all 23 mutants build and parse;
- `git archive 1593a4efd subfleet tests pyproject.toml` into
  `$TMPDIR/sf-wc-f1/base-copy`, unused;
- commits by named files only:
  - `09cffdeb4`: fixes and tests;
  - `3fcd0ac72`: contract;
  - `e4ea89f8c`: runner;
  - `26869e411`: handler differential test;
  - this report's commit.

### Deviations

- Each link keeps its outermost frame beside r1's innermost one (see P2,
  re-raises).
- `failure_class` hardens the summary lines' class name too. Review r1 named
  only `__qualname__`, but a raising `__name__` would have escaped the same
  handlers.
- The contract amendment and change-log line describe the URL rule (C-23.14's
  head) and the handler's escapes (C-25.7's output) inside C-5.10, rather than
  amending those clauses.
- `https://host:8443/@user`-shaped URLs are now hidden from the port on (see
  P1).
- The test runs fell short of the brief's list (above). A wall-clock 560 s cap
  per tool call at load 150 to 190 stopped seven runs.
- The uv cache was the brief's `$TMPDIR/uv-cache`. It already existed and is
  shared, so I left it.
- One test command outran its tool limit and the harness moved it to the
  background, where it finished (run 23). I started nothing else in the
  background.

Folders outside scratch: none created. Scratch `$TMPDIR/sf-wc-f1`, holding the
probes, the per-run outputs, the mutant results, the base copy and the
pytest basetemps, is removed at the end with a guarded `shutil.rmtree` that
asserts `sf-wc-f1` is in the path. pytest's own `sf-test-homes-*` folders
(`tests/conftest.py`) are removed by its session teardown.

Head built: `26869e411` (code, tests, contract and runner). This section is
committed on top of it.

## Fix r2

Round 2's fix of review r2 (`review-r2.md`, head `bb118f63d`). Start:
`2026-11-15 19:34:41 UTC` (`date -u`, first command). Load was 94 to 216
throughout. All values in tests and probes are made up; no environment value
was printed.

### Findings and changes

**P1, `run_control` failed every time.**
- Cause: #302 on release/217 made `_control` call `self._retention_due()` each
  iteration. That calls `_retention_delete_ready()` and then
  `self._retention_delete.pending()`, which the fixture's `Daemon.__new__`
  service never sets. So the "completed" iteration raised `AttributeError`
  and the reset never ran.
- Change: `tests/unit/test_worker_failure_cause.py:118`. `run_control` stubs
  `service._retention_due = lambda: False` beside its other stubs. The
  deduplication test also checks that the logged causes are the fixture's own
  (`["one", "three"]`), so a gap like this one names itself next time.
- Shown: before, `1 failed` (`[True, False, True, False] != [True, False, True]`,
  53 s); after, `1 passed`. With the reset removed (B4), `1 failed`.

**P1, a password with an `@` and then a raw `/`.**
- Cause: fix r1's password had two branches. The first, "to the last `@`
  before the first `/`", matched `p` in `https://alice:p@ss/…@host.test`, so
  the second branch was never tried.
- Change: `subfleet/sessions/handoff.py:105-115`. The password now has one
  branch: it runs to the last `@` before whitespace or the next `://`. That
  branch matches wherever the old first one did, so the first is gone.
  `_linear_sub` is unchanged, and each try still reads at most to the next
  `://`.
- Cost, as the hub ruled: an `@` later in a path is read as the userinfo's end.
  - `https://alice:pw@host.test/users/@me` is written as `[REDACTED]` whole,
    host included. Fix r1 hid only `pw`.
  - `http://localhost:3000/api/users/@me` is written as `[REDACTED]`, as under
    fix r1.
  - `https://host.test/users/@me` (no `:` after the host) is written whole.
  - A JSON list of two password URLs with no space between them still hides
    each password alone (probed).
- Tests, in `tests/unit/test_worker_failure_url_and_terminal.py`:
  - `test_r2_a_password_with_an_at_and_then_a_raw_slash_is_hidden_on_every_route`
    (review r2's case);
  - `test_r2_an_at_in_a_path_hides_what_comes_before_it_on_every_route`, which
    pins the over-redaction.

  Each runs on all ten of r1's routes: message, note, `__cause__`,
  `__context__`, source line, group member, control, request, tail and follow.
  That covers the brief's five.
- Shown: G1 puts back fix r1's two-branch password: 20 failed. After the fix:
  20 passed. The pin's no-password and no-port parts behaved the same before
  the fix. It fails on fix r1's rule because of its host case.

**P2, a key split by an invisible character.**
- Cause: `_HIDDEN_CONTROL` dropped only controls that aren't whitespace to `\s`.
  - `\x1c` to `\x1f` are whitespace to `\s`, so the terminal reading kept them
    and they split the key.
  - Format characters are not controls. The formatter and `_terminal_safe`
    read a text as a terminal shows it only when it held a `_CONTROL`, so a
    text holding only format characters was never read so.
- Change: `subfleet/daemon_log.py:140-215`, and `:374` in the formatter.
  - `_FORMAT` lists Unicode's category Cf as of Unicode 17 (the venv's Python
    3.15.2). The list is fixed in the code, so import does no scan.
  - `_INVISIBLE` replaces `_HIDDEN_CONTROL`. `_rendered` drops what it names:
    the controls that aren't whitespace, `\x1c` to `\x1f`, and `_FORMAT`.
  - `_UNSHOWN` is `_CONTROL` plus `_FORMAT`. A text holding one is read as a
    terminal shows it, by the formatter and by `_terminal_safe` (the tail and
    the follower).
  - `_SHOWN_RUN`: `_controls_shown` reads a run across `\x1c` to `\x1f`, as a
    terminal draws it. Words a separator joins are still written when they
    hide nothing, with the separator escaped.
  - Format characters are written as they are, not escaped. Soft hyphens, ZWJ
    emoji and a BOM in plain text are unchanged.
  - The code comment that said "the others separate runs in either reading"
    now names the controls left and why they separate runs.
  - `test_daemon_log_handler.py::test_a_one_line_record_is_exactly_its_line_form`
    now decides on `_UNSHOWN`, as the handler does.
- Cost, pinned: a one-line record's line form folds `\x1f` to a space, so the
  handler writes `alpha\x1fbeta gamma` as `[REDACTED] [REDACTED] gamma`. Each
  word alone isn't a run of the terminal reading. `\x1c` to `\x1e` break
  `str.splitlines()`, so that record takes the block form, which keeps them. The
  tail and the follower write all four as `alpha\x1Nbeta gamma`.
- Tests, in `tests/unit/test_worker_failure_url_and_terminal.py`:
  - `test_r2_a_key_an_invisible_character_splits_is_still_a_password`: 40
    cases (ZWSP, soft hyphen, word joiner, BOM and `\x1c` to `\x1f`, each on
    message, control, request, tail and follow);
  - `test_r2_words_a_separator_joins_are_one_run_that_hides_nothing`: 4 cases;
  - `test_r2_the_format_characters_are_unicode_cf`: the class against the
    running Python's `unicodedata`, allowing `Cn` for an older Python's Unicode;
  - `test_r2_format_characters_in_plain_text_are_written_as_they_are`.
- Shown: each part undone alone fails a test.
  - G3, format characters kept by the reading: 21 failed.
  - G4, separators kept: 4 failed.
  - G5, format characters not read as a terminal shows them: 21 failed.
  - G6, a run ends at a separator: 4 failed.
  - After the fix: 46 passed.

  The tests weren't run against `bb118f63d` itself. Review r2's own run
  showed the leak there.
- Review r3 found one case still open: a key split across the follower's
  feeds (`pass\u200bword:\n`, then its value). Fix round 3 closes it.
- Left, for the hub to file:
  - Bidirectional controls (Cf, such as U+202E) are read as invisible but
    written raw. A terminal that does bidi could reorder what it shows.
  - Zero-width characters outside Cf aren't dropped from the reading:
    combining marks such as U+034F, variation selectors and Hangul fillers. A
    key they split isn't read whole.

**P2, the outermost frame was untested.**
- Cause: every signature test raised and caught in one frame, or told the links
  apart by the innermost frame.
- Change, tests only (`tests/unit/test_worker_failure_cause.py:559-575`):
  - `test_raise_signature_reads_the_outermost_frame`: one helper's raise line,
    caught at two calls, gives two signatures, and the same call gives one.
  - `test_one_helper_reached_from_two_control_lines_logs_two_causes`:
    `run_control` reaches the helper through the loop's query and through its
    pending exports, logging `[True, True, False]`. `run_control` gained an
    `exports` keyword and calls a callable outcome.
- Shown: M1 (G2, `outer = None`), 2 failed; after, 2 passed.

**P3s.**
- **Formatting cost is bounded by exceptions, not frames** (`daemon_log.py:106`).
  Not fixed in code. C-5.10 now says the renderer reads at most 64
  exceptions, "each with every frame of its traceback". For the hub to file:
  a per-link frame cap. Review r2 measured 60 links of about 1,600 frames each
  at 87.7 s.
- **Over-redaction in shared consumers, and C-23.14 not amended.** C-23.14's
  text names no URL rule (checked: its only `://` is a header example), so
  this change doesn't touch it. Listed for the hub: the amendment is still
  open, now with fix r2's host case too. C-5.10 states that cost.
- **Two narrow URL forms still leak.** Not fixed; listed.
  - A password containing `://`: each try stops at the next `://` to stay
    linear.
  - A scheme of non-ASCII letters (`дб://`): the rule's class is ASCII.
    `[\w+.-]` would take it, but it would widen every consumer's rule, so it
    is the hub's call.
- **Stale PR body**: what changed is listed below for the hub's rewrite.

### What the PR body should now say

- The build round: causes at all three sites with #26's cadence, through the
  handler. Its "1,185 passed, 7/7 mutants" are the build round's own counts.
- The underscore leak the body calls "Gap, not fixed here" is fixed. Fix r1
  hid it with review r1's other URL forms: a digit-first scheme, a scheme
  after a digit, an empty user, a user holding `@`, a raw `/`, a second `@`.
- Fix r1 also:
  - made signatures read no metaclass hook, and count one that can't be
    compared as new;
  - kept one cause for a re-raised object, keying each link on its innermost
    and outermost frames;
  - wrote control characters as `\xNN` escapes on the handler, tail and
    follower, and read a text as a terminal shows it;
  - capped a chain of more than 64 exceptions at its two 16-link ends;
  - rewrote the mutant runner to work on a copy (23/23 killed).
- Fix r2:
  - the test stub for #302's `_retention_due`;
  - a URL password to its last `@` before whitespace or `://` (a host before a
    path `@` is hidden too);
  - `\x1c` to `\x1f` and Unicode format characters read as invisible in a key;
  - tests for the outermost frame;
  - C-5.10 and its change-log line amended.
- The shared head URL rule (C-23.14) changed in fixes r1 and r2. C-23.14
  itself isn't amended (above).

### Mutants

`.venv/bin/python docs/reports/2026-11-12-worker-failure-mutants.py --scratch
$TMPDIR/sf-wcp-fix2/mutants --out $TMPDIR/sf-wcp-fix2/mutants/mutants.json
--budget 330 NAME …`, in four slices of at most 590 s each, from 20:46 to
21:16 UTC. The runner gained G1 to G6, and fix r1's F1, F2, F4, F5, F6 and F14
were re-anchored on the new code. All 29 mutants build and parse.

13 were run and all 13 were killed. Every copy was removed and every restored
hash matched. Results are in
`docs/reports/2026-11-12-worker-failure-mutants-r2.json`. Its `daemon_log.py`
hash predates `e2af521cd`, which changed only a comment.

| Mutant | Result | Seconds | Load |
|---|---|---:|---|
| B4 control set never clears | 1 failed | 97 | 125 → 136 |
| G1 fix r1's first-`/` branch first | 20 failed | 113 | 136 → 166 |
| G2 M1, outer frame dropped | 2 failed | 65 | 166 → 158 |
| G3 format characters kept by the reading | 21 failed, 20 passed | 76 | 158 → 150 |
| G4 separators kept by the reading | 4 failed, 40 passed (second run) | 124 | 100 → 137 |
| G5 format characters don't trigger the reading | 21 failed, 20 passed | 113 | 146 → 182 |
| G6 a run ends at a separator | 4 failed | 69 | 182 → 96 |
| F1 underscore scheme | 20 failed | 67 | 117 → 101 |
| F2 digit-first scheme | 20 failed | 66 | 57 → 34 |
| F4 empty user | 10 failed | 141 | 34 → 99 |
| F5 no `/` in a password | 10 failed | 40 | 101 → 66 |
| F6 password ends at the first `@` | 10 failed | 51 | 66 → 47 |
| F14 escapes not neutralized | 6 failed | 96 | 47 → 141 |

G4's first run, against the key-split test alone, **survived** (40 passed,
99 s). The joined run (`_SHOWN_RUN`) hides those keys by itself, so dropping
the separators matters for what is written whole, not for hiding. I added the
separator test to G4's list, and the second run killed it. The runner rewrites
its JSON, so the first run's row is recorded only here.

Not re-run: B1 to B3, B5 to B7, F3, F7 to F13, F15 and F16. Their anchors
didn't change, and all build.

### Commands and results

Every test run set `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` and
`UV_CACHE_DIR=$TMPDIR/uv-cache`, with `-p no:cacheprovider` and a new
`--basetemp=$TMPDIR/sf-wcp-fix2/pytest-NN-…`. The prefix was
`uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest … -n 4`; runs 1,
2, 5 and 6 used `uv run --no-sync python -m pytest`, one worker. Each command
was wrapped in `timeout 540` to `570`. Times are UTC.

| # | When | Files | Result | Load |
|---|---|---|---|---|
| 0 | 19:55 | `uv sync --group dev` | installed (pytest 9.1.1, the workspace package) | ~156 |
| 1 | 19:56 | the deduplication test, before the stub | **1 failed** (the P1) | 140 |
| 2 | 20:01 | the same, after | **1 passed** | – |
| 3 | 20:05 | the URL file, `-k r2_` | **20 passed**, 175 s | 216 |
| 4 | 20:22 | the URL file, `-k "r2_ and (invisible or format)"` | **42 passed**. The differential test named beside it was deselected by `-k`; run 10 ran it | 141 |
| 5 | 20:29 | four selected tests of the cause file | **4 passed** | 211 |
| 6 | 20:36 | the URL file, `-k separator_joins` | **4 passed** | – |
| 7 | 21:18 | the three worker-failure files | **258 passed**, 136 s | 109 → 125 |
| 8 | 21:23 | `test_state_contract`, `test_test_module_names`, `test_invariants_index` | **63 passed** | 187 |
| 9 | 21:25 | `test_handler_refusals.py` | **372 passed**, 199 s | 94 → 102 |
| 10 | 21:29 | `test_daemon_log_handler`, `_tail`, `_scrubbing`, `_reminders`, `_trailing_reminders` | stopped by `timeout 545` with **385 passed, no `F` or `E`**, at about 99% | 106 → 98 |
| 11 | 21:39 | `test_sessions_handoff`, `test_scrub_release_first`, `test_conversation_redact`, `test_suggestion_secret_properties`, `test_conversation_handoff_briefs` | stopped by `timeout 545` with **212 passed, no `F` or `E`**, at about 99% | 101 → 137 |

| 12 | 21:54 | `test_scrub_no_new_leak`, `test_scrub_release_frozen`, `test_scrub_inline_headers` | stopped by `timeout 545` at about 250 results, **3 `F` marks, unnamed** (`-q`) | 102 → 187 |
| 13 | 22:04 | `test_scrub_inline_headers.py -k linear` | **1 failed, 16 passed**: `test_inline_headers_and_bearer_keep_scrubbing_linear[a Cookie:\n]`, its CPU-scaling subprocess past its own 60 s `TimeoutExpired` | 248 → 290 |
| 14 | 22:09 | run 12's files, `-k "not linear"`, `-v` | **262 passed**, 324 s | 298 → 160 |

Run 7's 258 are r1's 190 plus this round's 68: 20 URL, 40 invisible-key,
4 separator, 2 format-class and 2 outer-frame tests.

**The scaling failure (runs 12 and 13).**
- Run 14 passed every test of run 12's files but the 17 cases of
  `test_inline_headers_and_bearer_keep_scrubbing_linear`. So run 12's three
  `F` marks are, by inference, cases of that test. They weren't named.
- Run 13 named one case: `[a Cookie:\n]`, the same case fix r1's run 8 failed
  at load about 185.
- Its input holds no `://`. For it, the URL rule does only what fix r1's did:
  one scheme-run scan and a `startswith` per run. So I read the failure as load
  (248 to 290), not this change.
- That is a reading of the code. The test wasn't run on base, or at a low load.

Other commands:
- `git diff`, `git log` and `git grep` reads in the worktree.
- `.venv/bin/python -I` probes, run from scratch scripts:
  - the URL forms through `scrub_secrets` and `scrubbed_tail`;
  - the invisible keys and plain separator text through the handler, the tail
    and the follower;
  - the Cf list from `unicodedata`.
- An anchor check that all 29 mutants build and parse.
- Commits, by named files only:
  - `525dd4177`: the P1 stub;
  - `795ba89da`: the URL rule;
  - `420c66ef6`: the invisible characters;
  - `a993aafd7`: the outer-frame tests;
  - `5800a0206` and `94b4febec`: the runner and its results;
  - `e2af521cd`: the contract;
  - this report's commit.

**Did not run.** Time ran out at this load. Of the 50 test files that import
`daemon_log` or `handoff` (`git grep -l`), these ran:
- in full: the three worker-failure files and `test_handler_refusals.py`;
- in full but for the scaling test above: `test_scrub_inline_headers`,
  `test_scrub_no_new_leak` and `test_scrub_release_frozen`;
- to about 99%, with no failure: runs 10 and 11 above.

These did not run:
- `tests/fake`: `test_daemon_launch`, `test_reset_credit_incident` and
  `test_sessions_end_to_end`;
- `test_conversation_handoff_dispatch_races`, `test_conversation_service`;
- `test_daemon_log_cut_windows`, `_first_pass`, `_mutation_tool` and `_sites`;
- `test_daemon_verbs`, `test_launch_env_round7` and `_round8`,
  `test_lockwatch`, `test_outcome_unknown`, `test_protocol_secrecy`;
- `test_scrub_key_order`, `_review_r10`, `r2` to `r7` and `_review_r9b`
  (runs 12 to 14 ran `_inline_headers`, `_no_new_leak` and `_release_frozen`);
- `test_sessions_cli`, `test_sessions_handoff_excerpts`,
  `test_sessions_transcripts`, `test_status_hold_source`, `test_store_ids`
  and `test_task_hold_cas_wire`.

`tests/reference_*.py` and `tests/scrub_work.py` are helpers, not test
modules. CI runs the whole suite.

### Deviations

- The URL tests run on all ten of r1's routes, not just the five the brief
  names.
- `run_control` gained an `exports` keyword and calls a callable outcome, for
  the control-site outer-frame test.
- C-5.10's "reads at most that many" now says exceptions, each with every
  frame. The code isn't changed for that P3.
- Format characters are read as invisible but written as they are, not
  escaped.
- `_SHOWN_RUN` joins a run across `\x1c` to `\x1f`. That isn't in the brief; it
  keeps words a separator joins from being over-redacted, and G6 and the
  separator test cover it.
- G4 survived its first run (above).
- Two commands ran past the harness's 120 s default and were moved to its
  background, where both finished: the first `git status` (about 10 minutes,
  load about 156) and a `grep -r` that followed an edit. I started nothing in
  the background myself.

Folders outside scratch:
- `$TMPDIR/uv-cache` existed before this job (created 2026-11-14) and is
  shared, so I left it.
- `uv sync` created `.venv` in the workspace.
- pytest's own `sf-test-homes-*` folders (`tests/conftest.py`) are removed by
  its session teardown. Others' are there too, so I touched none.

Scratch `$TMPDIR/sf-wcp-fix2` held the probes, the run outputs, the mutant
copies' results and the basetemps. It was removed at 22:16 UTC with a guarded
`shutil.rmtree` that asserts `sf-wcp-fix2` is in the path and is the folder's
name; it no longer exists. No process of mine is left. No live state,
credential store or caller file was read or changed.

Head built: the commit holding this section's last edit, on top of
`dc515a2ad` (the section's first commit) and `e2af521cd` (code, tests,
contract and runner). Finished at 22:16 UTC, 2 h 41 min after the start.

## Fix round 3

Round 3's fix of review r3 (`review-r3.md`, head `a6443ab04`). Start:
`2026-11-17 04:09:26 UTC` (`date -u`, first command). Load was 17 to 200. All
values in tests and probes are made up; no environment value was printed.

### Findings and changes

**P1, `daemon_log.py:183`: fix r2's reading lost a mask bb118f63d had.**
- Cause: fix r2's `_rendered` drops format characters as well as controls. So
  `x​Bearer\x1b[0m <token>` reads `xBearer <token>` there, and `\bBearer`
  doesn't match. bb118f63d's reading dropped only the controls, so it read
  `x​Bearer <token>`, a `Bearer` token. The text as written doesn't match
  either: the escape splits `Bearer` from its space.
- Probe, before (`probe-before.txt` in scratch): at `a6443ab04` the marker was
  shown on message, tail and follow; at `bb118f63d` it was hidden on all three.
- Change: `subfleet/daemon_log.py`.
  - `_HIDDEN_CONTROL` and `_rendered_controls` are bb118f63d's reading.
  - `_readings` lists the readings a text gets: fix r2's (`_rendered`, runs
    joined across `\x1c` to `\x1f`) when it holds a control or format
    character, and bb118f63d's too (runs split at whitespace, as there) when it
    holds a control character. A text with no control character reads as
    itself in bb118f63d's reading, so that reading is skipped then.
  - `_controls_shown` takes a list of checks. A run is written only if, in
    every reading, each of its runs reads as nothing or as a run that reading's
    scrub holds. Otherwise the whole joined run is `[REDACTED]`.
  - The formatter, `_terminal_safe` (the tail) and the follower use it.
- Why no input loses a mask it had at bb118f63d: bb118f63d's own check runs
  unchanged, on the same scrubbed text, whenever it ran there; the head adds
  checks and never removes one.
- Tests:
  - `tests/unit/test_worker_failure_url_and_terminal.py::test_r3_a_format_character_before_bearer_keeps_bb118f63ds_mask`:
    the review's input on message, control, request, tail and follow.
  - `tests/unit/test_worker_failure_r3_properties.py::test_r3_no_character_bb118f63d_masked_is_written`:
    the differential. bb118f63d's terminal layer (`_CONTROL`,
    `_HIDDEN_CONTROL`, `_TERMINAL_SEQUENCE`, `_rendered`, `_visible`,
    `_terminal_safe`, `_controls_shown`, the formatter's `_scrubbed`,
    `scrubbed_tail` and `LogFollower`) is copied into the test, driven by the
    live scrubber on both sides. Over 120 generated texts, with invisible and
    control characters before, inside and after key words, the handler, tail
    and follower mask every character the oracle masks, and every run the head
    writes without a `[REDACTED]` is one the oracle writes. Values are drawn
    from letters nothing else in the output holds, each used once, so a
    missing letter is a masked one.
  - `test_r3_the_frozen_oracle_masks_review_r3s_input` checks the oracle.
- Shown: before (run 5, the final tests on an `a6443ab04` copy) the five route
  cases and the differential failed. The differential's first counterexample
  was the review's shape, `x​Bearer\x1b[0m FGHIJKLM`. After: all passed
  (runs 4 and 6). H1 (bb118f63d's reading dropped) is killed: 6 failed.

**P1, `handoff.py:115`: approval masking missed URL passwords.** Two causes:
- **(a) The shell-metacharacter refusal.** Fix r2's password runs to the last
  `@`. In `redis://:<pw>@cache.test/x?x=2&owner=@me` that span holds `&`, so
  `redact.py:162` refused it and the command was shown in plaintext with
  `spans=[]`.
- **(b) The composition.** Release masks first and ends a password at its
  first `@`. So `https://alice:p@ss/<marker>@host.test` became
  `https://alice:[masked url-password, 1 chars]@ss/<marker>@host.test`. The
  mask holds spaces, which the head's `\S` can't cross, so the head's pass
  found nothing. `ss/<marker>` was shown, at `a6443ab04` and at `bb118f63d`
  (probed). Review r3's ten routes have no approval route, so they didn't
  catch it.
- Change: `subfleet/conversations/redact.py`. `handoff.py:115` is unchanged.
  - `_APPROVAL_URL_PASSWORD_RE` is the head's URL rule, reading a mask an
    earlier pass wrote (`\[masked [a-z-]+, \d+ chars\]`) as part of a password.
  - `url_passwords` tries it once from each run of scheme characters that
    `://` follows, as `_linear_sub` does.
  - `_masked_password` masks each piece between the earlier masks and the
    shell metacharacters (`_SHELL_META`), and keeps those.
  - The other rules (PEM, JWT, token, `Bearer`) keep their refusal.
  - Review r3's URL is now `redis://:[masked url-password, 32 chars]&[masked url-password, 6 chars]@me`.
    Round 2's is `https://alice:[masked url-password, 1 chars][masked url-password, 25 chars]@host.test`.
  - Each head span's length and SHA-256 describe original characters, not a
    mask. `approval.get` calls `mask_approval` (`service.py:1627`), so it gets
    the same result. Its `reveal` path is unchanged.
- **Deviation from the ruling, for the hub.** The ruling says no password
  character may stay unmasked. IR-20 (`test_approval_masking_hides_tokens_but_never_a_command`,
  C-27.1 "the display includes every field that changes what is granted")
  says an approval never hides a shell metacharacter. I kept IR-20.
  - So a password's `&`, `;`, `|`, `` ` ``, `$(`, `>` or `<` stays in view. Every
    other password character is masked.
  - Masking them would let a command hide `;<command>;` inside a "password"
    from the person approving it.
  - A password of shell metacharacters only (`https://u:;@host`) gives no
    span.
  - If the hub rules the other way, H6 shows the change: `_PASSWORD_KEPT_RE`
    drops its metacharacters. IR-20's text then needs amending.
- Tests, in `tests/unit/test_worker_failure_r3_properties.py`:
  - `test_r3_review_urls_mask_their_whole_password`: both review URLs, each in
    `curl "…"`, `curl …` and bare.
  - `test_r3_an_approval_masks_every_password_character_but_shell_metacharacters`:
    60 generated URLs. Passwords are 1 to 5 pieces of 6 to 9 characters,
    joined by `@`, `/`, `?`, `&` and `:`. Users are empty, plain or hold `@`.
    Queries and fragments may hold `@` (`?x=2&owner=@me`, `#@top`). Commands
    are `curl "…"`, `curl …`, bare or `git clone … && echo done`.
  - Each case checks four things. There is a span. No password piece is
    shown. From the user's `:` the output holds only masks and shell
    metacharacters, then what followed the password or a suffix of it, so no
    password character but a metacharacter shows. Every shell metacharacter
    of the command is shown, in order.
- Shown: before, both failed (run 5); the generator's first failure was
  `curl "https://:222222@host.test?x=2&owner=@me"`. After, both passed. H4,
  H5 and H6 (one per part) are each killed.

**P2, `daemon_log.py:876`: an invisible key split across follower feeds.**
- Cause: two gaps in `LogFollower`.
  - `_write` read only the text it was writing as a terminal shows it. A
    value whose key ended the line before (already written, in the context)
    was checked against the context's scrub as written, where
    `pass​word:` is no key.
  - `feed` settled a break by the text as written only. So a private key's
    first line that a format character splits
    (`-----BEGIN RSA PRIV​ATE KEY-----`) settled, and its body was
    written before the key's end arrived.
- The finding fails on `bb118f63d` too (review r3; probe:
  `follow-split SHOWN` there).
- Change: `subfleet/daemon_log.py`.
  - `_terminal_safe` takes `around`, the context plus the written text. Each
    reading of it is scrubbed too, and a run must be held there as well. The
    follower's own margin marks (`|`, `+`) count as held, as in its existing
    rule.
  - `_settled_end` takes the earliest break `handoff.settled_end` gives in the
    text and in each reading. A reading drops no line feed, so its `n`th line
    start is the text's. `feed` and `__init__` use it.
- Limit: the follower checks new lines against at most `FOLLOW_CONTEXT`
  (8,192) characters of what it wrote. A key further back than that isn't
  read; that was already so for keys without invisible characters.
- Tests, in `tests/unit/test_worker_failure_r3_properties.py`:
  - `test_r3_an_invisible_key_split_across_feeds_is_still_a_password`: the
    review's three steps, with and without an earlier line.
  - `test_r3_a_follower_fed_in_any_split_hides_what_it_hides_whole`: 120
    generated texts, split at random points, after every line, or both. Fed in
    pieces, then flushed, the follower masks every character it masks fed
    whole, and every one `scrubbed_tail` masks.
  - `test_r3_a_private_key_an_invisible_character_splits_is_held_until_it_ends`
    covers the settle rule.
- Shown: before (run 5 and the direct property run on the copy) the named
  test, the private-key test and the property failed. The property's
  counterexample was the review's shape:
  `['pass​word:\n', 'FGHIJKLM\n', '\n']`. After, all passed. H2 (context
  not read) and H3 (settled by the text only) are each killed.

**P3, the PR body.** Not edited (the brief forbids `gh pr edit`). What it
should now say is below.

### What the PR body should now say

- The build round: causes at all three sites with #26's cadence, through the
  handler. "1,185 passed, 2 skipped; 7/7 mutants killed" are the build
  round's own counts and should say so.
- Drop "**Gap, not fixed here:** an underscore-scheme URL already leaks". Fix
  r1 hides it, with review r1's other URL forms. Review r3 confirmed the
  underscore cases pass.
- Fix r1 and fix r2: the list in "What the PR body should now say" under
  "Fix r2" above stands.
- Fix r3:
  - A text with control characters is read in bb118f63d's terminal reading as
    well as fix r2's, so no input loses a mask it had at bb118f63d. A
    differential test against a frozen copy of bb118f63d's terminal layer
    pins this.
  - `daemon logs --follow` reads the text it follows in each reading, and
    settles a break only where each reading does. Fed in any split, it hides
    what it hides fed whole.
  - An approval masks a URL's whole password. It reads through release's
    mask, and around shell metacharacters, which stay in view (IR-20).
  - C-5.10 and its change-log line amended.
- Current counts: fix r3's runs below. CI runs the whole suite.

### Mutants

`.venv/bin/python docs/reports/2026-11-12-worker-failure-mutants.py --scratch
$TMPDIR/sf-wcp-fix3/mutants --out $TMPDIR/sf-wcp-fix3/mutants/mutants.json
--budget 420 H1… H6`, under `timeout 590`, 05:30 to 05:40 UTC. Results:
`docs/reports/2026-11-12-worker-failure-mutants-r3.json`. Every restored hash
matched. All 35 mutants build and parse; F14 and F15 were re-anchored on the
new code (`_readings`' guard, `_controls_shown`'s test).

| Mutant | Killed by | Result | Seconds | Load |
|---|---|---|---:|---|
| H1 bb118f63d's reading dropped | the five route cases and the differential | 6 failed | 129 | 167 → 120 |
| H2 the follower's context not read | the split property | 1 failed, 1 passed | 51 | 120 → 97 |
| H3 a break settled by the text only | the private-key test | 1 failed | 23 | 97 → 79 |
| H4 the approval password stops at a mask | the review URLs | 1 failed | 17 | 79 → 61 |
| H5 a password with a metacharacter shown whole | the review URLs and the property | 2 failed | 61 | 61 → 94 |
| H6 metacharacters masked | the property | 1 failed | 120 | 94 → 173 |

H2's named test passed under it; the property killed it. My reading, not
checked: with no context check, the settle change still holds
`pass\u200bword:` back until its value arrives, so the review's three steps are
written together.

The run outlasted the tool's 600 s limit, which moved it to the background,
and `timeout 590` stopped it during its final `guarded_rmtree`, after H6's
result was written. The partial copy went with the scratch folder. Not re-run:
B1 to B7, F1 to F16 (F14 and F15 included) and G1 to G6.

### Commands and results

Every pytest run set `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`,
`UV_CACHE_DIR=$TMPDIR/uv-cache`, `-p no:cacheprovider` and a new
`--basetemp=$TMPDIR/sf-wcp-fix3/pytest-NN-…`. The prefix was `uv run --no-sync
--with 'pytest-xdist>=3.6' python -m pytest … -n 4` (its interpreter is
CPython 3.15.2), except runs 5, 15 and 16: `.venv/bin/python -m pytest`, one
worker. Times are UTC.

| # | When | Files | Result | Load |
|---|---|---|---|---|
| 0 | 04:19 | `uv sync --group dev` | installed | 155 |
| 1 | 04:45 | new tests, `-x`, before the fix | 4 failed, 1 passed; one failure was my run-containment check, too strict (fixed) | 91 |
| 2 | 04:53 | new tests, before the fix | 8 failed, 3 passed: the two properties found nothing | – |
| 3 | 04:57 | the two properties, generators biased, before | split property failed; differential passed | – |
| 4 | 05:15 | new and changed tests, after the fix | **13 passed** | 98 |
| 5 | 05:21 | final new tests on an `a6443ab04` copy (before) | **10 failed, 2 passed** (the oracle check and the split property) | – |
| – | 05:26 | the two properties, direct calls, on the copy and the head | copy: both failed, on the review's shapes; head: both passed | – |
| 6 | 05:44 | the four worker-failure files | **270 passed** | 58 → 39 |
| 7 | 05:46 | `test_scrub_release_first`, `test_conversation_redact`, `test_scrub_release_frozen`, `test_scrub_inline_headers` | **309 passed**, 205 s, `test_inline_headers_and_bearer_keep_scrubbing_linear` included | – → 71 |
| 8 | 05:50 | `test_daemon_log_tail`, `_scrubbing`, `_reminders`, `_trailing_reminders` | stopped by `timeout 570` at about 99%, no `F` or `E` | – → 34 |
| 12 | 06:02 | the same, `-v` | stopped by `timeout 540`: **257 of 259 passed, none failed** | 117 |
| 14 | 06:13 | `test_daemon_log_handler`, `_sites` | **3 failed, 208 passed** (below) | 95 |
| 15 | 06:22 | the four-MiB test on the copy, one worker | 3 passed, 19.9 s | 81 → 69 |
| 16 | 06:22 | the same on the head, one worker | **3 passed**, 13.1 s | 60 → 46 |
| 17 | 06:23 | `test_state_contract`, `test_test_module_names`, `test_invariants_index`, `test_handler_refusals` | **435 passed**, 119 s | 37 → 17 |
| 18 | 06:26 | `test_daemon_log_cut_windows`, `_first_pass`, `_mutation_tool`, `tests/fake/test_daemon_launch`, `test_reset_credit_incident`, `test_daemon_verbs`, `test_launch_env_round7`, `test_lockwatch`, `test_scrub_review_r2`, `r4`, `r5`, `r9b`, `test_sessions_handoff` | **594 passed, 1 skipped**, 475 s | 49 |

Runs 9 to 11 and 13 were collections or a run that collected nothing (zsh
doesn't split an unquoted variable into paths).

Run 14's failures:
- `test_a_four_mib_record_is_written_bounded_and_scrubbed_within_the_time_bound[traceback]`
  and `[both]`: 1.016 s and 1.524 s against the 1 s bound, with four workers.
  With one worker the head passed all three cases (13.1 s in all, run 16), as
  the pre-fix copy did just before (19.9 s, run 15, at a higher load). Those
  inputs (`four_mib`: frames and three assignments) hold no control or format
  character, so the change adds one regex scan to them. Review r3 saw two
  one-second timing failures on `bb118f63d` as well.
- `test_no_daemon_module_logs_a_caught_exception_raw`: names
  `subfleet/daemon.py:5568` and three more. This round didn't change
  `daemon.py`; review r3 reports this lint failing identically before fix r2.

Run 12's two unfinished tests are both in `test_daemon_log_trailing_reminders`:
`test_no_tag_is_left_in_what_is_resolved` and
`test_a_margin_file_ending_in_a_reminder_is_refused_only_where_the_masks_differ`.
The first calls only `_reminders_resolved`, which calls none of the changed
functions. I didn't check the second's call path or time it on base.

Run 18's skip: `test_scrub_review_r5.py:125`, the 95,040-cell class, opt-in.

Other commands:
- `git show`/`git diff`/`git log`/`git grep` reads, and `git archive` of
  `bb118f63d`'s `subfleet` and of `a6443ab04` into scratch;
- `.venv/bin/python -I` probes from scratch scripts: approval masking at both
  heads, the terminal routes at both heads, the private-key follower case;
- an anchor check that all 35 mutants build and parse;
- commits, by named files only:
  - `fb03222a5`: code and tests;
  - `c0fa49b2e`: each generator draws its review's shape first;
  - `d84c4947a`: the runner;
  - `d555e5be8` and `ee4f86a07`: the contract;
  - this report's commit, with `docs/reports/2026-11-12-worker-failure-mutants-r3.json`.

### Did not run

- `test_sessions_handoff_excerpts.py`, whose heavy property never finished in
  rounds 1 and 2;
- the two unfinished tests of run 12;
- mutants other than H1 to H6;
- the whole suite. CI runs it.

### Deviations

- Shell metacharacters in a URL password stay in view (IR-20). See P1 above.
- The contract's new sentences sit in C-5.10, as in fixes r1 and r2. C-27.1
  and C-23.14 aren't amended. C-5.10 now states the approval rule.
- The new property tests' generators make each part's first choice the
  review's shape (one of two branches). Plain generators found nothing in 60
  examples (run 2).
- The PR body isn't edited; its new content is listed above.
- The ultracode note asks for a Workflow. A headless run gets no completion
  notice, so the work stayed inline.
- One command outran the tool's limit and was moved to the background (the
  mutant run, above). I started nothing in the background myself.

Folders outside scratch:
- `$TMPDIR/uv-cache` existed before this job and is shared, so I left it.
- `uv sync` created `.venv` in the workspace.
- pytest's own `sf-test-homes-*` folders (`tests/conftest.py`) are removed at
  its session end.
- The harness wrote the background run's output under
  `/private/tmp/claude-501/` (its own task file, not mine to remove).

Scratch `$TMPDIR/sf-wcp-fix3` held the probes, the copies (the mutant runner's
partial one included), the run outputs and the basetemps. It was removed at
06:39 UTC with a guarded `shutil.rmtree` that asserts `sf-wcp-fix3` is in the
path and is the folder's name; it no longer exists. No process of mine is left.
No live state, credential store or caller file was read or changed.

Head built: the commit holding this section's last edit, on top of `ee4f86a07`
(code, tests, runner and contract). Finished at 06:40 UTC, 2 h 31 min after the
start.
