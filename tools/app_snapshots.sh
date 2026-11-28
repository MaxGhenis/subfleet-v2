#!/bin/sh
# Foreground only. No daemon, installed app, live preferences or visible window.
set -eu
ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
OUT=${1:?Usage: tools/app_snapshots.sh OUTDIR}
mkdir -p "$OUT" "$ROOT/build/visual-snapshots"
SOURCE=${SF_SNAPSHOT_SOURCE_ROOT:-"$ROOT/app/Sources"}
ARCH=$(uname -m)
FLAGS=""
if [ "$SOURCE" != "$ROOT/app/Sources" ]; then FLAGS="-D SUBFLEET_VISUAL_BASELINE"; fi
find "$SOURCE" -name '*.swift' -print0 | sort -z | \
  xargs -0 xcrun swiftc $FLAGS -parse-as-library -D SUBFLEET_VIEW_TEST -target "$ARCH-apple-macos14.0" \
  "$ROOT/tests/frontend/SnapshotProbe.swift" -o "$ROOT/build/visual-snapshots/probe"
SUBFLEET_HOME="$ROOT/build/visual-snapshots/home" \
  "$ROOT/build/visual-snapshots/probe" "$OUT" "$ROOT/tests/fixtures/visual/progress.json"
