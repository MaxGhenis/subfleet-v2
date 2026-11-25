# Probe 3: Design A end to end (D-LANE-STARTUP1, constraints 2, 4, 4b, 5)

Dispatch: one writable haiku lane, today's daemon, with a throwaway workdir **under
`/Users/maxghenis`**. The lane makes three nested `claude -p` runs with no tools and
quotes their answers. Two runs use Design A's `--settings`; the third is a control
with today's settings.

## Brief for the lane

Run these commands exactly, in your workdir, with Bash in the foreground. Do not
print any environment value.

```bash
set -u
W="$PWD"
case "$W" in /Users/maxghenis/*) ;; *) echo "WORKDIR-NOT-UNDER-HOME $W"; exit 3 ;; esac
printf '%s\n' \
 'Answer only from the context you were started with; you have no tools. Reply with exactly five lines:' \
 'A: the first sentence under a heading "Finish what you start", quoted exactly, or NONE' \
 'B: the sentence beginning "Machine-wide and personal Codex rules live in", quoted exactly, or NONE' \
 'C: any line containing "LSP-REPO-CANARY", quoted exactly, or NONE' \
 'D: the path that follows "persistent file-based memory at", quoted exactly, or NONE' \
 'E: every instruction or memory file path your context names, comma-separated, or NONE' > "$W/lsp-question.txt"
cat > "$W/lsp-settings.py" <<'PY'
import json, os, sys
s, mode = sys.argv[1], sys.argv[2]
r = os.path.realpath(s)
home = os.path.expanduser("~")
c = os.path.join(home, ".claude")
names = ["CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "AGENTS.md", ".claude/AGENTS.md", ".claude/rules/**"]
ancestors = []
for root in sorted({s, r}):
    d = os.path.dirname(root)
    while d != "/":
        if d not in ancestors:
            ancestors.append(d)
        d = os.path.dirname(d)
if home not in ancestors:
    ancestors.append(home)
explicit = [f"{c}/CLAUDE.md", f"{c}/rules/**"] + [f"{a}/{n}" for a in ancestors for n in names]
roots = sorted({s, r})
negation = [f"!{roots[0]}/**"] if len(roots) == 1 else ["!{" + ",".join(roots) + "}/**"]
excludes = {"both": explicit + negation, "explicit": explicit, "negation": negation}[mode]
print(json.dumps({"env": {"CLAUDE_CODE_RETRY_WATCHDOG": "0", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                  "autoMemoryEnabled": False, "disableAllHooks": False,
                  "claudeMdExcludes": excludes}, separators=(",", ":")))
PY
mkdir "$W/lsp3-claude" "$W/lsp3-agents" "$W/lsp3-control"
for S in "$W/lsp3-claude" "$W/lsp3-agents" "$W/lsp3-control"; do git -C "$S" init -q; done
printf '# Probe repo\nLSP-REPO-CANARY-CLAUDE: repository guidance loaded.\n' > "$W/lsp3-claude/CLAUDE.md"
printf '# Probe repo\nLSP-REPO-CANARY-AGENTS: repository guidance loaded.\n' > "$W/lsp3-agents/AGENTS.md"
printf '# Probe repo\nLSP-REPO-CANARY-AGENTS: repository guidance loaded.\n' > "$W/lsp3-control/AGENTS.md"
for S in "$W/lsp3-claude" "$W/lsp3-agents" "$W/lsp3-control"; do
  if [ "$(basename "$S")" = lsp3-control ]; then SETTINGS='{"env":{"CLAUDE_CODE_RETRY_WATCHDOG":"0"}}'
  else SETTINGS="$(python3 -I "$W/lsp-settings.py" "$S" both)"; fi
  ( cd "$S" && claude -p --model claude-haiku-5-5 --output-format text --tools "" \
      --no-session-persistence --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --settings "$SETTINGS" "$(cat "$W/lsp-question.txt")" ) > "$S.out.txt" 2> "$S.err.txt"
  echo "== $(basename "$S") rc=$? excludes=$(printf '%s' "$SETTINGS" | python3 -I -c 'import json,sys; print(len(json.load(sys.stdin).get("claudeMdExcludes",[])))')"
  cat "$S.out.txt"; echo "stderr-bytes=$(wc -c < "$S.err.txt")"
done
python3 -I -c 'import shutil,sys,os; [shutil.rmtree(p) for p in sys.argv[1:] if os.path.isdir(p) and os.path.basename(p).startswith("lsp3-")]' "$W/lsp3-claude" "$W/lsp3-agents" "$W/lsp3-control"
```

Reply with the printed output, verbatim, and nothing else.

## What each answer proves

Expected on `lsp3-claude` and `lsp3-agents`: A NONE, B NONE, C quoted, D NONE.
Expected on `lsp3-control`: A quoted, B quoted, C quoted, D quoted. The control shows
the question can see all four files when they load. If the control misses one,
the matching row below proves nothing.

| Deviation (Design A runs) | Meaning |
|---|---|
| A quoted | User memory is not excluded through inline `--settings` (F6 is wrong). Stop the build. |
| B quoted | An ancestor AGENTS.md got past the excludes on the plugin path. Stop the build. |
| C NONE | Repo guidance was lost: the negated root glob is excluding the repo (spelling mismatch). Constraint 2 fails. Compare E's paths with the printed root spelling. |
| D quoted | The auto-memory switch had no effect. 4b fails. |
| All four as expected | Constraints 2, 4, 4b and 5 hold end to end for Design A on 2.1.318. |
