#!/bin/sh
# Shadow-week canary runbook (docs/decisions/2026-09-05-cutover-prerequisites.md, "Execution order").
# One phase per invocation so each step's output is read before the next runs:
#   tools/canary_runbook.sh import | daemon | transfer-dry-run | transfer | canary | live | soak | verify
# Nothing here touches v1 except through `sf2 lanes transfer` (the roster record and the home rename).
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
SF2="$REPO/bin/sf2"
PY="$REPO/.venv/bin/python"
export SUBFLEET_HOME="${SUBFLEET_HOME:-$HOME/.subfleet}"
CANARY="$HOME/subfleet-v2-canary"
LANE="${LANE:-codex-3}"
LIVE_ACCOUNT="${LIVE_ACCOUNT:-max@thesisinstitute.org}"
PLIST="$HOME/Library/LaunchAgents/com.subfleet.soak-report.plist"
mkdir -p "$CANARY"

case "${1:-}" in
  import)
    # Every lane arrives owner: v1 (migration.md shadow week, step 2). Run before the daemon starts.
    "$PY" -E -P -m subfleet.importer --state-root "$SUBFLEET_HOME" --json > "$CANARY/import-report.json"
    "$PY" -c "import json; r=json.load(open('$CANARY/import-report.json')); print(json.dumps({k: r[k] for k in r if k in ('imported_total','skipped_total','dry_run','stores')}, indent=1))" | head -60
    ;;
  daemon)
    "$SF2" daemon install            # launchd com.subfleet.daemon, python -E -P, no --hooks
    "$SF2" daemon status
    "$SF2" doctor || true            # rows are read; a fail here is read, not skipped
    ;;
  transfer-dry-run)
    "$SF2" lanes transfer "$LANE" --to v2 --dry-run
    ;;
  transfer)
    "$SF2" lanes transfer "$LANE" --to v2 --i-understand-v1-edit
    date -u +'{"since": "%Y-%m-%dT%H:%M:%SZ", "lane": "'"$LANE"'"}' > "$SUBFLEET_HOME/soak.json"
    cat "$SUBFLEET_HOME/soak.json"
    "$0" verify
    ;;
  verify)
    n="${LANE#codex-}"
    if [ -d "$HOME/.codex-$n" ]; then echo "FAIL: $HOME/.codex-$n still exists"; exit 1; fi
    [ -f "$SUBFLEET_HOME/lanes/$LANE/auth.json" ] && echo "ok: home relocated to $SUBFLEET_HOME/lanes/$LANE"
    if subfleet status 2>/dev/null | /usr/bin/grep -q "codex-$n"; then echo "FAIL: v1 status still lists ~/.codex-$n"; exit 1; else echo "ok: v1 status no longer lists ~/.codex-$n"; fi
    "$SF2" lanes list --json 2>/dev/null | "$PY" -c "import json,sys; rows=json.load(sys.stdin); rows=rows.get('lanes', rows) if isinstance(rows, dict) else rows; print([ (r['lane_id'], r['owner'], r.get('home')) for r in rows if r['lane_id']=='$LANE'])"
    "$PY" -c "import json; print(json.load(open('$HOME/chief-of-staff/subfleet/codex-accounts.json')).get('transferred_to_v2'))"
    ;;
  enroll-logins)
    # Each completed full-scope login under ~/.subfleet/logins/<email>/ becomes a v2 home lane
    # (C-10.2), held until its account transfers from v1 (C-9.6 operator hold): measured by the
    # usage sensor every cycle, never a dispatch candidate while v1 still owns the account.
    for d in "$HOME"/.subfleet/logins/*/; do
      e="$(basename "$d")"
      who="$(CLAUDE_CONFIG_DIR="$d" claude auth status --json 2>/dev/null | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("email") or "" if d.get("loggedIn") else "")')"
      if [ "$who" != "$e" ]; then echo "skip $e: $( [ -n "$who" ] && echo "logged in as $who" || echo "not logged in")"; continue; fi
      if "$SF2" lanes list --json 2>/dev/null | "$PY" -c 'import json,sys; rows=json.load(sys.stdin); rows=rows.get("lanes", rows) if isinstance(rows, dict) else rows; sys.exit(0 if any(r.get("credential_ref")==sys.argv[1] for r in rows) else 1)' "${d%/}"; then echo "already enrolled: $e"; continue; fi
      line="$("$SF2" lanes enroll "${d%/}")" || { echo "enroll failed: $e"; continue; }
      echo "$line"; lane="${line%% *}"
      "$SF2" lanes hold "$lane" --until 2027-01-01T00:00:00Z
    done
    "$SF2" status | head -40
    ;;
  canary)
    [ -d "$CANARY/work/.git" ] || git clone -q https://github.com/MaxGhenis/subfleet-v2.git "$CANARY/work"
    "$PY" "$REPO/tools/canary_submit.py" --dry-run | head -3
    "$PY" "$REPO/tools/canary_submit.py"
    "$SF2" status | head -30
    ;;
  live)
    cd "$REPO" && SUBFLEET_LIVE=1 SUBFLEET_LIVE_CLAUDE_ACCOUNT="$LIVE_ACCOUNT" uv run pytest -q tests/live
    ;;
  soak)
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.subfleet.soak-report</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-E</string><string>-P</string><string>$REPO/tools/soak_report.py</string></array>
  <key>EnvironmentVariables</key><dict><key>SUBFLEET_HOME</key><string>$SUBFLEET_HOME</string><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$SUBFLEET_HOME/soak-report.log</string>
  <key>StandardErrorPath</key><string>$SUBFLEET_HOME/soak-report.log</string>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)/com.subfleet.soak-report" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl print "gui/$(id -u)/com.subfleet.soak-report" | /usr/bin/grep -E "state|program" | head -3
    "$PY" -E -P "$REPO/tools/soak_report.py" || echo "soak report: not clean (exit $?)"
    ;;
  *)
    echo "usage: $0 import | daemon | enroll-logins | transfer-dry-run | transfer | canary | live | soak | verify"; exit 2 ;;
esac
