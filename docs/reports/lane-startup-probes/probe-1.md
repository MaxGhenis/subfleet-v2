# Probe 1: what a writable lane loads today (D-LANE-STARTUP1)

Dispatch: one writable haiku lane, today's daemon, no special flags. Use a
throwaway workdir under `/Users/maxghenis` (a fresh subfleet-v2 worktree is fine).
The lane runs no command: it answers from the context it started with.

## Brief for the lane

Answer only from the context you were started with. Do not run any tool, read any
file or search. Reply with exactly six lines, nothing else:

```
A: <the first sentence under a heading "Finish what you start", quoted exactly, or NONE>
B: <the sentence that begins "Machine-wide and personal Codex rules live in", quoted exactly, or NONE>
C: <the path that follows the words "persistent file-based memory at", quoted exactly, or NONE>
D: <the first bullet line of any MEMORY.md index in your context, quoted exactly, or NONE>
E: <the heading or label your context gives for the file that holds the answer to B, quoted exactly, or NONE>
F: <every instruction or memory file path your context names, comma-separated, or NONE>
```

## What each answer proves

| Answer | Meaning |
|---|---|
| A quoted (expect "Finish the requested scope and verify the result.") | `~/.claude/CLAUDE.md` loads in writable lanes (the original probe, repeated). |
| B quoted | `~/AGENTS.md` loads. With E naming it "project instructions", the source is the `cc-plugin-agents-md` fallback (design F4), loading it as a Project file. |
| C quoted (a path under `~/.claude/projects/…/memory/`) | Auto-memory reaches writable lanes (F8). This justifies requirement 4b. |
| D quoted | The `MEMORY.md` index body is in context, not only the memory instructions. |
| C and D both NONE | Auto-memory does not reach haiku lanes. Drop 4b and the `autoMemoryEnabled` key from the build, and record which model or account difference explains it. |
| F | The file list to compare against probes 2–3. |
