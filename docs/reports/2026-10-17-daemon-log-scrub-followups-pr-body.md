#233's review r6 approved it with two P3s. This PR fixes both (D-LOG1): a needless marker fallback (P3-A) and C-25.7's stale cost sentences (P3-B). It targets `release/217`, from `c2e080e44`, which has #233 merged. The full report is the "Follow-ups after merge" section of `docs/reports/2026-10-17-daemon-log-scrub.md`.

**Changes:**

- **A margin file that ends in a reminder keeps its last lines (P3-A).**
  - **Cause:** `_reminders_resolved` compared the margin-free reading with its reminders taken out against `_margin_free(kept)` exactly. `_margin_free` strips trailing `\r\n`, so where reminders end the file the first kept the line break before them and the second did not. Six hundred admission lines, `  | member line`, then `<system-reminder>note</system-reminder>` printed only `… [31,746 characters omitted] …` at `c2e080e44`.
  - **The hub's direction leaked when executed.** Stripping trailing breaks from both sides, alone, resolves those files. But `_DATA_URI_RE` counts line breaks among the 32 characters a body needs. A body of 31 characters read without margins, before a reminder that ends the file, is hidden by the whole-text scrub, which keeps the break before the reminder. The tail's block form strips that break, so it showed the body: `SECRET7dataend9` at `-n 2`, on four of five shapes (probe at `c2e080e44`; that commit's strict comparison had refused those files).
  - **Fix:** `daemon_log._alike` compares the two readings with trailing line breaks stripped. It refuses only where those breaks bring a data URI body at the end to 32 characters and the kept reading's own leave it short.
    - It is a regex of the data URI rule's class and flags, searched in the last 39 characters.
    - It does not read the prefix, so a bare `;base64,` counts too, conservatively.
    - Equal texts return at once, so every file `c2e080e44` resolved resolves the same way. Nothing else in the guard changed.
  - **A refusal that stays:** in a CRLF text, a last member line that is a bare margin (`  |\r\n`) is read as a margin once its `\r` goes with the trailing breaks, so the readings differ by more than breaks. `daemon logs` reads daemon.log with universal newlines and prints the last lines. A test documents it.

- **C-25.7's cost sentences give this tree's numbers (P3-B).**
  - **The four-byte margin shape is linear.**
    - Counted with the count test's meter, the cut's regexes read 108,159 characters at every size from 1 to 32 MiB, and its `str` scans come to 4.00 to 4.06 passes.
    - Timed (best of 5 CPU times, each size in its own process), it took 13.4, 46.0, 89.1, 178.8 and 345.0 ms at 1, 4, 8, 16 and 32 MiB: 10.8 to 13.4 ms a MiB. Peak memory at 32 MiB was 1,603 MB.
    - No run reproduced round 7's 7× between 4 and 16 MiB. Three runs gave 3.9× to 4.4×.
    - The shape now joins the count test, so "linear" is tested.
  - **The stronger check's cost:** `daemon logs -n 40` with a whole reminder on line 1 took 465, 1,685 and 6,626 ms at 1, 4 and 16 MiB. Round 7's run measured 405, 1,900 and 6,360. Both are linear, about twice round 6's single check.
  - **The clause:**
    - The four-byte ratios replace review r5's 11.6, 8.9 and 6.9: round 7's 3.4, 8.1 and 15.7, and this build's 4.2 to 8.0.
    - The clause's other review-r5 numbers become round 7's measurements of this tree.
    - The realistic-log costs are this tree's.
    - A change-log entry covers P3-A and P3-B.

- **Tools:**
  - `tools/daemon_log_cut_mutations.py` gains the two mutants below and seven named tests.
  - `tools/reminder_join_census.py` counts each text three ways: round 6's control, `c2e080e44`'s comparison and this branch's. With `--margin-texts` it draws margin files ending in a reminder.
  - `tools/measure_cut_scaling.py` is new.

**Invariants** (each tested):

1. No tail shows a secret the whole-text scrub hides, over margin files ending in a reminder too.
2. A margin file ending in a reminder keeps only the marker exactly where the quoted pass's masks differ, the trailing breaks complete a data URI body, or (CRLF only) a bare margin becomes the last line. A trailing break alone never refuses it.
3. Every file `c2e080e44` resolved still resolves, to the same text.
4. The cut's work on the four-byte margin shape is linear: the same regex reads at 1 and 16 MiB, at most 8 passes of `str` scans.

**Fuzz:** 0 leaks over 46,000 texts and 0 over 115,854 tails.

| Corpus | Texts | Falls back at `c2e080e44` | Falls back here | Leaks here |
|---|---:|---:|---:|---:|
| Review r5's grammar | 20,000 | 13,888 | 13,812 | 0 (tails 0 of 55,854) |
| Margin files ending in a reminder (new) | 20,000 | 16,589 | 1,236 | 0 (tails 0 of 60,000) |
| Round 6's four corpora, 1,500 each | 6,000 | 78 | 0 | 0 |

Round 6's control leaked on 2,008 and 171 texts of the first two rows. The margin files' 1,236 fallbacks break down as:
- 822 where the masks differ;
- 332 where the breaks complete a data URI body (171 of which the hub's direction alone would have leaked);
- 82 CRLF bare margins.

**Tests**, at `1d3627968` (the commits after it touch only the docs and the census tool, which no test imports), one pytest a hold through the hub's wrapper:
- **First group,** with the new file, `test_daemon_log_first_pass.py` and `test_daemon_log_mutation_tool.py`: **673 passed**.
- **Second group,** with the three files that read the contract and the new file: **393 passed**.
- **New file** `tests/unit/test_daemon_log_trailing_reminders.py`: 56 cases, among them a bounded Hypothesis property of 150 examples at the real bound.

**Mutations:** the baseline passes all 40 named tests.
- `asymmetric-margin-free-comparison` (`c2e080e44`'s exact comparison) fails 4: the review's endings and `daemon logs -n 3`.
- `trailing-breaks-stripped-alone` (review r6's direction alone) fails 2: the data URI regressions.

**Not run:** `tests/e2e`, Python 3.12, CI, and the rest of `tests/unit` and `tests/fake`, including the runner and conversation files review r7 left for CI. `measure_cut_cost.py`'s full shape table was not rerun.

**Before merging:** `gh pr checks` must exit 0 at the head, and an independent review must agree.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
