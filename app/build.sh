#!/bin/sh
# Build a local app bundle. Installation and launch are separate operator actions.
#
#   app/build.sh [--dev] [OUTPUT_DIRECTORY]
#
# The release build is OUTPUT/Subfleet.app (bundle id org.maxghenis.subfleet).
# --dev builds OUTPUT/SubfleetDev.app, shown as "Subfleet Dev", with bundle id
# org.maxghenis.subfleet.dev; that build refuses to connect to ~/.subfleet
# (design D-21, C-29.4). Its bundle directory and executable carry no space:
# the daemon's person-only check compares the first word of the caller's
# command line with SUBFLEET_DEV_APP_EXECUTABLE (docs/desktop/app-needs.md).
set -eu

usage() {
  echo "Usage: app/build.sh [--dev] [OUTPUT_DIRECTORY]"
  echo "Builds OUTPUT_DIRECTORY/Subfleet.app, or SubfleetDev.app with --dev (default: checkout/build)."
}

DEV=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dev) DEV=1; shift ;;
    --) shift; break ;;
    -*) usage >&2; exit 2 ;;
    *) break ;;
  esac
done
if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi
if [ "$(uname -s)" != Darwin ]; then
  echo "The Subfleet app requires macOS and the Swift developer tools." >&2
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

if [ "$DEV" -eq 1 ]; then
  NAME=SubfleetDev
  DISPLAY="Subfleet Dev"
  BUNDLE_ID=org.maxghenis.subfleet.dev
  FLAGS="-D SUBFLEET_DEV_BUILD"
else
  NAME=Subfleet
  DISPLAY=Subfleet
  BUNDLE_ID=org.maxghenis.subfleet
  FLAGS=""
fi

ARCH=$(uname -m)
# Swift's optional inner sandbox cannot start inside some managed sandboxes.
# This opt-in only affects compiler plugins; the caller's outer sandbox remains.
if [ "${SUBFLEET_SWIFT_NESTED_SANDBOX:-on}" = off ]; then
  FLAGS="$FLAGS -Xfrontend -disable-sandbox"
fi
SDK=${SDKROOT:-$(xcrun --sdk macosx --show-sdk-path)}
STAGING=$(mktemp -d "$OUTPUT/.subfleet-build.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM
APP="$STAGING/$NAME.app"
mkdir -p "$APP/Contents/MacOS"
# Every source under app/Sources (including subdirectories such as Views/) in
# one compilation. xargs appends the file list after the options.
# shellcheck disable=SC2086 # FLAGS is a word list on purpose.
find "$ROOT/app/Sources" -name '*.swift' -print0 | sort -z | \
  xargs -0 xcrun swiftc -O -whole-module-optimization -parse-as-library -sdk "$SDK" -target "$ARCH-apple-macos14.0" $FLAGS \
    -o "$APP/Contents/MacOS/$NAME"
cp "$ROOT/app/Info.plist" "$APP/Contents/Info.plist"
if [ "$DEV" -eq 1 ]; then
  plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$APP/Contents/Info.plist"
  plutil -replace CFBundleName -string "$DISPLAY" "$APP/Contents/Info.plist"
  plutil -replace CFBundleDisplayName -string "$DISPLAY" "$APP/Contents/Info.plist"
  plutil -replace CFBundleExecutable -string "$NAME" "$APP/Contents/Info.plist"
fi
plutil -lint "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
if [ -e "$OUTPUT/$NAME.app" ]; then
  rm -rf "$OUTPUT/$NAME.app"
fi
mv "$APP" "$OUTPUT/$NAME.app"
printf 'Built: %s/%s.app (%s)\n' "$OUTPUT" "$NAME" "$BUNDLE_ID"
