# An excerpt keeps text the whole-text scrub replaces (D-SCRUB-BOUNDARY1)

Build job 20261105-160919-scrub-boundary-build, attempt a1, run 2026-11-07 08:42–11:40 EST on
release/217's head a8624a4899a2. Commits are on the job's detached checkout (the assigned branch
`fix/scrub-boundary-217` is checked out in the caller's checkout, so Subfleet's salvage ref
carries them): `acca833e5` (fix), `9137930b8` (tests), `9436a0695` (contract), then the
docstring and report commits.

## Cause

`test_an_excerpt_never_shows_a_value_the_whole_scrub_removes` replayed from the hub's copy of the
Hypothesis database fails on a8624a489 (1 failed in 16.2 s). The stored example (rebuilt exactly
by `stored_example()` in the tests; checked equal to the captured text) is 262,145 characters of
`x = 1` lines with:

- `data:image/png;base64,` and three `QUJD…` lines at 107,073 (the property's third anchor,
  `total - TAIL - 24_000`);
- `password = "Zq0x100000"` at 131,072 (the head excerpt's edge), and `password = "Zq1x100000"`
  on the next line;
- `strip=False`, `limit=None`.

The step that lets the value through is **the omission marker**: block resolution, before any
excerpt is cut. The other three candidates aren't involved.

- The whole-text scrub (`_scrub_rules` with `multiline`) masks quoted values **before** it
  resolves the blocks.
- The excerpt rules resolve the blocks **first**: release's `_resolve_blocks` over the whole text,
  as the head's own. A data URI's body is `[A-Za-z0-9+/=\s]{32,}`, and every character of an
  `x = 1` line and of `password = ` is a body character. So the body ran on for about 24,000
  characters and stopped at the value's opening quote. The text became
  `x =[BASE64 DATA OMITTED]"Zq0x100000"`, and the value no longer had a key.
- The data URI took about 24,000 characters, so the resolved text was within the budget. That
  gives one piece, with no excerpt cut and no join. Release's scrub and the head's scrub both read
  that resolved text and both kept the keyless value, so `shown_by_both` kept it too.

The same gap was in the excerpt scrub of texts within the budget (`scrub_secrets(text)` without
`whole`), at any length. A 277-character `data:…` followed by `password = "…"` showed the value
there too. It also covered three other shapes:

- a private key or reminder whose match takes a value's key and opening quote and leaves the
  rest;
- a value whose closer sits inside a quoted value: the whole-text scrub masks that value, and its
  block then runs on to the next closer;
- `api_key = "…"`, where the body stops at `_` and leaves the key `_key`, which isn't a key.

## Why the property and the #239 rounds missed it

- **The reference the rounds tested against.** The contract states the excerpt guarantee against
  "the same rules over the whole block" (C-23.14). Those rules resolve the blocks first, just as
  the excerpts do. C-23.14's only stated gap against the whole-text scrub was quoted values that
  span lines ("can remove more than its excerpts would"). The two scrubs' different rule order
  was never listed as a difference, and no #239 round looked at it.
- **The property's generator rarely builds the shape.** It needs all of these at once:
  - the `x = 1` or blank-line filler (2 of 5 fillers; the other three hold `,`, `:` or `(`,
    which end a data URI's body);
  - a data URI shape that ends in body characters at the third anchor;
  - a shape after it whose quoted value follows only body characters (`password = "{s}"`), with
    the third anchor before the head anchor.

  At 80 examples a run, CI found it only after many runs (shard 5 of a notice-retention build's
  suite).
- **No other test had this shape.** `test_blocks_that_shrink_a_text_within_the_budget_leave_it_whole`
  and the reviewed block cases all use a key that the body can't reach or a plain value.

## The fix (`subfleet/sessions/handoff.py`)

`_guarded_blocks` is the excerpt rules' first step now (`_scrub_rules` without `multiline`). It
calls `_split_masks`, which works in this order:

1. It resolves the blocks **exactly as release does**. `_block_layer` repeats `_linear_sub` and
   `re.sub`, layer by layer (reminders when stripped, then private keys, then data URIs), and
   records each match.
2. At each block's end it finds the quoted values the block splits. It reads them with the
   head's own quoted-value rule (`_scrub_named_values(..., quoted=True, spans=…)`, within a line,
   as the excerpt rules read values), in a short window:
   - **Data URI** (`_data_uri_splits`): the key a split value's rule would read is the run of
     letters and digits before the body's trailing whitespace or `=`, plus any identifier
     characters right after the body (`api` + `_key`). If that word ends in a credential word,
     the window runs from the key's line to the end of the body's last line. The rest of each
     value that crosses the body's end is masked.
   - **Private key or reminder** (`_closer_splits`): there is nothing to do unless the closer's
     line holds a quote inside the block (a C scan). Otherwise, each value that opens inside the
     block and holds the closer is masked from the closer to its end. The block then runs on to
     the next closer, because the whole-text scrub's block does so once its first pass has masked
     the value. The check repeats at each closer it reaches.
3. It carries each layer's masks into the next layer's text (`_carried`; a mask that meets a
   later block takes the whole placeholder), merges them, and replaces each mask with
   `[REDACTED]`, counted once (`_masked`).

**The bounded path.** `_released_pieces` still writes release's pieces, made by release's own
pipeline. The head's side of each piece is the head's scrub of the same resolved text with the
masks that fall in it: the one piece, or each excerpt sliced at the same cuts. Release's run that
holds the value (`OMITTED]"Zq0x100000"`) isn't among the head's runs, so it becomes `[REDACTED]`.
The stored example now reads `x =[BASE64 DATA [REDACTED]` and then `password = "[REDACTED]"`.
If the head's resolved text ever differed from release's, the head's side would hold nothing and
every run would go (fail closed). A differential test shows they agree.

`_head_scrub_bounded`, the head's own bounded mode, uses the same step.

**Work bound.** Block resolution stays linear: per block, a constant number of C scans in Python,
as `_linear_sub` already does. *(Corrected in fix round 1: the split step was not linear on 13fd1c8bc; see "Corrected claims".)* The quoted-value rule reads only windows:

- at most 16,384 characters each (`_SPLIT_WINDOW_MAX`);
- at most 131,072 in all per text (`_SPLIT_READ_MAX`, one excerpt's worth); *(corrected in fix round 1: per `_split_masks` call; `scrub_bounded` makes up to three)*
- past either limit it fails closed: the rest of the line goes, and a private key or reminder
  runs on to its next closer.

So no rule but the block rules runs over more than 262,144 characters at once (C-23.14, C-25.5).

**Files changed:**

- `subfleet/sessions/handoff.py`
- `tests/unit/test_sessions_handoff_excerpts.py`
- `docs/acceptance-contract.md` (change-log entry D-SCRUB-BOUNDARY1, and one sentence in C-23.14)
- `docs/reports/2026-11-05-scrub-boundary-data/timing.py`
- this report

## Invariants and their tests

All tests are in `tests/unit/test_sessions_handoff_excerpts.py`.

1. **No value the whole-text scrub removes shows in `scrub_bounded`, for every limit, `None`
   included.** `assert_keeps_only_what_the_whole_scrub_keeps` checks this for limits `None`, 0,
   40 and 2,048, with reminders stripped and kept, on fixed rows 1–4 and the excerpt-edge rows.
   `test_no_block_leaves_the_rest_of_a_value_it_splits` checks the excerpt scrub and
   `scrub_bounded` on nine split shapes, in a short text and at the tail's start of a
   300,000-character text. The property test is unchanged. Scope: values read within a line (see
   the open questions).
2. **Within the budget, `scrub_bounded(text, limit)` is exactly `truncate(scrub_secrets(text),
   limit)`**, and the result for `None` is `scrub_secrets(text)`. Rows at budget − 1 and budget
   check this. The existing `test_within_the_budget_*` properties still pass. The excerpt scrub
   itself now hides the split values, so the identity holds and invariant 1 holds within the
   budget too.
3. **The work stays bounded.** See the timings below. The window caps bound the rule's reads.
4. **The count is the larger of the two scrubs'.** For every over-budget row:
   `count == max(release's, head's) >= scrub_release.scrub_bounded(text, None)[1]`. Where the
   resolved text is one piece, the head's count equals `_head_scrub_bounded`'s. On the stored
   example, release's count is 2, the head's is 3 and the result is 3, which matches the
   whole-text scrub's 3.

Also: `test_the_head_resolves_the_blocks_as_release_does` checks that `_split_masks` gives the
same text and count as release's `_resolve_blocks` (stored example; a reminder followed by long
prose; both reminder modes). `test_a_quoted_data_uri_keeps_the_rest_of_its_line` checks that a
data URI closing a quoted string splits nothing: `img = "[BASE64 DATA OMITTED]", alt = "logo"`
is unchanged.

## Fixed rows

- **Row 1**: the stored example, rebuilt (`test_the_stored_example_keeps_only_what_the_whole_scrub_keeps`).
- **Rows 2–4** at budget − 1, budget, budget + 1 and budget + 2
  (`test_a_value_at_an_omission_marker_keeps_only_what_the_whole_scrub_keeps`, 12 cases):
  - `after-the-marker`: the data URI, then `password = "Zq0x200000"` at the head's edge;
  - `before-the-marker`: the value directly before the data URI;
  - `at-each-edge`: two data URIs, each 24,000 characters before a value at the head's edge or at
    the tail's start.
- **Extra deliberate rows, not a search** (`test_an_excerpt_edge_keeps_no_value_a_data_uri_split`,
  6 cases): rows 2–4 all end in the one-piece path, because a data URI shrinks the text by more
  than 2 characters. These rows put the split at the head's end and the tail's start of a
  300,000-character text whose resolved text stays over the budget, with 0, 6 or 600 filler
  characters between the marker and the value.

All 33 new tests passed on `9137930b8` (`-n 4`, 535.0 s at load about 110).

## Mutants

Each mutant undoes one part of the fix. The tests were run with `-x`; log in
`docs/reports/2026-11-05-scrub-boundary-data/mutants.log`.

| Mutant | Undoes | Tests that kill it |
|---|---|---|
| M1 | the head's side of the bounded path reads no masks | row 1 (stored example); row 2 at budget + 1; the excerpt-edge row at the head's end |
| M2 | the excerpt scrub resolves blocks unguarded | row 2 at budget − 1; the data URI split shape. Survives row 1, as expected: past the budget the bounded path's own masks still hide it, so both parts are needed |
| M3 | no run-on to the next closer | the private-key and reminder "closer in a value" shapes. Survives "holds key and quote", as expected: that shape needs only the remainder mask |
| M4 | a key split inside its identifier (`api` `_key`) is not read | the data URI "inside the key" shape. Survives the "before a colon" shape, as expected: there the whole key is in the body |

## Timings

Best process CPU of 5 runs, from `docs/reports/2026-11-05-scrub-boundary-data/timing.py`.
The text is the fixed rows' shape at the budget and at 10 times it. The host load average was
about 100 for the base run and 93–111 for the change.

| | budget, limit None | budget, limit 2,048 | 10× budget, limit None | 10× budget, limit 2,048 | 10× ÷ budget |
|---|---|---|---|---|---|
| base a8624a489 | 0.2271 s | 0.2294 s | 0.2529 s | 0.2471 s | 1.11 / 1.08 |
| change acca833e5 | 0.2907 s | 0.2873 s | 0.3015 s | 0.2788 s | 1.04 / 0.97 |

The bounded path's cost at 10 times the budget stays within a factor of about 1 of its cost at
the budget, before and after the change. In absolute terms the change costs about 0.06 s more on
this text. That covers a second, recorded resolution of the blocks (C scans) and the one window
the value's data URI needs. Part of the difference may be the load.

## Commands and results

All runs used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `unset
PYTHONPATH`, and `--basetemp` under the job's temp folder.

- `uv sync --group dev --extra api` failed (`Extra api is not defined`). `uv sync --group dev`
  succeeded.
- The replay of the stored example on a8624a489 failed: 1 failed in 16.22 s, `AssertionError:
  Zq0x100000`.
- With the fix, the same replay ran past the 10-minute background limit and was stopped. Once the
  stored example passes, Hypothesis goes on to generate its 80 examples of 262,145–382,144
  characters, at load about 110. Row 1 is byte-identical to the stored example and passes.
- `pytest -n 4 -k "<the new tests>"` on `9137930b8`: 33 passed in 535.01 s.
- **Final set: not completed.** The brief's set is the excerpts file, the other 18 `tests/unit/test_sessions_*` files, the 14 `tests/unit/test_scrub*` files and the three contract guards, run with `-n 4` (log: the job temp folder's `final.log`). It started at 11:22 at load 106–115. The shell's background limit stopped it at 11:42, 67% of the way through, with 1,118 tests passed and no failure, error or skip. It couldn't finish inside the brief's 3-hour limit, so the last third is unrun. The hub's CI run of the whole suite is the check.

