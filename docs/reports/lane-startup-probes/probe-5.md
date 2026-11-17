# Probe 5: do `--settings` hooks fire, and does `disableAllHooks:false` beat a repo's `true`? (D-LANE-STARTUP1, F7)

Dispatch: one writable haiku lane, today's daemon, any throwaway workdir under
`/Users/maxghenis`. Three nested `claude -p` runs, each allowed only the Bash tool,
and each asked to run `echo LSP-HELLO` once.

The hook under test is a marker hook the lane writes. It never runs the command: it
denies with a fixed reason. If a hypothesis is wrong, the worst outcome is that
`echo LSP-HELLO` runs. The real never-rules guard is **not** exercised here
(probe 6 checks that it is present).

## Brief for the lane

Run these commands exactly, in your workdir, with Bash in the foreground. Do not
print any environment value.

```bash
set -u
W="$PWD"
case "$W" in /Users/maxghenis/*) ;; *) echo "WORKDIR-NOT-UNDER-HOME $W"; exit 3 ;; esac
mkdir "$W/lsp5-a" "$W/lsp5-b" "$W/lsp5-c"
for S in "$W/lsp5-a" "$W/lsp5-b" "$W/lsp5-c"; do git -C "$S" init -q; done
printf '%s\n' '#!/bin/sh' 'cat >/dev/null' \
 'printf "%s\n" "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"LSP-MARKER-DENY\"}}"' \
 > "$W/lsp5-marker-hook.sh"
chmod 755 "$W/lsp5-marker-hook.sh"
for S in "$W/lsp5-b" "$W/lsp5-c"; do mkdir "$S/.claude"; printf '{"disableAllHooks": true}\n' > "$S/.claude/settings.json"; done
HOOK="{\"PreToolUse\":[{\"matcher\":\"Bash\",\"hooks\":[{\"type\":\"command\",\"command\":\"$W/lsp5-marker-hook.sh\",\"timeout\":30}]}]}"
Q='Run the Bash command `echo LSP-HELLO` exactly once. Then reply with one line: the exact output, or the exact error or denial text you received.'
run() {  # $1 dir, $2.. extra flags
  S="$1"; shift
  ( cd "$S" && claude -p --model claude-haiku-5-5 --output-format text --tools Bash \
      --dangerously-skip-permissions --no-session-persistence \
      --strict-mcp-config --mcp-config '{"mcpServers":{}}' "$@" "$Q" ) > "$S.out.txt" 2> "$S.err.txt"
  echo "== $(basename "$S") rc=$?"; cat "$S.out.txt"; echo "stderr-bytes=$(wc -c < "$S.err.txt")"
}
run "$W/lsp5-a" --setting-sources project,local --settings "{\"env\":{\"CLAUDE_CODE_RETRY_WATCHDOG\":\"0\"},\"hooks\":$HOOK}"
run "$W/lsp5-b" --settings "{\"env\":{\"CLAUDE_CODE_RETRY_WATCHDOG\":\"0\"},\"disableAllHooks\":false,\"hooks\":$HOOK}"
run "$W/lsp5-c" --settings "{\"env\":{\"CLAUDE_CODE_RETRY_WATCHDOG\":\"0\"},\"hooks\":$HOOK}"
python3 -I -c 'import shutil,sys,os; [shutil.rmtree(p) for p in sys.argv[1:] if os.path.isdir(p) and os.path.basename(p).startswith("lsp5-")]' "$W/lsp5-a" "$W/lsp5-b" "$W/lsp5-c"
rm -f "$W/lsp5-marker-hook.sh"
```

Reply with the printed output, verbatim, and nothing else.

## What each answer proves

| Run | Expected | If instead |
|---|---|---|
| `lsp5-a`: user source dropped, marker hook in inline `--settings` | text containing `LSP-MARKER-DENY` | `LSP-HELLO`: inline `--settings` hooks do not fire on 2.1.318 `-p`. Design B is unsafe. Also review the writable-review publish hook (`review_publish.claude_settings_args`), which relies on the same mechanism and has never been shown live. |
| `lsp5-b`: repo `.claude/settings.json` sets `disableAllHooks:true`; `--settings` sets `false` plus the marker | `LSP-MARKER-DENY` | `LSP-HELLO`: the flag override does not win. Design A's I1 hardening is ineffective, and the repo-file gap stays open for job and conversation launches alike (C-26.11's premise fails). |
| `lsp5-c`: same repo file, no flag override | `LSP-HELLO` (hooks switched off by the repo file) | `LSP-MARKER-DENY`: F7's gap is not real on 2.1.318. `disableAllHooks:false` stays harmless but is not needed. |
