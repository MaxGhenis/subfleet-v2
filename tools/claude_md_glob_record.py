"""Record Claude Code's own `claudeMdExcludes` verdicts on generated lane-startup cases.

D-LANE-STARTUP1 evidence helper. It builds pattern sets with Subfleet's
`job_startup_settings` for synthetic workdirs and homes whose names carry spaces,
non-ASCII letters and every glob metacharacter, picks paths inside and outside each
repository root (and decoys a widened pattern would catch), and asks the installed
bundle's matcher (`tools/claude_bundle_picomatch.py`: picomatch and `FWo` copied
from the binary, run under node) for each verdict. The same cases go through
`tests/claude_md_glob.py`; the tool prints the agreement and writes the cases and the
bundle's verdicts as a fixture, which `tests/unit/test_lane_startup_glob.py` replays
without node.

    uv run python tools/claude_md_glob_record.py BINARY WORK_DIR OUT.json [--sets N]

WORK_DIR receives the extracted matcher (third-party code, not for commit). Paths
are synthetic, under `/sf-lsb-glob`, and never touched: symlinks are a map that the
matcher's `No` shim and the Python oracle both read. Nothing runs the CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from subfleet.adapters import claude  # noqa: E402
from tests import claude_md_glob as oracle  # noqa: E402
from tools.claude_bundle_picomatch import extract  # noqa: E402

BASE = "/sf-lsb-glob"
#: Letters for generated folder names: plain ones, a space, non-ASCII, and every
#: character picomatch or `FWo` reads specially.
ALPHABET = list("abcxyz") + [" ", "é", "ß", "名", "*", "?", "[", "]", "{", "}", "(", ")", "!", "|", ",",
                             "+", "@", "^", "$", ".", "-", "#", "~", "'", "\\", "&", "=", '"', "`", "<",
                             ">", "%", ";", ":", "\t"]

HARNESS = r"""
const m = require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = input.sets.map((set) => {
  globalThis.__links = set.links;
  const test = m.matcher(set.patterns);
  return {fwo: m.FWo(set.patterns), verdicts: set.paths.map((p) => test(p))};
});
process.stdout.write(JSON.stringify(out));
"""


def name(rng: random.Random, low: int = 1, high: int = 6) -> str:
    text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(low, high)))
    # A folder name is never `.` or `..`, and holds no `/`.
    return text if text not in (".", "..") else text + "a"


def mutations(text: str) -> list[str]:
    out = set()
    for index in range(len(text)):
        out.add(text[:index] + ("x" if text[index] != "x" else "y") + text[index + 1:])
        out.add(text[:index] + text[index + 1:])
        out.add(text[:index] + text[index] + text[index:])
    out.discard(text)
    return sorted(item for item in out if item and "/" not in item)


def build_set(rng: random.Random, number: int) -> dict:
    depth = rng.randint(1, 4)
    parts = [name(rng) for _ in range(depth)]
    workdir = "/".join([BASE, f"s{number:03d}", *parts, "repo" + name(rng, 0, 3)])
    links: dict[str, str] = {}
    real = workdir
    if number % 3 == 0:                 # a symlinked spelling: R_real differs from R
        real = "/".join([BASE, "real", f"s{number:03d}", name(rng)])
        links[workdir] = real
    home = "/".join([BASE, f"home{number:03d}", name(rng)])
    env = {"HOME": home}
    if number % 4 == 1:
        env["CLAUDE_CONFIG_DIR"] = "/".join([BASE, f"cfg{number:03d}", name(rng)])
    with mock.patch.object(claude.os.path, "realpath", lambda path, **_: links.get(path, path)):
        settings = claude.job_startup_settings(workdir, env)
    patterns = settings["claudeMdExcludes"]
    roots = list(dict.fromkeys((workdir, real)))
    expect: dict[str, str] = {}
    folded = [root.replace("\\", "/") for root in roots]

    def add(label: str, *paths: str) -> None:
        for path in paths:
            # Claude Code turns `\` into `/` in patterns and paths alike, so a path
            # that folds to one under a root's folded spelling is under that root to
            # its matcher (`X/**` matches `X` and `X/…`).
            fold = path.replace("\\", "/")
            under = any(fold == root or fold.startswith(root + "/") for root in folded)
            expect.setdefault(path, "backslash-fold" if label == "exclude" and under else label)
    for root in roots:                   # the repository's own guidance, in either spelling
        add("keep", f"{root}/CLAUDE.md", f"{root}/sub/CLAUDE.md", f"{root}/AGENTS.md", f"{root}/.claude/AGENTS.md",
            f"{root}/.claude/rules/x.md", f"{root}/.claude/CLAUDE.md", f"{root}/CLAUDE.local.md",
            f"{root}/{name(rng)}/CLAUDE.md", f"{root}/a\nb/CLAUDE.md", root.replace("/", "\\") + "\\CLAUDE.md")
    for root in roots:
        parent, leaf = root.rsplit("/", 1)
        add("exclude", *[f"{parent}/{variant}/CLAUDE.md" for variant in mutations(leaf)[:6]])
        add("exclude", f"{parent}/{claude.glob_literal(leaf)}/CLAUDE.md", f"{root}x/CLAUDE.md", f"{root} /CLAUDE.md")
        # Outside the root, in a folder whose name holds a line terminator: JavaScript's
        # `.` matches none, so the negation cannot judge the path (a documented residue).
        add("line-terminator", f"{parent}/{leaf}\n/CLAUDE.md", f"{root}\u2028x/CLAUDE.md")
        add("dot-segment", f"{root}/./CLAUDE.md", f"{root}/../CLAUDE.md")    # never built (`path.join`)
        folder = root
        while folder.rsplit("/", 1)[0]:
            folder = folder.rsplit("/", 1)[0]
            add("exclude", *[f"{folder}/{item.replace('/**', '/x.md')}" for item in claude.STARTUP_FILE_NAMES])
    for folder in (home, *[value for key, value in env.items() if key == "CLAUDE_CONFIG_DIR"]):
        add("exclude", f"{folder}/CLAUDE.md", f"{folder}/rules/r.md", f"{folder}/.claude/CLAUDE.md",
            f"{folder}/.claude/rules/r.md", f"{folder}/AGENTS.md")
        add("line-terminator", f"{folder}/x\ny/CLAUDE.md")
    add("exclude", f"{BASE}/other/{name(rng)}/CLAUDE.md", "/CLAUDE.md", "/AGENTS.md", "relative/CLAUDE.md")
    add("empty", "")
    return {"workdir": workdir, "env": env, "links": links, "patterns": patterns,
            "paths": list(expect), "expect": list(expect.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary")
    parser.add_argument("work")
    parser.add_argument("out")
    parser.add_argument("--sets", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261115)
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)
    sets = [build_set(rng, number) for number in range(args.sets)]
    module = Path(args.work) / "claude-picomatch-record.cjs"
    module.write_text(extract(args.binary), encoding="utf-8")
    payload = json.dumps({"sets": [{key: item[key] for key in ("patterns", "links", "paths")} for item in sets]})
    done = subprocess.run(["node", "-e", HARNESS, str(module)], input=payload, capture_output=True, text=True,
                          timeout=600, check=True)
    results = json.loads(done.stdout)
    total = agree = 0
    disagreements = []
    for item, result in zip(sets, results):
        item["fwo"], item["verdicts"] = result["fwo"], result["verdicts"]
        resolve = lambda path, links=item["links"]: links.get(path, path)  # noqa: E731
        if oracle.fwo(item["patterns"], resolve) != item["fwo"]:
            disagreements.append(("fwo", item["workdir"]))
        test = oracle.matcher(item["patterns"], resolve)
        for path, verdict in zip(item["paths"], item["verdicts"]):
            total += 1
            if test(path) == verdict:
                agree += 1
            elif len(disagreements) < 20:
                disagreements.append((item["workdir"], path, verdict))
    with open(args.binary, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    record = {
        "bundle": os.path.basename(args.binary), "binary_sha256": digest,
        "node": subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
        "recorded_by": f"uv run python tools/claude_md_glob_record.py <claude {os.path.basename(args.binary)}> "
                       f"<work> <out> --sets {args.sets} --seed {args.seed}",
        "sets": sets,
    }
    with open(args.out, "x", encoding="utf-8") as handle:
        handle.write('{\n "bundle": ' + json.dumps(record["bundle"]) + ',\n "binary_sha256": '
                     + json.dumps(digest) + ',\n "node": ' + json.dumps(record["node"])
                     + ',\n "recorded_by": ' + json.dumps(record["recorded_by"]) + ',\n "sets": [\n'
                     + ",\n".join("  " + json.dumps(item, ensure_ascii=False, sort_keys=True) for item in sets)
                     + "\n ]\n}\n")
    excluded = sum(v for s in sets for v in s["verdicts"])
    print(f"sets {len(sets)} paths {total} agree {agree} disagree {total - agree} "
          f"(bundle excluded {excluded}, kept {total - excluded}); fwo mismatches "
          f"{sum(1 for d in disagreements if d[0] == 'fwo')}")
    tally: dict[tuple[str, bool], int] = {}
    for item in sets:
        for label, verdict in zip(item["expect"], item["verdicts"]):
            tally[label, verdict] = tally.get((label, verdict), 0) + 1
    print("bundle verdicts by label (label, excluded): "
          + ", ".join(f"{key[0]}/{key[1]}={count}" for key, count in sorted(tally.items())))
    for item in disagreements:
        print("DISAGREE", json.dumps(item, ensure_ascii=False))
    return 0 if agree == total and not disagreements else 1


if __name__ == "__main__":
    sys.exit(main())