## Deviations from the brief

- **Sync.** No `api` extra exists in `pyproject.toml`, so I synced with `uv sync --group dev`.
- **Commits.** They are on the job's detached checkout, not on `fix/scrub-boundary-217`. That
  branch is checked out in the caller's checkout, so it can't be checked out here; the salvage ref
  carries the commits.
- **Contract.** Besides the change-log entry, I added one sentence to C-23.14. Its account of the
  bounded path's block resolution ("exactly as in a scrub of the whole block") no longer covers
  what the excerpt rules do. C-23.36's text needed no change.
- **Rows beyond the brief.** Rows 2–4 at budget − 1 … budget + 2 never reach the excerpt cut,
  since the data URI shrinks the text below the budget. I added six deliberate rows that do reach
  it, plus nine short split shapes, a non-regression example and a differential test. None of
  them is a search.
- **Within the budget.** Invariant 1 "for every text" and the within-budget identity together
  meant the excerpt scrub of shorter texts had to change too. It now hides the split values,
  which is a behaviour change for `scrub_secrets(text)` without `whole` and for `clean`. The
  change only replaces text; it never reveals any.
- **Property replay.** The full replay with the fix couldn't finish within the shell's limit at
  this load (see the commands above).

## Open questions

1. **Quoted values that span lines are still the contract's stated difference.** The excerpt
   rules read a value within a line, while the whole-text scrub pairs quotes across lines. So
   `password = "abc`, `SECRET` and `"` on three lines shows `SECRET` in the excerpt scrub and
   `scrub_bounded`, at any length, and not in the whole-text scrub. Invariant 1 doesn't hold for
   that shape. The property's shapes leave it out, and C-23.14 states it. Closing it means
   pairing quotes across the whole text, or cutting only outside quoted values as `cut_whole`
   does. That's a design change for the hub to decide.
2. **A gap in every mode, the whole-text scrub included.** A data URI's body also takes a key
   whose value is plain or follows a `:`. `data:…` followed by `password: SECRET`, or by
   `Authorization: SECRET`, shows `SECRET` in every mode, because the whole-text scrub's data URI
   takes the key too. It's outside this invariant, which is relative to the whole-text scrub, but
   it is a real leak shape. It needs a follow-up: for example, a body that stops before a line
   holding a credential key and a separator.
3. **Approximations in the emulation.** The emulation of the whole-text scrub's first pass is
   within a line and is read layer by layer. A cascade isn't followed: a reminder that runs on
   (because a value holds its closer) and so would also remove a private key's closer. No test
   covers such a cascade.
