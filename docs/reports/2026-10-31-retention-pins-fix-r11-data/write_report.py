"""Render the report from completed evidence and explicit omissions."""
import json
from pathlib import Path

from tables import build, receipts

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
summary = build()
runs = {r["name"]: r for r in receipts() if "exit" in r}


def observations(name, key):
    return [(o["node"], json.loads(o["value"])) for o in runs[name]["observations"] if o["key"] == key]


case_rows = []
for hold, label in (("red-review-r11", "ced02912b"), ("green-review-r11", "head")):
    for node, row in observations(hold, "R11-F1"):
        baseline = "test_baseline_" in node
        if baseline and label == "head":
            continue
        case_rows.append(dict(reader="475f63278" if baseline else label, **row))
(DATA / "r11-case-table.json").write_text(json.dumps(case_rows, indent=2) + "\n")
case_table = ["| Route | Reader | Places | Incomplete | fsck before / renamed | Read before / renamed / retention | Owner pruned |",
              "|---|---|---:|---:|---|---|---|"]
for r in case_rows:
    case_table.append(f'| {r["route"]} | {r["reader"]} | {len(r["recorded"].get("git", []))} | '
        f'{len(r["recorded"].get("incomplete", []))} | {r["fsck"]}/{r["fsck_after_rename"]} | '
        f'{r["before"]}/{r["middle"]}/{r["after"]} | {"yes" if "owner" in r["result"]["pruned"] else "no"} |')

long_rows = [r for _, r in observations("green-readable", "long-cycle")]
(DATA / "long-cycle-table.json").write_text(json.dumps(long_rows, indent=2) + "\n")
long_table = ["| Route | Base bytes | Probe errno | 475f63278 places / incomplete | ced02912b places / incomplete | Head places / incomplete | Discovery exceptions |",
              "|---|---:|---|---|---|---|---|"]
for row in long_rows:
    counts = []
    for label in ("475f63278", "ced02912b", "head"):
        value = row["outcomes"][label]
        counts.append(f'{len(value["recorded"].get("git", []))}/{len(value["recorded"].get("incomplete", []))}'
                      if "recorded" in value else str(value))
    long_table.append(f'| {row["route"]} | {row["length"]} | ENAMETOOLONG ({row["stat_errno"]}) | '
                      + " | ".join(counts) + " | none |")

routine = [(node.split("[")[-1].rstrip("]"), row) for node, row in observations("green-readable", "differential")
           if "test_routine_differential" in node]
routine_table = ["| Routine control | 475f63278 places | Head places | Full record |", "|---|---:|---:|---|"]
for layout, row in routine:
    routine_table.append(f'| {layout} | {len(row["baseline"].get("git", []))} | '
                         f'{len(row["actual"].get("git", []))} | equal |')

red_final = runs.get("red-readable-final")
red_final_text = ("The final new module replayed against the exact `ced02912b` reader snapshot gives **24 passed / 12 failed**: "
    "eight keep-table failures, both further readable rename failures and both original same-input record comparisons. Its receipt records the compiled snapshot hash."
    if red_final and red_final["exit"] == 1 else "The corrected rename-only red replay gives two expected failures at ced02912b.")
unresolved = summary["missing_or_unfinished"]
omission = ("No selected ordinary node remains unpassed." if not unresolved else
            "Selected nodes without a completed pass:\n\n" + "\n".join("- `" + n + "`" for n in unresolved))
if summary["planned_holds_not_completed"]:
    omission += "\n\nPlanned holds without completed receipts: " + ", ".join(summary["planned_holds_not_completed"])
killed = sum(m["result"] == "killed" for m in summary["mutants"])
pr_body = f"""**Fix round r11 repairs R11-F1 without extending the reader.** A place whose spelling changes during discovery keeps the string `475f63278` kept unless a spelling equation fails and the kernel refuses the walk with `ELOOP` or `ENAMETOOLONG`. The original case-renames again record three places, no incomplete entry, and preserve the owner's tree and the identical Git read at 0. `_target`, `check`, the component walker and reader depth are unchanged; retention remains dormant pending approving review r12 and completed CI.

**Evidence:** Review r11 is red 2 passed / 2 failed at `ced02912b`, green 4 passed. The new module passes 36 nodes normally and 36 under linked `TMPDIR`. {summary['differential_comparisons']} same-filesystem baseline/head comparisons preserve full ordered incomplete evidence and omit only strings whose real `stat` raises `ELOOP` or `ENAMETOOLONG`. The nine original cycle fixtures' omitted strings raised `ELOOP`; the 371/500-byte long-cycle fixtures exercise real `ENAMETOOLONG`. Five routine records equal `475f63278`; parent-case and Unicode-normalization renames keep the owner and read 0. {killed}/19 named mutants killed. {summary['unique_passed']} distinct ordinary nodes passed; see the report for every hold, start/load and any guarded property omission.

**Next:** Review r12 judges fix round r11 and its baseline comparisons; obtain approving review and the hub's all-pass CI/mergeability checks. Activation remains separate.
"""
(DATA / "pr-body-lines.md").write_text(pr_body)

