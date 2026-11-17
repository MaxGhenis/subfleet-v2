"""Extract the installed Claude Code bundle's `claudeMdExcludes` matcher as a node module.

D-LANE-STARTUP1 evidence helper. Claude Code 2.1.318 judges a `claudeMdExcludes`
pattern with `v8t()`:

    function v8t(){let e=gt().claudeMdExcludes;if(!e||e.length===0)return()=>!1;
      let n=FWo(de(),e).filter((s)=>s.length>0);if(n.length===0)return()=>!1;
      let r=p8t.default(n,{dot:!0});
      return(s,h)=>(h==="User"||h==="Project"||h==="Local")&&r(s.replaceAll("\\\\","/"))}

`p8t` is the bundled picomatch (`var p8t=Oe(DXt(),1)`), whose CommonJS modules
sit next to each other in one chunk (`rp` constants, `sp` utils, `rN` scan, `lN`
parse, `uN` picomatch, `DXt` the posix index). `FWo` adds a real-path spelling of
each absolute pattern. This copies those six modules and `FWo` byte for byte from
the binary, checks that they are contiguous and found exactly once, and writes one
CommonJS file exporting `picomatch` and `matcher(patterns)`, the latter being `v8t`
with `FWo`'s file-system helpers shimmed (see `SHIMS`). It never runs the CLI.

    python3 -I tools/claude_bundle_picomatch.py BINARY OUT.cjs

The output is third-party code extracted for a local comparison; it is written to
the path given (a temporary folder) and is not committed.
"""
from __future__ import annotations

import argparse
import mmap
import sys

MARK = b"// @bun @bytecode"
MODULES = ("rp", "sp", "rN", "lN", "uN", "DXt")
#: The text the posix index module ends with, closing `var DXt=O(function(...){...});`.
DXT_END = b"hN.exports=pN});"
FWO = b"function FWo(e,n){"
FWO_END = b"return r}"

#: Claude Code's `No(fs, path)` gives `{resolvedPath, isSymlink, isCanonical}` from
#: `realpathSync`, and the path itself when that throws; `DN` refuses network, UNC
#: and device paths (none in a fixture); `s2` is quoted from the bundle with `iF`
#: (a `/network/...` path test) reduced to false for local fixture paths; `Cx` is
#: `path.dirname` (the anchor module's `import{... dirname as Cx ...}from"path"`).
#: With `globalThis.__links` set, `No` reads that map instead of the disk, so a
#: recorded fixture's symlinks need no files.
SHIMS = r"""
const path = require("path");
const fs = require("fs");
const Cx = path.dirname;
function No(e, n) {
  if (globalThis.__links) {           // a recorded fixture's synthetic links, instead of the disk
    const r = globalThis.__links[n];
    return r === undefined ? {resolvedPath: n, isSymlink: false, isCanonical: false}
                           : {resolvedPath: r, isSymlink: true, isCanonical: true};
  }
  try { const r = e.realpathSync(n); return {resolvedPath: r, isSymlink: r !== n, isCanonical: true}; }
  catch (i) { return {resolvedPath: n, isSymlink: false, isCanonical: false}; }
}
function DN(e) { return false; }
function iF(e) { return false; }
function s2(e,n){if(!n.isSymlink||n.isCanonical)return!1;return!(n.resolvedPath===e&&iF(e))}
"""


def find_once(data, needle: bytes, start: int = 0, end: int | None = None) -> int:
    end = len(data) if end is None else end
    first = data.find(needle, start, end)
    if first < 0:
        raise SystemExit(f"not found: {needle!r}")
    if data.find(needle, first + 1, end) >= 0:
        raise SystemExit(f"found more than once: {needle!r}")
    return first


def extract(binary: str) -> str:
    with open(binary, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        # Minified names repeat across chunks, so each module is looked for once
        # inside the one bundle module (`// @bun @bytecode` to the next) that
        # defines the posix index `DXt`.
        index = find_once(data, b"var DXt=O(function(")
        lo, hi = data.rfind(MARK, 0, index), data.find(MARK, index)
        lo, hi = max(lo, 0), (len(data) if hi < 0 else hi)
        starts = [find_once(data, f"var {name}=O(function(".encode(), lo, hi) for name in MODULES]
        if starts != sorted(starts):
            raise SystemExit(f"modules out of order: {dict(zip(MODULES, starts))}")
        end = find_once(data, DXT_END, starts[-1], starts[-1] + 4096) + len(DXT_END)
        # Each module must end with `});` right before the next one starts.
        for name, nxt in zip(MODULES[1:], starts[1:]):
            if data[nxt - 3:nxt] != b"});":
                raise SystemExit(f"module before {name} does not end at its start")
        modules = data[starts[0]:end].decode("utf-8")
        fwo_at = find_once(data, FWO)
        fwo_end = find_once(data, FWO_END, fwo_at, fwo_at + 2048) + len(FWO_END)
        fwo = data[fwo_at:fwo_end].decode("utf-8")
    return "\n".join([
        "// Extracted from the Claude Code bundle by tools/claude_bundle_picomatch.py; not for commit.",
        "var O=(a,b)=>()=>(b||a((b={exports:{}}).exports,b),b.exports);",
        modules,
        SHIMS,
        fwo,
        "const picomatch = DXt();",
        # v8t, with gt().claudeMdExcludes as the argument and de() as node's fs.
        "function matcher(e){if(!e||e.length===0)return()=>!1;let n=FWo(fs,e).filter((s)=>s.length>0);"
        "if(n.length===0)return()=>!1;let r=picomatch(n,{dot:!0});return(s)=>r(s.replaceAll(\"\\\\\",\"/\"))}",
        "module.exports = {picomatch, matcher, FWo: (e) => FWo(fs, e)};",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary")
    parser.add_argument("out")
    args = parser.parse_args(argv)
    text = extract(args.binary)
    with open(args.out, "x", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {len(text)} characters to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
