"""Fix round 1 evidence (review r1 of #326, P2 2): an NFD repository's own guidance, judged by the bundle.

Builds, under WORK, a fake home and two real repositories: `café` with its `é`
decomposed (NFD) and the ASCII control `cafe`, each holding `CLAUDE.md`,
`AGENTS.md` and `sub/CLAUDE.md`. For each repository and each workdir (its top and
`sub`, each given as stored and in NFC) it takes the patterns from TREE's
`job_startup_settings`, then asks node, as Claude Code 2.1.318 does, for
`process.cwd()` normalized to NFC (`Rn`) and judges paths built from it with the
bundle's own matcher (`tools/claude_bundle_picomatch.py`: picomatch and `FWo`,
which reads the real disk here). It prints one line per case and exits 1 unless
every repository file is kept and every personal or outside file excluded.

    HOME=WORK/home uv run --no-sync python -I nfd_bundle_check.py TREE BINARY WORK

Nothing runs the Claude CLI; WORK must be a new scratch folder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata

HARNESS = r"""
const m = require(process.argv[1]);
const path = require("path");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = input.map((c) => {
  process.chdir(c.workdir);
  const raw = process.cwd();
  const cwd = raw.normalize("NFC");                    // Claude Code's `Rn(process.cwd())`
  const top = c.sub ? path.dirname(cwd) : cwd;
  const test = m.matcher(c.patterns);
  const judge = (p) => ({path: p, excluded: test(p)});
  return {cwd, rawIsNfd: raw !== cwd && raw === raw.normalize("NFD"), kept: c.own.map((rel) => judge(path.join(top, rel))),
          excluded: [...c.outside.map((rel) => judge(path.join(top, rel))), ...c.personal.map(judge)]};
});
process.stdout.write(JSON.stringify(out));
"""

OWN = ["CLAUDE.md", "AGENTS.md", "sub/CLAUDE.md", ".claude/CLAUDE.md"]
OUTSIDE = ["../CLAUDE.md", "../AGENTS.md", "../other/CLAUDE.md"]


def main(argv: list[str]) -> int:
    tree, binary, work = (os.path.abspath(item) for item in argv[1:4])
    sys.path.insert(0, tree)
    from subfleet.adapters.claude import job_startup_settings
    from tools.claude_bundle_picomatch import extract
    os.makedirs(work, exist_ok=False)
    module = os.path.join(work, "claude-picomatch-nfd.cjs")
    with open(module, "x", encoding="utf-8") as handle:
        handle.write(extract(binary))
    home = os.path.join(work, "home")
    os.makedirs(os.path.join(home, ".claude"))
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    cases = []
    for label, leaf in (("nfd", unicodedata.normalize("NFD", "café")), ("ascii", "cafe")):
        repo = os.path.join(work, "places", leaf)
        os.makedirs(os.path.join(repo, "sub"))
        subprocess.run(["git", "init", "-q", repo], check=True, env=git_env, timeout=60)
        for rel in ("CLAUDE.md", "AGENTS.md", "sub/CLAUDE.md"):
            with open(os.path.join(repo, rel), "x", encoding="utf-8") as handle:
                handle.write("guidance\n")
        for sub in (False, True):
            stored = os.path.join(repo, "sub") if sub else repo
            for given in ("as-stored", "nfc"):
                workdir = stored if given == "as-stored" else unicodedata.normalize("NFC", stored)
                if label == "ascii" and given == "nfc":
                    continue
                patterns = job_startup_settings(workdir, {"HOME": home})["claudeMdExcludes"]
                cases.append({"label": label, "given": given, "sub": sub, "workdir": workdir,
                              "patterns": patterns, "own": OWN, "outside": OUTSIDE,
                              "personal": [f"{home}/CLAUDE.md", f"{home}/AGENTS.md", f"{home}/.claude/CLAUDE.md",
                                           "/CLAUDE.md"]})
    done = subprocess.run(["node", "-e", HARNESS, module], input=json.dumps(cases), capture_output=True,
                          text=True, timeout=300, check=True)
    ok = True
    for case, result in zip(cases, json.loads(done.stdout)):
        kept = [item for item in result["kept"] if not item["excluded"]]
        excluded = [item for item in result["excluded"] if item["excluded"]]
        good = len(kept) == len(result["kept"]) and len(excluded) == len(result["excluded"])
        ok &= good
        print(f"{case['label']:5} workdir={'sub' if case['sub'] else 'top'}:{case['given']:9} "
              f"process.cwd()-is-NFD={result['rawIsNfd']} patterns={len(case['patterns'])} "
              f"own kept {len(kept)}/{len(result['kept'])} "
              f"[{', '.join(os.path.relpath(i['path'], os.path.dirname(result['cwd']) if case['sub'] else result['cwd']) + ('=kept' if not i['excluded'] else '=EXCLUDED') for i in result['kept'])}] "
              f"outside+personal excluded {len(excluded)}/{len(result['excluded'])} {'ok' if good else 'FAIL'}")
    print("all ok" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