4. **This report is a new artifact.** It still needs the independent review the hub runs before
   merge.

## Fix round 1 (review r1)

Job 20261107-205221-sb-fix-r1, run 2026-11-13 18:29–21:29 UTC on 13fd1c8bc. Commits: `7fb040764` (fix),
`244bd73d5` (tests, timing script, a line-start fix), then this report and the contract. Host load ran
100–377 throughout. One test run stalled the host's processes for 57 minutes (20:19–21:17), which
cost most of the verification time. See "What didn't run".

### P1: linear split work

**Cause** (as review r1 found): `_line_start`'s `rfind("\r", 0, at)` read back to offset 0 for every
block. `_data_uri_splits` found that line start before checking the read budget. `_run_start`'s last
step copied `text[:end]` once a run passed 16,320 characters, which a key word or a run of spaces and
`=` past that length reaches (`datauri-longword-many`).

**Fix** (`subfleet/sessions/handoff.py`). The split code no longer calls `_run_start`, `_line_start`
or `_last_visible`. `_run_start` and `_last_visible` keep their behaviour for the other rules.
`_line_start` and `_line_end` were split-only, so they are gone.
- `_run_back` reads a run back to a floor, at most `_SPLIT_WINDOW_MAX` characters.
- `_Lines.start` reads back at most as far as the read budget allows; where a line starts further
  back, the window would be too long, so it fails closed. `_Lines.end` stores each line end it finds
  with the earliest position known to be on its line, so no character is read twice looking for ends.
- `_Ahead` reuses the last closer search wherever it answers.
- A run-on stops at a closer an earlier block's run-on already read from. So each closer is read once
  as a run-on step, and once as its own block's closer.
- `_data_uri_splits` checks `reader.room()` before it finds a window. Two cases now fail closed:
  - a run of spaces and `=` longer than a window, even with no key before it (13fd1c8bc kept the line
    when no key preceded the run);
  - a key word longer than a window that ends in a credential word (13fd1c8bc failed closed here too,
    because its window was too long).
- `_closer_value_end` keeps 13fd1c8bc's checks: the quote on the closer's line, two lines above for a
  key, and the window clamped at the block's inner. One look-back is not bounded by a constant. Where
  the block's part of the closer's line is longer than a window, and the last window's worth of it
  holds no quote, `_quote_on_line` reads back to the nearest quote or line break, within the block.
  So that read is bounded by the block's length, not its offset. Failing closed there instead would
  run a reminder on to its next closer over a long unquoted line, hiding far more text.

**Timings.** `docs/reports/2026-11-05-scrub-boundary-data/timing_shapes.py`, process CPU of
`scrub_bounded(text, None)`, best of 3 (13fd1c8bc at 10×: one run, as review r1 timed it). Base is a
`git archive` of a8624a489. The loads shown are the 1-minute averages before and after each time.

| Shape | base: budget / 10× | 13fd1c8bc: budget / 10× | change: budget / 10× | change ÷ base | load (base; head; change) |
|---|---|---|---|---|---|
| `fixed` | 0.401 / 0.448 (1.12) | 0.399 / 0.452 (1.14) | 0.412 / 0.465 (1.13) | 1.03 / 1.04 | 105–108; 118–129; 133–143 |
| `pem-many` | 0.161 / 0.485 (3.01) | 0.317 / 15.74 (49.7) | 0.169 / 0.737 (4.36) | 1.05 / 1.52 | 108–118; 129–171; 125–135 |
| `reminder-many` (stripped) | 0.071 / 0.442 (6.2) | 0.276 / 20.08 (72.7) | 0.083 / 0.644 (7.78) | 1.16 / 1.46 | 118–126; 171–190; 127–135 |
| `datauri-key-many` | 0.154 / 0.436 (2.83) | 0.332 / 13.43 (40.4) | 0.198 / 0.712 (3.59) | 1.29 / 1.63 | 125–130; 162–231; 116–127 |
| `pem-sparse` | 0.382 / 0.402 (1.05) | 0.376 / 0.654 (1.74) | 0.371 / 0.417 (1.12) | 0.97 / 1.04 | 125–129; 224–231; 109–116 |
| `datauri-longword-many` | 0.0081 / 0.0469 (5.79) | 0.0106 / 0.154 (14.5) | 0.0093 / 0.0969 (10.4) | 1.15 / **2.07** | 127; 220–224; 109 |

Raw lines: `.fix1/timing-{base,head,change}.jsonl` in the job's checkout (not committed).

Every pair is within 2× of base but one: `datauri-longword-many` at 10× (2.07×, 0.05 s more in
absolute terms). Base has no split step. On this shape the change reads each 20,000-character key
word back one window (16,384) through `_run_back`'s stepped scans before it fails closed. That is
linear (ratio 10.4 for 10× the text) but costs about a window of `rstrip` per block. The growth test
is the gate, not this time.

### P2: a masked closer ends no block

**The rule.** `_split_masks` passes the earlier layers' masks (in the coordinates of the text the
private-key layer reads) to `_closer_splits` (`_Covers`).
- A closer that a mask covers, even in part, no longer ends a block. The block runs on to the next
  closer outside every mask, the run-on is masked, and the split check runs there.
- A mask from an earlier split of the same block counts too: the next closer is searched from the
  value's end, as before.
- If no closer stands past the masks, nothing more is masked. In that case the whole-text scrub's
  block never closes, so it shows more than release, which ends the block at the covered closer.
- The opener side is unchanged.
- Release's resolved text is unchanged (`mine != resolved` still passes); only the masks grow.

**Rows it fixes.** Review r1's row (b) with reminders stripped: `Zq3x200005` no longer shows, within
the budget or at either excerpt edge. 13fd1c8bc showed it in all three placements (checked on its
archive). Row (b) without a second END and rows (a), (c) and (d) leaked on neither side, and still
don't.

### P3 gaps, as fixed

- **R1:** review r1's two probe rows are in `SPLITS` (`private-key-key-a-line-above`,
  `reminder-key-a-line-above`), so `test_no_block_leaves_the_rest_of_a_value_it_splits` checks them,
  short and long, in both modes.
- **R2:** `test_the_split_windows_stop_at_the_read_cap` uses eight windows of 16,380, then a ninth of
  32 (read: `KEEPC` kept) or 33 (not read: `KEEPC` goes). It found a bug in my first version: the
  ninth of 32 wasn't read, because `_Lines.start` missed a line break at `low - 1` when the read room
  was exactly used. Fixed in `244bd73d5`.
- **Differential:** `test_the_head_resolves_drawn_blocks_as_release_does` is a Hypothesis property
  (300 examples, derandomized) over drawn parts. It draws private keys (with a type and with `BLOCK`),
  reminder tags in any case, openers without closers, values that hold a closer, data URIs, and every
  layer inside or across another. `test_the_head_resolves_each_cascade_as_release_does` holds the
  cascade rows as fixed cases.
- **Cascade rows:** `CASCADES` holds (b) as review r1 gives it. I rebuilt (a), (c) and (d) and the
  variants from review r1's descriptions, because its texts aren't in the review.
  `test_a_closer_an_earlier_layer_masks_ends_no_block` runs each one:
  - within the budget, at budget + 4,096 ending at the head excerpt's edge, and on the tail's first
    line;
  - through `assert_keeps_only_what_the_whole_scrub_keeps` (both modes, limits None/0/40/2,048).
- **Report:** lines 100 and 104 are marked corrected below.

### Corrected claims

- **Line 100.** Block resolution itself is linear, but the split step on 13fd1c8bc was not. The split
  step now costs a constant per block (look-backs within `_SPLIT_WINDOW_MAX`, windows within the read
  budget) plus linear scans. Those scans are line ends and closer searches, each read once, and one
  look-back bounded by the block's own length.
