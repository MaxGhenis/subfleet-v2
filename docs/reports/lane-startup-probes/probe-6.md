# Probe 6: the never-rules guard still runs on a Design A launch (D-LANE-STARTUP1, constraint 1)

Dispatch: one writable haiku lane, today's daemon, with a throwaway workdir **under
`/Users/maxghenis`**. It needs `lsp-settings.py` from probe 3 (create it as probe 3
does). Two nested `claude -p` runs, each allowed only Bash and asked to run
`echo LSP-HELLO` once, with Claude Code's hook debug log on:

- run (a) carries Design A's settings;
- run (b) is a control with today's settings.

The guard is shown by its own entry running in the debug log. Nothing denied is
attempted: `echo` is not something the guard blocks.

## Brief for the lane

Run these commands exactly, in your workdir, with Bash in the foreground. Do not
print any environment value or any whole debug-log line. Print only the counts and
the command tokens the commands below extract.

```bash
set -u
W="$PWD"
case "$W" in /Users/maxghenis/*) ;; *) echo "WORKDIR-NOT-UNDER-HOME $W"; exit 3 ;; esac
test -f "$W/lsp-settings.py" || { echo "MISSING lsp-settings.py: create it as in probe 3"; exit 3; }
Q='Run the Bash command `echo LSP-HELLO` exactly once. Then reply with one line: the exact output, or the exact error or denial text you received.'
for V in a b; do
  S="$W/lsp6-$V"; mkdir "$S"; git -C "$S" init -q
  printf '# Probe repo\nLSP-REPO-CANARY-CLAUDE: repository guidance loaded.\n' > "$S/CLAUDE.md"
  if [ "$V" = a ]; then SETTINGS="$(python3 -I "$W/lsp-settings.py" "$S" both)"
  else SETTINGS='{"env":{"CLAUDE_CODE_RETRY_WATCHDOG":"0"}}'; fi
  ( cd "$S" && claude -p --model claude-haiku-5-5 --output-format text --tools Bash \
      --dangerously-skip-permissions --no-session-persistence \
      --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --debug hooks --debug-file "$S.debug.log" --settings "$SETTINGS" "$Q" ) > "$S.out.txt" 2> "$S.err.txt"
  echo "== lsp6-$V rc=$?"; cat "$S.out.txt"
  echo "guard-lines=$(grep -c -E 'never-rules|never_rules|subfleet-guard' "$S.debug.log" 2>/dev/null || echo 0)"
  echo "guard-tokens=$(grep -o -E '[^ \"'\'']*(never-rules|never_rules|subfleet-guard)[^ \"'\'']*' "$S.debug.log" 2>/dev/null | sort -u | head -3 | tr '\n' ' ')"
  echo "pretooluse-lines=$(grep -c 'PreToolUse' "$S.debug.log" 2>/dev/null || echo 0)"
done
python3 -I -c 'import shutil,sys,os; [shutil.rmtree(p) for p in sys.argv[1:] if os.path.isdir(p) and os.path.basename(p).startswith("lsp6-")]' "$W/lsp6-a" "$W/lsp6-b"
rm -f "$W/lsp6-a.debug.log" "$W/lsp6-b.debug.log"
```

Reply with the printed output, verbatim, and nothing else.

## What each answer proves

| Result | Meaning |
|---|---|
| (a) `guard-lines` ≥ 1 and `LSP-HELLO` | The user-settings guard entry runs on a Design A launch. Constraint 1 holds live. |
| (a) 0 and (b) ≥ 1 | Design A's settings removed the guard. **Stop the build** and compare the two runs' `pretooluse-lines`. |
| (a) 0 and (b) 0, `pretooluse-lines` > 0 | The debug log lists hooks but not this entry's command. The guard may be named differently: the hub compares `guard-tokens` with the entry in `~/.claude/settings.json`. Inconclusive, not a Design A failure. |
| (a) 0 and (b) 0, `pretooluse-lines` 0 | `--debug hooks` does not log hook runs here. Inconclusive; rely on build invariant I1 and probe 5. |
