"""Fix round 3: count lines, change-log entries and clause headings in the contract at the merge base, both
parents and the merge result: the working file, or the commit named by argv[1] (the merge commit, 7132ea201).
Reads committed versions with `git show` and the working file; writes nothing."""
import collections, re, subprocess, sys
PATH = "docs/acceptance-contract.md"
HEAD = re.compile(r"^- \*\*C-\d+\.\d+[a-z]?\b")
RESULT = sys.argv[1] if len(sys.argv) > 1 else None
def text(ref):
    if ref == "merged":
        if RESULT:
            return subprocess.run(["git", "show", f"{RESULT}:{PATH}"], capture_output=True, text=True, check=True).stdout
        return open(PATH, encoding="utf-8").read()
    return subprocess.run(["git", "show", f"{ref}:{PATH}"], capture_output=True, text=True, check=True).stdout
def counts(t):
    lines = t.split("\n")
    wc = t.count("\n")
    start = lines.index("## Changes in version 3")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    log = [l for l in lines[start + 1:end] if l.startswith("- ")]
    heads = [l for l in lines if HEAD.match(l)]
    ids = [HEAD.match(l).group(0)[4:] for l in heads]
    dups = sorted(c for c, n in collections.Counter(ids).items() if n > 1)
    c88 = [i + 1 for i, l in enumerate(lines) if l.startswith("- **C-8.8**")]
    return wc, len(log), len(heads), dups, c88
rows = {}
for ref in ("4aed7a3c9", "fc4fc603d", "53a7bac93", "merged"):
    rows[ref] = counts(text(ref))
    wc, nlog, nheads, dups, c88 = rows[ref]
    print(f"{ref:10} lines(wc -l)={wc} changelog_entries={nlog} clause_headings={nheads} duplicated={dups} C-8.8_at={c88}")
b, o, t, m = (rows[r] for r in ("4aed7a3c9", "fc4fc603d", "53a7bac93", "merged"))
print(f"lines: {m[0]} == {o[0]} + ({t[0]} - {b[0]}) = {o[0] + t[0] - b[0]}: {m[0] == o[0] + t[0] - b[0]}")
print(f"changelog: {m[1]} == {o[1]} + ({t[1]} - {b[1]}) = {o[1] + t[1] - b[1]}: {m[1] == o[1] + t[1] - b[1]}")
print(f"clause headings: merged {m[2]} == fc4fc603d {o[2]}: {m[2] == o[2]}; none duplicated: {not m[3]}")
merged_lines = text("merged").split("\n")
print("lines 9-14 of the result:")
for i in range(8, 14):
    print(f"  {i + 1}: {merged_lines[i][:110]}".rstrip())
sys.exit(0 if (m[0] == o[0] + t[0] - b[0] and m[1] == o[1] + t[1] - b[1] and m[2] == o[2] and not m[3]) else 1)
