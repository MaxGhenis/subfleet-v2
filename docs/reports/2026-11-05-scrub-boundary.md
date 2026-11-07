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
as `_linear_sub` already does. The quoted-value rule reads only windows:

- at most 16,384 characters each (`_SPLIT_WINDOW_MAX`);
- at most 131,072 in all per text (`_SPLIT_READ_MAX`, one excerpt's worth);
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
