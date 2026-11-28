# Queue tray #219 — fix-3 (2026-10-16)

## Fix-3

Base: `f636d22d23e0`, assigned workspace only. The CI coverage failure was a
generator defect, not a tray truncation defect: the old `long_texts` starts at
240 raw characters, and whitespace folding can reduce that to 215, below the
core's 280-character preview bound. Receipt-only timelines never drew long
message text either.

### Tests first

Added `test_c29_7_generated_long_text_always_needs_a_cut`. It reads
`queuePreviewLimit` from `app/Sources/QueueTray.swift`, requires every long-text
draw to exceed it after whitespace folding, and checks the real Swift preview
against the folded prefix plus `…`. Before the fix, the Python 3.12 locked run
failed at 215 folded characters versus 280 (1 failed, 45 deselected; 173.97 s).
The regression was committed separately as `31781aaf5`.

### Generator fix

Long-text lengths derive from the core bound, not a guessed size. They cover
single-line text, multiline text, and a short first line followed by a long
second line. Sends select short or long text through two explicit strategy
branches. Opening receipts and later receipts also draw these texts, so long
previews do not depend solely on the comparatively rare local-send path.

`REACHED = 5`, `COVERAGE_BATCH = 300`, `CASES`, and every existing assertion
remain unchanged. The coverage test prints its per-case counts. No production
Swift code changed; longer inputs exposed no new truncation defect in either
Python's tray properties. The generator fix is committed as `f4a9b6420`.

### Validation

Every pytest invocation runs alone in the foreground under its own blocking
`lockf -k /private/tmp/claude-501/subfleet-suites.lock` hold, without `-t`, with
`SUBFLEET_RETENTION_DORMANT` unset. Hypothesis uses bounded existing settings and
scratch-local storage; no deep profile is used. The default uv cache is not
writable, so uv uses scratch-local cache storage.

The first complete Python 3.12 tray hold passed all 46 core tests, including
the 200-example properties and coverage, but both notice tests had compiler
setup errors with the default Xcode/SDK. The prior queue-tray report documents
this toolchain problem. Subsequent holds select Command Line Tools and SDK
26.5 using job-local environment variables only, and disable nested Swift
compiler sandboxing through the existing test helper. No global developer
selection, installed application, or live state is changed.

The SDK 26.5 notice compile initially exceeded the helper's 540-second compile
budget (46 passed, 2 setup errors; 697.06 s). Precompiling the unchanged native
notice and review probes in the foreground, outside the test lock, succeeded
with a bounded 1100-second budget. The normal pytest fixtures then reused their
exact-source probe caches. No test helper, assertion, or timeout was edited.
The final successful runs below supersede both earlier notice setup errors.

The selected interpreters were CPython **3.12.14** and **3.14.7+freethreaded**
(the existing interpreter selected by the requested `uv run --python 3.14`).
Final results have **zero failures, errors, or skips**:

| Hold | Python | Passed | pytest seconds |
| --- | --- | ---: | ---: |
| Tray and notice | 3.12 | 48 | 119.80 |
| Tray and notice | 3.14 | 48 | 139.59 |
| Queue engine and steer | 3.12 | 120 | 31.54 |
| Review probes and timeline | 3.12 | 46 | 66.92 |

Every lock hold, including the initial failed attempts, was under 20 minutes.
Only one pytest process ran at a time; no parallel pytest workers were used.
There was no waiting for quieter load while holding the lock. The remaining
two groups were requested only on 3.12, so their 3.14 column is not applicable.

#### Test counts per file

| File under `tests/frontend/` | Python 3.12 passed | Python 3.14 passed |
| --- | ---: | ---: |
| `test_core_queue_tray.py` | 46 | 46 |
| `test_queue_tray_notice.py` | 2 | 2 |
| `test_core_queue_engine.py` | 75 | — |
| `test_core_steer_properties.py` | 2 | — |
| `test_core_steer.py` | 43 | — |
| `test_review_r219_probes.py` | 23 | — |
| `test_core_timeline.py` | 23 | — |
| **Total** | **214** | **48** |

The 200-example tray properties and the steer properties pass with the new
generator. The four offscreen opening-scroll cases are included in the 23
review probes; the two real notice-view checks pass on both Pythons.

#### Per-case coverage

Each count is the number of timelines, out of the unchanged 300-example batch,
reaching that case. Counts are identical on both Pythons; every case exceeds
the unchanged requirement of five. The cut-preview case now occurs in **156**
timelines instead of the CI run's **1**. The smallest case count is **23**.

| Case | Python 3.12 | Python 3.14 |
| --- | ---: | ---: |
| a send went straight to the tray | 139 | 139 |
| a send showed in the timeline | 186 | 186 |
| a send stayed in the tray when its receipt came | 23 | 23 |
| the queue's head in the timeline | 241 | 241 |
| a held tray | 152 | 152 |
| a tray behind a live turn | 132 | 132 |
| an idle tray | 122 | 122 |
| a tray of three or more | 73 | 73 |
| an unblock note in the tray | 75 | 75 |
| a repair message ahead of an earlier message | 65 | 65 |
| sending and queued rows together | 73 | 73 |
| a steer in the timeline | 161 | 161 |
| a missed steer in the timeline | 57 | 57 |
| a steer offer | 37 | 37 |
| a deferred row | 79 | 79 |
| a row with images | 73 | 73 |
| a cut preview | 156 | 156 |
| a withdrawn line | 62 | 62 |
| a tombstone | 34 | 34 |
| a pending card in the timeline | 44 | 44 |
| followed is not the last row | 224 | 224 |
| move stay | 273 | 273 |
| move jump | 300 | 300 |
| move glide | 242 | 242 |
| an own send glided while reading further up | 141 | 141 |
| a change stayed while reading further up | 140 | 140 |
| focused again | 70 | 70 |

### Delivery

The shared Git index is read-only. Commits therefore use workspace-local
`.git-local` metadata on `feat/queue-tray-217`, without rewriting history or
pushing. Only explicitly named source/report files are staged; logs, XML/JSON
evidence, and bundles are not staged.

An incremental transfer bundle, based on `f636d22d23e0`, is retained untracked
at `docs/reports/2026-10-16-queue-tray-fix3.bundle`. It contains the regression,
generator-fix, and report commits. Nothing is pushed. No history is rewritten.
The caller's checkout, live store, daemon, and installed application are
unchanged. Scratch-local uv, Hypothesis, compiler caches, and pytest evidence
are removed using Python `shutil.rmtree`; the generated workspace virtualenv
and native-probe build cache are removed as well. No `.hypothesis` directory or
running process from this job is retained.
