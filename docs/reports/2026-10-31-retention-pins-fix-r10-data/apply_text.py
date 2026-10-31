"""Final text step: preserve the invariant and document the cycle-filter cost."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
p = ROOT / 'subfleet/dependencies.py'
s = p.read_text()
old = '''    a physical link entry, and `folders.canonical` for every other place.
    What this cannot read is left out: environment variables (`GIT_DIR`,'''
new = '''    a physical link entry, and `folders.canonical` for every other place.
    A place inside a symlink cycle that cannot meet these spelling equations is
    left out, because nothing can be read through it. Its physical link entries
    and any incomplete evidence stay. The returned list is built before alias
    revalidation so a change during spelling keeps the existing alias-changed evidence.
    What this cannot read is left out: environment variables (`GIT_DIR`,'''
assert s.count(old) == 1
p.write_text(s.replace(old, new))
p = ROOT / 'docs/desktop/retention-archive.md'
s = p.read_text()
old = '''their canonical parent plus their own name. The supplied folder is spelled
before the routine-location comparison with it and its ancestors. This work'''
new = '''their canonical parent plus their own name. A place inside a symlink cycle
that cannot meet these spelling equations is left out, because nothing can be
read through it. Its physical link entries and any incomplete evidence stay.
The return list is built before aliases are revalidated, so changes during
spelling retain the existing alias-changed evidence. The supplied folder is spelled
before the routine-location comparison with it and its ancestors. This work'''
assert s.count(old) == 1
p.write_text(s.replace(old, new))
p = ROOT / 'docs/reports/2026-10-27-retention-dependency-pins.md'
s = p.read_text()
start = s.index('**Fix round r9 repairs')
end = s.index('## Review r9 and fix round r9')
replacement = '''**Fix round r10 repairs R10-F1: every returned discovery place has one spelling, whether or not discovery is complete.** Canonical storage strings and scanned targets must meet both spelling equations at the return boundary. Physical link entries, successful file locations and existing incomplete evidence stay. The returned list is built before alias revalidation. Discovery stays outside transactions. Retention remains dormant pending approving review r11 and completed CI.

## Review r10 and fix round r10

Review r10 found **R10-F1 (P3)**: an unused direct link into a suffix-growing cycle returned an unspelled string with complete discovery, falsifying r9's unconditional sentence. The checkout itself passes fsck and reads. The hub committed the review unchanged at `475f6327890e`.

The hub's seven-route table widened the fix beyond review r10's scan-only guard trial: alternates, gitfiles, commondir and linked objects also forward these strings through `found`. One shared predicate now filters canonical strings in `git_storage` and scan targets in `locations`, without changing the scan or physical link/file recording. The return list is computed before `check`, so a link or resolved target changed into a cycle during spelling gets the existing incomplete evidence.

The nine route/cycle rows meet both equations and preserve the exact stable-input incomplete lists. Every removed string independently raises `ELOOP`; healthy sibling storage and all five routine records match `475f6327890e`. Review r10 is red 10/2 and green 12/0; the new 20-case file is red 10/10 and green 20/0. The original strategies, listed reader differences and entire C-8.3/C-8.4 contract remain unchanged. The cycle cost is documented in the discovery description; review r10's temporary difference is retired. No reader was widened, including the 64-hop walker.

[Fix round r10](2026-10-31-retention-pins-fix-r10.md) contains every route, routine comparison, per-file count, UTC start/load, named mutant, PR-body replacement and delivery detail. Earlier rounds below are dated history.

'''
p.write_text(s[:start] + replacement + s[end:])
print('Updated the discovery docstring, desktop description and main report.')
