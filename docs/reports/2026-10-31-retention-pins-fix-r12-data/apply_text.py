"""Apply only r12's prose, retaining the complete dated r11 section."""
import hashlib
import json
from pathlib import Path
import subprocess

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]


def replace_once(path, old, new):
    source = path.read_text()
    assert source.count(old) == 1, (path, source.count(old))
    path.write_text(source.replace(old, new))


replace_once(ROOT / 'subfleet/dependencies.py', '''    On a filesystem that holds still, every returned place has one spelling:
    canonical parent plus entry name for a physical link entry, and
    `folders.canonical` for every other place. A place that fails these equations
    and that the kernel refuses to walk with ELOOP or ENAMETOOLONG is left out,
    because nothing can be read through it. Any other place stays, even when its
''', '''    On a filesystem that holds still, returned strings the kernel can walk have
    one spelling: canonical parent plus entry name for a physical link entry,
    and `folders.canonical` for every other place. A place that fails these
    equations and that the kernel refuses with ELOOP is left out, because its
    path holds a live cycle. Any other place stays, even when its
''')
replace_once(ROOT / 'docs/desktop/retention-archive.md', '''On a filesystem that holds still, every returned place uses one spelling:
physical file locations and resolved scan targets take `folders.canonical`,
''', '''On a filesystem that holds still, returned strings the kernel can walk use
one spelling: physical file locations and resolved scan targets take `folders.canonical`,
''')
replace_once(ROOT / 'docs/desktop/retention-archive.md', '''A place that fails these equations and that the kernel refuses to walk with
`ELOOP` or `ENAMETOOLONG` is left out, because nothing can be read through it.
''', '''A place that fails these equations and that the kernel refuses with `ELOOP`
is left out, because its path holds a live cycle.
''')
replace_once(ROOT / 'docs/desktop/retention-archive.md', '### Listed differences\n\n', '''### Listed differences

- **D-R12-1:** A suffix-growing cycle string past `PATH_MAX` stays in the
  record, unspelled, as at `475f63278`. `ENAMETOOLONG` rejects one spelling
  without showing that the place is unreadable. Nothing reads through this
  cycle string; it can add at most an extra pin until the job ends. Executed
  rows: `tests/unit/test_retention_location_readable.py::test_real_enametoolong_cycle`
  at fixture bases of 371 and 500 bytes. Baseline `gitfile` records have
  2 places / 4 incomplete and `commondir` 3 / 3; the ELOOP-only filter
  removes their shorter ELOOP cycle string, giving 1 / 4 and 2 / 3. The
  ENAMETOOLONG string and full incomplete evidence are retained.
  Both ordinary and linked-TMPDIR runs are retained in the
  [fix-r12 evidence](../reports/2026-10-31-retention-pins-fix-r12.md).

''')
main = ROOT / 'docs/reports/2026-10-27-retention-dependency-pins.md'
old = main.read_text()
dated = old[old.index('## Review r11 and fix round r11'):]
lead = '''**Fix round r12 repairs R12-F1: a rejected long spelling keeps its dependency pin.** A place is omitted only when a spelling equation fails and `os.stat` raises `ELOOP`; every other outcome keeps the string `475f63278` kept. The normalization-length alternates and scan fixtures preserve the owner and identical Git read at 0. One spelling is scoped to a filesystem that holds still and strings the kernel can walk. No evidence, observation or reader depth changes. Retention remains dormant pending approving review r13 and completed CI.

'''
section = '''## Review r12 and fix round r12

Review r12 reproduced **R12-F1 (P2)** on both alternates and scan routes: a normalization rename leaves a canonical absolute string past `PATH_MAX`, while Git can still read the store through its accepted short spelling. At `92a8635a0` the two-error filter drops that place with no incomplete evidence, retires the owner, and changes the same read from 0 to 128. `475f63278` keeps three places, preserves the owner and reads 0. Review r12 stopped on the provider's content filter with its work kept. Its report is partial and has no completed verdict; the hub committed its partial report, data and test byte for byte.

The repair narrows `_unreadable_location` to `return exc.errno == errno.ELOOP`. Review r12's walked-spelling trial is not used: no second map, observation, walker change or ordering change is introduced. The review file goes from 2 passed / 2 failed with `--runxfail` at the input head to 4 passed normally and 4 through linked TMPDIR. Both routes again record three places, no incomplete entry, and retain the owner and read 0.

`ENAMETOOLONG` rejects one spelling; it does not show that the place is unreadable. D-R12-1 states the accepted cost: a cycle string past `PATH_MAX` stays unspelled as in `475f63278`, adding at most an extra pin until the job ends. The executed gitfile and commondir cycle rows at 371- and 500-byte fixture bases retain the ENAMETOOLONG string and exact ordered incomplete evidence. Decision 4's requested full baseline equality conflicts with decision 1: each baseline also contains a shorter 701/830-byte ELOOP string. Keeping the product invariant and removing only that string gives 1 place / 4 incomplete and 2 / 3, versus baseline 2 / 4 and 3 / 3. This is the only decision deviation; the executed counterexample is retained in the fix-r12 data. The differential now requires every omitted string to raise ELOOP alone. C-8.3/C-8.4, both strategy modules, the cycle file and all earlier review files stay unchanged.

[Fix round r12](2026-10-31-retention-pins-fix-r12.md) contains the normal/linked counts, actual starts and loads, baseline comparisons, five routine controls, named-mutant table, PR-body lines and delivery. The r11 section below retains its dated results verbatim; r12 narrows its two-error rule.

'''
main.write_text(old[:old.index('\n\n') + 2] + lead + section + dated)
assert main.read_text().endswith(dated)
contract = ROOT / 'docs/acceptance-contract.md'
before_contract = subprocess.check_output(['git', 'show', '92a8635a05d6:docs/acceptance-contract.md'], cwd=ROOT)
assert contract.read_bytes() == before_contract
(DATA / 'text-audit.json').write_text(json.dumps(dict(r11_and_earlier_sections_unchanged=True,
    r11_and_earlier_sha256=hashlib.sha256(dated.encode()).hexdigest(),
    whole_contract_unchanged=True, contract_sha256=hashlib.sha256(before_contract).hexdigest(),
    new_listed_difference='D-R12-1'), indent=2) + '\n')
