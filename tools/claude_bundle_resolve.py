"""Resolve a minified name in one Claude Code bundle module to its definition.

D-LANE-STARTUP1 evidence helper. A Bun build splits Claude Code into chunks
whose minified names repeat from chunk to chunk, so `function Ct(` matches
several unrelated functions. This finds the module that holds an anchor text,
reads its `import{...}from"<chunk>"` statements, picks the one module whose
`export{...}` list carries every name imported from that chunk, and prints the
definition of each requested name there. It never runs the CLI.

    python3 -I tools/claude_bundle_resolve.py BINARY ANCHOR NAME [NAME ...] [--after N]
"""
from __future__ import annotations

import argparse
import mmap
import re
import sys

MARK = b"// @bun @bytecode"


def module_bounds(data, offset: int) -> tuple[int, int]:
    lo = data.rfind(MARK, 0, offset)
    hi = data.find(MARK, offset)
    return (0 if lo < 0 else lo), (len(data) if hi < 0 else hi)


def imports_of(text: str) -> dict[str, dict[str, str]]:
    """chunk -> {local name: exported name}."""
    found: dict[str, dict[str, str]] = {}
    for match in re.finditer(r'import\{([^}]*)\}from"([^"]+)"', text):
        names = found.setdefault(match.group(2), {})
        for part in match.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            exported, _, local = part.partition(" as ")
            names[(local or exported).strip()] = exported.strip()
    return found


def exports_of(text: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"export\{([^}]*)\}", text):
        for part in match.group(1).split(","):
            part = part.strip()
            if part:
                local, _, exported = part.partition(" as ")
                names.add((exported or local).strip())
    return names


def definition(text: str, name: str) -> int:
    for pattern in (rf"(?<![\w$.])function {re.escape(name)}\(", rf"(?<![\w$.])async function {re.escape(name)}\(",
                    rf"(?<![\w$.])(?:var|let|const) {re.escape(name)}=", rf"[,;]{re.escape(name)}=(?!=)"):
        match = re.search(pattern, text)
        if match:
            return match.start()
    return -1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary")
    parser.add_argument("anchor")
    parser.add_argument("names", nargs="+")
    parser.add_argument("--after", type=int, default=500)
    args = parser.parse_args(argv)
    with open(args.binary, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        at = data.find(args.anchor.encode())
        if at < 0:
            print(f"anchor not found: {args.anchor!r}")
            return 1
        lo, hi = module_bounds(data, at)
        home = data[lo:hi].decode("utf-8", "replace")
        imports = imports_of(home)
        starts = [m.start() for m in re.finditer(re.escape(MARK), data)]
        bounds = [(s, starts[i + 1] if i + 1 < len(starts) else len(data)) for i, s in enumerate(starts)]
        export_cache: dict[int, tuple[set[str], str]] = {}

        def module(start: int, end: int) -> tuple[set[str], str]:
            if start not in export_cache:
                text = data[start:end].decode("utf-8", "replace")
                export_cache[start] = (exports_of(text), text)
            return export_cache[start]

        for name in args.names:
            local = definition(home, name)
            if local >= 0:
                print(f"=== {name}: defined in the anchor's module (@{lo + local})")
                print(home[local:local + args.after])
                print()
                continue
            chunk = next((c for c, names in imports.items() if name in names), None)
            if chunk is None:
                print(f"=== {name}: neither defined nor imported in the anchor's module")
                continue
            wanted = set(imports[chunk].values())
            exported = imports[chunk][name]
            hits = [(s, e) for s, e in bounds if wanted <= module(s, e)[0]]
            if len(hits) != 1:
                print(f"=== {name}: {len(hits)} modules export all of {chunk}'s names; not resolved")
                continue
            start, end = hits[0]
            text = module(start, end)[1]
            where = definition(text, exported)
            print(f"=== {name} (exported as {exported}) from {chunk} (module @{start}, def @{start + where})")
            print(text[where:where + args.after] if where >= 0 else "(definition not found by pattern)")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
