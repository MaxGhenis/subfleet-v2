# Probe 4: which half of the exclude list does the work (D-LANE-STARTUP1, F6)

Dispatch: one writable haiku lane, today's daemon, with a throwaway workdir **under
`/Users/maxghenis`**. Two nested `claude -p` runs with no tools, on a repo that has
only an AGENTS.md: the hardest layout, because the plugin then searches every
ancestor.

## Brief for the lane

Run these commands exactly, in your workdir, with Bash in the foreground. Do not
print any environment value. `lsp-settings.py` is the same generator as in probe 3;
create it first exactly as probe 3 does if it is not already in your workdir.

```bash
set -u
W="$PWD"
case "$W" in /Users/maxghenis/*) ;; *) echo "WORKDIR-NOT-UNDER-HOME $W"; exit 3 ;; esac
test -f "$W/lsp-settings.py" || { echo "MISSING lsp-settings.py: create it as in probe 3"; exit 3; }
printf '%s\n' \
 'Answer only from the context you were started with; you have no tools. Reply with exactly five lines:' \
 'A: the first sentence under a heading "Finish what you start", quoted exactly, or NONE' \
 'B: the sentence beginning "Machine-wide and personal Codex rules live in", quoted exactly, or NONE' \
 'C: any line containing "LSP-REPO-CANARY", quoted exactly, or NONE' \
 'D: the path that follows "persistent file-based memory at", quoted exactly, or NONE' \
 'E: every instruction or memory file path your context names, comma-separated, or NONE' > "$W/lsp-question.txt"
for MODE in negation explicit; do
  S="$W/lsp4-$MODE"; mkdir "$S"; git -C "$S" init -q
  printf '# Probe repo\nLSP-REPO-CANARY-AGENTS: repository guidance loaded.\n' > "$S/AGENTS.md"
  SETTINGS="$(python3 -I "$W/lsp-settings.py" "$S" "$MODE")"
  ( cd "$S" && claude -p --model claude-haiku-5-5 --output-format text --tools "" \
      --no-session-persistence --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --settings "$SETTINGS" "$(cat "$W/lsp-question.txt")" ) > "$S.out.txt" 2> "$S.err.txt"
  echo "== $MODE rc=$?"; cat "$S.out.txt"; echo "stderr-bytes=$(wc -c < "$S.err.txt")"
done
python3 -I -c 'import shutil,sys,os; [shutil.rmtree(p) for p in sys.argv[1:] if os.path.isdir(p) and os.path.basename(p).startswith("lsp4-")]' "$W/lsp4-negation" "$W/lsp4-explicit"
```

Reply with the printed output, verbatim, and nothing else.

## What each answer proves

Expected for both modes: A NONE, B NONE, C quoted. D is NONE because both carry the
auto-memory switch.

| Result | Meaning |
|---|---|
| `negation` clean | Picomatch negation is honoured. The single `!<R>/**` pattern alone keeps everything outside the repo out, including nested and imported files the explicit list cannot name. |
| `negation` shows A or B | Negation is not honoured on this path. The build must not rely on it: keep the explicit list, and treat the negation as harmless padding (or drop it, and drop invariant I3's negation clause). |
| `negation` shows C NONE | Negation excludes the repo itself (spelling). Fix the root spelling before building. |
| `explicit` clean | Absolute-path entries are honoured for User memory and for the plugin's ancestor search. |
| `explicit` shows A or B | Absolute entries are not honoured (or FWo rewrote them). The build must rely on the negation, and probe 3's result decides. |