- **Line 104.** The windows read at most 131,072 characters **per `_split_masks` call**. Over the
  budget, `scrub_bounded` makes up to three calls: the whole text, then each excerpt's `_scrub`.

### Invariants and their tests

1. **No masked run shows.** `assert_keeps_only_what_the_whole_scrub_keeps` checks it on the cascade
   rows, the build's rows and the stored example; the split tests check the split shapes. The
   property test is unchanged.
2. **Within the budget, `scrub_bounded` is exactly `truncate(scrub_secrets(text), limit)`.** The same
   helper checks this on every short cascade row. The structure is unchanged: both sides run
   `_guarded_blocks`.
3. **Superset.** `.fix1/rows.py` dumped `_split_masks` for 73 fixed rows × 2 modes on 13fd1c8bc and on
   the change. The rows are the stored example, rows 2–4 at four lengths, the six edge rows, 11 split
   shapes short and long, 8 cascades × 3 placements, the two read-cap rows, and the six timing shapes
   at the budget. Within the budget it also dumped the `[REDACTED]` runs of `scrub_bounded(text, None)`.
   Result: the change's masked characters ⊇ 13fd1c8bc's on all 146 pairs. 143 were identical; 3 grew
   (row (b) stripped, +37 characters in each placement); none lost. No `[REDACTED]` run was lost.
4. **Linear work.** `test_the_split_step_s_work_is_linear_in_the_blocks` runs four shapes in a child
   process, at 2× and 8× the budget. It uses `fewest_instructions` under `COUNT_GROWTH_MAX` where
   `counts_instructions()` holds, and `best_cpu_ratio` under `GROWTH_RATIO_MAX` elsewhere. **Not
   shown to fail on 13fd1c8bc** (see below); the timing table shows 13fd1c8bc's 40–73× growth on the
   three shapes it covers.
5. **The count** is unchanged in form: `max(release's, head's)`, checked by
   `assert_keeps_only_what_the_whole_scrub_keeps` on the over-budget cascade rows.
6. **Same resolution:** the two differential tests above.

### Mutants

