"""Quote the installed Claude Code bundle around literal byte patterns.

D-LANE-STARTUP1 evidence helper. It reads the CLI executable (a Bun build
whose JavaScript sits in the file as text) and prints, for each pattern, every
match's byte offset and a window of the surrounding source, so a design note
can cite the code a startup flag reaches. It never runs the CLI.

    uv run python -I tools/claude_bundle_quotes.py BINARY PATTERN [PATTERN ...]
        [--before N] [--after N] [--max N] [--count]

`--count` prints only the number of matches per pattern. Bun builds hold two
copies of some modules, so a pattern can match twice with the same text.
"""
from __future__ import annotations

import argparse
import mmap
import sys


def matches(data, needle: bytes, limit: int | None):
    found, start = 0, 0
    while limit is None or found < limit:
        index = data.find(needle, start)
        if index < 0:
            return
        yield index
        found += 1
        start = index + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary")
    parser.add_argument("patterns", nargs="+")
    parser.add_argument("--before", type=int, default=300)
    parser.add_argument("--after", type=int, default=600)
    parser.add_argument("--max", type=int, default=6)
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args(argv)
    with open(args.binary, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for pattern in args.patterns:
            needle = pattern.encode("utf-8")
            if args.count:
                total = sum(1 for _ in matches(data, needle, None))
                print(f"{total}\t{pattern}")
                continue
            for index in matches(data, needle, args.max):
                lo, hi = max(0, index - args.before), min(len(data), index + len(needle) + args.after)
                text = data[lo:hi].decode("utf-8", "replace")
                print(f"=== {pattern!r} @ {index}")
                print(text)
                print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
