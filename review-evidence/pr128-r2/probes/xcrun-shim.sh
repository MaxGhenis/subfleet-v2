#!/bin/sh
# Review-only: same compile, private module cache (avoids the shared cache contention).
if [ "$1" = "swiftc" ]; then shift; exec /usr/bin/xcrun swiftc -module-cache-path /private/tmp/pr128r2/mc "$@"; fi
exec /usr/bin/xcrun "$@"
