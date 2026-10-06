#!/bin/sh
# Review-only: same compile, private module cache (avoids shared-cache contention).
if [ "$1" = "swiftc" ]; then shift; exec /usr/bin/xcrun swiftc -module-cache-path /Users/maxghenis/.subfleet/worktrees/20261005-185509-pr128-review-r3/.review-tmp/mc "$@"; fi
exec /usr/bin/xcrun "$@"