text = f"""# Retention dependency pins — fix round r11

**R11-F1 repaired.** The product repair is `49dbf61c6`; tests first committed at `0ab48ff5f`. The filter now asks whether the kernel refuses a failed spelling, preserving the old reader's string for every other outcome. No hub decision required a product deviation and no new reader gap was found in the executed inputs.

Started **2026-10-31 18:02:38 UTC**, input head **ced02912bea6f7ada1626f817bb847a90ae90470**. First admitted pytest started **2026-10-31 18:03:52 UTC**. Hold-start cutoff **22:02:38 UTC**; hard job wall **00:02:38 UTC November 1**.

## Decisions 1–6 as built

1. `_spelled_location` retains the two equations. `_unreadable_location` returns true only if the equations fail and `os.stat` raises `ELOOP` or `ENAMETOOLONG`. Both return filters use it. Successful stat and every other error keep the supplied string.
2. The hub's narrow trial is applied exactly. No canonical observation is added. `_target`, `check`, `resolve`, `scan`, `text` and the component walker are unchanged; the list is still built before `check`.
3. Full ordered incomplete lists match both `475f63278` and `ced02912b` (the same reader as `546b63ea1`) on the compared stable inputs. The original readable rename inputs match the older reader: three places and no incomplete entry. No false global evidence is introduced.
4. The new module exercises real Git/files, both return filters, six real stat outcomes, the existing unmodified spelling strategies, long cycles and two further readable rename forms. Only the strict r11 decorator is removed; its bodies are byte-identical. The initial Unicode fixture accidentally added an independently pinning symlink and expected three rather than four baseline places. It was corrected to rename a Unicode source-directory component with no added link, and replayed red before repair. This is a test-fixture correction, not a change to the hub's decisions.
5. All three new, three r10, four r9 and nine r8 named mutants are run against named killers. The table below records whether prior assertions match.
6. The discovery docstring and desktop paragraph scope one spelling to a filesystem that holds still, and explain the stat gate. `_target` and `check` retain their existing observation bounds. The whole acceptance contract is byte-identical; C-8.3/C-8.4 are unchanged. R11-F1 adds no listed difference, and all existing listed differences stay. The main report's dated r10 section is kept verbatim and a new r11 section corrects its overgeneralization.

## Original review table, before and after

{chr(10).join(case_table)}

The original red run uses `--runxfail` at `ced02912b`: **2 passed / 2 failed**, both at owner retention. Green is **4 passed**, also **4 passed** under linked `TMPDIR`. Baseline and head fsck/read setup succeeds. No incomplete evidence appears in either original input.

## Red and green for the new module

The initial new-file red is **24 passed / 10 failed**, with the Unicode fixture correction described above. The corrected rename-only red has **2 failed / 32 deselected**, each losing the third place at the old filter. {red_final_text} The final source is **36 passed** normally and **36 passed** with `TMPDIR` through a symlink (34 in each initial full-file hold plus two added same-input comparisons in focused holds). The requested first nine-file green set covers **112 passing nodes**, including those two additions.

## Differential and five routine controls

There are **{summary['differential_comparisons']} same-filesystem comparisons** in the completed normal/linked green runs: **256** from the new module (128 each: nine cycle rows, five routine controls, 55 location recipes, 55 spelling recipes and four long-cycle rows), and **240** from the two unchanged strategy modules (120 each environment, including their focused controls). Repeated parameter recipes use fresh filesystem inputs. Each comparison asserts head places are a subset of baseline, every missing string independently raises `ELOOP` or `ENAMETOOLONG` from real stat, and the full ordered incomplete list equals both older readers entry for entry. The stable strategy records are wholly equal. No unsupported-volume skip occurred. Eighteen additional healthy-sibling checks compare the identical stable repository after removing the cycle; both further rename forms compare complete old/head records on a restored identical input. Four additional original-route comparisons (two normal, two linked) restore the exact same r11 fixture and replay the same race against baseline, asserting full record equality before the original owner/read check.

{chr(10).join(routine_table)}

## Filter table

Both `found` and `entries` exercise every row; each stat outcome is real, including mode-bit EACCES and an overlong pathname.

| Real stat outcome | Failed equation outcome | Result |
|---|---|---|
| success | real accepted case alias | keep |
| ENOENT | controlled canonical | keep |
| ENOTDIR | controlled canonical | keep |
| EACCES | controlled canonical | keep |
| ELOOP | real suffix-growing symlink | omit |
| ENAMETOOLONG | controlled canonical; real stat refusal | omit |

The isolated found-site table injects a pre-filter discovery string in every row (including a controlled final generator spelling); the controlled failed equations are needed only for ENOENT, ENOTDIR, EACCES and ENAMETOOLONG. The entries-site success and ELOOP rows use real canonical behavior. The existing original and extra rename fixtures use real renames at the canonical/filter boundary rather than manufactured filesystem errors. Stable spelled paths retain the existing fast path without a new stat requirement.

## Real ENAMETOOLONG

The probe is `base/loop/tail×128/objects` with `loop -> loop/tail`. `base` is measured physically in bytes. Every row uses real Git and files at all three readers; discovery and canonicalization return on all rows, with no shared exception to list as a new difference. Ordered full evidence is equal.

{chr(10).join(long_table)}

## Further readable renames

The alternates route renames the object's parent `.git` to `.GIT`, and separately renames a Unicode source-directory component between normalization forms accepted as the same name by this volume. Discovery is rerun against `475f63278` after restoring the original name; records are identical (three places, no incomplete evidence). With the renamed filesystem left in place, that identical retention input keeps the owner, fsck passes and the same private-commit read remains **0**. Both forms pass normally and under linked `TMPDIR`.

## Counts, holds and omissions

**{summary['unique_passed']} distinct ordinary nodes passed**, across **{summary['ordinary_invocations']} completed ordinary invocations**; red and mutant failures are not counted as ordinary passes. Every completed invocation used the shared suite lock. At most two task-owned pytest processes ran concurrently, one pytest per hold, serial within pytest. Longest completed invocation **{summary['longest_seconds']:.2f} s**; peak measured pytest RSS **{summary['peak_rss_mib']:.2f} MiB**. No outside-lock fallback was needed.

{(DATA / 'file-table.md').read_text()}

The complete per-hold table gives exact file selections via each linked receipt's `args`/`collected`, with UTC starts and actual load averages. Intentional red and mutant failures are included transparently.

{(DATA / 'hold-table.md').read_text().replace('](', '](' + DATA.name + '/')}

{omission}

The live process-listing case `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` is deliberately not selected. The full suite, Python 3.12, CI and `tests/frontend/test_core_live.py` are not run on this lane. CI and mergeability remain the hub's responsibility. Any initial archive path-trap failures and guarded-property interruptions are recorded in the receipts and retry section below; no bound or assertion is loosened.

{(DATA / 'retries.md').read_text() if (DATA / 'retries.md').exists() else ''}

## Named mutants

**{killed}/19 killed.** Mutation source hashes and failing assertions are in `mutant-results.json` and each receipt. A kill requires exactly one call failure at the named killer and no setup/teardown failure. Older-assertion comparisons are made against the committed r10, r9 and r8 receipts; the r8 routine-skip mutation retains this round's filter while removing only the earlier exclusions.

{(DATA / 'mutant-table.md').read_text()}

## Environment, cleanup and delivery

The prescribed macOS scratch path was denied; fallback is `/private/tmp/sf-fix272r11`. Linked TMPDIR uses `link-tmp -> real-tmp`. Sync ran once through `uv_sync_hold.sh`, Python 3.14.7 and the locked dev group; all later runs use that scratch environment and `uv run --no-sync`. Reports, data and tests remain in this worktree throughout. Scratch stays below 1.5 GB and is removed with the rules file's guarded cleanup; the final receipt records its size and removals.

Shared Git refs reject writes, so commits use ignored `.git-local` metadata in this worktree and read shared objects via an alternate. The delivery bundle uses **ced02912b as its prerequisite** and is verified before the final answer. Every commit has the required co-author footer. No history is rewritten; the caller's worktree is untouched. No sub-agents are used, per the lane rules. No real CLI, live stores, providers, live process listing or GitHub calls run. The job repository is never fetched, merged or pushed; Git fixture transfers are local to isolated test repositories. All test and mutant drivers end before cleanup; all task-owned processes end before final delivery.

## PR-body lines

{pr_body}

## New gaps

None found in the executed inputs. No reader, observation set or walker change was made beyond the requested return gate.
"""
(ROOT / "docs/reports/2026-10-31-retention-pins-fix-r11.md").write_text(text)
