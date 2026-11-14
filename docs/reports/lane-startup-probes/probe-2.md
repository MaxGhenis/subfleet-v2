# Probe 2: does dropping the `user` source alone drop `~/.claude/CLAUDE.md`? (D-LANE-STARTUP1, F3)

Dispatch: one writable haiku lane, today's daemon, with a throwaway workdir **under
`/Users/maxghenis`**. The question is about directories above the workdir, so a
workdir under `/private/var` or `/tmp` would prove nothing. The lane makes two
nested `claude -p` runs with no tools and quotes their answers. A nested run writes
nothing: no tools, `--no-session-persistence`.

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
mkdir "$W/lsp2-claude" "$W/lsp2-agents"
git -C "$W/lsp2-claude" init -q; git -C "$W/lsp2-agents" init -q
printf '# Probe repo\nLSP-REPO-CANARY-CLAUDE: repository guidance loaded.\n' > "$W/lsp2-claude/CLAUDE.md"
printf '# Probe repo\nLSP-REPO-CANARY-AGENTS: repository guidance loaded.\n' > "$W/lsp2-agents/AGENTS.md"
for S in "$W/lsp2-claude" "$W/lsp2-agents"; do
  ( cd "$S" && claude -p --model claude-haiku-5-5 --output-format text --tools "" \
      --no-session-persistence --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --setting-sources project,local \
      --settings '{"env":{"CLAUDE_CODE_RETRY_WATCHDOG":"0"}}' \
      "$(cat "$W/lsp-question.txt")" ) > "$S.out.txt" 2> "$S.err.txt"
  echo "== $(basename "$S") rc=$?"; cat "$S.out.txt"; echo "stderr-bytes=$(wc -c < "$S.err.txt")"
done
python3 -I -c 'import shutil,sys,os; [shutil.rmtree(p) for p in sys.argv[1:] if os.path.isdir(p) and os.path.basename(p).startswith("lsp2-")]' "$W/lsp2-claude" "$W/lsp2-agents"
```

Reply with the printed output, verbatim, and nothing else.

## What each answer proves

Run `lsp2-claude` (the repo has its own CLAUDE.md):

| Answer | Meaning |
|---|---|
| A quoted | **F3 confirmed.** With `user` dropped, `~/.claude/CLAUDE.md` still loads, as a Project file of ancestor `~`. Design B must keep the config-home excludes; dropping the source is not enough. |
| A NONE | F3 is wrong: something filters the config home from the ancestor walk. Design A is unaffected, but its config-home excludes become belt-and-braces. |
| C quoted | The repo's own CLAUDE.md loads under `project,local`, as expected. |
| D quoted | Auto-memory is not gated by setting sources (F2). |

Run `lsp2-agents` (the repo has only AGENTS.md):

| Answer | Meaning |
|---|---|
| A quoted and C NONE | **F3 second-order effect confirmed.** The masquerading `~/.claude/CLAUDE.md` makes the plugin think the project has a CLAUDE.md, so an AGENTS.md-only repo loses its own guidance. Design B without excludes fails constraint 2. |
| C quoted | The plugin still loads the repo's AGENTS.md. B tells whether `~/AGENTS.md` came with it. |
| A NONE, B quoted, C quoted | Today's fallback behaviour with no user memory. F3 is wrong for this layout. |
