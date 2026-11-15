# Worker failure causes on release/217

Implemented causes at all three sites with #26's cadence. Final targeted
validation: **1,185 passed, 2 skipped; all seven mutants killed**. The build
round left the underscore-scheme URL leak for the hub, and its commits were
blocked by the workspace's shared `index.lock` permissions; the hub then
committed the build as `0b750661a` and this report as `1593a4efd`. Review r1
found more (URL forms, hostile types, re-raises, escapes, chain cost); "Fix r1"
at the end records what changed.

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
