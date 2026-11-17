"""Fix round 2 evidence (review r2 of #326, P1 1): the bundle's matcher on the deepest paths.

Builds `job_startup_settings` for the review's case (400 one-character folders, an
839-byte path) and for `PATH_MAX - 1`-byte paths of one-character folders whose
character the escapes or JSON widen most, each with a real path as long again (so up
to four spellings of R). Paths are synthetic and never touched: `realpath` is a map,
and git is not run. Claude Code 2.1.318's own matcher (picomatch and `FWo`, extracted
by `tools/claude_bundle_picomatch.py`, run under node) then judges, for every
spelling, every ancestor's six names (excluded), R's own files (kept) and decoys
beside R (excluded). It prints each case's payload size, pattern count, compile time
and verdict tally, and exits 1 on any verdict that differs from those expectations.

    python -I deep_bundle_check.py WORKTREE BINARY WORK_DIR
"""
from __future__ import annotations

import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from unittest import mock

HARNESS = r"""
const m = require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = input.map((set) => {
  globalThis.__links = set.links;
  const t0 = Date.now();
  const test = m.matcher(set.patterns);
  const verdicts = set.paths.map((p) => test(p));
  return {ms: Date.now() - t0, verdicts};
});
process.stdout.write(JSON.stringify(out));
"""
PATH_MAX = 1024
FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "AGENTS.md", ".claude/AGENTS.md", ".claude/rules/x.md")


def fill(piece: str, prefix: str = "") -> str:
    path = prefix
    while len((path + piece).encode()) < PATH_MAX:
        path += piece
    return path


def main() -> int:
    worktree, binary, work = (Path(arg) for arg in sys.argv[1:4])
    sys.path.insert(0, str(worktree))
    from subfleet.adapters import claude
    from tools.claude_bundle_picomatch import extract
    cases = [("review-400", "/" + "x" * 38 + "/x" * 400, None)]
    for label, piece in (("plain", "/x"), ("quote", '/"'), ("comma", "/,"), ("control", "/\x01"),
                         ("nfd-control", "/é\x01"), ("nfd-quote", '/é"'), ("brace", "/{"),
                         ("cjk", "/名")):
        cases.append((label, fill(piece), fill(piece, prefix="/r")))
    sets = []
    for label, workdir, real in cases:
        links = {workdir: real} if real else {}
        with mock.patch.object(claude.os.path, "realpath", lambda path, **_: links.get(path, path)), \
                mock.patch.object(claude, "_job_git", lambda *args: None):
            settings = claude.job_startup_settings(workdir, {"HOME": "/sf-deep-home/h"})
        size = len(json.dumps(claude.lane_settings(settings), separators=(",", ":")).encode())
        root, other = workdir, links.get(workdir, workdir)
        spellings = list(dict.fromkeys((root, other, unicodedata.normalize("NFC", root),
                                        unicodedata.normalize("NFC", other))))
        expect: dict[str, bool] = {}
        for spelling in spellings:
            parts = spelling.split("/")[1:]
            for depth in range(1, len(parts)):
                folder = "/" + "/".join(parts[:depth])
                for name in FILES:
                    expect.setdefault(f"{folder}/{name}", True)
            for name in (*FILES, "sub/CLAUDE.md", "a/b/AGENTS.md"):
                expect[f"{spelling}/{name}"] = False
            parent, leaf = spelling.rsplit("/", 1)
            for decoy in (leaf + "x", leaf[:-1] + "y", leaf + " "):
                expect.setdefault(f"{parent}/{decoy}/CLAUDE.md", True)
        sets.append({"label": label, "size": size, "patterns": settings["claudeMdExcludes"],
                     "links": {}, "paths": list(expect), "expect": list(expect.values()),
                     "spellings": len(spellings), "path_bytes": len(workdir.encode())})
    module = work / "claude-picomatch-deep.cjs"
    module.write_text(extract(str(binary)), encoding="utf-8")
    payload = json.dumps([{key: item[key] for key in ("patterns", "links", "paths")} for item in sets])
    done = subprocess.run(["node", "-e", HARNESS, str(module)], input=payload, capture_output=True, text=True,
                          timeout=600, check=True)
    bad = 0
    for item, result in zip(sets, json.loads(done.stdout)):
        wrong = [path for path, want, got in zip(item["paths"], item["expect"], result["verdicts"]) if want != got]
        bad += len(wrong)
        print(f"{item['label']}: path {item['path_bytes']} bytes, {item['spellings']} spellings, payload "
              f"{item['size']} bytes, {len(item['patterns'])} patterns, {len(item['paths'])} paths "
              f"({sum(item['expect'])} excluded, {len(item['paths']) - sum(item['expect'])} kept expected), "
              f"{len(wrong)} differ, {result['ms']} ms")
        for path in wrong[:3]:
            print("  DIFFER", json.dumps(path, ensure_ascii=False)[:200])
    print("all ok" if bad == 0 else f"{bad} verdicts differ")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