`.fix1/mutants.py` (in the job's checkout) applied each mutant alone, ran its killer as a direct call of
the test function in a child, restored `handoff.py` from the pristine copy, and checked the bytes
(sha256 equal each time; `shasum` after: `f4d31bd7…` for both).

| Mutant | Change | Result |
|---|---|---|
| 2 | `_Covers([])`: a covered closer ends the block again | killed by `test_row_b_hides_what_the_whole_scrub_hides` |
| 3 | R1: `range(2)` → `range(0)` in `_closer_value_end` | killed by `test_no_block_leaves_the_rest_of_a_value_it_splits[private-key-key-a-line-above]` |
| 4 | R2: `self.left = 10 ** 12` (no total cap) | killed by `test_the_split_windows_stop_at_the_read_cap[ninth-of-33-not-read]` |
| 1 | `_Lines.start` reading back to offset 0 | **not run** (the host stall); expected killer: the growth test |
| 5 | R3: `_carried`'s end maps to the match's start | **not run**; no expected killer named, and per the brief I didn't search for one |

### Commands and results

Every run used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and `unset PYTHONPATH`.
- **Sync.** `uv sync --group dev --extra api` fails (`Extra api is not defined`); `uv sync --group dev`
  succeeded.
- **The shared cache was broken.** The shared `$TMPDIR/uv-cache` overlay for
  `--with 'pytest-xdist>=3.6'` had no `xdist`. A private cache, `$TMPDIR/sf-sb-fix1/uv-cache`, built
  it, which took 20 minutes at that load.
- **Probes.** Row (b) and the R1 rows with `.fix1/probe.py`: no leak on the change. 13fd1c8bc
  leaks `Zq3x200005` with reminders stripped, short and at the head edge.
- **xdist, first run.** `pytest -n 4 -k "<new tests>"`: stopped by its 580 s alarm after 3 passes.
- **Serial run.** `pytest -k "read_cap or row_b or each_cascade or drawn_blocks"`: 19 passed and
  1 failed (the read-cap case of 32: the line-start bug, then fixed), in 61 s.
- **After the fix.** `pytest -k "read_cap or row_b or splits or quoted_data_uri or resolves_the_blocks"`:
  16 passed, then the 560 s alarm stopped it (5% CPU share).
- **xdist, last run.** `pytest -n 4 -k "<all new and changed tests>"` (20:19): the host stalled it for
  57 minutes, and it ended with no summary line, only xdist's teardown `OSError`. Result unknown.
- **Final runs, load about 18–25:**
  - `pytest -n 4 tests/unit/test_sessions_handoff_excerpts.py::test_the_split_step_s_work_is_linear_in_the_blocks tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_cpu_scaling.py`: **74 passed in 42.1 s** (the growth test passes on all four shapes).
  - `pytest -n 4 -k "earlier_layer or row_b or read_cap or drawn_blocks or each_cascade or splits or quoted_data_uri or resolves_the_blocks"` on the excerpts file: **72 passed in 34.3 s**. That covers every new and changed test.
  - The rest of the excerpts file, without the 80-example property, hit its 200 s alarm with no summary line. Result unknown.
- **Superset and timings:** above.

### What didn't run, and deviations

- **Not run:**
  - the excerpts file's other tests (one run hit its alarm) and its 80-example property, every `tests/unit/test_scrub*.py`, the other `tests/unit/test_sessions_*`
    files;
  - the growth test on 13fd1c8bc, and mutants 1 and 5.

  CI's run is the check for these.
- **Rows (a), (c), (d):** rebuilt from review r1's descriptions, as above.
- **Timings:** 13fd1c8bc at 10× was timed once per shape, not best of 3.
- **Behaviour changes beyond the brief, all toward hiding more:** *(Corrected in "Reminder-erased openers (issue #325)": not all of them; the `seen` stop hides less on one input. See "Corrected claims" there.)* the long spaces-and-`=` run fails
  closed, and a closer whose block-part of its line passes a window fails closed when a quote stands
  in its last window. A later block's run-on stops at a closer an earlier run-on read from. Within one
  block, a run-on step's result can differ from 13fd1c8bc's only where that block's opener sits within
  the window above the closer. The superset check shows no loss on the fixed rows.
- **Scratch:** `$TMPDIR/sf-sb-fix1` held the archives, the private uv cache and the basetemps. I removed it at the end with a guarded `shutil.rmtree`. The probe, rows, mutant and timing outputs stay in the job checkout's untracked `.fix1/`.

Built head: see the final answer.

## Reminder-erased openers (issue #325)

Build job 20261115-094656-scrub-op-build-r1, run 2026-11-15 15:35–19:20 UTC on a4aded885 (release/217
with #300 merged). Host load ran 30–195 on 18 cores; this job's processes got 2–9% of a CPU each. The
host stalled from about 17:40 to 19:15 UTC during the last test run (see the deviations). Commits
are on the job's detached checkout: `fb4344951` (fix), `45f638981` (tests), `254e2a37b` (contract), then
this report.

### Cause

With reminders stripped, the whole-text scrub masks quoted values (`_scrub_named_values(...,
multiline=True)`) before it removes reminders. So it keeps a reminder in two cases:

- **The reminder never closes.** A value holds its last closer and no closer follows the value (row A).
  `_linear_sub` finds no closer and stops, so that reminder and every later one stay in the text.
- **A value holds the reminder's opener** (row B). The whole-text rule never sees that opener.

The private-key layer then reads the kept text and masks a key that opens inside the reminder. The
excerpt rules resolve the reminders as release does, which erases the key's opener, so the private-key
layer finds no block there. The part of the key past the reminder showed.

On a4aded885, `_closer_splits` handled row A's closer and stopped (:2675): it masked the rest of the
value, the closing `"`, and nothing more. Nothing handled an opener held by a value.

### The rule

`_split_masks` now ends with a step for reminders (`_erased_keys`, run only with reminders stripped).

1. **Which reminders the whole-text scrub keeps.** `_closer_splits` now reports, for each reminder, where
   the whole-text block ends, or None where it never closes (its new `ends` argument; masks unchanged).
   - From the first reminder that never closes onwards, every reminder is kept.
   - A reminder that holds a private key opener is checked once: does a quoted value, read within a
     line as `_closer_value_end` reads one, hold its opener? (`_closer_value_end` gains `point`, the
     position a value must hold, and `scan`; its defaults behave exactly as before.)
   - Every other reminder counts as removed, with its run-on (`gone`).
2. **The keys inside.** For each private key opener in a kept reminder, the step walks the closers after
   it, as `_closer_splits` walks a key's:
   - a closer inside a removed reminder ends nothing (`_Covers(gone)`);
   - a closer that a value opening inside the key holds ends nothing either;
   - the first other closer ends the key.

   The mask runs from the key's opener to that closer's end. `_carried` maps the opener, inside the
   reminder, to the reminder's place in release's text, and then through the private-key and data URI
   layers.
3. **What it never does.**
   - It never changes release's resolved text: `mine != resolved` in `_released_pieces` still passes.
   - It reads its windows last, from what the other layers left of the read budget, so their masks are
     exactly a4aded885's.
   - Its masks are not passed to the private-key layer as `earlier` masks. In the whole-text scrub the
     closer that ends such a key ends it, so release's block there must not run on.
   - It never masks to the end of the text. Each mask ends at a closer. Where no closer follows a key,
     the whole-text scrub resolves no more keys, and neither does the step.

**Where it hides more than the whole-text scrub** (allowed by invariant 3):

- A reminder with no key opener inside it is not checked. It counts as removed, so its closers end no
  key.
- So does a reminder whose held opener has another opener after it, where the whole-text block may
  start.
- A key whose opener a value holds is resolved anyway.
- A key whose walk ends without a closing closer is masked to the last closer it reached.
- A check the window or read budget can't make treats the opener or closer as held.

**Data URIs need no mask.** A data URI's body (`[A-Za-z0-9+/=\s]`) stops at a reminder's closer (`<`).
It also stops at the quote of the value that holds the closer, or at that value's `[REDACTED]`. So the
whole-text match of a data URI that opens inside a reminder ends inside the reminder, in text release
removes. `test_a_data_uri_a_reminder_s_removal_erased_keeps_only_what_the_whole_scrub_keeps` checks
both cases. The private-key layer can't erase a data URI opener that matters either: inside a key, the
body stops at the closer's `-` or at a value's quote.

`_closer_splits`'s docstring now states the never-closing case and the `ends` it reports.

### Rows A and B, before and after

Row A is review r2's. Row B is rebuilt from its description ("the reminder opener inside a quoted
password value", 115 characters):

```text
password = "<system-reminder>"
-----BEGIN PRIVATE KEY-----
</system-reminder>
Zq9x100002
-----END PRIVATE KEY-----
```

Row C, beyond the brief, adds a later reminder whose body holds the key's first closer. The whole-text
scrub removes that reminder, so the key runs on to the next closer.

With reminders stripped, the whole-text scrub hides each row's value. Each value is 10 characters at
input offset 78 alone, 131,034 at the head's edge and 135,246 on the tail's first line. On a4aded885
(probe and the new tests run on a `git archive` of it):

| Row | Alone: limit None / 2,048 | Head edge, limit None | Tail's first line, limit None |
|---|---|---|---|
| A | shown at output 11 / 11 | shown at 130,964 | shown at 131,122 |
| B | shown at output 22 / 22 | shown at 130,978 | shown at 131,134 |
| C | shown (the test fails at limit None) | shown | shown |

- At the two edge placements, limit 2,048 doesn't keep the value on a4aded885, and limits 0 and 40
  never do.
- With reminders kept, a4aded885 already masks all three. The helper checks that mode first, and it
  passed on a4aded885 before the stripped mode failed.

**After the change:** no value the whole-text scrub hides shows, at any placement, limit or reminder
mode. *(Corrected in "Reminder-erased openers, fix round 1 (review r1)": review r1 of #331 found
rows in this class that still showed their value on this head; see "Corrected claims" there.)*

The masks, in release's resolved text, with reminders stripped:

| Row | Alone | Head edge | Tail's first line |
|---|---|---|---|
| A | `[0,38)` | `[130956,130994)` | `[135168,135206)` |
| B | `[12,49)` | `[130968,131005)` | `[135180,135217)` |
| C | `[12,61)` | `[130883,130932)` | `[135180,135229)` |

A variant of B with a closer inside the reminder (`password = "<system-reminder>"`, the key, a value,
`-----END`, `</system-reminder>`, then a later `-----END`) masks nothing new. The whole-text key closes
inside the reminder, release removes it whole, and the text after the reminder still shows, as in the
whole-text scrub.

### Invariants and their tests

All tests are in `tests/unit/test_sessions_handoff_excerpts.py`.

1. **No masked run shows.** `test_a_key_a_reminder_s_removal_erased_keeps_only_what_the_whole_scrub_keeps`
   runs rows A, B and C at each placement (9 cases) through `assert_keeps_only_what_the_whole_scrub_keeps`:
   limits None, 0, 40 and 2,048, reminders stripped and kept. The helper checks the rows' made-up
   `Zq…` values, not every run the whole-text scrub replaces.
   `test_a_data_uri_a_reminder_s_removal_erased_keeps_only_what_the_whole_scrub_keeps` (2 cases) does
   the same for data URIs.
2. **Within the budget, `scrub_bounded(text, limit)` is exactly `truncate(scrub_secrets(text), limit)`.**
   The same helper asserts it on every row within the budget: the three rows alone and the data URI
   rows. Both sides still run `_guarded_blocks`.
3. **Superset.**
   - `test_the_erased_key_step_only_adds_masks` (22 cases: rows A–C, the 8 cascades and the 11 split
     shapes, both modes) checks two things: `_split_masks` resolves the same text with and without the
     step (`_erased_keys` stubbed to `[]`, which is a4aded885's code path), and masks every character
     masked without it.
   - A one-off comparison against a `git archive` of a4aded885 covered 88 fixed rows × 2 modes, dumping
     `_split_masks`. The rows are the stored example, rows 2–4 at four lengths, the six edge rows, the
     11 split shapes short and long, the cascades at three placements, the two read-cap rows, the six
     growth shapes at the budget, rows A–C at three placements, the two data URI rows, and the key and
     reminder blocks at both edges.
   - Result: all 176 pairs resolve to the same length. 165 have identical masks and 11 grew: the
     stripped mode of rows A–C (9) and of the two new growth shapes. None lost a character.
4. **Linear work.** The new step reads each look-back within one window (`scan=False`, so it never
   makes `_quote_on_line`'s block-length read) and each window within the read budget. It scans each
   reminder once for a key opener and each closer at most once, and its walk only moves forward.
   `test_the_split_step_s_work_is_linear_in_the_blocks` gains two shapes: `reminder-open-key-many`, one
   chain of run-ons that never closes, and `reminder-held-key-many`, an opener check per reminder. All
   six shapes passed. I didn't record the ratios: the test prints nothing on a pass, and there was no
   time to rerun it.
5. **Same resolution.** `test_the_head_resolves_drawn_blocks_as_release_does` now also draws
   `ERASED_PARTS`. Each is a private key or data URI opener inside a reminder, with the reminder's
   closer in a quoted value (with or without a later reminder closer) or its opener in one, followed by
   a `Zq` line. The test compares `_split_masks`'s text and count with release's `_resolve_blocks`
   (300 examples, derandomized), and it passes. The superset test also asserts the same resolved text.

### Mutant

Each mutant changed one line of a copy of `handoff.py` in the job's scratch folder; the worktree's file
was never touched. Its killers ran against that copy. A probe test placed in one copy (m3) confirmed
that its `subfleet` was the one imported; the others ran the same way.

| Mutant | Change | Killed by |
|---|---|---|
| m1, a4aded885's handling | `_erased_keys` returns `[]` | `test_a_key_a_reminder_s_removal_erased_keeps_only_what_the_whole_scrub_keeps[a-closer-in-a-value-alone]`, and the B and C rows |
| m2 | a held opener isn't recognized | the B and C rows |
| m3 | `_Covers([])`: a removed reminder's closer ends the key | `[c-a-later-reminder-takes-a-closer-alone]` |
| m4 | a reminder that never closes is reported closed | `[a-closer-in-a-value-alone]` |

### Corrected claims

- **Line 425 of this report** (fix round 1's "all toward hiding more") is marked corrected. Not every
  change in fix round 1 hides more. The `seen` stop ends a run-on at a closer an earlier block's run-on
  read from. On review r2's 131,144-character input, that leaves 38 characters (resolved offsets
  `[267,305)`) that 13fd1c8bc masked. The whole-text scrub keeps that value too, so the change removed
  over-redaction; it shows nothing the whole-text scrub hides. These figures are review r2's; I didn't
  rerun them. This round's step only adds masks (invariant 3).
- **C-23.14 and its fix round 1 change-log entry** said every look-back stays within one window. One
  doesn't: where the block's part of a closer's line is longer than a window and the window before the
  closer holds no quote, `_quote_on_line` reads back to the nearest quote or line break within the
  block. Review r2 measured 23,629 characters for a quoted password with 40,000 spaces before a private
  key closer, against a 16,384-character window. The block's length bounds that read, as the code's
  docstring says.
  - C-23.14 now says so.
  - A new change-log entry states the new rule and the correction.
  - The fix round 1 entry keeps its words, with a correction marker.
  - The new step never makes that read.

### Commands and results

Every run used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `unset
PYTHONPATH`, a new `--basetemp` under `$TMPDIR/sf-scrub-op1`, and `uv run --no-sync --with
'pytest-xdist>=3.6' python -m pytest -n 4` unless noted.

- **Sync.** `uv sync --group dev --extra api` failed (`Extra api is not defined`). `uv sync --group dev`
  succeeded.
- **Probe on a4aded885** (`probe.py`, rows A and B, three placements): the leaks in the table above.
  With reminders kept, nothing showed.
- **Probe on the change** (`probe_short.py`, six short rows): rows A, B and the later-closer variant hide
  what the whole-text scrub hides; the B-with-a-closer-inside variant masks nothing new; the data URI
  rows show nothing the whole-text scrub hides; the resolution is the same on every row.
- **New and changed tests, short**
  (`-k "(erased or row_a_is or drawn_blocks) and not head-edge and not tail-first-line"`): 29 passed in
  140.7 s.
- **New tests, long placements** (`-k "erased and (head-edge or tail-first-line)"`): 6 passed in 511.7 s.
- **The same row tests on a4aded885** (a `git archive` with the new test file, `PYTHONPATH` pointing at
  it): 9 failed and 3 passed in 425.7 s.
  - Rows A–C failed at every placement, each at reminders stripped and limit None: `(115, True, None)`
    twice, `(200, True, None)`, and `(266240, True, None)` six times.
  - `test_row_a_is_the_review_s` and the two data URI tests passed, as expected.
- **Mutants** (serial, the alone placements): m1 3 failed; m2 2 failed, 1 passed; m3 1 failed, 2
  passed; m4 1 failed, 2 passed.
- **Growth test** (`-k linear_in_the_blocks`): 6 passed in 246.8 s.
- **Contract guards** (`tests/unit/test_cpu_scaling.py tests/fake/test_state_contract.py
  tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py`): 70 passed in 283.4 s.
- **Superset comparison** (`rows.py` on the archive and on the change, `compare.py`): 176 pairs, 165
  identical, 11 grew, 0 lost.
- **More of the excerpts file**:
  - The within-budget tests and the split, cascade, read-cap and stored-example tests, run with `-q`
    (`-k "within_the_budget or resolves or row_b or quoted_data_uri or read_cap or no_block_leaves or
    stored_example"`), hit their 575 s limit with 39 passed and none failed. `-q` doesn't name them.
  - Run again with `-v` and without the long split and stored-example tests (`-k "within_the_budget or
    resolves or row_b or quoted_data_uri or read_cap"`): 29 passed in 67.8 s. Those are the four
    within-budget tests (with both 200-example properties), the drawn-block and cascade
    differentials, `resolves_the_blocks`, row (b), the quoted data URI and both read-cap cases.
- **`tests/unit/test_scrub*.py`** (14 files, `-v`): stopped unfinished, after 205 passed, none failed
  and none skipped. They were all in `test_scrub_inline_headers.py` (some 200) and
  `test_scrub_key_order.py` (15). The host stalled during the run; its 575 s `timeout` didn't fire
  until about 98 minutes later, and I stopped its processes by pid.

### What didn't run

At this host's load a test got 2–9% of a CPU, and the host then stalled, so these runs didn't fit:

- the rest of `tests/unit/test_sessions_handoff_excerpts.py`. That includes the 80-example property
  and the long rows of the block-edge, chained-cut, split-shape, cascade, omission-marker and edge
  tests. Inference, not a run: the superset comparison found `_split_masks` unchanged on every such
  fixed row (the same text and masks as a4aded885). `_split_masks` is the only changed step in their
  path, so their results should be a4aded885's (all passed in review r2's run);
- the rest of `tests/unit/test_scrub*.py` (12 of 14 files unreached, the other two in part);
- every other `tests/unit/test_sessions_*.py`.

CI's run of the whole suite is the check for these. I also didn't rerun review r2's two P3 measurements
(the 38 characters and the 23,629-character look-back), and I didn't record the growth ratios.

### Deviations from the brief

- **Sync.** No `api` extra exists, so I synced with `uv sync --group dev`.
- **Commits.** They are on the job's detached checkout. No `fix/scrub-reminder-opener-217` branch exists
  in the repository's refs, and I didn't create one, since the caller's checkout is the one named
  for it. Subfleet's salvage ref carries the commits.
- **Beyond the brief:**
  - Row C, the two data URI rows, the 22-case superset test, two growth shapes, and three mutants besides
    a4aded885's handling.
  - `_closer_value_end` gained two keyword arguments, whose defaults keep its behaviour.
- **Data URIs.** The brief names data URIs as a later layer too. The step adds no data URI masks; the
  reason and its tests are above.
- **Scope of invariant 1's tests.** The helper checks the rows' `Zq…` values, not every run the
  whole-text scrub replaces.
- **Time.** The brief's 3-hour limit ended at 18:35 UTC. The host stalled from about 17:40 to 19:15
  during the scrub-file run, so I stopped that run's processes at 19:16 and wrote up at once. The
  report and the cleanup finished at about 19:20.
- **Placements.** "Alone" is the 115-character row itself, as the issue's offsets give it (not the
  cascades' 500 characters of prose either side).
- **Folders outside the job's scratch.** `$TMPDIR/sf-scrub-op1`
  (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-scrub-op1`) held the a4aded885 archive, the
  mutant copies, the probe, rows and mutant scripts, their outputs, the logs and every `--basetemp`.
  I removed it at the end with a guarded `shutil.rmtree`. The shared `$TMPDIR/uv-cache` is not this
  job's and stays.

Built head: see the final answer.

## Reminder-erased openers, fix round 1 (review r1)

Fix job 20261117-114948-scrubop-fix-r1, run 2026-11-17 16:52–19:52 UTC on 620f3b514 (the hub's merge
of release/217 into 289f15a5c; `git diff 289f15a5c 620f3b514` is empty for `handoff.py`, the test
module and this report, so "289f15a5c" below is that code). Host load ran 140–280 on 18 cores. Commits:
`7f81b80e0` (tests), `65161c4e4` (fix), then the contract and this report.

### Each finding's cause and fix

All in `subfleet/sessions/handoff.py`.

- **P0-3, the shared closer test.** `_closer_value_end` counted a value as holding a closer only if it
  ended strictly after the closer's end, so an unterminated quote masked to its line's end didn't hold
  a closer that ended the line. Now `_value_holding` (the body of `_closer_value_end`, which also says
  whether a read decided the answer) counts a value that opens before the closer's end and ends at or
  after it. An opener's question (`point`) is unchanged: opens before it, ends after its start. Every
  caller reads the one test: `_closer_splits` for both layers, and the new step.
- **P0-1, every outcome.** `_closer_splits`' `ends` is now `(closes, low, reach)`: where the walk ends
  the block (None: never), the earliest end the whole-text block may have (equal to `closes` when every
  answer was read; else the end of the first closer held unread), and the end of the walk's last mask.
  A walk that stops at a closer an earlier run-on read from takes that run-on's outcome. `_erased_keys`
  now follows the whole-text scrub's reminder blocks over release's reminders: it checks every reminder
  that no earlier block surely covers (`sure`), whether a value holds its opener or, if so, an opener
  inside it; a block that opens raises `sure` to `low` and `maybe` to `closes`; a reminder between them
  may or may not be covered. A reminder is kept (`live`: a key opening in it is masked), removed
  (`gone`: a closer in it ends no key) or, where a read can't decide, both. Only a block that surely
  opens and surely never closes keeps every later reminder; one that may never close makes every later
  reminder both.
- **P0-2, the held opener.** A value on the opener's line that opens anywhere before it holds it
  (`inner=0`). The look-back stays within one window; a longer line fails closed, which now means
  "undecided" (kept and removed), not "held". An opener with no quote before it on its line is decided
  without a window (`_QuoteBefore`, a forward tracker of the last quote and line break).
- **P1-1, the walk.** The key walk only moves forward: each key's closer searches start past the last,
  the next key opens after the one before ends, and a key no closer ends is masked to the end of what
  it ran over and stops the walk (no closer follows, so the whole-text scrub closes no later key).
- **P0-4, the mirror.** `_split_masks` asks the private-key layer's walk for its outcomes too, and
  `_open_key` finds the first key it never closes (or the opener after release's last key, which no
  closer follows). From that opener on (`_uncarried` maps it back to the original text) the step also
  walks keys that open outside the reminders the whole-text scrub surely removes, so a closer in a kept
  reminder ends the key, as in the whole-text scrub. The mask runs from the opener to that closer, which
  carries to the reminder's place.
- **P0-5, data URIs.** `_data_uri_run_ons`: for each reminder block the whole-text scrub may remove
  that runs on past release's reminder or opens at an opener inside it, where a data URI's body may
  reach the block's opener (`_body_reaches`: body characters back to a data URI prefix, within a
  window, or to an earlier removed block's end, or longer than a window: fail closed), and release's own
  data URI doesn't run past the block's end, it masks the run the whole-text body takes from the block's
  end (body characters, on past each removed block that opens where they stop) and the rest of a quoted
  value whose key that run ends in. The fuzz below found the inner-opener case (a data URI that opens
  inside a reminder whose opener a value holds, reaching an inner opener), so the old argument is wrong
  there too, not only for a data URI that ends at a reminder's opener.
- **P2.** The P2 rows are tests (below); m5, m6 and m7's intents are mutants of this code.

The step still reads its windows last, from what the other layers left of the read budget, and never
changes release's resolved text.

### Rows before and after

Output offsets of the row's value, reminders stripped (G also kept), limit None / 2,048 alone, and limit
None at the head's edge and on the tail's first line. "289f15a5c" is the review's figure, rerun here
alone (`probe.py`, all match) and at the edges where marked; "base" is a4aded885.

| Row | 289f15a5c alone | 289f15a5c head edge / tail line | After (fix), alone |
|---|---|---|---|
| D | 44 / 44 | 130,936 / 131,225 (rerun) | hidden |
| D-longline | 34 / 33 | 113,953 / 135,211 (run here; the review didn't) | hidden |
| E | 22 / 22 | 130,943 / 131,201 | hidden |
| A-unterminated | 1 / 0 (and 0 at 40) | 130,958 / 131,113 | hidden |
| G (both modes) | 23 / 23 | not run | hidden |
| F-held | 28 / 28 | 130,984 / 131,140 | hidden |
| F-never | 28 / 28 | not run | hidden |
| DU-before | 46 / 46 | 130,964 / 131,158; base: 130,964 / 131,158 (run here) | hidden |
| m5, m6, m7 rows | hidden | — | hidden |

After the fix, `probe.py` found no hidden value shown for any of the 12 rows alone, at limits None, 0,
40 and 2,048, both modes (96 cases). At the edges, see "Commands" (12 of the 24 edge tests passed
before their calls' caps, none failed).

### P1-1's counts

Characters the private-key closer searches read in `_split_masks` (`p11.py`, counting `_Ahead`'s
searches), on the review's shape:

| m | text | 289f15a5c | fix |
|---|---|---|---|
| 100 | 18,568 | 1,253,300 | 12,533 |
| 200 | 37,068 | 5,006,600 | 25,033 |
| 400 | 74,068 | 20,013,200 | 50,033 |
| 800 | 148,068 | 80,026,400 | 100,033 |
| 2,834 | 524,358 | not run | 354,283 (0.005 s CPU) |

### Invariants and their tests

1. **No masked run shows.** `test_every_reminder_outcome_the_step_reads_keeps_only_what_the_whole_scrub_keeps`
   runs the 12 rows (D, D-longline, E, A-unterminated, G, F-held, F-never, DU-before, m5, m6, two m7)
   alone, at the head's edge and on the tail's first line through `assert_keeps_only_what_the_whole_scrub_keeps`
   (both modes; limits None, 0, 40, 2,048), and asserts the whole-text and within-line scrubs hide the
   value. `test_drawn_reminder_outcomes_keep_only_what_the_whole_scrub_keeps` (300 derandomized examples
   of `REVIEW_R1_PARTS`) runs the same helper with `within=True`: a value counts as hidden only if
   `within_line_scrub` (the head's whole-text scrub with values read within a line) hides it too, which
   takes out C-23.14's stated multiline difference. Rows A–C keep their test.
2. **Within the budget**, the same helper asserts `scrub_bounded(text, limit) == truncate(scrub_secrets(text), limit)`
   on every row alone and on every drawn text.
3. **Superset.** `test_the_erased_key_step_only_adds_masks` now covers 34 rows (adds the 12) and stubs
   both new steps; it passed (34). Against 289f15a5c and a4aded885 directly: not run (no mask dump this
   round). By construction: P0-3 only adds held closers, so `_closer_splits` only runs on further; the
   step's masks are added after the layers'. But the step is not a superset of 289f15a5c's step: where
   a value holds both release's opener and every opener inside it, the whole-text scrub keeps the
   reminder, so its closers now end keys where 289f15a5c's step ran them on. No fixed row has that shape
   (the 34-row test and every row test pass), so the brief's row-wise invariant isn't shown broken, but
   it is not shown either.
4. **Linear work.** `test_the_erased_key_walk_reads_each_closer_once` (fails on 289f15a5c: 1,253,300 at
   m = 100) and the growth test's new scaled shape `reminder-inner-opener-keys`. The growth test was
   not run on either code this round.
5. **Same resolution.** `test_the_head_resolves_drawn_blocks_as_release_does` now also draws
   `REVIEW_R1_PARTS`; it passed. The fuzz below checked it on 7,062 texts: no difference.

### Mutants

`mutants.py` copies the worktree to `mut/<name>`, applies each mutant's substitutions (asserting each
hit once), runs its killers with pytest in the copy, and checks the worktree's `handoff.py` hash after.

| Mutant (intent) | Killed by |
|---|---|
| p0-3: closer test back to `point < b` | `[a-unterminated-alone]`, `[g-a-key-closer-ends-an-unterminated-value-alone]`, `test_a_value_masked_to_its_line_s_end_holds_the_closer_that_ends_the_line` |
| p0-2: the opener check's `inner` is the previous reminder's end | `[e-a-value-opens-in-an-earlier-reminder-alone]` |
| p0-5: no data URI step | `[du-before-alone]` |
| m5: no inner opener checked | `[m5-a-second-opener-inside-alone]` |
| m6: a closer always ends the key | `[m6-a-closer-in-a-value-in-the-key-alone]` |
| m7: no carry through the later layers | `[m7-a-key-before-row-a-alone]`, `[m7-a-data-uri-before-row-a-alone]` |
| p0-1 (an unchecked reminder opens a block), p0-4 (no mirror), p1-1 (the walk restarts behind a key) | **not run**: each driver call hit its cap (590 s, then 560 s) before reaching them |

Each killed mutant failed on the helper's value-shows assertion (`(length, True, None)`), not on a bounds
check; m7 included (`(180, True, None)`). Expected killers of the three not run: D's row (p0-1), F-held and
F-never (p0-4), `test_the_erased_key_walk_reads_each_closer_once` and the growth test's scaled shape (p1-1). On the unfixed head's code (`pre`, a `git archive` of 620f3b514 with the new test file), the new
tests that fail are the same intents restored all at once: 12 failed (the 8 review rows alone, the three
unit tests, the property), and the 4 P2 rows passed, as they should.

### Fuzz

`fuzz.py` draws texts from the review's parts (reminder tags, values holding them, unterminated
values, key tags, data URIs, `Zq` lines) and counts a leak only where both the whole-text scrub and a
within-line oracle hide the value. On 289f15a5c: 130 leaks in 3,000 texts. On the fix: 10 before the
data URI step was generalized, then 1 in 3,000 and 0 in 4,062 more. The 1 (seed 2075, reminders kept)
also leaks on a4aded885 and 289f15a5c: a data URI's body inside a quoted value runs into the next data
URI's `data` in release's text, which the whole-text scrub's value mask prevents. **Open leak, outside
this round's classes**, not fixed.

### Commands and results

All runs: `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `unset PYTHONPATH`,
scratch `$TMPDIR/sf-scrubop-fix1`, a new `--basetemp` with `pytest` in its name.

- `uv sync --group dev --extra api`: failed (`Extra api is not defined`); `uv sync --group dev`: ok.
- `probe.py` (rows alone, 620f3b514): every row's offsets as the review's. On the fix: 96 cases, none shown.
- `probe.py` edges: D on 620f3b514 (130,936 / 131,225, 103 s); D-longline on `pre`; DU-before on `base`.
- `fuzz.py`: as above. `p11.py`: as above.
- Collect: 319 tests in the excerpts file.
- `pytest -n 4 -k "<new and changed, alone>"` on the fix: 22 failed, 23 passed (the superset test's
  stub returned the old shape); fixed, then `-k "erased_key_step_only_adds or (every_reminder_outcome and alone)"`:
  **46 passed in 74.7 s**.
- On `pre` (serial, `-k "(every_reminder_outcome and alone) or masked_to_its_line or held_unread or
  reads_each_closer_once or drawn_reminder_outcomes"`): **12 failed, 4 passed in 197.5 s**.
- Edges on the fix, three `-n 4` calls of 8 tests each, capped at 585 s: each printed 4 passes and was
  cut before its summary. **12 passed, none failed, 12 unfinished**; xdist's `-q` doesn't name which.
- Mutants: `mutants.py` in three calls (19:22–19:31 for the last): six killed, as above. No call reached
  the driver's own hash check (each hit its cap first); `git status` after them shows the worktree clean,
  with `handoff.py` as committed in `65161c4e4`.

### What didn't run, and deviations

- **Not run:** the rest of `tests/unit/test_sessions_handoff_excerpts.py`; every `tests/unit/test_scrub*.py`
  (shard 1's three included); every other `tests/unit/test_sessions_*.py`; `tests/unit/test_cpu_scaling.py
  tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py`;
  the growth test on both codes; mutants p0-1, p0-4, p1-1; G and F-never at the edges on
  289f15a5c; the superset mask dump against 289f15a5c and a4aded885. CI's run is the check for these.
  Cause: at this load each long-placement test took 100–150 s, and calls I issued together ran one after
  another, so the edge and mutant calls took 18:11–19:16.
- **One code commit.** The brief asks for a failing test and a fix per finding, in order. The fixes share
  the reworked step, so they are one commit after one test commit; the failing tests are shown on `pre`.
- **Beyond the brief:** the inner-opener data URI case (the fuzz's), `within_line_scrub` and the helper's
  `within` flag, and three unit tests.
- **Folders outside the job's scratch:** none; `$TMPDIR/sf-scrubop-fix1` (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-scrubop-fix1`)
  held the archives, scripts, mutant copies and basetemps, and is removed at the end with a guarded
  `shutil.rmtree`. The shared `$TMPDIR/uv-cache` stays. Tool output went to the harness's own task folder.

### Corrected claims

- **Line 537 of this report** ("no value the whole-text scrub hides shows, at any placement, limit or
  reminder mode") is marked corrected. On 289f15a5c rows D, D-longline, E and A-unterminated showed
  their value with reminders stripped. On this head those rows, and F, G and DU-before, hide it alone
  at every limit and mode; 12 of the 24 edge cases are shown, and the fuzz found one open leak of
  another class (above). So the corrected claim is the rows', not the class's.
- **`_erased_keys`' docstring** said "It errs toward hiding more" (row D contradicted it) and "the walk
  reads each closer once" (P1-1's shape contradicted it). Both are rewritten to what the code does: an
  undecided reminder is kept and removed both, and each closer is read once because every search starts
  past the last. `_closer_splits`' docstring states the new `ends`.
- **The data URI argument** ("a data URI needs none of this"; C-23.14's "a data URI there needs
  nothing") holds only for a data URI that opens inside a reminder and stops at its closer. It is false
  for one that ends at a reminder whose closer a value holds (DU-before) and for one that reaches an
  inner opener (the fuzz's). Corrected in the docstrings, C-23.14 and here.
- **C-23.14** is amended, with a 2026-11-17 change-log line, and the 2026-11-15 entry keeps its words
  with a correction marker.

### PR #331 body sentences this round makes false

- "Data URIs get no new mask: a data URI's body stops at a reminder's closer, so its whole-text match
  ends inside text that release removes."
- "It works out which reminders the whole-text scrub keeps. For each private key opener inside a kept
  reminder, it walks …": now also keys that open before a kept reminder holding their closer.
- "Release's resolved text and every earlier layer's masks stay as on a4aded885": the resolved text
  does; the run-on masks grow (P0-3).
- "`_closer_splits` now reports where each reminder's whole-text block ends, or that it never closes.
  `_closer_value_end` gains two keyword arguments whose defaults keep its behaviour.": it reports
  `(closes, low, reach)`, and the default closer test changed (P0-3).
- "A superset test over 22 rows" (now 34), "the growth test adds two shapes" (now three), "Each of four
  mutants failed a test" (this round: six more killed, three not run).

Built head: see the final answer.
