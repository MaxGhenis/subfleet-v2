#!/bin/sh
# Build a local app bundle. Installation and launch are separate operator actions.
set -eu

case "${1-}" in
  -h|--help)
    echo "Usage: app/build.sh [OUTPUT_DIRECTORY]"
    echo "Builds OUTPUT_DIRECTORY/Subfleet.app (default: checkout/build)."
    exit 0
    ;;
esac
if [ "$#" -gt 1 ]; then
  echo "Usage: app/build.sh [OUTPUT_DIRECTORY]" >&2
  exit 2
fi
if [ "$(uname -s)" != Darwin ]; then
  echo "The Subfleet menu bar app requires macOS and the Swift developer tools." >&2
  exit 1
fi

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
OUTPUT=${1:-"$ROOT/build"}
case "$OUTPUT/" in
  /Applications/*|"$HOME/Applications/"*)
    echo "Build to a local output directory; installation into Applications is a separate action." >&2
    exit 2
    ;;
esac
mkdir -p "$OUTPUT"
OUTPUT=$(CDPATH= cd "$OUTPUT" && pwd -P)
case "$OUTPUT/" in
  /Applications/*|"$HOME/Applications/"*)
    echo "Build to a local output directory; installation into Applications is a separate action." >&2
    exit 2
    ;;
esac

ARCH=$(uname -m)
SDK=${SDKROOT:-$(xcrun --sdk macosx --show-sdk-path)}
STAGING=$(mktemp -d "$OUTPUT/.subfleet-build.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM
APP="$STAGING/Subfleet.app"
mkdir -p "$APP/Contents/MacOS"
xcrun swiftc -O -parse-as-library -sdk "$SDK" -target "$ARCH-apple-macos14.0" \
  "$ROOT/app/SubfleetApp.swift" -o "$APP/Contents/MacOS/Subfleet"
cp "$ROOT/app/Info.plist" "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
if [ -e "$OUTPUT/Subfleet.app" ]; then
  rm -rf "$OUTPUT/Subfleet.app"
fi
mv "$APP" "$OUTPUT/Subfleet.app"
printf 'Built: %s/Subfleet.app\n' "$OUTPUT"
