"""Apply the requested prose after results are assembled; retain dated r10 text."""
import hashlib
import json
from pathlib import Path
import subprocess

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]


def replace_once(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, (path, text.count(old))
    path.write_text(text.replace(old, new))


replace_once(ROOT / "subfleet/dependencies.py", """    Every returned place has one spelling: canonical parent plus entry name for
    a physical link entry, and `folders.canonical` for every other place.
    A place inside a symlink cycle that cannot meet these spelling equations is
    left out, because nothing can be read through it. Its physical link entries
    and any incomplete evidence stay. The returned list is built before alias
    revalidation so a change during spelling keeps the existing alias-changed evidence.
""", """    On a filesystem that holds still, every returned place has one spelling:
    canonical parent plus entry name for a physical link entry, and
    `folders.canonical` for every other place. A place that fails these equations
    and that the kernel refuses to walk with ELOOP or ENAMETOOLONG is left out,
    because nothing can be read through it. Any other place stays, even when its
    spelling changed during discovery. Physical link entries and any incomplete
    evidence stay. The returned list is built before the existing alias
    revalidation of walked targets, identities, link chains and pointer contents.
""")

replace_once(ROOT / "docs/desktop/retention-archive.md", """Every returned place uses one spelling: physical file locations and resolved
scan targets take `folders.canonical`, including Data-volume, letter-case and
Unicode-normalization aliases the volume accepts. Physical link entries keep
their canonical parent plus their own name. A place inside a symlink cycle
that cannot meet these spelling equations is left out, because nothing can be
read through it. Its physical link entries and any incomplete evidence stay.
The return list is built before aliases are revalidated, so changes during
spelling retain the existing alias-changed evidence. The supplied folder is spelled
""", """On a filesystem that holds still, every returned place uses one spelling:
physical file locations and resolved scan targets take `folders.canonical`,
including Data-volume, letter-case and Unicode-normalization aliases the volume
accepts. Physical link entries keep their canonical parent plus their own name.
A place that fails these equations and that the kernel refuses to walk with
`ELOOP` or `ENAMETOOLONG` is left out, because nothing can be read through it.
Any other place stays, even when its spelling changed during discovery.
Physical link entries and any incomplete evidence stay. The return list is
built before the existing alias revalidation of walked targets, identities,
link chains and pointer contents. The supplied folder is spelled
""")

main = ROOT / "docs/reports/2026-10-27-retention-dependency-pins.md"
old = main.read_text()
start = old.index("## Review r10 and fix round r10")
end = old.index("## Review r9 and fix round r9")
dated = old[start:end]
lead = """**Fix round r11 repairs R11-F1: a readable place stays when its spelling changes during discovery.** Both return filters omit a failed spelling only when the kernel refuses the walk with `ELOOP` or `ENAMETOOLONG`; every other result keeps the string `475f63278` kept. Existing evidence, alias observations and reader depth are unchanged. One spelling is guaranteed on a filesystem that holds still. Retention remains dormant pending approving review r12 and completed CI.

"""
section = """## Review r11 and fix round r11

Review r11 found **R11-F1 (P2)**: r10's equation-only filter could drop a still-readable place after a case-only rename between canonicalization and filtering. Both healthy Git fixtures kept complete evidence, retired the running borrower's required owner and changed the identical read from 0 to 128. `475f63278` kept the third place and the owner. The hub committed that review unchanged at `39404ea61`; `ced02912b` adds its remeasured CI shard map.

The repair follows the hub's narrow unreadability trial. `_spelled_location` retains its two equations. A shared predicate returns true only when those equations fail and `os.stat` raises `ELOOP` or `ENAMETOOLONG`. Success and every other error keep the original string. Both filters use it. `_target`, `check`, the component walker and the list-before-check order stay unchanged; no new canonical observation or incomplete entry is added.

The dated r10 results below describe its nine stable cycle fixtures. Review r11 corrected the generalization that every rejected string raised `ELOOP` or was unreadable: the old return filter also rejected readable case aliases. The repaired code enforces an `ELOOP`/`ENAMETOOLONG` gate for omitted failed spellings. The original r11 file is red 2 passed / 2 failed at `ced02912b` and green 4 passed after removing only its strict mark. New tests cover real stat outcomes, same-input baseline differentials, long cycles, parent-component and Unicode-normalization renames. C-8.3, C-8.4 and existing listed reader differences are unchanged; R11-F1 adds no listed difference.

[Fix round r11](2026-10-31-retention-pins-fix-r11.md) contains full normal/linked results, input counts, UTC starts and loads, named mutant comparisons, omissions and PR-body lines. Earlier sections below retain their dated results.

"""
main.write_text(old[:old.index("\n\n") + 2] + lead + section + old[start:])
assert dated in main.read_text()
contract = ROOT / "docs/acceptance-contract.md"
baseline = subprocess.check_output(["git", "show", "ced02912b:docs/acceptance-contract.md"], cwd=ROOT)
assert contract.read_bytes() == baseline
audit = dict(r10_section_unchanged=dated in main.read_text(),
             r10_section_sha256=hashlib.sha256(dated.encode()).hexdigest(),
             whole_contract_unchanged=True, contract_sha256=hashlib.sha256(baseline).hexdigest())
desktop = ROOT / "docs/desktop/retention-archive.md"
before_desktop = subprocess.check_output(["git", "show", "ced02912b:docs/desktop/retention-archive.md"], cwd=ROOT, text=True)
def listed(text):
    return text[text.index("### Listed differences"):text.index("## 11. Accounting")]
assert listed(before_desktop) == listed(desktop.read_text())
audit['listed_differences_unchanged'] = True
audit['listed_differences_sha256'] = hashlib.sha256(listed(before_desktop).encode()).hexdigest()
(DATA / "text-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
