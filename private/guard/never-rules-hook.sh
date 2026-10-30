#!/bin/bash
# subfleet-guard-hook — Codex CLI PreToolUse hook: the never-rules guard for
# subfleet codex lanes. Denies the nine standing NEVER rules that
# ~/.claude/hooks/guard-never-rules.sh enforces for Claude Code sessions.
#
# SOURCE OF TRUTH: ~/.claude/hooks/guard-never-rules.sh (and its harness,
# ~/.claude/tests/guard-never-rules-test.sh). Two sync contracts:
#   - The seven Bash rule blocks below are copied from that file BYTE-FOR-BYTE,
#     each from its column-0 "# --- <rule>:" header line up to (not including)
#     the next column-0 "# --- " (or "# >>>") line — comments, blank lines and
#     `block "…"` calls included; `block` is this hook's deny, so the ported
#     blocks run unmodified. Change the Claude hook first, run its harness,
#     then re-extract here (~/.cache/subfleet-guard-v2-port/proto/
#     extract_blocks.sh); never edit only one side.
#     tests/test_guard_never_rules.py::DriftTests pins the per-rule byte
#     identity, the prefilter literal, and the hf-dest literals.
#   - The unscoped-search rule is the region between the marker lines
#         (# >>> unscoped-search: shared region begin … >>>)
#         (# <<< unscoped-search: shared region end <<<)
#     once intended byte-identical to the same region of the Claude hook.
#     That 2026-08-19 port-back (guard-portback) never landed, and the Claude
#     hook no longer carries the region: its 2026-10-20 rewrite moved the
#     rule to ~/.claude/hooks/unscoped_search.py (BROAD_RE, HOME_SUBDIRS), so
#     tests/test_guard.py::DriftTests stays skip-gated and the begin marker's
#     "byte-identical … edit the Claude hook first" note is historical. The
#     region is edited in THIS file, and only on a hub decision: D-LANE-SCOPE1
#     (2026-10-24) added ~/.subfleet and each direct child of it to the broad
#     roots after one lane's `rg --files -g AGENTS.md ~/.subfleet` walked
#     14,121 job folders and 1,765 job worktrees for 4 minutes at load
#     190-220. D-LANE-SCOPE1 r2 (2026-10-25) added $SUBFLEET_ROOT (set on
#     every attempt) and $SUBFLEET_HOME (passed through to lanes), both
#     ~/.subfleet by default, as spellings of it. D-LANE-SCOPE1 r3
#     (2026-10-25) judged path words on disk (the "r3 path words" blocks): a
#     file is never a broad root, a trailing `/*` after a root is that root,
#     and a glob word that can match a root is judged by its matches. r3b
#     (2026-10-25) made those blocks judge the roots, globs and mounts of the
#     Claude half's snapshot-5: its BROAD_RE and home children (normalized,
#     in any letter case), links followed except under a mount that may
#     stall, and globs listed component by component where a root can be.
#     r3c (2026-10-26) closed the walk-arounds of the hub's review r2: globs
#     that match every entry, /Volumes/<entry> paths, braces and zsh's
#     alternation, links before .., searchers named by path or quote, shell
#     escapes, nested scripts and a depth bound outside its own command.
#     r4 (2026-10-26) closed those of review r3: a .. after a glob, a **/
#     after a wildcard, every script a shell or eval runs, searchers after
#     keywords, wrapper options and quotes, brace groups across a / or a ..,
#     hostile words' cost, a device on stdin, print-and-exit forms, ~$USER,
#     $"..." and /Volumes/<entry>/.. .
#     r5 (2026-10-27) closed those of review r4: a long word's cost,
#     heredoc bodies, a depth bound beside xargs or -exec, a root before a
#     closing backtick, bash's .* and .?, the data volume's alias before a
#     mount, commands too long to read, ~{...}, ~$NAME and $(...), zsh's
#     =cmd, pipes into a shell, "$@" scripts, eval $(...) and <&0.
#     r6 (2026-10-28) closed those of review r5: substitutions in an
#     unquoted heredoc body, xargs into a shell -c script, any producer's
#     heredoc into a shell, an arithmetic <<, the commit idiom's lone ",
#     =cmd after a backtick, hostile $ # \ words and dense substitutions,
#     <(...) scripts and loop variables.
#     Change a root on both sides in one reviewed step: here, and in
#     unscoped_search.py (BROAD_RE, HOME_SUBDIRS, expand()).
#
# Rules (one line each; details in docs/guard.md):
#   tg-getupdates    Telegram getUpdates polling steals the OpenClaw gateway's updates
#   keychain-dump    security dump-keychain -d on the login keychain (prompt storm)
#   trust-root       mutating AXIOM_*PUBLIC_KEY org variables (org-wide trust anchors)
#   corpus-squash    squash-merging PRs in axiom-corpus / rulespec-* (.github#39 rule 6)
#   corpus-push      pushing axiom-corpus main directly (.github#39 rule 6)
#   stash-shared     git stash in a repo with >1 worktree (stash stack is repo-global)
#   local-main       branching from local main when origin/main exists
#   unscoped-search  whole-tree find/rg/grep -r over broad roots (8/8, 8/18 and 10/24 incidents)
#   hf-dest          editing HF upload destinations in policyengine-uk-data (adapted:
#                    Codex edits arrive as apply_patch payloads or shell heredocs)
#
# I/O contract (Codex 0.144.0, codex-rs/hooks/src/events/pre_tool_use.rs):
#   stdin  : {"tool_name":"Bash","cwd":"...","tool_input":{"command":"<shell command>"},...}
#         or {"tool_name":"apply_patch","cwd":"...","tool_input":{"command":"<raw patch text>"},...}
#            (apply_patch is the freeform edit tool; matcher aliases Write/Edit.
#            Relative patch paths resolve against the turn cwd. A shell command
#            carrying an `apply_patch <<'EOF' …` heredoc arrives as tool_name
#            Bash and is judged as shell text PLUS the fenced hf-dest scan; its
#            deny reasons carry a codex-lane suffix saying so.)
#   allow  : no stdout, exit 0
#   deny   : stdout {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#            "permissionDecision":"deny","permissionDecisionReason":"..."}}, exit 0
# Any parse failure, missing jq, or a tool other than Bash/apply_patch -> allow
# (fail-open, like the Claude hook). The hook runs on the host, outside the
# sandbox, so `git -C "$cwd" ...` works in read-only lanes too.
# Telemetry: each denial appends one line to
#   ${CODEX_GUARD_LOG:-$HOME/.cache/subfleet-codex/guard-denials.log}
#   <ISO8601>\t<cwd>\t<command-or-patch, first 300 chars, tabs/newlines -> spaces>
# and logging failures never change the decision.
# Codex-only code is the hf-dest adapter (its helpers and the apply_patch
# branch) plus the fenced "codex-lane additions (hf-dest via shell heredoc)"
# step after the shared region; everything else is Claude-hook text verbatim.
set -u
# carpool → subfleet rename (2026-08-23): honour legacy CARPOOL_* names for a while.
for _legacy in $(env | sed -n 's/^\(CARPOOL_[A-Z0-9_]*\)=.*/\1/p') CLAUDE_LANE_CARPOOL DELEGATE_CARPOOL; do
  _new=${_legacy/CARPOOL/SUBFLEET}
  [ -n "${!_new:-}" ] || [ -z "${!_legacy:-}" ] || export "$_new=${!_legacy}"
done
unset _legacy _new
# Deterministic BSD grep/sed/awk/tr regardless of the caller's PATH; jq may
# still come from Homebrew via the fallback chain below.
export PATH="/usr/bin:/bin:${PATH:-/usr/sbin:/sbin}"
: "${HOME:=/var/empty}"

input=$(cat 2>/dev/null) || input=""

JQ=""
if command -v jq >/dev/null 2>&1; then JQ=$(command -v jq)
elif [ -x /opt/homebrew/bin/jq ]; then JQ=/opt/homebrew/bin/jq
elif [ -x /usr/bin/jq ]; then JQ=/usr/bin/jq
fi
if [ -z "$JQ" ]; then
  echo "subfleet-guard-hook: jq not found; allowing (fail-open)" >&2
  exit 0
fi

tool=$(printf '%s' "$input" | "$JQ" -r '.tool_name // empty' 2>/dev/null) || exit 0
case "$tool" in Bash|apply_patch) ;; *) exit 0 ;; esac

# Codex sends tool_input.command as a string (the shell command for Bash, the
# raw patch text for apply_patch); accept an argv array defensively.
command=$(printf '%s' "$input" | "$JQ" -r '.tool_input.command // empty | if type=="array" then map(tostring) | join(" ") else tostring end' 2>/dev/null) || exit 0
[ -z "$command" ] && exit 0
cwd=$(printf '%s' "$input" | "$JQ" -r '.cwd // empty' 2>/dev/null) || cwd=""

# block: every rule (the seven verbatim Claude blocks, the shared region, and
# the hf-dest adapter) calls this with its reason; here it is a Codex
# PreToolUse deny (the Claude hook's block() prints {decision:"block",reason}).
# When a Bash command carries an apply_patch patch body, the patch text was
# judged as shell text (exactly as the Claude hook judges its own Bash
# heredocs); the deny suffix says so, keeping the Claude reason a
# byte-identical prefix (the parity tests assert startswith).
block() {
  local reason=$1
  if [ "$tool" = "Bash" ] && printf '%s' "$command" | grep -qE '\*\*\* Begin Patch'; then
    reason="${reason} [codex lane: this shell command carries an apply_patch body; the patch text was judged as shell text, as the Claude hook judges Bash heredocs. Use the apply_patch tool for file edits.]"
  fi
  # Telemetry first, fully fail-safe; then the decision.
  local log_file="${CODEX_GUARD_LOG:-$HOME/.cache/subfleet-codex/guard-denials.log}"
  {
    mkdir -p "$(dirname "$log_file")" 2>/dev/null
    printf '%s\t%s\t%s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$cwd" \
      "$(printf '%s' "$command" | tr '\t\r\n' '   ' | cut -c1-300)" >> "$log_file"
  } 2>/dev/null || true
  "$JQ" -cn --arg r "$reason" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# hf-dest (codex adapter). Claude Edit-branch literals, byte-identical to
# ~/.claude/hooks/guard-never-rules.sh (the regex, the block message, and the
# *policyengine-uk-data* path test; the drift tests compare all three).
claude_hf_re='repo_id|huggingface\.co/|hf://|upload_file|upload_folder'
claude_hf_msg="[hf-dest] HF upload destinations in policyengine-uk-data are frozen (standing rule: NEVER change them). If this edit is not a destination change, restructure it to avoid touching upload/repo_id lines; if it is, Max makes that change himself."

# hf_dest_added_lines TEXT BASES
# Mirrors codex-rs apply-patch/src/streaming_parser.rs (used by the freeform
# tool AND the heredoc/lenient path): a line state machine. Prints the content
# of every '+' line that belongs to a section whose file, resolved against ANY
# base (relative paths joined to a base and normalised ., ..; absolute paths as
# is), contains "policyengine-uk-data".
#   NS  not started: trim(line)=="*** Begin Patch" -> SP; else ignored
#   SP/AF/DF: headers on trim(line): Environment ID (ignored), End Patch -> EP,
#       Add File -> AF (file), Delete File -> DF (out of scope), Update File -> UF.
#       In AF a RAW line starting with '+' is added content.
#   UF: headers only at column 0 (Codex: trim_end only; an indented header in an
#       update hunk is a CONTEXT line): End Patch -> EP, Add/Delete/Update File,
#       Move to: <q> (section also in scope if q resolves into uk-data).
#       RAW '+' lines are added content; ' ', '-', '@@', '*** End of File' ignored.
#   EP: everything ignored except trim(line)=="*** Begin Patch" -> SP (a second
#       patch in the same shell command also executes via the apply_patch alias).
# trim() here matches Rust str::trim()/trim_end() (Codex's parser): it strips
# the full Unicode White_Space set (U+0085, U+00A0, U+1680, U+2000-U+200A,
# U+2028, U+2029, U+202F, U+205F, U+3000), not just the ASCII bytes POSIX
# [:space:] sees under LC_ALL=C — otherwise a header prefixed with e.g. a
# no-break space parses for Codex but not here, and the change slips through.
# BASES are passed as the leading stdin lines (count via -v NB), not ENVIRON:
# it avoids the ENVIRON size limit and lets scope be tested against every base
# in ONE pass (a command with thousands of `cd` targets was O(bases x length)).
hf_dest_added_lines() {
  local nb
  nb=$(printf '%s\n' "$2" | LC_ALL=C awk 'END { print NR }')
  { printf '%s\n' "$2"; printf '%s\n' "$1"; } | LC_ALL=C awk -v NB="$nb" '
    BEGIN {
      # UTF-8 byte sequences for the Unicode White_Space code points Rust
      # str::trim() strips (ASCII \t\v\f\r space, then U+0085 U+00A0, U+1680,
      # U+2000-U+200A U+2028 U+2029 U+202F, U+205F, U+3000).
      USP = "([ \t\v\f\r]|\302[\205\240]|\341\232\200|\342\200[\200-\212\250\251\257]|\342\201\237|\343\200\200)"
      mode = "NS"; in_scope = 0
    }
    function ltrim(s) { sub("^(" USP ")+", "", s); return s }
    function rtrim(s) { sub("(" USP ")+$", "", s); return s }
    function norm(p,   n, i, parts, k, r) {
      n = split(p, parts, "/"); k = 0
      for (i = 1; i <= n; i++) {
        if (parts[i] == "" || parts[i] == ".") continue
        if (parts[i] == "..") { if (k > 0) k--; continue }
        stack[++k] = parts[i]
      }
      r = ""
      for (i = 1; i <= k; i++) r = r "/" stack[i]
      return r
    }
    # scoped(p): absolute p judged as is; relative p is in scope if it resolves
    # into uk-data under ANY base (Codex applies the patch against the shell
    # cwd, which may be any cd/pushd target — see hf_dest_bases).
    function scoped(p,   i) {
      if (substr(p, 1, 1) == "/") return index(tolower(norm(p)), "policyengine-uk-data") > 0
      for (i = 1; i <= NB; i++)
        if (index(tolower(norm(bases[i] "/" p)), "policyengine-uk-data") > 0) return 1
      return 0
    }
    # header(t): t is the (trimmed) line; returns 1 and sets mode/in_scope when it is a header
    function header(t,   p) {
      if (t == "*** End Patch") { mode = "EP"; in_scope = 0; return 1 }
      if (index(t, "*** Add File: ") == 1) { p = substr(t, 15); in_scope = scoped(p); mode = "AF"; return 1 }
      if (index(t, "*** Delete File: ") == 1) { in_scope = 0; mode = "DF"; return 1 }
      if (index(t, "*** Update File: ") == 1) { p = substr(t, 18); in_scope = scoped(p); mode = "UF"; return 1 }
      return 0
    }
    NR <= NB { bases[NR] = $0; next }
    {
      line = $0; sub(/\r$/, "", line)
      t = rtrim(ltrim(line))
      if (mode == "NS" || mode == "EP") { if (t == "*** Begin Patch") { mode = "SP"; in_scope = 0 } ; next }
      if (mode == "SP") { if (index(t, "*** Environment ID:") == 1) next; if (header(t)) next; next }
      if (mode == "AF") { if (header(t)) next; if (in_scope && substr(line, 1, 1) == "+") print substr(line, 2); next }
      if (mode == "DF") { if (header(t)) next; next }
      if (mode == "UF") {
        u = rtrim(line)
        if (header(u)) next
        if (index(u, "*** Move to: ") == 1) { p = substr(u, 14); if (scoped(p)) in_scope = 1; next }
        if (in_scope && substr(line, 1, 1) == "+") print substr(line, 2)
        next
      }
    }
  '
}

# hf_dest_bases TOOL COMMAND CWD: one base dir per line — the payload cwd, plus
# (Bash only) every `cd`/`pushd` target anywhere in the command (word, "…" or
# '…'; ~ -> $HOME; relative -> CWD/<dir>). Non-intercepted heredoc forms still
# apply the patch via Codex's apply_patch PATH alias with the shell's cwd
# (codex-rs/arg0/src/lib.rs + apply-patch/src/standalone_executable.rs), so a
# `cd`/`pushd` anywhere can be the effective base. Leading option tokens and a
# `--` end-of-options terminator are skipped so the real operand of
# `cd -- <dir>` / `cd -L <dir>` is captured (a shell-variable target like
# `cd "$VAR"` cannot be resolved from the text and is a documented gap).
hf_dest_bases() {
  local tool=$1 cmd=$2 cwd=$3 d
  printf '%s\n' "$cwd"
  [ "$tool" = "Bash" ] || return 0
  printf '%s\n' "$cmd" | LC_ALL=C grep -oE "(^|[[:space:];&|(])[\\]?(cd|pushd)[[:space:]]+((-[^[:space:]]*|--)[[:space:]]+)*(\"[^\"]*\"|'[^']*'|[^[:space:]'\"&|;)]+)" 2>/dev/null \
    | LC_ALL=C sed -E "s/^[[:space:];&|(]*[\\]?(cd|pushd)[[:space:]]+//; s/^((-[^[:space:]]*)[[:space:]]+)*//; s/^\"(.*)\"\$/\\1/; s/^'(.*)'\$/\\1/" \
    | while IFS= read -r d; do
        [ -n "$d" ] || continue
        d=${d//\\/}   # shell removes escaping backslashes at parse time
        # after the sed the operand should not start with '-'; a residual dash
        # token is a no-operand match (e.g. `cd -L` with nothing after), drop it.
        case "$d" in -*) continue ;; esac
        case "$d" in '~') d=$HOME ;; '~/'*) d="$HOME/${d#\~/}" ;; esac
        case "$d" in /*) printf '%s\n' "$d" ;; *) printf '%s\n' "$cwd/$d" ;; esac
      done
}

# hf_dest_check TOOL TEXT CWD: block (no return) when the patch adds an
# upload-destination line to a file resolving into policyengine-uk-data from
# ANY base; silent return 0 otherwise.
hf_dest_check() {
  local hf_tool=$1 text=$2 hf_cwd=$3 bases dbases added
  bases=$(hf_dest_bases "$hf_tool" "$text" "$hf_cwd")
  # Keep only DECISIVE bases: the payload cwd (always — it carries absolute and
  # already-in-path patches) plus any cd/pushd target that itself contains the
  # repo string; deduped. A base lacking the string can only scope a path that
  # already contains it (the cwd base scopes that too), so dropping it changes
  # no decision — and it bounds the scan, defeating a command with thousands of
  # `cd` targets that was O(bases x length) (a 71 KB / 6000-cd command took ~72 s,
  # long enough to time the hook out and fail open).
  dbases=$( { printf '%s\n' "$hf_cwd"; printf '%s\n' "$bases" | LC_ALL=C grep -iF 'policyengine-uk-data'; } \
            | LC_ALL=C awk 'length { if (!seen[$0]++) print }' )
  # Claude path literal, kept functional: nothing can be in scope unless the
  # repo name appears in the patch text or in a (decisive) base dir.
  case "$(printf '%s' "$text$dbases" | LC_ALL=C tr 'A-Z' 'a-z')" in
    *policyengine-uk-data*) ;;
    *) return 0 ;;
  esac
  # One scan pass; scope is tested against every decisive base inside the awk.
  added=$(hf_dest_added_lines "$text" "$dbases")
  if printf '%s' "$added" | grep -qE "$claude_hf_re"; then
    block "$claude_hf_msg"
  fi
  return 0
}

# ---------- apply_patch branch (runs first, like the Claude Edit branch) ----------
if [ "$tool" = "apply_patch" ]; then
  hf_dest_check "$tool" "$command" "$cwd"
  exit 0
fi

# ---------- Bash branch ----------
# Cheap prefilter: bail immediately unless a rule keyword appears. The first
# grep literal is BYTE-IDENTICAL to the Claude hook's prefilter (its tail is
# the searcher term the drift tests pin in both hooks); the second is the
# codex addition that lets the fenced hf-dest heredoc scan below run; the
# third (D-LANE-SCOPE1 r3c) lets a searcher named by a path, after a quote or
# a backslash, through $(which …), or as egrep, fgrep, rgrep or ugrep reach the
# unscoped-search rule; the fourth and fifth (r4) one with a quote or a
# backslash inside its name (r''g, f\ind, $'rg'), and a $'...' holding an
# escape, which may spell one; the sixth (r5) zsh's =rg, =find and =grep,
# also right after a backtick (r6).
printf '%s' "$command" | grep -qE 'stash|AXIOM_|getUpdates|get_updates|dump-keychain|checkout[[:space:]]+-b|switch[[:space:]]+-c|pr[[:space:]]+merge|git[[:space:]]+push|(^|[[:space:];&|(])(find|rg|grep)[[:space:]]' \
  || printf '%s' "$command" | grep -qE '\*\*\* Begin Patch' \
  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(/\\\\'\"\`])(find|rg|[efru]?grep)[\`'\")]?([[:space:]]|\$)" \
  || printf '%s' "$command" | LC_ALL=C tr -d "\\\\'\"\$" | grep -qE "(^|[[:space:];&|(/\`])(find|rg|[efru]?grep)[\`)]?([[:space:]]|\$)" \
  || printf '%s' "$command" | grep -qE "\\\$'[^']*\\\\[0-7xX]" \
  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(\`])=(find|rg|[efru]?grep)([[:space:]]|\$)" \
  || exit 0

# --- tg-getupdates: Telegram polling steals updates from the OpenClaw gateway ---
# Match API-call contexts only (URL path, telegram host nearby, or bot-lib call),
# not prose that merely mentions the word (commit messages, docs).
if printf '%s' "$command" | grep -qE '/getUpdates|\.get_updates\(' \
   || { printf '%s' "$command" | grep -q 'getUpdates' && printf '%s' "$command" | grep -qi 'api\.telegram'; }; then
  block "[tg-getupdates] NEVER call Telegram getUpdates — the ai.openclaw.gateway launchd job owns polling on the shared bot token; a second poller steals its updates. Chief-of-staff traffic is outbound-only (sendMessage/sendDocument are fine)."
fi

# --- keychain-dump: login-keychain dumps cause macOS prompt storms ---
if printf '%s' "$command" | grep -qE 'dump-keychain' && printf '%s' "$command" | grep -qE '(^|[[:space:]])-d([[:space:]]|$)'; then
  named=$(printf '%s' "$command" | grep -oE '[^[:space:]]+\.keychain(-db)?' | grep -v 'login' | head -1)
  if [ -z "$named" ]; then
    block "[keychain-dump] Do not run 'security dump-keychain -d' against the login keychain — it triggers a macOS prompt storm and touches unrelated credentials (CLAUDE.md). Use 'agent-secret' / 'security find-generic-password -s <item>' for specific secrets."
  fi
fi

# --- trust-root: Axiom org trust anchors are rotated only via coordinated migration ---
if printf '%s' "$command" | grep -qE 'AXIOM_[A-Z_]*PUBLIC_KEY'; then
  if printf '%s' "$command" | grep -qE 'variable[[:space:]]+(set|delete)|-X[[:space:]]+(PATCH|POST|PUT|DELETE)|--method[[:space:]]+(PATCH|POST|PUT|DELETE)|-f[[:space:]]+value='; then
    block "[trust-root] NEVER mutate AXIOM_*PUBLIC_KEY org variables — they are org-wide trust anchors; all nine encoding repos verify signed release objects against them, and rotation breaks every validate CI simultaneously (7/14 incident, feedback_never_rotate_axiom_trust_roots). Rotation is a coordinated re-sign-everything migration Max authorizes, never an in-flight swap. Reading the vars is fine."
  fi
fi

# --- corpus-squash: signed-manifest repos merge-commit, never squash ---
if printf '%s' "$command" | grep -qE 'pr[[:space:]]+merge' && printf '%s' "$command" | grep -qE '(^|[[:space:]])(--squash|-s)([[:space:]]|$)'; then
  repo_ctx=$(printf '%s' "$command" | grep -oE '(-R|--repo)[[:space:]]+[^[:space:]]+' | awk '{print $2}')
  [ -z "$repo_ctx" ] && repo_ctx=$(git -C "$cwd" remote get-url origin 2>/dev/null)
  if printf '%s' "$repo_ctx" | grep -qiE 'TheAxiomFoundation/(axiom-corpus|rulespec-)'; then
    block "[corpus-squash] NEVER squash-merge in axiom-corpus / rulespec-* — squash orphans manifest signing commits and breaks the guard's ancestor invariant repo-wide (corpus#428: 226 manifests orphaned). Use 'gh pr merge --merge' (.github#39 rule 6)."
  fi
fi

# --- corpus-push: never push corpus main directly ---
if printf '%s' "$command" | grep -qE 'git[[:space:]]+push'; then
  origin_url=$(git -C "$cwd" remote get-url origin 2>/dev/null)
  if printf '%s' "$origin_url" | grep -qiE 'TheAxiomFoundation/axiom-corpus'; then
    # tokenize the push segment: first non-flag token = remote, second = refspec
    seg=$(printf '%s' "$command" | sed -E 's/.*git[[:space:]]+push//' | sed -E 's/[;&|].*//')
    refspec="" remote_seen=""
    for tok in $seg; do
      case "$tok" in
        -*) continue ;;
        *) if [ -z "$remote_seen" ]; then remote_seen="$tok"; else refspec="$tok"; break; fi ;;
      esac
    done
    target=""
    if [ -n "$refspec" ]; then
      case "$refspec" in
        main|master|+main|+master|*:main|*:master) target=1 ;;
        HEAD) cur=$(git -C "$cwd" branch --show-current 2>/dev/null)
              { [ "$cur" = "main" ] || [ "$cur" = "master" ]; } && target=1 ;;
      esac
    else
      # bare `git push` / `git push origin`: pushes the current branch
      cur=$(git -C "$cwd" branch --show-current 2>/dev/null)
      { [ "$cur" = "main" ] || [ "$cur" = "master" ]; } && target=1
    fi
    if [ -n "$target" ]; then
      block "[corpus-push] Never push axiom-corpus main directly — releases publish only from corpus main via the gated flow, and stray commits there broke every consumer pin once already (.github#39 rule 6, axiom-corpus#320). Push a branch and open a PR."
    fi
  fi
fi

# --- stash-shared: the stash stack is repo-global across worktrees ---
if printf '%s' "$command" | grep -qE '\bgit\b[^|;&]*\bstash\b' && ! printf '%s' "$command" | grep -qE '\bstash[[:space:]]+list\b'; then
  wt_count=$(git -C "$cwd" worktree list 2>/dev/null | wc -l | tr -d ' ')
  if [ "${wt_count:-0}" -gt 1 ]; then
    block "[stash-shared] NEVER git stash in shared-worktree repos — the stash stack lives in .git and is shared by ALL worktrees; a pop can dump another session's WIP into yours (7/17 near-miss, feedback_never_stash_in_shared_worktrees). To temporarily revert an edit: Edit tool remove/re-add, or 'git show HEAD:path > file' then restore. 'git stash list' is allowed for recovery."
  fi
fi

# --- local-main: branch from origin/main, never local main ---
if printf '%s' "$command" | grep -qE '(checkout[[:space:]]+-b|switch[[:space:]]+-c)'; then
  if git -C "$cwd" show-ref --verify --quiet refs/remotes/origin/main 2>/dev/null || git -C "$cwd" show-ref --verify --quiet refs/remotes/origin/master 2>/dev/null; then
    reason="[local-main] Branch from origin/main, never local main — local main may carry unmerged work from other sessions (CLAUDE.md). Use: git fetch origin && git checkout -b <name> origin/main"
    # explicit local start point: ... -b name main
    if printf '%s' "$command" | grep -qE '(checkout[[:space:]]+-b|switch[[:space:]]+-c)[[:space:]]+[^[:space:]]+[[:space:]]+(main|master)([[:space:]]|$|;|&|\|)'; then
      block "$reason"
    fi
    # implicit start point while sitting on main
    if printf '%s' "$command" | grep -qE '(checkout[[:space:]]+-b|switch[[:space:]]+-c)[[:space:]]+[^[:space:]]+[[:space:]]*($|;|&|\|)'; then
      cur=$(git -C "$cwd" branch --show-current 2>/dev/null)
      if [ "$cur" = "main" ] || [ "$cur" = "master" ]; then
        block "$reason"
      fi
    fi
  fi
fi

# >>> unscoped-search: shared region begin — byte-identical in ~/.claude/hooks/guard-never-rules.sh and ~/chief-of-staff/subfleet/bin/subfleet-guard-hook; ~/chief-of-staff/subfleet/tests/test_guard.py::DriftTests compares. Edit the Claude hook first, run its harness, then copy the region verbatim into the codex hook. >>>
# --- unscoped-search: whole-tree searches over broad roots saturate the SSD ---
# Incidents:
#   2026-08-08  8+ parallel encoder-lane find/rg crawls of the mirrors, home
#               and /tmp drove the load average to 47 and beachballed the UI.
#   2026-08-18  the e8 sol batch (codex lanes) ran ~22 parallel `find / ...`
#               and `find /Users/maxghenis ...` crawls; load held at 57 for
#               hours.
# Design (a regex judgment over the command text, not a parse):
#   searcher   find, rg, or grep with a recursive flag, preceded by a line
#              start, whitespace, `;`, `&`, `|` or `(` — the three detection
#              regexes both hooks have always used (also the prefilter term).
#              r3c: also after a quote or a backslash, and named by basename:
#              a path, a backslash or a quote before the name, egrep, fgrep,
#              rgrep and ugrep, $(which X) and `which X` are read as the
#              searcher first (pw_cmd, "the command as the rules read it").
#              r4: also after a keyword, a function body's {, a wrapper's
#              option values (a table) or a wrapper named by basename, with a
#              quote or a backslash inside the name, and in every script a
#              shell or eval runs (pw_cmd adds each as its own lines); a
#              print-and-exit form (rg --version, find --version) walks nothing.
#   escape     a `-maxdepth` / `--max-depth` / `-d N` that is an unquoted
#              argument of the searcher's own simple command exempts that
#              command (r3c; before, one anywhere in the command disarmed the
#              rule, a comment or a commit message included).
#   roots      `/`, `/*`, `/Users`, `/Users/<user>`, `~`, `$HOME` (`${HOME}`
#              is read as `$HOME`), their TheAxiomFoundation / RulesFoundation
#              / PolicyEngine / Library/Caches / .cache / .local children,
#              their .subfleet child and each one path component directly
#              under it (`~/.subfleet/jobs`; `~/.subfleet/jobs/<id>` is
#              scoped), `$SUBFLEET_ROOT` and `$SUBFLEET_HOME` as spellings of
#              ~/.subfleet (alone or with one component under them),
#              `/tmp`, `/private/tmp` — each with or without a trailing slash.
#              r3: a root by name that is an existing non-directory is not
#              broad, a root before a trailing `/*` run or a `**/` is that
#              root, and a glob word is judged by its matches ("Path words").
#              r3b: every path word is judged against snapshot-5's roots
#              (also /usr, /Library, /opt/homebrew, /Volumes/<entry> and the
#              rest, in any letter case, aliases folded), through its links,
#              and a glob component by component ("Path words").
#              r3c: a glob matching every entry is its folder, a glob directly
#              in a folder of ~/.subfleet is that folder, /Volumes/<entry>/x
#              is judged as /x, braces and zsh's (a|b) by their alternatives,
#              a searcher's .. after a link physically, and backslashes, ***,
#              a / inside brackets, ~user, $'…' and APFS's folded letters as
#              the shell and the volume read them ("Path words").
#   judged     backslash-newline continuations are joined first (as the shell
#              does) and `${HOME}`, `${SUBFLEET_ROOT}` and `${SUBFLEET_HOME}`
#              lose their braces; then collapse_quotes
#              turns every quoted string into the placeholder Q, except a
#              quoted string whose whole content IS a root (kept, unquoted:
#              `find "$HOME"/ …`, `rg pat "/"`) and a sub-script that
#              mentions a searcher (`bash -c "cd x && find / -name y"`,
#              kept visible). In the main position matcher, a root inside a
#              pattern, prose, or a commit message does not fire. The raw
#              searcher/cwd check is intentionally more conservative; its
#              quoted-prose residue is documented in the README.
#   position   a root counts only where a searcher would read it as a path:
#              anywhere in find's own pipeline segment (up to -exec/-ok); in
#              PATH position of rg/grep — after the pattern positional, with
#              value-taking options and their values skipped; with -e/-f/
#              --regexp/--file/--files in that rg/grep segment anywhere in
#              the segment (the option's own value excepted); anywhere in the
#              command when `xargs` runs a searcher (a deliberately
#              conservative exception to the ordinary pipeline ownership
#              rule); as the target of cd/pushd
#              while any searcher is present (a bare `cd` is `cd ~`, r3c);
#              and `.`/`./` when the cwd itself is a broad root, as is a
#              recursive searcher given no path (r3c). A sh/bash/zsh/dash/ksh
#              -c or eval script holding a searcher is judged again as its own
#              line, to depth 4; a searcher deeper is denied (r3c); r4: also
#              with option values or -- before the script, a quoted shell name,
#              eval's joined arguments, and a here-string or an echo, printf or
#              cat pipe into a shell given no script. rg reading a device or a
#              closed stdin, with no path, searches . (r4). A root in
#              another command of the pipeline
#              (`ls ~ | rg x`) or in pattern position (`rg / src`) is not a
#              crawl and passes.
#   matched    the token that fired is named at the end of the block message
#              (`Matched broad root: '…'.`), `'<root> (after cd)'` for the cd
#              rule and `'. (cwd <cwd>)'` for the cwd rule.
#   no override: a blocked command stays blocked — no env var, no magic
#              comment, nothing in the command text lets a crawl through
#              (r3's two test seams are names no lane's environment carries).
# Accepted false positives and known gaps (the residue of a regex judgment)
# are listed in ~/chief-of-staff/subfleet/docs/guard.md, "Known false positives"
# and "Known gaps".

# First match of $2 in $1, minus quotes and the surrounding delimiters (the
# token that fired, for the block message). Best-effort; empty is fine.
first_match() {
  printf '%s' "$1" | grep -oE "$2" 2>/dev/null | head -1 | tr -d "'\"" | sed 's/^[[:space:]|;&(]*//;s/[[:space:]|;&)]*$//;s/[|;&)].*$//'
}
# Last word of the first match of $2 in $1 (the path token of a matched
# `rg <pattern> ... <root>` run).
last_word_of_match() {
  first_match "$1" "$2" | sed 's/.*[[:space:]]//'
}
# The command with every quoted string collapsed to the placeholder Q — except
# a quoted string that is a SUB-SCRIPT and mentions a searcher: it must contain
# a searcher word AND be the argument of a shell (`bash -c "..."`, `sh -lc`,
# `zsh -c`, `eval "..."`, `xargs ... sh -c '...'`), i.e. the text right before
# the quote ends in `-c`/`-lc`/`eval`/`sh`/`bash`/`zsh` plus whitespace. Such a
# string stays visible (its own inner quotes collapsed the same way) so
# `bash -c "cd x && find / -name y"` is still judged; ordinary prose that
# merely mentions a searcher (`git commit -m "Add find / replace dialog"`,
# `echo "run: find / -name x" >> notes.md`) becomes Q like anything else.
# Mode "roots" (arg 2, with the root regex as arg 3, passed through the
# environment so awk sees it byte-for-byte) emits a quoted string whose WHOLE
# content is a broad root unquoted instead (`rg pat "/"` -> `rg pat /`,
# `find "$HOME"/ ...` -> `find $HOME/ ...`), so the position-aware rules
# below can judge it. Line by line; a quote spanning lines is left as is
# (BSD awk, always at /usr/bin/awk).
collapse_quotes() {
  # (rs/rl keep the match bounds across the recursive call, which clobbers the
  # global RSTART/RLENGTH.)
  printf '%s' "$1" | CQ_MODE="${2:-q}" CQ_ROOT="${3:-^\$}" LC_ALL=C awk "function collapse(s,   out, q, inner, rs, rl, pre) { out = \"\"; while (match(s, /'[^']*'|\"[^\"]*\"/)) { rs = RSTART; rl = RLENGTH; q = substr(s, rs, rl); inner = substr(q, 2, rl - 2); pre = out substr(s, 1, rs - 1); out = pre; if (inner ~ /(^|[[:space:];&|(])(find|rg|grep)[[:space:]]/ && pre ~ /(^|[[:space:]])(-[A-Za-z]*c|eval|sh|bash|zsh|dash|ksh)[[:space:]]+$/) out = out substr(q, 1, 1) collapse(inner) substr(q, 1, 1); else if (ENVIRON[\"CQ_MODE\"] == \"roots\" && inner ~ ENVIRON[\"CQ_ROOT\"]) out = out inner; else out = out \"Q\"; s = substr(s, rs + rl) } return out s } { print collapse(\$0) }" 2>/dev/null
}
# >>> r3 path words: begin >>>
# Path words judged on disk (D-LANE-SCOPE1 r3, 2026-10-25; r3b, 2026-10-25,
# for parity with snapshot-5 of the Claude half's unscoped_search.py; r3c,
# 2026-10-26, on the hub's review r2). Before the text rules below, every path
# word of the command is judged once. A path word is a run of characters other
# than whitespace and | ; & ( ) < >, where a quoted string, a backslash escape
# and a ( ) group with no space in it count as part of its run
# (`"/Volumes/Macintosh HD"` and `~/(a|b)` are one word), read as the shell
# reads it (quote marks gone, backslash escapes applied, a quoted { } , ( | )
# kept literal, each $'...' a *), that starts with a root spelling: ~, ~user,
# $HOME, $SUBFLEET_ROOT or $SUBFLEET_HOME (each alone or before a /), / or a
# brace group. A run that is no path word has the words of each of its quoted
# strings judged in turn (`bash -c 'cd ~/x && rg y'`), and when it is the
# script of a sh, bash, zsh, dash or ksh -c or of eval and names a searcher,
# that script is also added to the command as its own line, to depth 4; a
# searcher deeper is denied (r3c).
#   r3c         A word's brace groups are expanded (64 words at most; a nested
#               group, a {x..y} sequence or more reads each group as *) and,
#               outside a bash/sh/dash/ksh -c script, zsh's (a|b) taken by its
#               plain-name alternatives (any other group reads its component
#               as *); the word is broad when one of its words is. ~name is
#               /Users/name, or the home for this user; *** is ** (zsh); a /
#               inside a bracket expression splits no component (zsh); a
#               searcher's .. is applied after the links before it resolve
#               (a cd target's stays logical); /Volumes/<entry>/<rest> is
#               judged by name as /<rest>; U+017F and U+212A are s and k, as
#               APFS folds them on this volume.
#   r4          (the hub's decisions on review r3) Each <component>/.. after a
#               word's first glob component is folded away by name, and the
#               word is broad when the folded or the unfolded reading is (a
#               candidate a later .. may climb out of is kept, so its .. is
#               physical); the trailing-run rule also runs on the part before a
#               **/; a ${...} is no brace group, and a word whose group read as *
#               holds a / or a .. component is broad; a word with more than 64 {
#               or [, or over 4 KB, is broad unscanned, and the brace, bracket
#               and folder scans are one pass; ~$USER, ~${USER} and ~$(whoami)
#               are the home, $"..." is "...", and a searcher's
#               /Volumes/<entry>/.. is also read with the entry as /.
#   roots       A spelling is resolved to an absolute path ($SUBFLEET_ROOT and
#               $SUBFLEET_HOME as $HOME/.subfleet, never their environment
#               values) and normalized: // collapsed, . and .. resolved by
#               name, /System/Volumes/Data folded to / and /private/tmp,
#               /private/var and /private/etc to /tmp, /var and /etc. It is a
#               broad root by name, matched case-insensitively as APFS is, if
#               it is /, /Users, /Users/<any>, its TheAxiomFoundation,
#               RulesFoundation, PolicyEngine, Library, Library/Caches,
#               .cache, .local or .subfleet and each folder directly in that
#               .subfleet, /Volumes, /Volumes/<any>, /System, /System/Volumes,
#               /private, /tmp, /var, /var/folders, /opt, /opt/homebrew,
#               /Library, /Applications or /usr, or the home or one of those
#               home children wherever the home is (snapshot-5's BROAD_RE,
#               HOME_SUBDIRS and HOME_PARENTS).
#   links       A written path is broad when it is a broad root by name and
#               not an existing non-directory (a file, or a link to one:
#               `grep -rn x ~/.subfleet/policy.json` reads one file), or when
#               it leads through symlinks (resolved as realpath does) to a
#               different path that is (`rg x ~/link-to-home`). A written path
#               under a mount that may stall (/Volumes, /Network, /net, /home
#               and anything under them) is never resolved or stat'ed: it reads
#               as no file, and as leading nowhere else; a link leading into
#               one still is resolved (a limit snapshot-5 shares).
#   walks       A root followed by a trailing run of components made only of
#               wildcards (`*`, `?`, brackets; r3c), or `.*`, after trailing /,
#               /. and <glob>/.. are read (`rg foo ~/.subfleet/jobs/?*`,
#               `~/*/*`, `jobs/*/..`), or by a `**/` component, is that root,
#               listed or not: the shell hands the searcher every entry of
#               it, and zsh walks it for `**/`. Inside a script run by bash,
#               sh, dash or ksh -c, `**` is `*` (no globstar). `/*` itself
#               stays the text rule's root.
#   globs       Any other glob word is judged component by component, as the
#               shells glob (case-sensitive, a leading dot matched only by a
#               leading dot): the literal components before the first glob
#               component are its prefix, and a word whose prefix is no broad
#               root is not judged. A glob component lists a folder only when
#               a name directly in it can be a broad root (pw_holds: /,
#               /Users, /Users/<any>, its Library and .subfleet, /Volumes,
#               /System, /System/Volumes, /private, /var, /opt, the home and
#               its Library and .subfleet); a plain component keeps a path
#               only where something is there, and under /Volumes it is
#               matched against the listing, never stat'ed. Between
#               components only broad roots that are no file go on; at the
#               end a broad root that is no file is a match, and so is a
#               plain path or a listed link that leads to one. At most 20,000
#               directory entries are read per word. No match, or a form these
#               patterns cannot express: the word is not judged (it becomes F).
#               r3c: any glob component directly in a folder of a .subfleet
#               (jobs, worktrees, ...) makes the word that folder; inside a
#               mount entry nothing is listed or stat'ed: a plain name is kept
#               by name and a glob matches the names a root can have plus any
#               other name. A glob word holding $ or a backtick is judged by
#               its text.
# A word whose verdict the text rules would not give becomes a placeholder:
# a broad one /Users/@sfglob<n>@, which every rule reads like any other root
# and the block message names as the word was written (plus " (glob)" for a
# glob word), and one that is not broad F. Only the five pw_* disk functions
# below touch the file system. Two test seams, which the daemon's launch
# environment (an exact allowlist, subfleet/launch_env.py) never carries to a
# lane: CODEX_GUARD_TEST_FSROOT, a folder that stands in for / in every
# check made on disk here, and CODEX_GUARD_TEST_GLOB_CAP, the entry cap.
pw_fs=${CODEX_GUARD_TEST_FSROOT:-}
pw_fs=${pw_fs%/}
pw_cap=${CODEX_GUARD_TEST_GLOB_CAP:-20000}
case "$pw_cap" in ''|*[!0-9]*) pw_cap=20000 ;; esac
# The root table (r3c: pw_isbroad and the block message's root list both read
# it): the fixed roots, and the home's children (each also under /Users/<any>).
pw_fixed="/ /Users /Volumes /System /System/Volumes /private /tmp /var /var/folders /opt /opt/homebrew /Library /Applications /usr"
pw_kids="TheAxiomFoundation RulesFoundation PolicyEngine Library Library/Caches .cache .local .subfleet"
# --- the file system: P is an absolute path, judged under the seam ---
# pw_islink P: P is a symlink (not followed).
pw_islink() { [ -L "$pw_fs$1" ]; }
# pw_lexists P: something is at P, a dangling link included.
pw_lexists() { [ -e "$pw_fs$1" ] || [ -L "$pw_fs$1" ]; }
# pw_nondir P: P exists and is not a directory, links followed.
pw_nondir() { [ -e "$pw_fs$1" ] && [ ! -d "$pw_fs$1" ]; }
# pw_readlink P: pw_rl is the target of the link at P.
pw_readlink() { pw_rl=$(readlink "$pw_fs$1" 2>/dev/null) || pw_rl=""; }
# pw_ls DIR: pw_ents is the name of every entry of DIR in byte order (bash's
# own pathname expansion of a quoted folder and a bare *, which never runs a
# substitution); every setting it changes is put back.
pw_ls() {
  local LC_ALL=C f="" n="" d="" c="" e="" g="" gi=${GLOBIGNORE-} gi_set=${GLOBIGNORE+x} x="" i=0
  case $- in *f*) f=1 ;; esac
  if shopt -q nullglob; then n=1; fi
  if shopt -q dotglob; then d=1; fi
  if shopt -q nocaseglob; then c=1; fi
  if shopt -q extglob; then e=1; fi
  if shopt -q failglob; then g=1; fi
  unset GLOBIGNORE
  set +f
  shopt -s nullglob dotglob
  shopt -u nocaseglob extglob failglob
  pw_ents=( "$pw_fs$1"/* )
  if [ -n "$gi_set" ]; then GLOBIGNORE=$gi; fi
  if [ -n "$n" ]; then shopt -s nullglob; else shopt -u nullglob; fi
  if [ -n "$d" ]; then shopt -s dotglob; else shopt -u dotglob; fi
  if [ -n "$c" ]; then shopt -s nocaseglob; else shopt -u nocaseglob; fi
  if [ -n "$e" ]; then shopt -s extglob; else shopt -u extglob; fi
  if [ -n "$g" ]; then shopt -s failglob; else shopt -u failglob; fi
  if [ -n "$f" ]; then set -f; fi
  for x in ${pw_ents[@]+"${pw_ents[@]}"}; do pw_ents[$i]=${x##*/}; i=$((i + 1)); done
}
# --- names ---
# pw_abs WORD: pw_p is the absolute path a root spelling names, or empty.
# ~name is /Users/name, or the home when name is this user (r3c).
pw_abs() {
  local n=""
  pw_p=""
  case "$1" in
    '~'|'$HOME') pw_p=$HOME ;;
    '~/'*) pw_p="$HOME/${1#\~/}" ;;
    '~'?*) n=${1#\~}; n=${n%%/*}
      case "$n" in *[!A-Za-z0-9._-]*) return 0 ;; esac
      [ -n "${pw_me+x}" ] || pw_me=$(id -un 2>/dev/null)
      if [ "$n" = "$pw_me" ]; then pw_p="$HOME${1#\~"$n"}"; else pw_p="/Users/$n${1#\~"$n"}"; fi ;;
    '$HOME/'*) pw_p="$HOME/${1#\$HOME/}" ;;
    '$SUBFLEET_ROOT'|'$SUBFLEET_HOME') pw_p="$HOME/.subfleet" ;;
    '$SUBFLEET_ROOT/'*|'$SUBFLEET_HOME/'*) pw_p="$HOME/.subfleet/${1#*/}" ;;
    /*) pw_p=$1 ;;
  esac
}
# pw_norm PATH: pw_n is the absolute PATH with //, /./ and /../ resolved by name.
pw_norm() {
  local rest="$1/" out="" c=""
  while [ -n "$rest" ]; do
    c=${rest%%/*}; rest=${rest#*/}
    case "$c" in ''|.) ;; ..) out=${out%/*} ;; *) out="$out/$c" ;; esac
  done
  pw_n=${out:-/}
}
# pw_normalize PATH: pw_norm, then the two code points APFS folds to an ASCII
# letter on this volume (U+017F long s to s, U+212A Kelvin sign to k; r3c), and
# the data volume folded to / and /private/tmp, /private/var and /private/etc
# to /tmp, /var and /etc (any letter case).
pw_normalize() {
  local nc=""
  pw_norm "$1"
  case "$pw_n" in
    *$'\305\277'*|*$'\342\204\252'*) pw_n=${pw_n//$'\305\277'/s}; pw_n=${pw_n//$'\342\204\252'/k} ;;
  esac
  # only /System/... and /private/... fold (r4 decision 6: no option set else)
  case "$pw_n" in /[SsPp]*) ;; *) return 0 ;; esac
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  case "$pw_n" in
    /system/volumes/data) pw_n=/ ;;
    /system/volumes/data/*) pw_n=${pw_n:20} ;;
  esac
  case "$pw_n" in
    /private/tmp|/private/tmp/*|/private/var|/private/var/*|/private/etc|/private/etc/*) pw_n=${pw_n:8} ;;
  esac
  [ -n "$nc" ] || shopt -u nocasematch
}
# pw_stall P: P (normalized) is a mount that may stall a stat, or under one.
pw_stall() {
  case "$1" in
    /[Vv][Oo][Ll][Uu][Mm][Ee][Ss]|/[Vv][Oo][Ll][Uu][Mm][Ee][Ss]/*|/[Nn][Ee][Tt][Ww][Oo][Rr][Kk]|/[Nn][Ee][Tt][Ww][Oo][Rr][Kk]/*) return 0 ;;
    /[Nn][Ee][Tt]|/[Nn][Ee][Tt]/*|/[Hh][Oo][Mm][Ee]|/[Hh][Oo][Mm][Ee]/*) return 0 ;;
  esac
  return 1
}
# pw_mount P: P (absolute, not normalized) is a mount that may stall, or under one, once the data volume's
# alias (/System/Volumes/Data, any letter case) is folded: r5 decision 7, so no step through the alias
# (/System/Volumes/Data/Volumes/<entry>, .../Data/net, .../Data/home, .../Data/Network) is stat'ed.
pw_mount() {
  local p=$1
  while :; do
    case "$p" in
      /[Ss][Yy][Ss][Tt][Ee][Mm]/[Vv][Oo][Ll][Uu][Mm][Ee][Ss]/[Dd][Aa][Tt][Aa]) p=/ ;;
      /[Ss][Yy][Ss][Tt][Ee][Mm]/[Vv][Oo][Ll][Uu][Mm][Ee][Ss]/[Dd][Aa][Tt][Aa]/*) p=${p:20} ;;
      *) break ;;
    esac
  done
  pw_stall "$p"
}
# pw_child S: S (a path below a home, or below /Users/<any>) is one of its
# broad children, or a folder directly in its .subfleet. Caller has nocasematch.
pw_child() {
  case " $pw_kids " in *" $1 "*) return 0 ;; esac
  case "$1" in .subfleet/?*) case "${1:10}" in */*) return 1 ;; esac; return 0 ;; esac
  return 1
}
# pw_isbroad P: P (normalized) is a broad root by name. /Volumes/<entry>/<rest>
# is judged as /<rest> (r3c decision 2: the boot volume's alias, or a disk's
# Users), by name only; pw_n is left as the caller had it.
pw_isbroad() {
  local p=$1 rest="" nc="" r=1 sv=$pw_n
  # r4 decision 6: six or more components deep, outside /Volumes, is no root
  # by name when the home is at most three deep (pw_hfast)
  if [ -n "${pw_hfast-}" ]; then case "$p" in /[Vv]*) ;; /*/*/*/*/*/*) return 1 ;; esac; fi
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  while [[ $p == /volumes/*/* ]]; do rest=${p:9}; pw_normalize "/${rest#*/}"; p=$pw_n; done
  pw_n=$sv
  case " $pw_fixed " in *" $p "*) r=0 ;; esac
  [ "$r" -eq 0 ] || case "$p" in
    /volumes/*) case "${p:9}" in */*) ;; *) r=0 ;; esac ;;
    /users/*) rest=${p:7}
      case "$rest" in
        */*) pw_child "${rest#*/}" && r=0 ;;
        *) r=0 ;;
      esac ;;
  esac
  if [ "$r" -ne 0 ] && [ -n "$pw_home" ] && [ "$pw_home" != / ]; then
    if [[ $p == "$pw_home" ]]; then r=0
    elif [[ $p == "$pw_home"/* ]]; then pw_child "${p:${#pw_home}+1}" && r=0
    fi
  fi
  [ -n "$nc" ] || shopt -u nocasematch
  return $r
}
# pw_holds DIR: a name directly in DIR (normalized) can be a broad root by name:
# one of the names roots have, or any name (snapshot-5's holds_root).
pw_holds() {
  local n=""
  if pw_stall "$1" && ! pw_volumes "$1"; then return 1; fi
  for n in TheAxiomFoundation RulesFoundation PolicyEngine Library Caches .cache .local .subfleet \
           Users Volumes System private tmp var folders opt homebrew Applications usr Data @sf-any@; do
    pw_normalize "$1/$n"
    pw_isbroad "$pw_n" && return 0
  done
  return 1
}
# pw_volumes P: P (normalized) is /Volumes itself, the one stall folder listed.
pw_volumes() {
  local nc="" r=1
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  [[ $1 == /volumes ]] && r=0
  [ -n "$nc" ] || shopt -u nocasematch
  return $r
}
# pw_subchild P: P (normalized) is a broad root directly in a .subfleet: jobs,
# worktrees and the rest (r3c 1(c)).
pw_subchild() {
  local nc="" r=1
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  [[ ${1%/*} == */.subfleet ]] && pw_isbroad "$1" && r=0
  [ -n "$nc" ] || shopt -u nocasematch
  return $r
}
# --- where links lead ---
# pw_isfile P: P (normalized) exists and is no directory; never asked under a
# mount that may stall.
pw_isfile() { pw_stall "$1" && return 1; pw_nondir "$1"; }
# pw_physical P: pw_r is P (normalized) with its symlinks resolved, as
# realpath resolves them (a missing part is kept as written); P itself under
# a mount that may stall, or when the links loop. r4 decision 6: P's folder is
# resolved first and kept (pw_pcd, pw_pcr), so the words of one folder (300
# files on one line) resolve it once. r5 decision 7: the stall test folds the
# data volume's alias first (pw_mount), and a step under a mount that may
# stall is taken by name, never stat'ed.
pw_physical() {
  local path="" rest="" name="" hops=0 d=${1%/*} top=""
  pw_r=$1
  pw_mount "$1" && return 0
  rest=${1#/}
  if [ -n "$d" ] && [ "$d" = "${pw_pcd-}" ]; then path=${pw_pcr%/}; rest=${1##*/}
  elif [ -n "$d" ] && [ -n "${1##*/}" ]; then top=${1##*/}; rest=${d#/}
  fi
  while :; do
    while [ -n "$rest" ]; do
      case "$rest" in */*) name=${rest%%/*}; rest=${rest#*/} ;; *) name=$rest; rest="" ;; esac
      case "$name" in ''|.) continue ;; ..) path=${path%/*}; continue ;; esac
      if pw_mount "$path/$name"; then path="$path/$name"; continue; fi
      if pw_islink "$path/$name"; then
        hops=$((hops + 1))
        [ "$hops" -le 40 ] || { pw_r=$1; return 0; }
        pw_readlink "$path/$name"
        if [ -z "$pw_rl" ]; then path="$path/$name"; continue; fi
        case "$pw_rl" in /*) path="" ;; esac
        rest="$pw_rl${rest:+/$rest}"
      else
        path="$path/$name"
      fi
    done
    [ -n "$top" ] || break
    pw_normalize "${path:-/}"; pw_pcd=$d pw_pcr=$pw_n
    path=${pw_n%/}; rest=$top; top=""
  done
  pw_normalize "${path:-/}"
  pw_r=$pw_n
}
# pw_physdots PATH: pw_pd is the absolute PATH with each .. applied where a
# searcher applies it, after the links before it are resolved (pw_physical),
# as find and rg resolve it (r3c decision 4); under a mount that may stall,
# by name. A cd target keeps the shell's logical .. (its caller skips this).
pw_physdots() {
  local rest="$1/" c="" path=""
  pw_pd=$1
  case "$rest" in */../*) ;; *) return 0 ;; esac
  while [ -n "$rest" ]; do
    c=${rest%%/*}; rest=${rest#*/}
    case "$c" in
      ''|.) ;;
      ..) pw_physical "${path:-/}"; path=${pw_r%/*} ;;
      *) path="$path/$c" ;;
    esac
  done
  pw_pd=${path:-/}
}
# pw_broad P: a walk of P (normalized) covers a broad root: P is one and no
# file, or P leads through links to a different path that is.
pw_broad() {
  if pw_isbroad "$1"; then
    pw_isfile "$1" && return 1
    return 0
  fi
  pw_physical "$1"
  [ "$pw_r" != "$1" ] && pw_isbroad "$pw_r" && ! pw_isfile "$pw_r"
}
# --- globs ---
# pw_pattern COMP: pw_pat is a bash pattern matching what fnmatch matches for
# the glob component COMP (every * ? [ a glob character); status 1 for a form
# it cannot express (a POSIX class other than alpha, digit, alnum, upper,
# lower, xdigit and space). Every other character is escaped, so no pattern
# is ever an extglob or holds a substitution.
pw_pattern() {
  local LC_ALL=C s=$1 n=${#1} k=0 j=0 ch="" lead="" body="" rest="" cls="" end=0 bl=0 seen="" x=""
  local -a dead
  pw_pat=""
  while [ "$k" -lt "$n" ]; do
    ch=${s:k:1}
    case "$ch" in
      '*'|'?') pw_pat="$pw_pat$ch"; k=$((k + 1)); continue ;;
      '[') ;;
      *) pw_pat="$pw_pat\\$ch"; k=$((k + 1)); continue ;;
    esac
    j=$((k + 1)); lead=""; body=""; bl=0
    case "${s:j:1}" in '!'|'^') j=$((j + 1)); lead='!' ;; esac
    if [ "${s:j:1}" = "]" ]; then j=$((j + 1)); body='\]'; bl=1; fi
    seen=""
    while [ "$j" -lt "$n" ] && [ "${s:j:1}" != "]" ]; do
      # one pass (r4 decision 6): where a bracket that never closed walked, none closes
      if [ -n "${dead[$j]-}" ]; then j=$n; break; fi
      seen="$seen $j"
      ch=${s:j:1}
      if [ "$ch" = "[" ] && [ "${s:j+1:1}" = ":" ]; then
        rest=${s:j+2}; cls=${rest%%:]*}
        [ "$cls" != "$rest" ] || return 1
        end=${#cls}
        case "$cls" in
          alpha) body="${body}a-zA-Z" ;; digit) body="${body}0-9" ;; alnum) body="${body}a-zA-Z0-9" ;;
          upper) body="${body}A-Z" ;; lower) body="${body}a-z" ;; xdigit) body="${body}0-9A-Fa-f" ;;
          space) body="$body"$' \t\n\r\f\v' ;;
          *) return 1 ;;
        esac
        bl=$((bl + 1)); j=$((j + end + 4)); continue
      fi
      case "$ch" in
        -) if [ "$bl" -eq 0 ] || [ "$((j + 1))" -ge "$n" ] || [ "${s:j+1:1}" = "]" ]; then body="$body\\-"; else body="$body-"; fi ;;
        '\'|'['|']'|'!'|'^') body="$body\\$ch" ;;
        $'\007') body="$body/" ;;
        *) body="$body$ch" ;;
      esac
      bl=$((bl + 1)); j=$((j + 1))
    done
    if [ "$j" -lt "$n" ]; then pw_pat="$pw_pat[$lead$body]"; k=$((j + 1))
    else for x in $seen; do dead[$x]=1; done; pw_pat="$pw_pat\\["; k=$((k + 1))
    fi
  done
}
# pw_match PATTERN DOT NAME...: pw_hits is every NAME the bash PATTERN matches,
# case-sensitive, byte by byte; a leading dot only when DOT is set.
pw_match() {
  local LC_ALL=C pat=$1 dot=$2 nc="" e="" x=""
  shift 2
  pw_hits=()
  shopt -q nocasematch && nc=1
  shopt -q extglob && e=1
  shopt -u nocasematch extglob
  for x in "$@"; do
    case "$x" in .*) [ -n "$dot" ] || continue ;; esac
    [[ $x == $pat ]] && pw_hits[${#pw_hits[@]}]=$x
  done
  [ -z "$nc" ] || shopt -s nocasematch
  [ -z "$e" ] || shopt -s extglob
}
# pw_globbroad PATH [c]: status 0 iff the glob PATH (absolute, unnormalized) can
# match a broad root, judged component by component (see "globs" above); c: a
# cd target, whose .. stays logical.
pw_globbroad() {
  local rest="$1/" logical=${2-} c="" nc=0 first=-1 k=0 text="" glob="" pat="" p="" q="" l="" i=0 budget=$pw_cap last=0 m=0 dot="" up=-1
  local -a comps cp cl np nl
  while [ -n "$rest" ]; do
    c=${rest%%/*}; rest=${rest#*/}
    [ -n "$c" ] || continue
    comps[$nc]=$c
    case "$c" in *[*?[]*) [ "$first" -ge 0 ] || first=$nc ;; ..) up=$nc ;; esac
    nc=$((nc + 1))
  done
  [ "$first" -ge 0 ] || return 1
  rest=""
  k=0
  while [ "$k" -lt "$first" ]; do rest="$rest/${comps[$k]}"; k=$((k + 1)); done
  [ "$logical" = c ] || { pw_physdots "${rest:-/}"; rest=$pw_pd; }
  pw_normalize "${rest:-/}"
  pw_isbroad "$pw_n" || return 1
  cp=( "$pw_n" ); cl=( n )
  k=$first
  while [ "$k" -lt "$nc" ]; do
    text=${comps[$k]}; glob=""; last=""
    [ "$k" -lt "$((nc - 1))" ] || last=1
    case "$text" in *[*?[]*) glob=1; pw_pattern "$text" || return 1; pat=$pw_pat ;; esac
    np=(); nl=()
    i=0
    while [ "$i" -lt "${#cp[@]}" ]; do
      p=${cp[$i]}; i=$((i + 1))
      if [ -n "$glob" ] && pw_subchild "$p" && ! pw_isfile "$p"; then return 0; fi
      if pw_stall "$p" && ! pw_volumes "$p"; then
        # Inside a mount entry nothing is listed or stat'ed: a plain name is
        # kept by name, and a glob matches the names a root can have plus a
        # placeholder for any other name (r3c decision 2).
        if [ -z "$glob" ]; then
          pw_normalize "$p/$text"; np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=n
        else
          case "$text" in .*) dot=1 ;; *) dot="" ;; esac
          pw_match "$pat" "$dot" TheAxiomFoundation RulesFoundation PolicyEngine Library Caches .cache .local .subfleet \
                   Users Volumes System private tmp var folders opt homebrew Applications usr Data
          pw_hits[${#pw_hits[@]}]=@sf-any@
          for q in "${pw_hits[@]}"; do pw_normalize "$p/$q"; np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=n; done
        fi
      elif [ -z "$glob" ] && ! pw_stall "$p"; then
        if [ "$text" = .. ] && [ "$logical" != c ]; then pw_physical "$p"; q=${pw_r%/*}; pw_normalize "${q:-/}"
        else pw_normalize "$p/$text"
        fi
        if pw_lexists "$pw_n"; then np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=u; fi
      elif pw_holds "$p"; then
        pw_ls "$p"
        if [ "${#pw_ents[@]}" -gt "$budget" ]; then pw_ents=( ${pw_ents[@]+"${pw_ents[@]:0:$budget}"} ); fi
        budget=$((budget - ${#pw_ents[@]}))
        [ "${#pw_ents[@]}" -gt 0 ] || continue
        if [ -n "$glob" ]; then
          case "$text" in .*) pw_match "$pat" 1 "${pw_ents[@]}" ;; *) pw_match "$pat" "" "${pw_ents[@]}" ;; esac
        else
          pw_hits=()
          for q in "${pw_ents[@]}"; do [ "$q" != "$text" ] || pw_hits[0]=$q; done
        fi
        [ "${#pw_hits[@]}" -gt 0 ] || continue
        for q in "${pw_hits[@]}"; do
          pw_normalize "$p/$q"
          l=n
          if [ -n "$last" ] && ! pw_isbroad "$pw_n" && ! pw_stall "$pw_n" && pw_islink "$p/$q"; then l=y; fi
          np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=$l
        done
      fi
    done
    if [ -z "$last" ]; then
      # r4 decision 1: a candidate a later .. may climb back out of stays, broad
      # or not, so that .. is applied to it physically
      cp=(); cl=(); m=0
      while [ "$m" -lt "${#np[@]}" ]; do
        if { [ "$k" -lt "$up" ] || pw_isbroad "${np[$m]}"; } && ! pw_isfile "${np[$m]}"; then cp[${#cp[@]}]=${np[$m]}; cl[${#cl[@]}]=${nl[$m]}; fi
        m=$((m + 1))
      done
    else
      cp=( ${np[@]+"${np[@]}"} ); cl=( ${nl[@]+"${nl[@]}"} )
    fi
    [ "${#cp[@]}" -gt 0 ] || return 1
    k=$((k + 1))
  done
  m=0
  while [ "$m" -lt "${#cp[@]}" ]; do
    q=${cp[$m]}; l=${cl[$m]}; m=$((m + 1))
    if pw_isbroad "$q"; then
      pw_isfile "$q" || return 0
    elif [ "$l" != n ]; then
      pw_physical "$q"
      [ "$pw_r" != "$q" ] && pw_isbroad "$pw_r" && ! pw_isfile "$pw_r" && return 0
    fi
  done
  return 1
}
# pw_wild COMP: COMP is made only of wildcards: *, ? and bracket expressions,
# no literal character (r3c 1(b)).
pw_wild() {
  local LC_ALL=C s=$1 n=${#1} k=0 j=0 rest="" cls=""
  [ "$n" -gt 0 ] || return 1
  while [ "$k" -lt "$n" ]; do
    case "${s:k:1}" in
      '*'|'?') k=$((k + 1)) ;;
      '[') j=$((k + 1))
        case "${s:j:1}" in '!'|'^') j=$((j + 1)) ;; esac
        [ "${s:j:1}" != "]" ] || j=$((j + 1))
        while [ "$j" -lt "$n" ] && [ "${s:j:1}" != "]" ]; do
          if [ "${s:j:1}" = "[" ] && [ "${s:j+1:1}" = ":" ]; then
            rest=${s:j+2}; cls=${rest%%:]*}
            if [ "$cls" != "$rest" ]; then j=$((j + ${#cls} + 4)); continue; fi
          fi
          j=$((j + 1))
        done
        [ "$j" -lt "$n" ] || return 1
        k=$((j + 1)) ;;
      *) return 1 ;;
    esac
  done
}
# pw_stars WORD CONTEXT: pw_w is WORD with each *** component read as ** (zsh
# reads them alike), and in CONTEXT b each ** and *** as * (r3c).
pw_stars() {
  local x="/$1/" y="" a='/***/' z='/**/'
  while :; do y=${x//"$a"/"$z"}; [ "$y" != "$x" ] || break; x=$y; done
  if [ "${2-}" = b ]; then
    while :; do y=${x//"$z"/"/*/"}; [ "$y" != "$x" ] || break; x=$y; done
  fi
  x=${x#/}; pw_w=${x%/}
}
# pw_brackets WORD: pw_w is WORD with each / inside a bracket expression made
# \007, so it splits no component: zsh reads /[^/]* as every entry of / (r3c).
# One pass (r4 decision 6): a position a bracket that never closed walked
# through ends any later bracket's walk there.
pw_brackets() {
  local LC_ALL=C s=$1 n=${#1} k=0 j=0 out="" body="" rest="" cls="" seen="" x=""
  local -a dead
  case "$s" in *'['*) ;; *) pw_w=$s; return 0 ;; esac
  while [ "$k" -lt "$n" ]; do
    rest=${s:k}
    if [ "${rest:0:1}" != "[" ]; then body=${rest%%\[*}; out="$out$body"; k=$((k + ${#body})); continue; fi
    j=$((k + 1))
    case "${s:j:1}" in '!'|'^') j=$((j + 1)) ;; esac
    [ "${s:j:1}" != "]" ] || j=$((j + 1))
    seen=""
    while [ "$j" -lt "$n" ] && [ "${s:j:1}" != "]" ]; do
      if [ -n "${dead[$j]-}" ]; then j=$n; break; fi
      seen="$seen $j"
      if [ "${s:j:1}" = "[" ] && [ "${s:j+1:1}" = ":" ]; then
        rest=${s:j+2}; cls=${rest%%:]*}
        if [ "$cls" != "$rest" ]; then j=$((j + ${#cls} + 4)); continue; fi
      fi
      j=$((j + 1))
    done
    if [ "$j" -lt "$n" ]; then
      body=${s:k:j-k+1}
      case "$body" in */*) body=$(printf '%s' "$body" | tr '/' '\007') ;; esac
      out="$out$body"; k=$((j + 1))
    else for x in $seen; do dead[$x]=1; done; out="$out["; k=$((k + 1))
    fi
  done
  pw_w=$out
}
# pw_groups WORD: the brace groups of WORD, found in one pass with a stack (r4
# decision 6): each { with its matching }, held by no other closed group, in
# order. pw_gs and pw_ge are its { and }, pw_gk its kind (comma, nested, seq or
# lit), pw_gn its alternatives, pw_gx set when it holds a / or a .. component
# (r4 decision 5). A ${...} span is no group (decision 5); an unclosed { is none.
pw_groups() {
  local LC_ALL=C s=$1 n=${#1} k=0 c="" d=0 top=0 m=0 x="" t="" body=""
  local -a os oc ok cs ce cc cn cp shut
  pw_gs=(); pw_ge=(); pw_gk=(); pw_gn=(); pw_gx=()
  while [ "$k" -lt "$n" ]; do
    body=${s:k}; x=${body%%[\{\},\$]*}
    if [ -n "$x" ]; then k=$((k + ${#x})); continue; fi
    c=${s:k:1}
    case "$c" in
      '$') if [ "${s:k+1:1}" = "{" ]; then
             d=1; k=$((k + 2))
             while [ "$k" -lt "$n" ] && [ "$d" -gt 0 ]; do
               case "${s:k:1}" in '{') d=$((d + 1)) ;; '}') d=$((d - 1)) ;; esac
               k=$((k + 1))
             done
             continue
           fi ;;
      '{') os[$top]=$k; oc[$top]=0; ok[$top]=""; top=$((top + 1)) ;;
      ',') [ "$top" -eq 0 ] || oc[top-1]=$((oc[top-1] + 1)) ;;
      '}') if [ "$top" -gt 0 ]; then
             top=$((top - 1))
             cs[$m]=${os[$top]}; ce[$m]=$k; cc[$m]=${oc[$top]}; cn[$m]=${ok[$top]}; shut[${os[$top]}]=1
             if [ "$top" -gt 0 ]; then cp[$m]=${os[top-1]}; ok[top-1]=1; else cp[$m]=-1; fi
             m=$((m + 1))
           fi ;;
    esac
    k=$((k + 1))
  done
  k=0; d=0
  while [ "$k" -lt "$m" ]; do
    # the closed groups in the order they closed: a group is top-level when the
    # { around it never closed; then sort by position (they never overlap)
    if [ "${cp[$k]}" -lt 0 ] || [ -z "${shut[${cp[$k]}]-}" ]; then
      body=${s:cs[k]+1:ce[k]-cs[k]-1}
      if [ -n "${cn[$k]}" ]; then t=nested
      elif [ "${cc[$k]}" -gt 0 ]; then t=comma
      else case "$body" in *..*) t=seq ;; *) t=lit ;; esac
      fi
      x=""
      case ",$body," in */*|*[,\{]..[,\}]*) x=1 ;; esac
      # insert by position
      c=$d
      while [ "$c" -gt 0 ] && [ "${pw_gs[c-1]}" -gt "${cs[$k]}" ]; do
        pw_gs[$c]=${pw_gs[c-1]}; pw_ge[$c]=${pw_ge[c-1]}; pw_gk[$c]=${pw_gk[c-1]}; pw_gn[$c]=${pw_gn[c-1]}; pw_gx[$c]=${pw_gx[c-1]}
        c=$((c - 1))
      done
      pw_gs[$c]=${cs[$k]}; pw_ge[$c]=${ce[$k]}; pw_gk[$c]=$t; pw_gn[$c]=$((cc[k] + 1)); pw_gx[$c]=$x
      d=$((d + 1))
    fi
    k=$((k + 1))
  done
}
# pw_expand WORD CONTEXT: pw_alts is every word WORD can be after brace expansion
# and, outside CONTEXT b, zsh's (a|b) alternation (r3c decision 3): each comma
# group expanded, 64 words at most; a nested group, a {x..y} sequence or more
# than 64 words reads each group as *, and an alternation that is not a list of
# plain names, or would pass 64, reads its component as * (pw_grp set then). A
# group read as * that holds a / or a .. component sets pw_fc: the word is broad,
# failing closed (r4 decision 5). A quoted or escaped { } , ( | ) arrives as
# \001-\006, and is itself again at the end.
pw_expand() {
  local LC_ALL=C w=$1 ctx=${2-z} k=0 i=0 prod=1 star="" x="" pre="" post="" inner="" a="" one="" lit="" r="" lb='{' rb='}' cm=',' lp='(' vb='|' rp=')'
  local -a todo out new
  pw_grp="" pw_fc=""
  case "$w" in
    *[\{\(]*) ;;
    *) pw_alts=( "$w" )
       case "$w" in *[$'\001'-$'\006']*) ;; *) return 0 ;; esac ;;
  esac
  case "$w" in *'{'*) pw_groups "$w" ;; *) pw_gs=(); pw_ge=(); pw_gk=(); pw_gn=(); pw_gx=() ;; esac
  while [ "$k" -lt "${#pw_gs[@]}" ]; do
    case "${pw_gk[$k]}" in nested|seq) star=1 ;; comma) prod=$((prod * pw_gn[k])); [ "$prod" -le 64 ] || star=1 ;; esac
    k=$((k + 1))
  done
  if [ -n "$star" ]; then
    k=$(( ${#pw_gs[@]} - 1 ))
    while [ "$k" -ge 0 ]; do
      if [ "${pw_gk[$k]}" != lit ]; then
        w="${w:0:pw_gs[k]}*${w:pw_ge[k]+1}"
        [ -z "${pw_gx[$k]}" ] || pw_fc=1
      fi
      k=$((k - 1))
    done
    pw_alts=( "$w" ); pw_grp=1
  else
    # every comma group expanded at once (none is nested here), the first slowest
    pw_alts=( "" ); i=0
    for k in ${pw_gs[@]+"${!pw_gs[@]}"}; do
      lit=${w:i:pw_gs[k]-i}
      if [ "${pw_gk[$k]}" != comma ]; then lit="$lit${w:pw_gs[k]:pw_ge[k]-pw_gs[k]+1}"; new=()
        for r in "${pw_alts[@]}"; do new[${#new[@]}]="$r$lit"; done
      else
        new=()
        for r in "${pw_alts[@]}"; do
          inner="${w:pw_gs[k]+1:pw_ge[k]-pw_gs[k]-1},"
          while [ -n "$inner" ]; do new[${#new[@]}]="$r$lit${inner%%,*}"; inner=${inner#*,}; done
        done
      fi
      pw_alts=( "${new[@]}" ); i=$((pw_ge[k] + 1))
    done
    lit=${w:i}; new=()
    for r in "${pw_alts[@]}"; do new[${#new[@]}]="$r$lit"; done
    pw_alts=( "${new[@]}" )
  fi
  if [ "$ctx" != b ]; then
    todo=( "${pw_alts[@]}" ); out=()
    while [ "${#todo[@]}" -gt 0 ]; do
      x=${todo[0]}; todo=( ${todo[@]+"${todo[@]:1}"} )
      case "$x" in *'('*')'*) ;; *) out[${#out[@]}]=$x; continue ;; esac
      pre=${x%%'('*}; inner=${x#*'('}; post=${inner#*')'}; inner=${inner%%')'*}
      one=""
      case "$inner" in
        *'|'*) case "$inner" in *['*?[/~#^<>(']*) ;; *)
            pw_count '|' "$inner"; [ $(( ${#todo[@]} + ${#out[@]} + pw_c + 1 )) -le 64 ] && one=1 ;; esac ;;
      esac
      if [ -n "$one" ]; then
        inner="$inner|"
        while [ -n "$inner" ]; do a=${inner%%'|'*}; inner=${inner#*'|'}; todo[${#todo[@]}]="$pre$a$post"; done
      else
        case "$pre" in */*) pre="${pre%/*}/" ;; *) pre="" ;; esac
        case "$post" in */*) post="/${post#*/}" ;; *) post="" ;; esac
        todo[${#todo[@]}]="$pre*$post"; pw_grp=1
      fi
    done
    pw_alts=( "${out[@]}" )
  fi
  k=0
  while [ "$k" -lt "${#pw_alts[@]}" ]; do
    x=${pw_alts[$k]}
    case "$x" in *[$'\001'-$'\006']*) x=$(printf '%s' "$x" | tr '\001\002\003\004\005\006' '{},(|)') ;; esac
    pw_alts[$k]=$x; k=$((k + 1))
  done
}
# --- words ---
# pw_sentinel WORD [SUFFIX]: pw_t is a new placeholder root, named WORD SUFFIX.
pw_sentinel() {
  pw_nt=$((pw_nt + 1))
  pw_t="/Users/@sfglob${pw_nt}@"
  pw_shown[$pw_nt]="$1${2-}"
}
# pw_word WORD CONTEXT [QUOTED [CD [SHOWN]]]: pw_t is a placeholder root
# (broad), F (not broad), or empty (the text rules' verdict stands). WORD is
# the word as the shell reads it (r3c: quote marks and backslash escapes gone,
# a quoted { } , ( | ) as \001-\006, a $'...' as *). CONTEXT b: the word is in
# a script run by bash, sh, dash or ksh -c, where `**` is `*`; z: zsh's
# reading as well (alternation, a / inside brackets, `**/`). QUOTED q: a quote
# mark or a backslash was part of the word, so a broad one is placed even where
# the text rules read it as a root by name (they see it otherwise). CD c: a
# cd or pushd target, whose .. stays logical. SHOWN: the word for the block
# message. Judged with nocasematch and nocaseglob off (bash 3.2's =~ obeys
# either), both put back.
pw_word() {
  local nc="" ng=""
  # r4 decision 6: a plain absolute word six or more components deep, outside
  # /Volumes, /Network, /net, /home, /System and /private and with no
  # component starting with a dot, in the folder the last word resolved when
  # that folder holds no link (pw_pcd, pw_pcr), is no root unless it is a link
  # itself: one lstat, where pw_word_judge would give the same verdict
  case "$1" in
    /[!VvNnHhSsPp]*/*/*/*/*/*)
      case "$1" in *[\{\(\[*?\$\`\\]*|*/.*|*//*|*[$'\001'-$'\006']*) ;; *)
        if [ -n "${pw_hfast-}" ] && [ "${#1}" -le 1024 ] && [ "${1%/*}" = "${pw_pcd-}" ] && [ "${pw_pcr-}" = "${1%/*}" ] && ! pw_islink "$1"; then
          pw_t=""; return 0
        fi ;;
      esac ;;
  esac
  if shopt -q nocasematch; then nc=1; shopt -u nocasematch; fi
  if shopt -q nocaseglob; then ng=1; shopt -u nocaseglob; fi
  pw_word_judge "$@"
  [ -z "$nc" ] || shopt -s nocasematch
  [ -z "$ng" ] || shopt -s nocaseglob
}
pw_word_judge() {
  local w=$1 ctx=${2-z} q=${3-} f=${4-p} shown=${5-$1} a="" broad="" glob=""
  pw_t=""
  # r4 decision 6: a word past the scan cap is broad, unscanned (fails closed)
  if pw_capped "$w"; then pw_sentinel "${shown:0:120}" " (not judged: more than 64 { or [, 256 components, or 4 KB)"; return 0; fi
  # r4 decision 9, widened by r5 decision 10: ~ followed by $, ${, $( or a
  # backtick is the home, failing closed (zsh expands the parameter or the
  # command before the ~: ~$USER, ~$LOGNAME, ~$(id -un)); placed even where
  # the text rules would read ~ by name: they see the word as written
  case "$w" in '~$'*|'~`'*) pw_tilde "$w"; w=$pw_w; q=q ;; esac
  # r4 decision 6: a plain absolute word (no glob, brace, alternation, $ or
  # backtick) goes straight to the written-path judgment pw_alt would give it;
  # a word with six or more slashes is no root the text rules know by name
  case "$w" in
    /*) case "$w" in *[\{\(\[*?\$\`]*|*[$'\001'-$'\006']*) ;; *)
          if pw_written "$w" "$f"; then
            if [ "$q" = q ] || ! [[ $w =~ $pw_root_re ]]; then pw_sentinel "$shown"; fi
          else
            case "$w" in /*/*/*/*/*/*) ;; *) if [[ $w =~ $pw_root_re ]]; then pw_t=F; fi ;; esac
          fi
          return 0 ;;
        esac ;;
  esac
  pw_expand "$w" "$ctx"
  if [ -n "$pw_fc" ]; then pw_sentinel "$shown" " (glob)"; return 0; fi
  if [ "${#pw_alts[@]}" -eq 1 ] && [ -z "$pw_grp" ]; then
    a=${pw_alts[0]}
    pw_alt "$a" "$ctx" "$f"
    case "$pw_av" in
      B) if [ "$q" = q ] || ! [[ $a =~ $pw_root_re ]]; then pw_sentinel "$shown"; fi ;;
      N) if [[ $a =~ $pw_root_re ]]; then pw_t=F; fi ;;
      G) pw_sentinel "$shown" " (glob)" ;;
      NG) pw_t=F ;;
    esac
    return 0
  fi
  for a in ${pw_alts[@]+"${pw_alts[@]}"}; do
    pw_alt "$a" "$ctx" "$f"
    case "$pw_av" in
      B) broad=1 ;;
      G) broad=1; glob=1 ;;
      T) if [[ $a =~ $pw_root_re ]]; then broad=1; fi ;;
    esac
    [ -z "$broad" ] || break
  done
  if [ -n "$broad" ]; then pw_sentinel "$shown" "${glob:+ (glob)}"; else pw_t=F; fi
}
# pw_tilde WORD: pw_w is WORD (~ followed by $ or a backtick) read from the home:
# its user part (to the matching ), } or backtick, then to the first /) dropped
# (r5 decision 10).
pw_tilde() {
  local rest="" d=1 k=0
  case "$1" in
    '~$('*) rest=${1:3}
      while [ "$k" -lt "${#rest}" ] && [ "$d" -gt 0 ]; do
        case "${rest:k:1}" in '(') d=$((d + 1)) ;; ')') d=$((d - 1)) ;; esac
        k=$((k + 1))
      done
      rest=${rest:k} ;;
    '~${'*) rest=${1#*\}} ;;
    '~`'*) rest=${1:2}; rest=${rest#*\`} ;;
    *) rest=${1:2} ;;
  esac
  case "$rest" in */*) pw_w="~/${rest#*/}" ;; *) pw_w="~" ;; esac
}
# pw_capped WORD: WORD has more than 64 { or [, or over 4 KB, or more than 256
# components (r4 decision 6; the last: each component costs the folder walks
# a pass over the word, so 2,000 of them in 4 KB took seconds).
pw_capped() {
  # (a word of at most 1,024 characters is at most 4 KB in any encoding, so
  # only a longer one has its bytes counted, under LC_ALL=C)
  [ "${#1}" -le 4096 ] || return 0
  [ "${#1}" -le 1024 ] || pw_bytes "$1" || return 0
  if [ "${#1}" -gt 256 ]; then pw_count '/' "$1"; [ "$pw_c" -le 256 ] || return 0; fi
  case "$1" in *[\{\[]*) ;; *) return 1 ;; esac
  pw_count '{[' "$1"
  [ "$pw_c" -gt 64 ]
}
# pw_bytes WORD: WORD is at most 4,096 bytes.
pw_bytes() { local LC_ALL=C; [ "${#1}" -le 4096 ]; }
# pw_count CHARS TEXT: pw_c is how many bytes of TEXT are in CHARS (no
# whitespace among them), counted in one pass by field splitting; bash's
# ${x//...} is quadratic on a long word.
pw_count() {
  local LC_ALL=C IFS=$1 f=""
  local -a parts
  case $- in *f*) f=1 ;; esac
  set -f
  parts=( x${2}x )
  [ -n "$f" ] || set +f
  pw_c=$(( ${#parts[@]} - 1 ))
}
# pw_alt WORD CONTEXT CD: pw_av is how one alternative of a word (no group
# left) is judged: B broad, N not broad, G broad as a glob, NG not broad as a
# glob, T left to the text rules (it still holds $ or a backtick, or is /*).
# r4 decision 1: a glob word is broad when its reading with each
# <component>/.. after the first glob component folded away (pw_fold) is, as
# well as when the unfolded reading (pw_glob) is.
pw_alt() {
  local w=$1 ctx=$2 f=$3 b="" fd="" sv="" r=""
  pw_av=T
  case "$w" in
    '/*') return 0 ;;
    *[*?[]*) ;;
    *) pw_abs "$w"
       case "$pw_p" in ''|*'$'*|*'`'*) return 0 ;; esac
       if pw_written "$pw_p" "$f"; then pw_av=B; else pw_av=N; fi
       return 0 ;;
  esac
  pw_stars "$w" "$ctx"; b=$pw_w
  if [ "$ctx" != b ]; then pw_brackets "$b"; b=$pw_w; fi
  pw_fold "$b"; fd=$pw_fd
  pw_glob "$b" "$ctx" "$f"
  if [ "$pw_av" != G ] && [ "$fd" != "$b" ]; then
    sv=$pw_av; pw_glob "$fd" "$ctx" "$f"; [ "$pw_av" = G ] || pw_av=$sv
  fi
  # r5 decision 10: a glob word climbing out of a /Volumes entry with .. is
  # also read from / (pw_voldots, as a written path's is), so /Volumes/M*/../x
  # is judged like /Volumes/Macintosh HD/../x
  if [ "$pw_av" != G ] && [ "$f" != c ]; then
    pw_abs "$b"
    case "$pw_p" in
      /*) if pw_voldots "$pw_p"; then sv=$pw_av; pw_glob "$pw_vd" "$ctx" "$f"; [ "$pw_av" = G ] || pw_av=$sv; fi ;;
    esac
  fi
  # r5 decision 6: a component bash can match against .. (.*, .?, .[.], .[!a])
  # also reads as its folder and as that folder's parent, in every context;
  # each reading is judged as the word is (folded too), and more than three
  # such components fail closed
  if [ "$pw_av" != G ]; then
    pw_dotdots "$b"
    if [ -n "$pw_ddc" ]; then pw_av=G; return 0; fi
    sv=$pw_av
    for r in ${pw_dd[@]+"${pw_dd[@]}"}; do
      pw_glob "$r" "$ctx" "$f"; [ "$pw_av" != G ] || return 0
      pw_fold "$r"
      if [ "$pw_fd" != "$r" ]; then pw_glob "$pw_fd" "$ctx" "$f"; [ "$pw_av" != G ] || return 0; fi
    done
    pw_av=$sv
  fi
}
# pw_dotdot COMP: bash 3.2 can match the glob component COMP against the name
# .. (r5 decision 6): COMP starts with a literal dot (a bracket never matches a
# leading one) and, as a pattern, matches ..; a form pw_pattern cannot express
# counts (fails closed). zsh never matches .., so this is bash's reading.
pw_dotdot() {
  local nc="" e="" r=1
  case "$1" in .*[*?[]*) ;; *) return 1 ;; esac
  pw_pattern "$1" || return 0
  shopt -q nocasematch && nc=1
  shopt -q extglob && e=1
  shopt -u extglob nocasematch
  [[ .. == $pw_pat ]] && r=0
  [ -z "$nc" ] || shopt -s nocasematch
  [ -z "$e" ] || shopt -s extglob
  return $r
}
# pw_dotdots WORD: pw_dd is every reading of WORD with each component bash can
# match against .. as itself, . (its folder) or .. (its parent), the word as
# written left out; pw_ddc is set when more than three components can (each
# reading is then not made: the word fails closed).
pw_dotdots() {
  local rest="$1/" c="" n=0 m=0 i=0 x=1 k=1 d=0 r=""
  local -a cs at ws
  pw_dd=(); pw_ddc=""
  case "/$1" in */.*[*?[]*) ;; *) return 0 ;; esac
  while [ -n "$rest" ]; do
    c=${rest%%/*}; rest=${rest#*/}
    cs[$n]=$c
    if pw_dotdot "$c"; then at[$m]=$n; m=$((m + 1)); fi
    n=$((n + 1))
  done
  [ "$m" -gt 0 ] || return 0
  if [ "$m" -gt 3 ]; then pw_ddc=1; return 0; fi
  while [ "$i" -lt "$m" ]; do x=$((x * 3)); i=$((i + 1)); done
  while [ "$k" -lt "$x" ]; do
    ws=( "${cs[@]}" ); d=$k; i=0
    while [ "$i" -lt "$m" ]; do
      case $((d % 3)) in 1) ws[${at[$i]}]=. ;; 2) ws[${at[$i]}]=.. ;; esac
      d=$((d / 3)); i=$((i + 1))
    done
    r=${ws[0]}; i=1
    while [ "$i" -lt "$n" ]; do r="$r/${ws[$i]}"; i=$((i + 1)); done
    pw_dd[${#pw_dd[@]}]=$r
    k=$((k + 1))
  done
}
# pw_fold WORD: pw_fd is WORD with each <component>/.. after its first glob
# component folded away by name (r4 decision 1); a .. that reaches back into
# the literal part before that component stays, for pw_physdots.
pw_fold() {
  local rest="$1/" c="" pre="" g="" n=0 i=0 out=""
  local -a st
  pw_fd=$1
  case "/$1/" in */../*) ;; *) return 0 ;; esac
  while [ -n "$rest" ]; do
    c=${rest%%/*}; rest=${rest#*/}
    if [ -z "$g" ]; then
      case "$c" in *[*?[]*) g=1; st[0]=$c; n=1 ;; *) pre="$pre$c/" ;; esac
      continue
    fi
    case "$c" in
      ''|.) ;;
      ..) if [ "$n" -gt 0 ] && [ "${st[n-1]}" != .. ]; then n=$((n - 1)); else st[$n]=..; n=$((n + 1)); fi ;;
      *) st[$n]=$c; n=$((n + 1)) ;;
    esac
  done
  [ -n "$g" ] || return 0
  out=$pre
  while [ "$i" -lt "$n" ]; do out="$out${st[$i]}/"; i=$((i + 1)); done
  out=${out%/}
  pw_fd=${out:-/}
}
# pw_written P CD: the written path P (absolute) is broad: its .. applied where
# a searcher applies it (CD c: a cd target, whose .. stays logical), normalized
# and judged where its links lead; and (r4 decision 9) a searcher's
# /Volumes/<entry>/.. read with that entry as / (pw_voldots) as well.
pw_written() {
  local p=$1
  [ "$2" = c ] || { pw_physdots "$p"; p=$pw_pd; }
  pw_normalize "$p"
  pw_broad "$pw_n" && return 0
  [ "$2" != c ] && pw_voldots "$1" && pw_broad "$pw_vd"
}
# pw_voldots P: P (absolute, as written) climbs out of a /Volumes entry with ..:
# pw_vd is the rest of P read from / (the boot volume's alias is a link to /),
# normalized by name.
pw_voldots() {
  local p=$1 rest="" nc="" r=1
  while :; do case "$p" in *//*) p=${p//\/\//\/} ;; */./*) p=${p//\/.\//\/} ;; *) break ;; esac; done
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  if [[ $p == /volumes/*/* ]]; then
    rest=${p#/*/*/}
    case "/$rest/" in */../*) pw_normalize "/$rest"; pw_vd=$pw_n; r=0 ;; esac
  fi
  [ -n "$nc" ] || shopt -u nocasematch
  return $r
}
# pw_glob WORD CONTEXT CD: pw_av (G, NG or T) for one reading of a glob word,
# after pw_stars and pw_brackets.
pw_glob() {
  local b=$1 ctx=$2 f=$3 x="" c=""
  pw_av=T
  if [ "$ctx" != b ]; then
    case "/$b" in */'**'/*) b=${b%%/'**'/*} ;; esac
  fi
  # r3c 1(a) and 1(b): trailing slashes, /. components and <glob>/.. go first;
  # then a trailing run of components made only of wildcards (or .* or **) is
  # the folder they sit in, whether or not it is ever listed. r4 decision 2:
  # also before a **/, where zsh walks every match of the part before it.
  while :; do
    case "$b" in
      /|'') break ;;
      */) b=${b%/} ;;
      */.) b=${b%/.} ;;
      */..) x=${b%/..}; c=${x##*/}
        case "$c" in *[*?[]*) b=${x%/*} ;; *) break ;; esac ;;
      */*) c=${b##*/}
        if [ "$c" = '.*' ] || [ "$c" = '**' ] || pw_wild "$c"; then b=${b%/*}; else break; fi ;;
      *) break ;;
    esac
    [ -n "$b" ] || b=/
  done
  [ -n "$b" ] || b=/
  case "$b" in
    *[*?[]*) ;;
    *) pw_abs "$b"
       case "$pw_p" in ''|*'$'*|*'`'*) if [[ $b =~ $pw_root_re ]]; then pw_av=G; fi; return 0 ;; esac
       if pw_written "$pw_p" "$f"; then pw_av=G; else pw_av=NG; fi
       return 0 ;;
  esac
  pw_abs "$b"
  case "$pw_p" in ''|*'$'*|*'`'*) return 0 ;; esac
  if pw_globbroad "$pw_p" "$f"; then pw_av=G; else pw_av=NG; fi
}
# --- the command as the rules read it (r3c; r4) ---
# pw_lib_awk: the awk functions the command prep and the judge pass share. value is a word as the shell
# reads it (quotes, backslash escapes, $'...' decoded as bash decodes it, $"..." as "..."); shstep reads a
# shell invocation word by word (r4 decision 3): -o/+o/-O/+O and a cluster ending in o or O take the next
# word, as --rcfile and --init-file do, -- or - ends the options, and the first other word is the -c
# script, or the script file (- and /dev/stdin name stdin); rdop is the length of the redirection operator
# a word starts with.
pw_lib_awk='
  function base(v, n, a) { n = split(v, a, "/"); return a[n] }
  function isname(v) { return v ~ /^(find|rg|grep|egrep|fgrep|rgrep|ugrep)$/ }
  function canon(v) { if (v ~ /^(egrep|fgrep|ugrep)$/) return "grep"; if (v == "rgrep") return "grep -r"; return v }
  function hexv(c) { return index("0123456789abcdef", tolower(c)) - 1 }
  function ansic(t,   out, i, n, c, e, v, m) {
    out = ""; n = length(t); i = 1
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\]+/)) { out = out substr(t, i, RLENGTH); i += RLENGTH; continue }
      c = substr(t, i, 1)
      if (c != "\\" || i == n) { out = out c; i++; continue }
      e = substr(t, i + 1, 1)
      if (e ~ /[0-7]/) {
        v = 0; m = 0
        while (m < 3 && substr(t, i + 1 + m, 1) ~ /[0-7]/) { v = v * 8 + substr(t, i + 1 + m, 1); m++ }
        if (v % 256) out = out sprintf("%c", v % 256)
        i += 1 + m; continue
      }
      if (e == "x" && substr(t, i + 2, 1) ~ /[0-9A-Fa-f]/) {
        v = hexv(substr(t, i + 2, 1)); m = 1
        if (substr(t, i + 3, 1) ~ /[0-9A-Fa-f]/) { v = v * 16 + hexv(substr(t, i + 3, 1)); m = 2 }
        if (v) out = out sprintf("%c", v)
        i += 2 + m; continue
      }
      if (e == "n") out = out "\n"; else if (e == "t") out = out "\t"; else if (e == "r") out = out "\r"
      else if (e == "a") out = out "\007"; else if (e == "b") out = out "\010"; else if (e == "e" || e == "E") out = out "\033"
      else if (e == "f") out = out "\014"; else if (e == "v") out = out "\013"
      else if (e == "\\" || e == "\047" || e == "\"" || e == "?") out = out e
      else out = out "\\" e
      i += 2
    }
    return out
  }
  # (sqend and dqend skip a run of plain characters at once: this awk takes the length of t on every substr,
  # so a character at a time is quadratic on a long word, r5 decision 1)
  function sqend(t, i,   n, c) {
    n = length(t); i++
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\\047]+/)) { i += RLENGTH; continue }
      c = substr(t, i, 1); if (c == "\\") { i += 2; continue }; if (c == "\047") return i; i++
    }
    return 0
  }
  function dqend(t, i,   n, c) {
    n = length(t); i++
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\"]+/)) { i += RLENGTH; continue }
      c = substr(t, i, 1); if (c == "\\") { i += 2; continue }; if (c == "\"") return i; i++
    }
    return 0
  }
  function value(t,   out, i, n, c, j) {
    if (t !~ /[\\\047"$]/) return t
    out = ""; n = length(t); i = 1
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\\047"$]+/)) { out = out substr(t, i, RLENGTH); i += RLENGTH; continue }
      c = substr(t, i, 1)
      if (c == "\\") { out = out substr(t, i + 1, 1); i += 2; continue }
      if (c == "$" && substr(t, i + 1, 1) == "\047") { j = sqend(t, i + 1); if (!j) j = n + 1; out = out ansic(substr(t, i + 2, j - i - 2)); i = j + 1; continue }
      if (c == "$" && substr(t, i + 1, 1) == "\"") { i++; continue }
      if (c == "\047") { j = index(substr(t, i + 1), "\047"); if (!j) j = n - i + 1; out = out substr(t, i + 1, j - 1); i += j + 1; continue }
      if (c == "\"") {
        i++
        while (i <= n && substr(t, i, 1) != "\"") {
          if (match(substr(t, i, 512), /^[^"\\]+/)) { out = out substr(t, i, RLENGTH); i += RLENGTH; continue }
          c = substr(t, i, 1)
          if (c == "\\" && i < n && index("$`\"\\\n", substr(t, i + 1, 1))) { i++; c = substr(t, i, 1) }
          out = out c; i++
        }
        i++; continue
      }
      out = out c; i++
    }
    return out
  }
  function shstep(st, v,   b) {
    if (st["s"] == "" || st["s"] == 0) {
      b = base(v)
      if (b !~ /^(sh|bash|zsh|dash|ksh)$/) return ""
      st["s"] = 1; st["x"] = (b == "zsh") ? "z" : "b"; st["c"] = 0; st["S"] = 0; st["f"] = 0
      return "shell"
    }
    if (st["s"] == 9) return ""
    if (st["s"] == 2) { st["s"] = 1; return "val" }
    if (st["s"] == 1) {
      if (v == "--" || v == "-") { st["s"] = 3; return "opt" }
      if (v == "--rcfile" || v == "--init-file") { st["s"] = 2; return "opt" }
      if (v ~ /^--./) return "opt"
      if (v ~ /^[-+][A-Za-z]+$/) {
        if (v ~ /^-/ && v ~ /c/) st["c"] = 1
        if (v ~ /^-/ && v ~ /s/) st["S"] = 1
        if (v ~ /[oO]$/) st["s"] = 2
        return "opt"
      }
    }
    st["s"] = 9
    if (st["c"]) return "script"
    if (v == "-" || v == "/dev/stdin" || v == "/dev/fd/0") return "stdin"
    st["f"] = 1
    return "file"
  }
  function sx(st, s) { return (st["x"] == "b" && s ~ /globstar/) ? "z" : st["x"] }
  function rdop(v) { if (match(v, /^[0-9]*(<<<|<<-|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)/)) return RLENGTH; return 0 }'
# pw_prep_awk, mode prep: the command with, in every simple command and in every quoted string holding a
# searcher's name, (1) a searcher named by a path, a backslash or a quote, or as egrep, fgrep or ugrep, made
# find, rg or grep (rgrep grep -r), as is $(which X), $(command -v X) and `which X` (r3c decision 5), in
# command position: past shell keywords, a function body's {, assignments and redirections, and the
# wrappers by basename with their options' values (snapshot-5's WRAPPERS table; r4 decision 4); (2) a
# simple command that holds a searcher and its own unquoted depth bound (-maxdepth, --max-depth, -d N), or
# whose searcher prints and exits (rg --version, -V, --help, -h, --type-list; find --version; grep
# --version, -V, --help; not another option's value; r4 decision 8), made `:`; (3) a bare `cd` or `cd --`
# made `cd ~`; (4) a comment dropped. Every other byte is kept, but for a space between an opening
# backtick and the searcher it runs, so the rules see its name. Then (r4 decision 3) every script a shell or eval runs is added
# as its own lines, each line led by \003<context>\003 and a space (b in a script run by bash, sh, dash or
# ksh without globstar, z otherwise), itself read the same way, to depth 4 (a searcher's script deeper adds
# the line \003D\003): a shell's -c script; eval's arguments, joined; and, for a shell given no script, the
# here-string it reads, or what an echo, print, printf or cat <<< piped into it writes. Mode nopath prints 1
# when some simple command is a recursive searcher given no path, so it walks ".": find with an
# expression first, rg with no path and nothing readable on its stdin (no pipe into it, no < from a file,
# no here-string; a /dev/ file or a closed stdin is nothing, r4 decision 7; --files lists "." whatever its
# stdin), grep -r with none.
pw_prep_awk="$pw_lib_awk"'
  function wv(w, list,   a, i, n) { n = split(list, a, " "); for (i = 1; i <= n; i++) WVAL[w, a[i]] = 1 }
  function lv(c, list,   a, i, n) { n = split(list, a, " "); for (i = 1; i <= n; i++) LVAL[c, a[i]] = 1 }
  function subst(t,   out, m, w) {
    out = ""
    while (match(t, /\$\((which|command[[:space:]]+-v)[[:space:]]+[^[:space:]()`]+[[:space:]]*\)|`(which|command[[:space:]]+-v)[[:space:]]+[^[:space:]()`]+[[:space:]]*`/)) {
      m = substr(t, RSTART, RLENGTH); w = m
      sub(/^(\$\(|`)(which|command[[:space:]]+-v)[[:space:]]+/, "", w); sub(/[[:space:]]*(\)|`)$/, "", w)
      out = out substr(t, 1, RSTART - 1) (isname(base(w)) ? w : m); t = substr(t, RSTART + RLENGTH)
    }
    return out t
  }
  function inner(q, depth, ctx) { return (mode == "prep" && depth < 6 && q ~ SRCH) ? prep(q, depth + 1, 1, ctx) : q }
  # parend(t, i): where the ) matching the ( at i is, quotes and escapes skipped; 0 at none. btend(t, i): where
  # the backtick closing the one at i is; 0 at none. r6 decision 5: the body of each heredoc is read, the lines
  # after the line holding its << to its delimiter line (a body that never ends disables only its <<, and PF
  # remembers it), and a double-quoted string ends where dqhend says. Every call shares one budget of 20,000
  # steps: past it, no end is found and the command is too complex to judge (cplx), since a dense run of ( or $(
  # costs a step each and each $( or (( read walks the rest of the command once. (A special character is taken
  # without the window match, which costs its whole window when it fails.) r7 decision 1: a (( the shells read as
  # arithmetic (arend) is passed over whole, and so is a $[...]: in them a << is a shift, never a heredoc, and the
  # span may cross lines, while a $( or a backtick in them is still a command (parend(t, i, ar): ar 1 reads such a
  # span to the ) closing the ( at i, PAI keeping where its first inner ( closed, and ar 2 to the ] closing the [
  # at i). r7 decision 2: a heredoc body is read a line at a time, the end of each line found 512 bytes at a time
  # (lnend, never a copy of the rest of the command) and kept in LE for the reading of the next delimiter, and each
  # line read costs a step.
  function parend(t, i, ar,   n, d, c, j, np, PD, PS, PF, m, e, s, f, L, LE, ai) {
    n = length(t); d = 0; np = 0; ai = 0
    while (i <= n) {
      if (++pst > 20000) { cplx = 1; return 0 }
      c = substr(t, i, 1)
      if (ar) {
        if (c !~ /[]\\\047"()$`[]/ && match(substr(t, i, 512), /^[^]\\\047"()$`[]+/)) { i += RLENGTH; continue }
        if (c == "\\") { i += 2; continue }
        if (c == "\047") { j = sqend(t, i); if (!j) return 0; i = j + 1; continue }
        if (c == "\"") { j = dqhend(t, i); if (!j) return 0; i = j + 1; continue }
        if (c == "`") { j = btend(t, i); if (!j) return 0; i = j + 1; continue }
        if (c == "$" && substr(t, i + 1, 1) == "(" && substr(t, i + 2, 1) != "(") { j = parend(t, i + 1); if (!j) return 0; i = j + 1; continue }
        if (ar == 1 && c == "(") d++
        else if (ar == 1 && c == ")") { if (--d == 0) { PAI = ai; return i }; if (d == 1 && !ai) ai = i }
        else if (ar == 2 && c == "[") d++
        else if (ar == 2 && c == "]" && --d == 0) return i
        i++; continue
      }
      if (c !~ /[\\\047"()<\n$]/ && match(substr(t, i, 512), /^[^\\\047"()<\n$]+/)) { i += RLENGTH; continue }
      if (c == "\\") { i += 2; continue }
      if (c == "\047") { j = sqend(t, i); if (!j) return 0; i = j + 1; continue }
      if (c == "\"") { j = dqhend(t, i); if (!j) return 0; i = j + 1; continue }
      if (c == "$") { if (substr(t, i + 1, 1) == "[" && (j = parend(t, i + 1, 2))) i = j + 1; else i++; continue }
      if (c == "<") {
        if (substr(t, i, 3) == "<<<") { i += 3; continue }
        if (substr(t, i + 1, 1) == "<" && (j = hdword(t, i))) {
          if (!((HDS, HDD) in PF)) { PD[++np] = HDD; PS[np] = HDS }
          i = j; continue
        }
        i++; continue
      }
      if (c == "\n") {
        i++
        for (m = 1; m <= np; m++) {
          s = i; f = 0; L = length(PD[m])
          while (i <= n) {
            if (++pst > 20000) { cplx = 1; return 0 }
            if (!(i in LE)) { LE[i] = lnend(t, i, n); if (cplx) return 0 }
            e = LE[i]
            if (!PS[m]) f = (e - i == L && substr(t, i, L) == PD[m])
            else f = (e - i >= L && substr(t, e - L, L) == PD[m] && (e - L == i || substr(t, i, e - L - i) !~ /[^\t]/))
            i = e + 1
            if (f) break
          }
          if (!f) { i = s; PF[PS[m], PD[m]] = 1 }
        }
        np = 0; continue
      }
      if (c == "(" && substr(t, i + 1, 1) == "(" && (j = arend(t, i))) { if (!d) return j; i = j + 1; continue }
      if (cplx) return 0
      if (c == "(") d++; else if (--d == 0) return i
      i++
    }
    return 0
  }
  # arend(t, i): for the (( at i, the ) closing it when the shells read it as arithmetic (its inner ( closing right
  # before its outer one, read in the arithmetic mode of parend), 0 when they read a subshell (r7 decision 1). r8
  # decision 3: a $(( whose span ends in )) is arithmetic whatever its inner ( closes on, as bash reads
  # $((1) + (1<<X)), so a << in it is a shift; where a shell reads a substitution there, the lines after it are
  # still read as commands (an over-block at worst)
  function arend(t, i,   j) { j = parend(t, i, 1); return (j && (PAI == j - 1 || (substr(t, i - 1, 1) == "$" && substr(t, j - 1, 1) == ")"))) ? j : 0 }
  # lnend(t, i, n): where the line starting at i ends (its newline, or n + 1), read 512 bytes at a time, each window
  # past the first a step of the budget (r7 decision 2)
  function lnend(t, i, n,   k, e) {
    for (k = i; k <= n; k += 512) {
      if (e = index(substr(t, k, 512), "\n")) return k + e - 1
      if (k > i && ++pst > 20000) { cplx = 1; return n + 1 }
    }
    return n + 1
  }
  # dqhend(t, i): dqend, but a $( inside the double quotes runs to the ) parend finds, its heredocs read, so a lone
  # " in one hides nothing (r6 decision 5); a $( with no end is read as dqend reads it
  function dqhend(t, i,   n, c, j) {
    n = length(t); i++
    while (i <= n) {
      c = substr(t, i, 1)
      if (c !~ /[\\"$]/ && match(substr(t, i, 512), /^[^\\"$]+/)) { i += RLENGTH; continue }
      if (c == "\\") { i += 2; continue }
      if (c == "\"") return i
      if (c == "$" && substr(t, i + 1, 1) == "(" && (j = parend(t, i + 1))) { i = j + 1; continue }
      i++
    }
    return 0
  }
  # spans(s, ctx, depth, ev): each command substitution in s ($(...) or a backtick pair, quotes and escapes
  # skipped) that names a searcher is judged as its own lines; with ev, what it writes is (r5 decision 11)
  function spans(s, ctx, depth, ev,   n, i, c, j, x) {
    n = length(s); i = 1
    while (i <= n && !cplx) {
      if (match(substr(s, i, 512), /^[^\\$`]+/)) { i += RLENGTH; continue }
      c = substr(s, i, 1); j = 0
      if (c == "\\") { i += 2; continue }
      if (c == "`") { j = btend(s, i); if (j) x = substr(s, i + 1, j - i - 1) }
      else if (substr(s, i + 1, 1) == "(") { j = parend(s, i + 1); if (j) x = substr(s, i + 2, j - i - 2) }
      if (!j) { i++; continue }
      if (ev) { x = subout(x, depth, ctx); if (x != "") script(x, ctx, depth) }
      else if (named(x)) script(x, ctx, depth)
      i = j + 1
    }
  }
  # subout(s, depth, ctx): what the commands of s write, each read as a pipe producer is (r5 decision 11)
  function subout(s, depth, ctx,   sv, o) {
    if (depth >= 4) { if (named(s)) deep = 1; return "" }
    sv = coll; coll = ""; collect++
    prep(s, depth + 1, 1, ctx)
    collect--; o = coll; coll = sv
    return o
  }
  # pfmt(val, rd, k, nt): what printf writes for its format (the first word from k) and the words after it: each
  # conversion takes the next word (%b with its escapes decoded), the format repeating while words remain (r5
  # decision 11)
  function pfmt(val, rd, k, nt,   f, o, i, n, a, m, j, used, r) {
    while (k <= nt && rd[k]) k++
    if (k > nt) return ""
    f = ansic(val[k]); n = length(f); m = 0
    for (j = k + 1; j <= nt; j++) if (!rd[j]) a[++m] = val[j]
    j = 1; o = ""; r = 0
    do {
      used = 0; i = 1
      while (i <= n) {
        if (match(substr(f, i, 512), /^[^%]+/)) { o = o substr(f, i, RLENGTH); i += RLENGTH; continue }
        if (substr(f, i + 1, 1) == "%") { o = o "%"; i += 2; continue }
        if (match(substr(f, i + 1, 32), /^[-+ #0-9.]*[a-zA-Z]/)) {
          o = o (j > m ? "" : (substr(f, i + RLENGTH, 1) == "b") ? ansic(a[j]) : a[j]); j++; used = 1; i += 1 + RLENGTH; continue
        }
        o = o "%"; i++
      }
    } while (used && j <= m && ++r < 64)
    return o
  }
  function btend(t, i,   n, c) {
    n = length(t); i++
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\`]+/)) { i += RLENGTH; continue }
      c = substr(t, i, 1); if (c == "\\") { i += 2; continue }; if (c == "`") return i; i++
    }
    return 0
  }
  function named(s,   d) { d = s; gsub(/[\047"\\$]/, "", d); return s ~ SRCH || d ~ SRCH }
  # pwn(s): s names a searcher as pw_named reads one, its four tests (r6 decision 2)
  function pwn(s,   d) {
    if (s ~ /(^|[[:space:];&|(\/\\\047"`])(find|rg|[efru]?grep)[`\047")]?([[:space:]]|$)/) return 1
    d = s; gsub(/[\\\047"$]/, "", d)
    if (d ~ /(^|[[:space:];&|(\/`])(find|rg|[efru]?grep)[`)]?([[:space:]]|$)/) return 1
    if (s ~ /\$\047[^\047]*\\[0-7xX]/) return 1
    return s ~ /(^|[[:space:];&|(`])=(find|rg|[efru]?grep)([[:space:]]|$)/
  }
  function script(s, x, depth,   y) {
    if (depth >= 4) { if (named(s) || named(ansic(s))) deep = 1; return }
    y = prep(subst(s), depth + 1, 0, x)
    gsub(/\n/, "\n\003" x "\003 ", y)
    xlines = xlines "\n\003" x "\003 " y
  }
  function ismeta(b, val, rd, t, nt,   k, v, j, sv, c) {
    if (b == "find") {
      for (k = t + 1; k <= nt; k++) {
        if (rd[k]) continue
        v = val[k]
        if (v ~ /^-(exec|execdir|ok|okdir)$/) { while (k < nt && val[k + 1] != ";" && val[k + 1] != "+") k++; k++; continue }
        if (v == "-fprintf") { k += 2; continue }
        if (("find", v) in LVAL || v ~ /^-newer/) { k++; continue }
        if (v ~ /^--?(help|version)$/) return 1
      }
      return 0
    }
    if (b == "rg") { sv = "ABCEMTdefgjmrt"; c = "rg" } else if (b == "grep" || b == "grep -r") { sv = "ABCDdefmgOtMNKJ"; c = "grep" } else return 0
    for (k = t + 1; k <= nt; k++) {
      if (rd[k]) continue
      v = val[k]
      if (v == "--") return 0
      if (v ~ /^--./) {
        if (c == "rg" && v ~ /^--(help|version|type-list|generate|pcre2-version)(=|$)/) return 1
        if (c == "grep" && v ~ /^--(help|version)$/) return 1
        if (v !~ /=/ && ((c, v) in LVAL)) k++
        continue
      }
      if (v ~ /^-./) {
        for (j = 2; j <= length(v); j++) {
          if (c == "rg" && substr(v, j, 1) ~ /[hV]/) return 1
          if (c == "grep" && substr(v, j, 1) == "V") return 1
          if (index(sv, substr(v, j, 1))) { if (j == length(v)) k++; break }
        }
      }
    }
    return 0
  }
  # (this awk takes at most 50 arguments and locals in a call, so r6 keeps its own in st: st["ps"] what a process
  # substitution writes, st["pr"] the roots a pipe carries; r7: st["vt"] the vtop of the prep, st["ka"] a keyword
  # before the command word, st["lp"] whether this command passes on what its process substitution writes, kept as
  # lastps)
  function simple(seg, depth, piped, noext, ctx, bt, hd,   ex, isfr, fw, xe, nt, i, n, c, j, t, b, hs, hb, out, w, tk, val, sp, qd, stdin, npos, hasE, rec, k, files, rd, hstr, st, pin, prod, pc, scr, ev, op, tg, vrp) {
    pin = pipein; st["pr"] = piperoots; st["ps"] = pipeps; st["vt"] = vtop; st["ka"] = 0
    nt = 0; i = 1; n = length(seg); sp[0] = ""
    while (i <= n) {
      # (runs of blanks and of plain characters are taken at once: one at a time is quadratic on a long word)
      if (match(substr(seg, i, 512), /^[[:space:]]+/)) { sp[nt] = sp[nt] substr(seg, i, RLENGTH); i += RLENGTH; continue }
      nt++; tk[nt] = ""; sp[nt] = ""
      while (i <= n) {
        if (match(substr(seg, i, 512), /^[^[:space:]\\\047"$]+/)) { tk[nt] = tk[nt] substr(seg, i, RLENGTH); i += RLENGTH; continue }
        c = substr(seg, i, 1)
        if (c ~ /[[:space:]]/) break
        if (c == "\\") { tk[nt] = tk[nt] substr(seg, i, 2); i += 2; continue }
        if (c == "$" && substr(seg, i + 1, 1) == "\047") {
          j = sqend(seg, i + 1)
          if (!j) { tk[nt] = tk[nt] substr(seg, i); i = n + 1; continue }
          tk[nt] = tk[nt] substr(seg, i, j - i + 1); i = j + 1; continue
        }
        if (c == "\047") {
          j = index(substr(seg, i + 1), "\047")
          if (!j) { tk[nt] = tk[nt] substr(seg, i); i = n + 1; continue }
          tk[nt] = tk[nt] "\047" inner(substr(seg, i + 1, j - 1), depth, ctx) "\047"; i += j + 1; continue
        }
        if (c == "\"") {
          j = dqhend(seg, i)
          if (!j) { tk[nt] = tk[nt] substr(seg, i); i = n + 1; continue }
          # r5 (decision 5): a command substitution in double quotes runs, so its command is judged on its own
          if (mode == "prep" && !noext) spans(substr(seg, i + 1, j - i - 1), ctx, depth)
          tk[nt] = tk[nt] "\"" inner(substr(seg, i + 1, j - i - 1), depth, ctx) "\""; i = j + 1; continue
        }
        tk[nt] = tk[nt] c; i++
      }
    }
    for (t = 1; t <= nt; t++) { val[t] = value(tk[t]); qd[t] = (tk[t] ~ /["\047\\]/) }
    # redirections: rd is 1 for an operator word and 2 for its target; hstr is a here-string
    hstr = ""
    for (k = 1; k <= nt; k++) {
      if (rd[k] || !(j = rdop(tk[k]))) continue
      rd[k] = 1
      if (j == length(tk[k])) { if (k < nt) { rd[k + 1] = 2; if (substr(tk[k], j - 2, 3) == "<<<") hstr = val[k + 1] } }
      else if (substr(tk[k], j - 2, 3) == "<<<") hstr = value(substr(tk[k], j + 1))
    }
    t = 1
    if (t <= nt && tk[t] ~ /^\003.\003$/) t++
    # (r7 decision 5: ld counts the compound commands open at the top of the command, st["ka"] a keyword before the
    # command word; r8 decision 1: time or time -p before a compound command is a keyword too, so the command it
    # times is counted, while before a simple command it stays a wrapper)
    while (t <= nt) {
      if (rd[t]) { t++; continue }
      if (!qd[t] && val[t] ~ /^(if|then|else|elif|while|until|do|done|fi|esac|coproc|!|\{|\})$/) {
        if (mode == "prep" && !depth) { if (val[t] ~ /^(if|while|until|\{)$/) ld++; else if (val[t] ~ /^(fi|done|esac|\})$/ && ld > 0) ld-- }
        st["ka"] = 1; t++; continue
      }
      if (!qd[t] && val[t] == "time") {
        k = t + 1; if (k <= nt && val[k] == "-p") k++
        if (k <= nt && !qd[k] && val[k] ~ /^(if|while|until|for|select|case|repeat|foreach|coproc|!|\{)$/) { st["ka"] = 1; t = k; continue }
      }
      if (!qd[t] && val[t] == "function" && t < nt) { st["ka"] = 1; t += 2; continue }
      if (val[t] ~ /^[A-Za-z_][A-Za-z0-9_]*=/) { t++; continue }
      break
    }
    # (r8 decision 1: the repeat and foreach of zsh open one too; the end of a foreach is not counted, so it stays open)
    if (mode == "prep" && !depth && t <= nt && !qd[t] && val[t] ~ /^(for|select|case|repeat|foreach)$/) ld++
    # r7 decision 5: a plain assignment where it lasts (st["vt"]), with no compound command open, ends the reading
    # of a loop variable. r8 decision 1: never in a command that defines a function (FN: a name() or the word
    # function anywhere in it), whose body runs out of textual order; from there the name stands for the word
    # assigned when that word is plain (no $, backtick, quote, backslash or blank), and any other value joins the
    # words of the loop, so neither the roots of the loop nor those of the value are lost. r9 decision 1: where
    # that reset does not apply (vrp: a compound command open, a keyword before it, an assignment that may not
    # last, or a command that defines a function), the value joins the words of the loop too, since the shell may
    # hold it when the loop variable is read. r10 decision 1: inside a substitution or a nested script too (depth),
    # where st["vt"] is 0, so there the value always joins
    if (mode == "prep" && t > nt) {
      vrp = (!st["ka"] && !ld && st["vt"] && !FN)
      for (k = 1; k <= nt; k++) {
        if (rd[k] || !match(tk[k], /^[A-Za-z_][A-Za-z0-9_]*=/)) continue
        w = substr(tk[k], 1, RLENGTH - 1); op = substr(tk[k], RLENGTH + 1)
        if ((w in LVAR) && op != "") LVAR[w] = (vrp && op !~ /[$`\047"\\[:space:]]/) ? op : LVAR[w] " " op
      }
    }
    # r5 decision 11: zsh reads =cmd in command position as cmd (its path), after a wrapper too
    if (t <= nt && !qd[t] && val[t] ~ /^=[A-Za-z_.\/]/) val[t] = substr(val[t], 2)
    while (t <= nt && (base(val[t]) in WRAP)) {
      w = base(val[t]); t++; if (w == "xargs") st["a"] = 1
      while (t <= nt) {
        if (rd[t]) { t++; continue }
        if (val[t] == "--") { t++; break }
        if (w == "env" && val[t] ~ /^[A-Za-z_][A-Za-z0-9_]*=/) { t++; continue }
        if (val[t] !~ /^-./) break
        if (val[t] ~ /^--/) { if (val[t] !~ /=/ && ((w, val[t]) in WVAL)) t++; t++; continue }
        for (j = 2; j <= length(val[t]); j++) if ((w, "-" substr(val[t], j, 1)) in WVAL) break
        if (j == length(val[t])) t++
        t++
      }
      t += WPOS[w]
      if (t <= nt && !qd[t] && val[t] ~ /^=[A-Za-z_.\/]/) val[t] = substr(val[t], 2)
    }
    if (t <= nt) { b = base(val[t]); if (isname(b) && tk[t] != canon(b)) { tk[t] = canon(b); val[t] = tk[t] } }
    # r6 decision 9: a for loop variable stands for its words, and a variable a piped while-read reads for the roots
    # of what the pipe carries (find: its roots; any other command: its words), as {} does for -exec; a searcher
    # operand that is only $V, "$V", ${V} or "${V}" is read as them (LVAR)
    if (mode == "prep" && t <= nt) {
      if (val[t] == "for" && t + 2 <= nt && val[t + 2] == "in" && val[t + 1] ~ /^[A-Za-z_][A-Za-z0-9_]*$/) {
        w = ""; for (k = t + 3; k <= nt; k++) if (!rd[k]) w = w (w == "" ? "" : " ") tk[k]
        if (w != "") LVAR[val[t + 1]] = w
      } else if (val[t] == "read" && st["pr"] != "") {
        for (k = t + 1; k <= nt; k++) {
          if (rd[k]) continue
          if (val[k] ~ /^-[a-zA-Z]*[adnNptu]$/) { k++; continue }
          if (val[k] ~ /^[A-Za-z_][A-Za-z0-9_]*$/) LVAR[val[k]] = st["pr"]
        }
      } else if (isname(base(val[t])) || val[t] == "grep -r") {
        for (k = t + 1; k <= nt; k++) {
          if (rd[k] || tk[k] !~ /^"?\$(\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)"?$/) continue
          w = tk[k]; gsub(/["${}]/, "", w)
          if (w in LVAR) { tk[k] = LVAR[w]; val[k] = value(tk[k]); qd[k] = (tk[k] ~ /["\047\\]/) }
        }
      }
    }
    # a searcher right after an opening backtick gets a space before it, so the rules see its name
    if (bt && t == 1 && sp[0] == "" && isname(base(val[1]))) sp[0] = " "
    # r5 decision 3: find arguments end at -exec, -execdir, -ok or -okdir (ex: 1 for the operator and its
    # terminator, 2 for the command it runs), and its roots (isfr; fw as written) are the words before its
    # expression
    fw = ""
    if (t <= nt && val[t] == "find") {
      for (k = t + 1; k <= nt; k++) {
        if (rd[k] || val[k] !~ /^-(exec|execdir|ok|okdir)$/) continue
        ex[k] = 1
        for (k++; k <= nt && val[k] != ";" && !(val[k] == "+" && val[k - 1] == "{}"); k++) if (!rd[k]) ex[k] = 2
        if (k <= nt) ex[k] = 1
      }
      k = t + 1
      while (k <= nt && (rd[k] || val[k] ~ /^-([HLPEXdsx]|O[0-9]*)$/)) k++
      for (; k <= nt; k++) {
        if (rd[k]) continue
        if (val[k] ~ /^-./ || val[k] == "(" || val[k] == "!") break
        fw = fw (fw == "" ? "" : " ") tk[k]; isfr[k] = 1
      }
    }
    hs = 0; hb = 0
    for (k = 1; k <= nt; k++) {
      if (isname(base(val[k])) || val[k] == "grep -r") hs = 1
      if (!ex[k] && !qd[k] && (tk[k] ~ /^--?max-?depth(=.*)?$/ || tk[k] ~ /^-d[0-9]+$/ || (tk[k] == "-d" && k < nt && tk[k + 1] ~ /^[0-9]+$/))) hb = 1
    }
    if (mode == "nopath") {
      if (hb || t > nt) return ""
      b = val[t]; stdin = piped; npos = 0; hasE = 0; rec = 0; files = 0
      if (b == "grep -r") { b = "grep"; rec = 1 }
      if (b == "find") {
        k = t + 1
        while (k <= nt && val[k] ~ /^-([HLPEXdsx]|O[0-9]*)$/) k++
        if (k > nt || val[k] ~ /^-./ || val[k] == "(" || val[k] == "!") found = 1
        return ""
      }
      if (b != "rg" && b != "grep") return ""
      for (k = t + 1; k <= nt; k++) {
        w = tk[k]
        if (rd[k] == 2) continue
        if (rd[k]) {
          # stdin as the redirections leave it, in order: a here-string or heredoc, or a < from a file, is
          # readable; a < from a /dev/ file, or <&-, is not (r4 decision 7); another fd leaves it alone
          j = rdop(w); op = substr(w, 1, j); tg = (j < length(w)) ? value(substr(w, j + 1)) : val[k + 1]
          if (op !~ /^0?(<|<<|<<-|<<<|<&|<>)$/) continue
          sub(/^0/, "", op)
          # r5 decision 11: <&0 leaves stdin as it was
          if (op == "<&" && tg == "0") continue
          if (op ~ /^<</) stdin = 1
          else if (op == "<&") stdin = (tg == "-") ? 0 : 1
          else stdin = (tg ~ /^\/dev\//) ? 0 : 1
          continue
        }
        if (w == "--") { for (k++; k <= nt; k++) if (!rd[k]) npos++; break }
        if (w ~ /^--/) {
          if (w ~ /^--(regexp|file|files)(=|$)/) hasE = 1
          if (w == "--files") files = 1
          if (w ~ /^--(recursive|dereference-recursive)$/ || w ~ /^--directories=recurse$/) rec = 1
          if (w !~ /=/ && w ~ /^--(regexp|file|glob|iglob|type|type-not|type-add|type-clear|max-count|max-columns|max-filesize|context|after-context|before-context|include|exclude|exclude-dir|replace|threads|encoding|sort|sortr|colors|pre|engine|ignore-file|path-separator|context-separator|label|directories|devices)$/) {
            if (w == "--directories" && val[k + 1] == "recurse") rec = 1
            k++
          }
          continue
        }
        if (w ~ /^-./) {
          if (b == "grep" && w ~ /^-[a-zA-Z]*[rR]/) rec = 1
          if (w ~ /^-[a-zA-Z]*[ef]$/ || w ~ /^-[ef]/) hasE = 1
          if (b == "grep" && w == "-d" && val[k + 1] == "recurse") rec = 1
          if (w ~ /^-[a-zA-Z]*[efgtTABCmMjdDEr]$/ && !(b == "grep" && w ~ /[rET]$/)) k++
          continue
        }
        npos++
      }
      if (b == "grep") { if (rec && npos - (hasE ? 0 : 1) <= 0) found = 1; return "" }
      if (npos - (hasE ? 0 : 1) <= 0 && (files || !stdin)) found = 1
      return ""
    }
    # what this command writes to a pipe after it, when it is an echo, print, printf or cat <<<
    prod = ""; st["lp"] = 0
    if (t <= nt) {
      pc = base(val[t]); k = t + 1
      if (pc == "echo" || pc == "print") {
        while (k <= nt && (rd[k] || val[k] ~ /^-[neErRl]+$/)) k++
        for (; k <= nt; k++) if (!rd[k]) prod = prod (prod == "" ? "" : " ") val[k]
      } else if (pc == "printf") {
        # r5 decision 11: the format with its arguments in order, then each word (r4)
        prod = pfmt(val, rd, k, nt)
        for (; k <= nt; k++) if (!rd[k]) prod = prod "\n" val[k]
      } else if (pc == "cat" && hstr hd st["ps"] != "") {
        prod = hstr (hstr != "" && hd != "" ? "\n" : "") hd (hstr hd != "" && st["ps"] != "" ? "\n" : "") st["ps"]
        st["lp"] = (st["ps"] != "")
      } else {
        # r5 decision 11: any other command (cat, tee, a filter) writes its words, failing closed, and passes
        # on what it reads (the pipe carries every earlier stage); r6 decision 3: its here-string and its
        # heredoc too, as cat does
        for (; k <= nt; k++) if (!rd[k]) prod = prod (prod == "" ? "" : " ") val[k]
        if (hstr hd != "") prod = prod (prod == "" ? "" : "\n") hstr (hstr != "" && hd != "" ? "\n" : "") hd
        if (st["ps"] != "") { prod = prod (prod == "" ? "" : "\n") st["ps"]; st["lp"] = 1 }
      }
    }
    # r5 decision 3: the command find runs with -exec, -execdir, -ok or -okdir is judged on its own, each {}
    # standing for the roots of find (. when it names none), and leaves this line
    if (t <= nt && val[t] == "find" && mode == "prep" && !noext) xe = execs(tk, val, ex, t, nt, (fw == "") ? "." : fw, ctx, depth)
    # r5 decision 3: a depth bound covers the walk of its own command only: its words stay for the xargs rule
    # (its searcher names made :, the roots of find as the shell reads them), and what find runs was judged
    # on its own above
    if (hs && hb) { out = bound(tk, val, ex, isfr, sp, nt); lastprod = prod; lastps = st["lp"]; lastroots = proots(val[t], fw, prod); if (collect) coll = coll "\n" prod; return out }
    lastprod = prod; lastps = st["lp"]; lastroots = proots(val[t], fw, prod)
    if (t <= nt && ismeta(val[t], val, rd, t, nt)) return sp[0] ":" sp[nt]
    # the scripts a shell or eval runs (r4 decision 3), each judged as its own lines
    if (!noext) {
      if (t <= nt && val[t] == "eval") {
        scr = ""; ev = 0
        for (k = t + 1; k <= nt; k++) {
          if (rd[k]) continue
          if (!ev && val[k] == "--") { ev = 1; continue }
          ev = 1; scr = scr (scr == "" ? "" : " ") val[k]
        }
        if (scr != "") script(scr, ctx, depth)
        # r5 decision 11: what a command substitution in eval arguments writes is a script too
        if (scr ~ /\$\(|`/) spans(scr, ctx, depth, 1)
      }
      st["s"] = 0; st["g"] = 0
      for (k = t; k <= nt; k++) if (!rd[k] && shstep(st, val[k]) == "script") { st["g"] = k; script(val[k], sx(st, val[k]), depth) }
      # r6 decision 2: xargs running a shell whose -c script names a searcher (as pw_named reads it) puts the whole
      # command under the xargs rule (xsh: the prep ends with \003X\003)
      if (mode == "prep" && st["a"] && st["s"] && st["c"] && st["g"] && pwn(val[st["g"]])) xsh = 1
      if (st["s"] && !st["c"] && (!st["f"] || st["S"])) {
        # its process substitution (r6 decision 9: bash <(...), bash < <(...)), here-string, heredoc (r5 decision
        # 2) or what every earlier stage of its pipeline writes (r5 decision 11)
        scr = (st["ps"] != "") ? st["ps"] : (hstr != "") ? hstr : (hd != "") ? hd : pin
        if (scr != "") script(scr, sx(st, scr), depth)
      }
      # r6 decision 9: source <(...) and . <(...) run what the substitution writes in this shell
      if (t <= nt && (val[t] == "source" || val[t] == ".") && st["ps"] != "") script(st["ps"], ctx, depth)
      # r5 decision 11: a -c script that only runs its arguments ("$@", $@, "$1", $*) runs the words after its
      # name operand; under xargs such a script, or a -c with no script, runs what xargs reads
      # (st["g"]: the script word; st["a"]: xargs runs the shell)
      k = st["g"]
      if (k && val[k] ~ /^"?\$\{?[@*1]\}?"?$/) {
        scr = ""; for (k += 2; k <= nt; k++) if (!rd[k]) scr = scr (scr == "" ? "" : " ") tk[k]
        if (scr != "") script(scr, sx(st, scr), depth)
      }
      k = st["g"]
      if (st["a"] && st["s"] && pin != "" && (k ? val[k] ~ /^"?\$\{?[@*1]\}?"?$/ : st["c"])) script(pin, sx(st, pin), depth)
    }
    lastprod = prod; lastps = st["lp"]; lastroots = proots(val[t], fw, prod)
    if (collect) coll = coll "\n" prod
    if (nt == 1 && tk[1] == "cd") tk[1] = "cd ~"
    else if (nt == 2 && tk[1] == "cd" && tk[2] == "--") tk[2] = "~"
    out = sp[0]
    for (t = 1; t <= nt; t++) out = out ((xe && ex[t] == 2) ? "" : tk[t]) sp[t]
    return out
  }
  # proots(b, fw, prod): the roots a command whose name is b writes to a pipe, for a while-read after it (r6 decision
  # 9): the roots of find (. when it names none), any other command its words
  function proots(b, fw, prod) { return (b == "find") ? ((fw == "") ? "." : fw) : prod }
  # execs(tk, val, ex, t, nt, roots, ctx, depth): each command the find at t runs (ex 2) judged as its own lines,
  # {} as roots (a {} inside a word as the first root); 1 when there was one (r5 decision 3).
  function execs(tk, val, ex, t, nt, roots, ctx, depth,   k, cmd, q, x, any) {
    cmd = ""; x = roots; sub(/ .*/, "", x); x = value(x); gsub(/&/, "\\\\&", x)
    for (k = t + 1; k <= nt + 1; k++) {
      if (k <= nt && ex[k] == 2) {
        q = tk[k]
        if (val[k] == "{}") q = roots; else if (index(q, "{}")) gsub(/\{\}/, x, q)
        cmd = cmd (cmd == "" ? "" : " ") q; any = 1; continue
      }
      if (cmd != "") { script(cmd, ctx, depth); cmd = "" }
    }
    return any
  }
  # bound(tk, val, ex, isfr, sp, nt): a bounded simple command as the rules read it (r5 decision 3): : and its
  # words, its searcher names made :, find roots as the shell reads them, what find runs left out.
  function bound(tk, val, ex, isfr, sp, nt,   k, out, q) {
    out = sp[0] ":"
    for (k = 1; k <= nt; k++) {
      if (ex[k]) continue
      q = tk[k]
      if (isname(base(val[k])) || val[k] == "grep -r") q = ":"
      else if (isfr[k] && val[k] !~ /[[:space:]]/) q = val[k]
      out = out " " q
    }
    return out sp[nt]
  }
  # r5 decision 2, heredocs. eol(t, i): where the line holding position i ends (its newline outside quotes; a
  # comment runs to it), 0 when the text ends first.
  function eol(t, i,   n, c, j) {
    n = length(t)
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\\047"#$\n]+/)) { i += RLENGTH; continue }
      c = substr(t, i, 1)
      if (c == "\n") return i
      if (c == "\\") { i += 2; continue }
      if (c == "$" && substr(t, i + 1, 1) == "\047") { j = sqend(t, i + 1); if (!j) return 0; i = j + 1; continue }
      if (c == "\047") { j = index(substr(t, i + 1), "\047"); if (!j) return 0; i += j + 1; continue }
      if (c == "\"") { j = dqhend(t, i); if (!j) return 0; i = j + 1; continue }
      if (c == "#" && (i == 1 || substr(t, i - 1, 1) ~ /[[:space:]]/)) { j = index(substr(t, i), "\n"); return j ? i + j - 1 : 0 }
      i++
    }
    return 0
  }
  # hdword(t, i): for the << at i, HDW is the operator with its word as written, HDD the delimiter (the word,
  # quotes removed) and HDS 1 for <<-; the position after the word, 0 when no word follows.
  function hdword(t, i,   n, k, c, j, w) {
    n = length(t); k = i + 2; HDS = 0
    if (substr(t, k, 1) == "-") { HDS = 1; k++ }
    while (k <= n && substr(t, k, 1) ~ /[ \t]/) k++
    w = ""
    while (k <= n) {
      if (match(substr(t, k, 512), /^[^[:space:];&|()<>`\\\047"]+/)) { w = w substr(t, k, RLENGTH); k += RLENGTH; continue }
      c = substr(t, k, 1)
      if (c ~ /[[:space:];&|()<>`]/) break
      if (c == "\\") { w = w substr(t, k, 2); k += 2; continue }
      if (c == "\047") { j = index(substr(t, k + 1), "\047"); if (!j) return 0; w = w substr(t, k, j + 1); k += j + 1; continue }
      j = dqend(t, k); if (!j) return 0; w = w substr(t, k, j - k + 1); k = j + 1
    }
    if (w == "") return 0
    HDW = substr(t, i, k - i); HDD = value(w)
    return k
  }
  # hdbody(t, LN, LP, nl, k, d, s): the body of a heredoc whose delimiter is d (s: <<-, its leading tabs go) that
  # starts at line k of t (LN its lines, LP where each starts): HDB is the body, and the line after the
  # delimiter line is returned; 0 when no line is the delimiter. r7 decision 2: each line read is a step of the
  # shared budget, so delimiters that are never found cannot each read every later line for free.
  function hdbody(t, LN, LP, nl, k, d, s,   m, line) {
    for (m = k; m <= nl; m++) { if (++pst > 20000) { cplx = 1; return 0 }; line = LN[m]; if (s) sub(/^\t+/, "", line); if (line == d) break }
    if (m > nl) return 0
    HDB = (m > k) ? substr(t, LP[k], LP[m] - LP[k]) : ""
    if (s) { gsub(/\n\t+/, "\n", HDB); sub(/^\t+/, "", HDB) }
    return m + 1
  }
  # arith(t, i, AO, AD, na, AV): 1 when the << at i is inside $((...)) or ((...)): one of the na open (( (AO their
  # first ( and AD the depth before it, innermost last) that is arithmetic, its inner ( closing right before its
  # outer one, as the shells read them (AV keeps each verdict: the end, or -1; the step budget bounds the cost)
  # (r6 decision 4; r7 decision 1: read by arend, so a << inside it is a shift and the span may cross lines).
  function arith(t, i, AO, AD, na, AV,   m, k, j) {
    for (m = na; m >= 1; m--) {
      k = AO[m]
      if (!(k in AV)) AV[k] = (j = arend(t, k)) ? j : -1
      if (AV[k] > i) return 1
    }
    return 0
  }
  function prep(t, depth, noext, ctx,   n, i, c, seg, res, j, sep, piped, nextin, r, bt, x, k, sb, hz, hn, he, hl, nl, LN, LP, bto, pi, pd, na, AO, AD, AV, HZ, psb, nextroots, ri, abe, oe, oo, oc, ow, pv, pn, pps) {
    n = length(t); i = 1; seg = ""; res = ""; piped = 0; nextin = ""; bt = 0; sb = ""; hz = 0; hn = 0; he = 0; nl = 0; bto = 0
    pd = 0; na = 0; abe = 0; oe = 0; oo = 0; oc = ""; ow = 0; pv = ""; pn = 0
    while (i <= n) {
      # (a command past the step budget is denied as too complex to judge: nothing more need be read)
      if (cplx) return res
      if (match(substr(t, i, 512), /^[^\\\047"#;&|()`$\n<]+/)) { seg = seg substr(t, i, RLENGTH); i += RLENGTH; continue }
      c = substr(t, i, 1)
      if (c == "\\") { seg = seg substr(t, i, 2); i += 2; continue }
      # r5 decision 2: the body of a heredoc (the lines after this one, to its delimiter line) is data: out of
      # the quote and separator tracking, it is written out as it stands when this line ends, and handed to
      # simple() as the stdin of its command (sb); an unterminated one leaves the rest read as commands (r6
      # decision 4: a << whose body never ends disables only itself; hz and HZ remember a line end or a
      # delimiter already not found)
      if (c == "<" && substr(t, i, 3) == "<<<") { seg = seg "<<<"; i += 3; continue }
      if (c == "<" && substr(t, i + 1, 1) == "<" && (j = hdword(t, i)) && i > abe && !(na && arith(t, i, AO, AD, na, AV))) {
        if (!hn && !hz) {
          if (x = eol(t, j)) {
            hn = x + 1
            if (!nl) { nl = split(t, LN, "\n"); x = 1; for (k = 1; k <= nl; k++) { LP[k] = x; x += length(LN[k]) + 1 }; hl = 1 }
            while (hl <= nl && LP[hl] < hn) hl++
          } else hz = 1
        }
        x = (hn && !((HDS, HDD) in HZ)) ? hdbody(t, LN, LP, nl, hl, HDD, HDS) : 0
        if (!x && hn) HZ[HDS, HDD] = 1
        if (x) {
          if (!match(seg, /(^|[[:space:]])[0-9]+$/) || substr(seg, RSTART) ~ /^[[:space:]]*0$/) sb = sb (sb == "" ? "" : "\n") HDB
          hl = x; hn = (x <= nl) ? LP[x] : n + 1; he = hn
          # r6 decision 1: under a delimiter word with no quote and no backslash the shell runs the command
          # substitutions of the body, so each $(...) and backtick span in it is judged as a command; the rest of
          # the body stays data (HDW is kept across the reading of those scripts)
          if (mode == "prep" && !noext && HDW !~ /[\\\047"]/) { x = HDW; spans(HDB, ctx, depth); HDW = x }
        }
        seg = seg HDW; i = j; continue
      }
      # r6 decision 9: what a process substitution <(...) writes goes to the command it is a word of (psb), as its
      # script when that is a shell or source (its commands are read as commands here too)
      if (c == "<" && substr(t, i + 1, 1) == "(" && mode == "prep" && !noext && (j = parend(t, i + 1))) {
        x = subout(substr(t, i + 2, j - i - 2), depth, ctx); if (x != "") psb = psb (psb == "" ? "" : "\n") x
        # r7 decision 3: it is also the script of the command it is a word of, which the prep splits at the ( (oo:
        # the next separator is that split; oe: the ) that closes the outermost one)
        if (!oe) { oe = j; oo = 1 }
      }
      if (c == "<") { seg = seg c; i++; continue }
      if (c == "$" && substr(t, i + 1, 1) == "\047") { j = sqend(t, i + 1); if (!j) j = n; seg = seg substr(t, i, j - i + 1); i = j + 1; continue }
      # r5 decision 10: a word that starts ~$( or ~` is read from the home, and the command its substitution
      # runs is judged on its own
      if ((c == "`" || (c == "$" && substr(t, i + 1, 1) == "(")) && !noext && seg ~ /(^|[[:space:]])~$/) {
        j = (c == "`") ? btend(t, i) : parend(t, i + 1)
        if (j) { x = (c == "`") ? substr(t, i + 1, j - i - 1) : substr(t, i + 2, j - i - 2); if (mode == "prep" && named(x)) script(x, ctx, depth); i = j + 1; continue }
      }
      # r5 decision 11: what a command substitution in the arguments of eval writes is a script too
      if ((c == "`" || (c == "$" && substr(t, i + 1, 1) == "(")) && mode == "prep" && !noext && seg ~ /^[[:space:]]*eval[[:space:]]/) {
        j = (c == "`") ? btend(t, i) : parend(t, i + 1)
        if (j) { x = subout((c == "`") ? substr(t, i + 1, j - i - 1) : substr(t, i + 2, j - i - 2), depth, ctx); if (x != "") script(x, ctx, depth) }
      }
      # r7 decision 1: a << inside $[...] is a shift too (abe: where the outermost open one ends)
      if (c == "$" && substr(t, i + 1, 1) == "[" && i > abe && (j = parend(t, i + 1, 2))) abe = j
      if (c == "$") { seg = seg c; i++; continue }
      if (c == "\047") { j = index(substr(t, i + 1), "\047"); if (!j) j = n - i; seg = seg substr(t, i, j + 1); i += j + 1; continue }
      if (c == "\"") { j = dqhend(t, i); if (!j) j = n; seg = seg substr(t, i, j - i + 1); i = j + 1; continue }
      if (c == "#" && (seg == "" || seg ~ /[[:space:]]$/)) { while (i <= n && substr(t, i, 1) != "\n") i++; continue }
      if (c == "#") { seg = seg c; i++; continue }
      # a redirection holding & (>&, <&, &>) is no separator
      if (c == "&" && (seg ~ /[<>]$/ || substr(t, i + 1, 1) == ">")) { seg = seg c; i++; continue }
      sep = c
      if ((c == "|" || c == "&") && substr(t, i + 1, 1) == c) sep = c c
      else if (c == "|" && substr(t, i + 1, 1) == "&") sep = "|&"
      # r8 decision 1: a newline right after &&, ||, | or |& (blanks and a comment between) continues the line, as in
      # the shells: the operator stays pv, and a pipe carries on to the command on the next line
      if (sep == "\n" && (pv == "&&" || pv == "||" || pv == "|" || pv == "|&") && seg !~ /[^[:space:]]/) {
        res = res seg sep; seg = ""; i++
        if (he > i) { res = res substr(t, i, he - i); i = he }; hn = 0; he = 0
        continue
      }
      # r5 decision 11: a pipe carries what every earlier stage writes. r7 decision 5: vtop, a plain assignment
      # here would last: the top of the command (not in a subshell, a substitution or a backtick, and no case
      # pattern seen, pn), after ;, a newline, & or nothing, and before ;, a newline, && or ||
      vtop = (!depth && !pd && !pn && !bto && (pv == "" || pv == ";" || pv == "\n" || pv == "&") && (sep == ";" || sep == "\n" || sep == "&&" || sep == "||"))
      pi = nextin; pipein = pi; ri = nextroots; piperoots = ri; pipeps = psb; pps = psb; r = simple(seg, depth, piped, noext, ctx, bt, sb); sb = ""; psb = ""
      nextin = (sep == "|" || sep == "|&") ? pi (pi == "" ? "" : "\n") lastprod : ""
      nextroots = (sep == "|" || sep == "|&") ? ri (ri == "" ? "" : " ") lastroots : ""
      # r7 decision 3: a command that passes on what its <(...) writes (cat, tee, any filter: lastps) passes it down
      # the pipe after the substitution closes, with what came into it (oc), and so for each later <(...) of it,
      # until its words end (ow); r8 decision 4: the words of what it passes on are also roots a piped while read
      # reads (nextroots), as for any other producer
      if (oo) { if (ow) oc = oc "\n" pps; else if (lastps) { oc = pi (pi == "" ? "" : "\n") lastprod; ow = 1 }; oo = 0 }
      else if (!oe && ow) {
        if (sep == "|" || sep == "|&") {
          nextin = nextin (nextin == "" ? "" : "\n") oc
          x = oc; gsub(/[[:space:]]+/, " ", x); sub(/^ /, "", x); sub(/ $/, "", x)
          if (x != "") nextroots = nextroots (nextroots == "" ? "" : " ") x
        }
        oc = ""; ow = 0
      }
      if (i == oe) oe = 0
      pv = sep
      # r5 decision 5: a space before a closing backtick too, so a root right before it ends there
      if (sep == "`") { if (bto) r = r " "; bto = !bto }
      res = res r sep; seg = ""
      # r6 decision 4: the open (( a << may sit in (pd the depth of ( as read here)
      if (sep == "(") { if (substr(t, i + 1, 1) == "(") { AO[++na] = i; AD[na] = pd }; pd++ }
      else if (sep == ")") { pd--; if (pd < 0) pn = 1; while (na && AD[na] >= pd) na-- }
      i += length(sep)
      if (sep == "\n") { if (he > i) { res = res substr(t, i, he - i); i = he }; hn = 0; he = 0 }
      piped = (sep == "|" || sep == "|&"); bt = (sep == "`")
    }
    pipein = nextin; piperoots = nextroots; pipeps = psb
    vtop = (!depth && !pd && !pn && !bto && (pv == "" || pv == ";" || pv == "\n" || pv == "&"))
    return res simple(seg, depth, piped, noext, ctx, bt, sb)
  }
  BEGIN {
    SRCH = "(^|[[:space:];&|(\047\"\\\\/`])(find|rg|grep|egrep|fgrep|rgrep|ugrep)([[:space:]\047\")`]|$)"
    n = split("env command builtin exec nice nohup time timeout gtimeout xargs sudo doas noglob nocorrect stdbuf caffeinate ionice chronic unbuffer", a, " ")
    for (i = 1; i <= n; i++) WRAP[a[i]] = 1
    WPOS["timeout"] = 1; WPOS["gtimeout"] = 1
    wv("sudo", "-u -g -C -D -h -p -r -t -U -T --user --group --host --prompt --role --type --other-user --chdir --close-from --command-timeout")
    wv("doas", "-u -C"); wv("exec", "-a"); wv("env", "-u -C -P -S --unset --chdir --split-string")
    wv("time", "-o -f --output --format"); wv("nice", "-n --adjustment"); wv("ionice", "-c -n -p -P -u --class --classdata")
    wv("timeout", "-s -k --signal --kill-after"); wv("gtimeout", "-s -k --signal --kill-after"); wv("caffeinate", "-t -w")
    wv("stdbuf", "-i -o -e --input --output --error")
    wv("xargs", "-I -J -L -n -P -R -S -s -d -E -a --max-args --max-procs --delimiter --arg-file --replace --max-lines --max-chars --eof --process-slot-var")
    lv("rg", "--after-context --before-context --context --color --colors --context-separator --dfa-size-limit --encoding --engine --field-context-separator --field-match-separator --file --glob --iglob --ignore-file --max-columns --max-count --max-depth --maxdepth --max-filesize --path-separator --pre --pre-glob --regex-size-limit --regexp --replace --sort --sortr --threads --type --type-not --type-add --type-clear --hostname-bin --hyperlink-format")
    lv("grep", "--regexp --file --max-count --after-context --before-context --context --directories --devices --include --exclude --exclude-dir --exclude-from --include-dir --include-from --label --glob --iglob --file-extension --file-type --file-magic --neg-regexp --range --jobs --format --sort --tag --encoding --filter --depth --max-depth --maxdepth --exclude-fs --include-fs --group-separator")
    lv("find", "-name -iname -path -ipath -wholename -iwholename -lname -ilname -regex -iregex -regextype -type -xtype -user -group -uid -gid -perm -size -mtime -atime -ctime -Btime -mmin -amin -cmin -Bmin -samefile -inum -links -fstype -mindepth -maxdepth -used -context -fprint -fprint0 -fls -printf -xattrname -flags -limit -anewer -cnewer -Bnewer -D -S -f -files0-from")
  }
  # (r8 decision 1: FN, the command defines a function, read once over the whole command)
  mode == "prep" { gsub(/\003/, "?"); FN = ($0 ~ /\([[:space:]]*\)/ || $0 ~ /(^|[^A-Za-z0-9_])function([^A-Za-z0-9_]|$)/); printf "%s%s", (NR > 1 ? "\001" : ""), prep(subst($0), 0, 0, "z") }
  mode == "nopath" { prep($0, 0, 1, "z") }
  END {
    if (mode == "nopath") print ((found || cplx) ? 1 : 0)
    if (mode == "prep") printf "%s%s%s%s", xlines, (xsh ? "\n\003X\003" : ""), (deep ? "\n\003D\003" : ""), (cplx ? "\n\003C\003" : "")
  }'
# pw_nopath CMD: CMD (prepped) holds a recursive searcher given no path.
pw_nopath() { [ "$(printf '%s' "$1" | LC_ALL=C awk -v RS=$'\001' -v mode=nopath "$pw_prep_awk" 2>/dev/null)" = 1 ]; }
# pw_named CMD: CMD names a searcher where the prep reads one: plainly, after a quote, a backslash, a / or
# a backtick, once its quote marks, backslashes and $ are gone (r4 decision 4), in a $'...' holding an
# escape (decision 3), or as zsh's =cmd (r5 decision 11; r6 decision 6: also right after a backtick).
pw_named() {
  printf '%s' "$1" | grep -qE "(^|[[:space:];&|(/\\\\'\"\`])(find|rg|[efru]?grep)[\`'\")]?([[:space:]]|\$)" \
    || printf '%s' "$1" | LC_ALL=C tr -d "\\\\'\"\$" | grep -qE "(^|[[:space:];&|(/\`])(find|rg|[efru]?grep)[\`)]?([[:space:]]|\$)" \
    || printf '%s' "$1" | grep -qE "\\\$'[^']*\\\\[0-7xX]" \
    || printf '%s' "$1" | grep -qE "(^|[[:space:];&|(\`])=(find|rg|[efru]?grep)([[:space:]]|\$)"
}
pw_cmd=$command pw_xsh=""
if pw_named "$command"; then
  # r5 decision 8: a command too long to prep (over 64 KB) that names a searcher fails closed, since none of
  # the prep's readings (names, keywords, scripts, heredocs, the no-path judgment) can be made
  [ "${#command}" -le 65536 ] || block "[unscoped-search] Command too long to judge: it is over 64 KB and names a searcher (find, rg or grep), so the guard cannot read where it searches. Split it into shorter commands, and scope each search: an exact subdirectory or -maxdepth/--max-depth."
  # r6 decision 8: so does one holding more than 4,096 of any one of $, # or \ (the prep reads each of them a
  # step at a time, so hostile words of them cost minutes), counted in one pass; r8 decision 2: and of ' or of
  # backtick (each quote and each backtick span costs the prep and the stages after it a read of its own, which the
  # step budget does not count: 60,000 quotes beside a scoped search took 4 s of CPU); r9 decision 2: and of "
  # (60,000 double quotes beside a scoped search took 2-3 s)
  pw_many=$(printf '%s' "$command" | LC_ALL=C awk -v RS=$'\001' '{ s = $0; a += gsub(/\$/, "", s); s = $0; b += gsub(/#/, "", s); s = $0; c += gsub(/\\/, "", s); s = $0; d += gsub(/\047/, "", s); s = $0; e += gsub(/`/, "", s); s = $0; f += gsub(/"/, "", s) } END { print (a > 4096 || b > 4096 || c > 4096 || d > 4096 || e > 4096 || f > 4096) ? 1 : 0 }' 2>/dev/null) || pw_many=1
  [ "$pw_many" = 0 ] || block "[unscoped-search] Command too complex to judge: it holds more than 4,096 of one of \$, #, \\, ', \" or \` and names a searcher (find, rg or grep), so the guard cannot read where it searches. Split it into shorter commands, and scope each search: an exact subdirectory or -maxdepth/--max-depth."
  pw_cmd=$(printf '%s' "$command" | LC_ALL=C awk -v RS=$'\001' -v mode=prep "$pw_prep_awk" 2>/dev/null) || pw_cmd=""
  # r6 decisions 4, 5 and 9: a command whose $(...), $((...)), ((...)), <(...) and heredoc forms take the prep past
  # its step budget (parend, shared) is too complex to judge; the prep stops there and ends with \003C\003
  case "$pw_cmd" in *$'\003C\003') block "[unscoped-search] Command too complex to judge: it names a searcher (find, rg or grep) beside more nested \$(...), \$((...)), <(...) and heredoc forms than the guard reads, so the guard cannot read where it searches. Split it into shorter commands, and scope each search: an exact subdirectory or -maxdepth/--max-depth." ;; esac
  [ -n "$pw_cmd" ] || pw_cmd=$command
  # r6 decision 2: the xargs rule below also judges xargs running a shell whose -c script names a searcher
  case "$pw_cmd" in *$'\n\003X\003'*) pw_xsh=1 ;; esac
fi
# <<< r3 path words: end <<<

searcher=""
if printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|('\"\\\\])find[[:space:]]"; then searcher="find"
elif printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|('\"\\\\])rg[[:space:]]"; then searcher="rg"
elif printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|('\"\\\\])grep[[:space:]]" \
  && printf '%s' "$pw_cmd" | grep -qE '(^|[[:space:]])(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive)([[:space:]]|$)'; then searcher="recursive grep"
fi
if [ -n "$searcher" ]; then
  broad_re="(/Users/[^/[:space:]'\"]+|~|\\\$HOME)(/(TheAxiomFoundation|RulesFoundation|PolicyEngine|Library/Caches|\.cache|\.local|\.subfleet(/[^/[:space:]'\"]+)?))?|\\\$SUBFLEET_(ROOT|HOME)(/[^/[:space:]'\"]+)?|(/private)?/tmp"
  # Every root spelling: bare `/`, `/*` (the shell expands it to every
  # top-level directory), `/Users`, `/Users/`, and broad_re with an optional
  # trailing slash. A root is a whole word: preceded by whitespace (or the
  # searcher itself), followed by whitespace, `|;&)`, an optional closing
  # quote, or the end; r5: also a closing backtick.
  root_core="/|/\\*|/Users/?|(${broad_re})/?"
  root_re="(${root_core})"
  root_end="['\"]?([[:space:]|;&)\`]|\$)"
  # Shell redirections terminate a searcher's argv: a broad-looking output or
  # input file after < or > is not a search path (`rg pat src > /Users/out`).
  find_seg="(^|[[:space:];&|(])find[[:space:]]+([^|;&<>]*[[:space:]])?"
  rg_seg="(^|[[:space:];&|(])(rg|grep)[[:space:]]+([^|;&<>]*[[:space:]])?"
  word_re="(^|[[:space:]])${root_re}${root_end}"
  find_re="${find_seg}${root_re}${root_end}"
  # rg/grep options that take a value (so `-g Q` / `-t py` is never read as the
  # pattern positional). -r/-E/-T/--color are deliberately NOT here: in grep
  # they are flags and `grep -r foo /` must stay a crawl.
  valopt_re="(^|[[:space:]])(-[gtABCmMjdD]|--(glob|iglob|type|type-not|type-add|type-clear|max-count|max-columns|max-filesize|context|after-context|before-context|include|exclude|exclude-dir|replace|threads|encoding|sort|sortr|colors|pre|engine|ignore-file|path-separator|context-separator|label|directories|devices))[[:space:]]+[^-[:space:]][^[:space:]]*"
  # rg/grep PATH position: after the first non-option token (the pattern).
  rgpath_re="(^|[[:space:];&|(])(rg|grep)[[:space:]]+(-[^[:space:]]*[[:space:]]+)*[^-[:space:]|;&<>][^[:space:]|;&<>]*[[:space:]]+([^|;&<>]*[[:space:]])?${root_re}${root_end}"
  # With -e/-f/--regexp/--file/--files every positional of that rg/grep is a
  # path: a root anywhere in the segment counts, the option's own value never.
  nopat_opt="(-e|-f|--regexp|--file|--files)([[:space:]=]|\$)"
  nopattern_re="${rg_seg}${nopat_opt}"
  patval_re="(^|[[:space:]])(-e|-f|--regexp|--file)([[:space:]]+|=)[^[:space:]|;&<>]+"
  # option ... root, or root ... option, within one rg/grep segment
  optroot_re="${rg_seg}(${nopat_opt}([^|;&<>]*[[:space:]])?${root_re}${root_end}|${root_re}${root_end}([^|;&<>]*[[:space:]])?${nopat_opt})"
  # The root-before-option half needs its own expression for token naming:
  # last_word_of_match on the combined expression would name the trailing
  # option (`-e`, `--files`) instead of the broad root that actually fired.
  root_before_opt_re="${rg_seg}${root_re}${root_end}([^|;&<>]*[[:space:]])?${nopat_opt}"
  xargs_re="(^|[[:space:];&|(])xargs[[:space:]]+([^|;&]*[[:space:]])?(find|rg|grep)[[:space:]]"
  # A root immediately feeding xargs remains a path even when quoted in the
  # producer (`echo "$HOME" | xargs rg pat`). Keep the quoted-pattern escape
  # in xargs's own argv (`git ls-files | xargs rg "/Users"`).
  xargs_feed_re="(^|[[:space:]])${root_re}['\"]?[[:space:]]*\|[[:space:]]*xargs[[:space:]]+([^|;&]*[[:space:]])?(find|rg|grep)[[:space:]]"
  cd_re="(^|[[:space:];&|('\"])(cd|pushd)[[:space:]]+(-[^[:space:]]*[[:space:]]+)*['\"]?${root_re}${root_end}"
  subscript_cmd_re="((^|[[:space:];&|(])(sh|bash|zsh|dash|ksh)[[:space:]]+-[A-Za-z]*c[[:space:]]|(^|[[:space:];&|(])eval[[:space:]])"
  # Raw forms for a shell -c argument assembled from adjacent quote chunks;
  # multiple quote marks can surround the interpolated root in the command
  # text even though the invoked shell receives one ordinary word.
  subscript_cd_re="(^|[[:space:];&|('\"])(cd|pushd)[[:space:]]+(-[^[:space:]]*[[:space:]]+)*['\"]*${root_re}['\"]*([[:space:]|;&)]|\$)"
  subscript_xargs_feed_re="(^|[[:space:]'\"])[ '\"]*${root_re}['\"]*[[:space:]]*\|[[:space:]]*xargs[[:space:]]+([^|;&]*[[:space:]])?(find|rg|grep)[[:space:]]"
  subscript_find_re="${subscript_cmd_re}['\"]*[[:space:]]*find[[:space:]]+([^|;&<>]*[[:space:]])?['\"]*${root_re}['\"]*([[:space:]|;&)]|\$)"
  subscript_rgpath_re="${subscript_cmd_re}['\"]*[[:space:]]*(rg|grep)[[:space:]]+(-[^[:space:]]*[[:space:]]+)*[^-[:space:]|;&<>][^[:space:]|;&<>]*[[:space:]]+([^|;&<>]*[[:space:]])?['\"]*${root_re}['\"]*([[:space:]|;&)]|\$)"
  loop_head_re="(^|[[:space:];&|(])for[[:space:]]+[A-Za-z_][A-Za-z0-9_]*[[:space:]]+in[[:space:]]+${root_re}${root_end}"
  # judged: the command with backslash-newline continuations joined exactly as
  # the original port-back contract specifies, then `${HOME}` spelled `$HOME`
  # (and `${SUBFLEET_ROOT}`/`${SUBFLEET_HOME}` without braces too). Since r3c it
  # starts from pw_cmd (searcher names, depth bounds and a bare cd read, see
  # "the command as the rules read it"), as do the searcher detection above and
  # the cwd rule below.
  judged=$(printf '%s' "$pw_cmd" | LC_ALL=C awk '{ if (sub(/\\$/, "")) printf "%s", $0; else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
  [ -n "$judged" ] || judged=$pw_cmd
  # Two trailing backslashes escape each other, so the following newline is a
  # real command separator. The mandated one-backslash join above cannot tell
  # that case apart; retain the raw newline when an even run is present so an
  # old blocked crawl on the next line cannot disappear during normalization.
  if printf '%s' "$pw_cmd" | LC_ALL=C awk 'match($0, /\\+$/) && RLENGTH % 2 == 0 { even=1 } END { exit !even }' 2>/dev/null; then
    judged=$(printf '%s' "$pw_cmd" | LC_ALL=C awk '{ if (match($0, /\\+$/) && RLENGTH % 2 == 1) printf "%s", substr($0, 1, length($0) - 1); else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
    [ -n "$judged" ] || judged=$pw_cmd
  fi
  # >>> r3 path words: judge >>>
  # Path words judged on disk (see "Path words" above): pass 1 lists every
  # path word with its context (b inside a script run by bash, sh, dash or ksh
  # without globstar, z otherwise) and whether a quote mark was part of it
  # (q, else u), each is judged here, and pass 2
  # rewrites the ones whose verdict moves (F, or a placeholder root, inside
  # the word's first quote mark when it has one), so every rule below reads
  # the rewritten command. A line the command prep added for a script a shell
  # or eval runs (\003<context>\003 and a space first; r4 decision 3) is judged
  # in its context; a quoted string that is a shell's -c script, found word by
  # word (shstep), is judged in that shell's.
  pw_root_re="^(${root_core})\$"
  pw_normalize "$HOME"; pw_home=$pw_n
  case "$pw_home" in /*/*/*/*) pw_hfast="" ;; *) pw_hfast=1 ;; esac
  pw_nt=0 pw_nd=0 pw_dec="" pw_deep=""
  pw_awk="$pw_lib_awk"'
    function lit(c) {
      if (c == "{") return "\001"; if (c == "}") return "\002"; if (c == ",") return "\003"
      if (c == "(") return "\004"; if (c == "|") return "\005"; if (c == ")") return "\006"
      return c
    }
    # lits(s): s with each { } , ( | ) as lit() reads it, in one pass per character kind
    function lits(s) {
      gsub(/\{/, "\001", s); gsub(/\}/, "\002", s); gsub(/,/, "\003", s)
      gsub(/\(/, "\004", s); gsub(/\|/, "\005", s); gsub(/\)/, "\006", s)
      return s
    }
    function sform(run, mode,   out, i, n, c, j, seg, k) {
      if (run !~ /[\\\047"$]/) return run
      out = ""; n = length(run); i = 1
      while (i <= n) {
        if (match(substr(run, i, 512), /^[^\\\047"$]+/)) { out = out substr(run, i, RLENGTH); i += RLENGTH; continue }
        c = substr(run, i, 1)
        if (c == "\\") { if (i < n) out = out (mode ? lit(substr(run, i + 1, 1)) : substr(run, i + 1, 1)); i += 2; continue }
        if (c == "$" && substr(run, i + 1, 1) == "\047") {
          j = sqend(run, i + 1)
          if (j) { out = out (mode ? "*" : ansic(substr(run, i + 2, j - i - 2))); i = j + 1; continue }
        }
        if (c == "$" && substr(run, i + 1, 1) == "\"") { i++; continue }
        if (c == "\047") {
          j = index(substr(run, i + 1), "\047"); if (!j) j = n - i + 1
          seg = substr(run, i + 1, j - 1)
          if (mode) out = out lits(seg); else out = out seg
          i += j + 1; continue
        }
        if (c == "\"") {
          i++
          while (i <= n && substr(run, i, 1) != "\"") {
            if (match(substr(run, i, 512), /^[^"\\{},(|)]+/)) { out = out substr(run, i, RLENGTH); i += RLENGTH; continue }
            c = substr(run, i, 1)
            if (c == "\\" && i < n && index("$`\"\\\n", substr(run, i + 1, 1))) { i++; c = substr(run, i, 1) }
            out = out (mode ? lit(c) : c); i++
          }
          i++; continue
        }
        out = out c; i++
      }
      return out
    }
    function walk(s, ctx,   out, run, orun, b, sf, qc, rest, piece, q, inner, nctx, gap, g, first, cd, cdp, val, st, role, rdn) {
      out = ""; first = 1; cd = 0; cdp = 0; st["s"] = 0
      while (match(s, /(\$\047([^\047\\]|\\.)*\047|\\.|[^[:space:]|;&()<>"\047\\]|"([^"\\]|\\.)*"|\047[^\047]*\047|\(([^[:space:]()<>;&"\047\\]|\\.)*\))+/)) {
        gap = substr(s, 1, RSTART - 1)
        g = gap; gsub(/[0-9]*(<<<|<<-|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)/, "", g)
        if (g ~ /[;&|()\n]/) { first = 1; cd = 0; cdp = 0; st["s"] = 0 }
        rdn = (gap ~ /[<>|&][[:space:]]*$/ && g !~ /[;&|()\n][[:space:]]*$/)
        out = out gap
        run = substr(s, RSTART, RLENGTH); s = substr(s, RSTART + RLENGTH); orun = run
        val = sform(orun, 0)
        role = (rdn || (val ~ /^[0-9]+$/ && substr(s, 1, 1) ~ /[<>]/)) ? "" : shstep(st, val)
        nctx = (role == "script") ? sx(st, val) : ctx
        b = run; gsub(/["\047]/, "", b)
        sf = sform(run, 1)
        # (r5: a root spelling right before a brace group is a path word too, decision 9, and so is ~ followed
        # by $ or a backtick, decision 10)
        if (sf ~ /^(~[A-Za-z0-9._-]*|\$HOME|\$SUBFLEET_(ROOT|HOME))(\/|$|\{)/ || sf ~ /^~[$`]/ || sf ~ /^\// || (sf ~ /^\{/ && sf ~ /,|\.\./)) {
          k++
          if (pass == 1) printf "%d\t%s\t%s\t%s\t%s\t%s\n", k, ctx, (run == sf) ? "u" : "q", (cd && !cdp) ? "c" : "p", sf, b
          else if (k in tok) {
            qc = ""
            if (match(run, /["\047]/)) qc = substr(run, RSTART, 1)
            run = qc tok[k] qc
          }
          out = out run
        } else {
          rest = run; piece = ""
          while (match(rest, /\$\047([^\047\\]|\\.)*\047|"([^"\\]|\\.)*"|\047[^\047]*\047/)) {
            piece = piece substr(rest, 1, RSTART - 1)
            q = substr(rest, RSTART, RLENGTH); rest = substr(rest, RSTART + RLENGTH)
            if (substr(q, 1, 1) == "$") { piece = piece q; continue }
            inner = substr(q, 2, length(q) - 2)
            if (nctx == "b" && inner ~ /globstar/) nctx = "z"
            piece = piece substr(q, 1, 1) walk(inner, nctx) substr(q, 1, 1)
          }
          out = out piece rest
        }
        if (first) { cd = (val == "cd" || val == "pushd"); first = (val == "builtin" || val == "command") }
        else if (cd && val == "-P") cdp = 1
      }
      return out s
    }
    pass == 2 && NR <= nb { split($0, f, "\t"); tok[f[1]] = f[2]; next }
    $0 == "\003D\003" { if (pass == 1) print "0\tdeep"; else print; next }
    substr($0, 1, 1) == "\003" && substr($0, 3, 2) == "\003 " { line = substr($0, 1, 4) walk(substr($0, 5), substr($0, 2, 1)); if (pass == 2) print line; next }
    { line = walk($0, "z"); if (pass == 2) print line }'
  # (pass 1's lines are split in memory: read takes a pipe a byte at a time)
  pw_out=$(printf '%s\n' "$judged" | LC_ALL=C awk -v pass=1 "$pw_awk" 2>/dev/null) || pw_out=""
  pw_sf=""; case $- in *f*) pw_sf=1 ;; esac
  pw_ifs=${IFS-}; pw_ifs_set=${IFS+x}
  IFS=$'\n'; set -f
  pw_lines=( $pw_out )
  [ -n "$pw_sf" ] || set +f
  if [ -n "$pw_ifs_set" ]; then IFS=$pw_ifs; else unset IFS; fi
  for pw_l in ${pw_lines[@]+"${pw_lines[@]}"}; do
    # (its fields split the same way: ${x%%...} is quadratic on a long word)
    set -f; IFS=$'\t'; pw_fl=( $pw_l )
    [ -n "$pw_sf" ] || set +f
    if [ -n "$pw_ifs_set" ]; then IFS=$pw_ifs; else unset IFS; fi
    if [ "${pw_fl[0]-}" = 0 ]; then
      [ "${pw_fl[1]-}" != deep ] || pw_deep=1
      continue
    fi
    [ "${#pw_fl[@]}" -eq 6 ] || continue
    pw_k=${pw_fl[0]} pw_c=${pw_fl[1]} pw_q=${pw_fl[2]} pw_f=${pw_fl[3]} pw_b=${pw_fl[4]} pw_s=${pw_fl[5]}
    pw_word "$pw_b" "$pw_c" "$pw_q" "$pw_f" "$pw_s"
    if [ -n "$pw_t" ]; then pw_dec="$pw_dec$pw_k	$pw_t
"; pw_nd=$((pw_nd + 1)); fi
  done
  if [ "$pw_nd" -gt 0 ]; then
    pw_j=$( { printf '%s' "$pw_dec"; printf '%s\n' "$judged"; } | LC_ALL=C awk -v pass=2 -v nb="$pw_nd" "$pw_awk" 2>/dev/null) && [ -n "$pw_j" ] && judged=$pw_j
  fi
  # A searcher nested deeper than 4 shells is denied (r3c decision 6).
  if [ -n "$pw_deep" ]; then
    pw_sentinel "a searcher nested deeper than 4 shells"
    judged="$judged
rg --files $pw_t"
  fi
  # <<< r3 path words: judge <<<
  # stripped: judged with quoted strings collapsed, a whole-root quoted string
  # unquoted (`rg pat "/"` -> `rg pat /`, `find "$HOME"/ ...` -> `find $HOME/ ...`)
  # and searcher-mentioning sub-scripts kept visible (see collapse_quotes).
  stripped=$(collapse_quotes "$judged" roots "^(${root_core})\$") || stripped=""
  [ -n "$stripped" ] || stripped=$judged
  hit="" matched=""
  # xargs rule: xargs invoking a searcher feeds it paths from the previous
  # segment, so a root ANYWHERE in the command counts (`echo / | xargs rg pat`)
  # — judged on the all-Q view (built on demand), so `git ls-files | xargs rg
  # "/Users"` keeps its quotes as the documented escape. xargs running
  # something else (`| xargs -n1 dirname`) does not fire.
  # r6: so does xargs running a shell whose -c script names a searcher
  # (pw_xsh, from the command prep), a root right before an xargs included.
  xfeed_re=$xargs_feed_re
  [ -z "${pw_xsh-}" ] || xfeed_re="(^|[[:space:]])${root_re}['\"]?[[:space:]]*\|[[:space:]]*xargs[[:space:]]"
  if [ -n "${pw_xsh-}" ] || printf '%s' "$stripped" | grep -qE "$xargs_re"; then
    if printf '%s' "$stripped" | grep -qE "$xfeed_re"; then
      hit=1; matched=$(first_match "$stripped" "$xfeed_re" | sed 's/[[:space:]]*$//')
    else
      stripped_q=$(collapse_quotes "$judged" q) || stripped_q=""
      [ -n "$stripped_q" ] || stripped_q=$judged
      if printf '%s' "$stripped_q" | grep -qE "$word_re"; then hit=1; matched=$(first_match "$stripped_q" "$word_re"); fi
    fi
  fi
  # A literal broad root assigned by a `for ... in <root>` loop and then fed
  # to find through that loop variable is still a crawl. The old Claude rule
  # blocked the literal root; retain that protection while leaving unrelated
  # loop data alone.
  if [ -z "$hit" ] && printf '%s' "$judged" | grep -qE "$loop_head_re"; then
    loop_head=$(first_match "$judged" "$loop_head_re")
    loop_var=$(printf '%s' "$loop_head" | LC_ALL=C sed -E 's/^.*for[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]+in[[:space:]].*$/\1/' 2>/dev/null) || loop_var=""
    case "$loop_var" in
      ''|*[!A-Za-z0-9_]*) loop_var="" ;;
    esac
    if [ -n "$loop_var" ]; then
      loop_find_re="(^|[[:space:];&|(])find[[:space:]]+(-[^[:space:]]+[[:space:]]+)*['\"]?\\\$[{]?${loop_var}[}]?['\"]?([[:space:]|;&)]|\$)"
      if printf '%s' "$judged" | grep -qE "$loop_find_re"; then
        hit=1; matched=$(last_word_of_match "$judged" "$loop_head_re")
      fi
    fi
  fi
  # find rule: a root anywhere in find's own pipeline segment (find has no
  # pattern positional; a `/` after a `|` belongs to the next command). find's
  # own arguments end at -exec/-execdir/-ok/-okdir — what follows is the exec'd
  # command (its grep/rg is judged by the rg/grep rules below), so `find .
  # -name "*.py" -exec grep -l "/" {} +` is not a crawl of /.
  if [ -z "$hit" ] && [ "$searcher" = "find" ]; then
    stripped_find=$(printf '%s' "$stripped" | LC_ALL=C sed -E "s/((^|[[:space:];&|(])find[[:space:]][^|;&<>]*[[:space:]])-(exec|execdir|ok|okdir)[[:space:]][^|;&<>]*/\\1/g" 2>/dev/null) || stripped_find=$stripped
    [ -n "$stripped_find" ] || stripped_find=$stripped
    if printf '%s' "$stripped_find" | grep -qE "$find_re"; then hit=1; matched=$(last_word_of_match "$stripped_find" "$find_re"); fi
  fi
  # rg/grep rules: whenever an rg/grep segment is present (also behind a find).
  if [ -z "$hit" ] && printf '%s' "$stripped" | grep -qE '(^|[[:space:];&|(])(rg|grep)[[:space:]]'; then
    # The value of -e/-f/--regexp/--file is a pattern or a pattern file, never
    # a search path: blank it to Q before either rule looks (`rg -e / src` and
    # `rg -e pat src/ -e /` stay scoped).
    stripped_e=$(printf '%s' "$stripped" | LC_ALL=C sed -E "s/${patval_re}/\\1\\2 Q/g" 2>/dev/null) || stripped_e=$stripped
    [ -n "$stripped_e" ] || stripped_e=$stripped
    if printf '%s' "$stripped_e" | grep -qE "$nopattern_re"; then
      # -e/-f/--regexp/--file/--files: every positional of that rg/grep is a
      # path, so a root anywhere in the SAME segment as the option counts.
      if printf '%s' "$stripped_e" | grep -qE "$optroot_re"; then
        hit=1
        if printf '%s' "$stripped_e" | grep -qE "$root_before_opt_re"; then
          matched=$(first_match "$(first_match "$stripped_e" "$root_before_opt_re")" "$word_re")
        else
          matched=$(last_word_of_match "$stripped_e" "$optroot_re")
        fi
      fi
    fi
    if [ -z "$hit" ]; then
      # PATH position: after the pattern positional. Drop value-taking options
      # with their values first, so `-g Q` or `-t py` cannot be mistaken for
      # the pattern (regex alternation would otherwise backtrack into that
      # reading).
      unopt=$(printf '%s' "$stripped_e" | LC_ALL=C sed -E "s/${valopt_re}/\\1/g" 2>/dev/null) || unopt=$stripped_e
      [ -n "$unopt" ] || unopt=$stripped_e
      if printf '%s' "$unopt" | grep -qE "$rgpath_re"; then
        hit=1; matched=$(last_word_of_match "$unopt" "$rgpath_re")
      fi
    fi
  fi
  # cd/pushd rule: a broad root as the target of cd/pushd with any searcher
  # present (a `find .` after `cd /`), also right after an opening quote
  # (`bash -c "cd / && find . -name x"` — the sub-script stays visible in
  # stripped) and after cd options (`cd -- /`). Judged on stripped, so a
  # `cd /` inside prose (`git commit -m "cd / then find"`) is Q like any
  # other quoted string.
  if [ -z "$hit" ] && printf '%s' "$stripped" | grep -qE "$cd_re"; then
    hit=1; matched="$(last_word_of_match "$stripped" "$cd_re") (after cd)"
  fi
  # Shell -c arguments may be assembled from adjacent quoted and unquoted
  # pieces (`bash -c "cd "$HOME" && find ."`). collapse_quotes necessarily
  # sees those pieces separately, so retain the raw normalized view for this
  # actual sub-script form only; ordinary quoted prose remains collapsed.
  if [ -z "$hit" ] && printf '%s' "$judged" | grep -qE "$subscript_cmd_re"; then
    if printf '%s' "$judged" | grep -qE "$subscript_cd_re"; then
      hit=1; matched="$(last_word_of_match "$judged" "$subscript_cd_re") (after cd)"
    elif printf '%s' "$judged" | grep -qE "$subscript_xargs_feed_re"; then
      hit=1; matched=$(first_match "$judged" "$subscript_xargs_feed_re" | sed 's/[[:space:]]*$//')
    elif printf '%s' "$judged" | grep -qE "$subscript_find_re"; then
      hit=1; matched=$(last_word_of_match "$judged" "$subscript_find_re")
    elif printf '%s' "$judged" | grep -qE "$subscript_rgpath_re"; then
      hit=1; matched=$(last_word_of_match "$judged" "$subscript_rgpath_re")
    fi
  fi
  # cwd rule: `find .` / `rg pat .` executed while sitting IN a broad root, and
  # (r3c) a recursive searcher given no path, which walks "." (pw_nopath).
  if [ -z "$hit" ]; then
    case "$cwd" in
      "$HOME"|"$HOME/TheAxiomFoundation"|"$HOME/RulesFoundation"|"$HOME/PolicyEngine"|/tmp|/private/tmp|/|/Users)
        if printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|(])(find|rg|grep)[[:space:]]+([^|;&]*[[:space:]])?\.(/)?([[:space:]]|\$)" || pw_nopath "$pw_cmd"; then hit=1; matched=". (cwd $cwd)"; fi ;;
    esac
  fi
  if [ -n "$hit" ]; then
    [ -n "$matched" ] || matched="?"
    # >>> r3 path words: name >>>
    pw_roots_msg="/, /Users, /Users/<user>, ~"
    for pw_x in $pw_kids; do pw_roots_msg="$pw_roots_msg, ~/$pw_x"; done
    pw_roots_msg="$pw_roots_msg, ~/.subfleet/<child>"
    for pw_x in $pw_fixed; do
      case "$pw_x" in /|/Users) ;; *) pw_roots_msg="$pw_roots_msg, $pw_x" ;; esac
      [ "$pw_x" != /Volumes ] || pw_roots_msg="$pw_roots_msg, /Volumes/<entry>"
    done
    pw_i=1
    while [ "$pw_i" -le "$pw_nt" ]; do
      pw_t="/Users/@sfglob${pw_i}@"
      case "$matched" in *"$pw_t"*) matched="${matched%%"$pw_t"*}${pw_shown[$pw_i]}${matched#*"$pw_t"}" ;; esac
      pw_i=$((pw_i + 1))
    done
    # <<< r3 path words: name <<<
    block "[unscoped-search] Whole-tree ${searcher} over a broad root (${pw_roots_msg}) — 8 of these ran in parallel on 8/8 and drove load to 47, and ~22 sol-lane finds on 8/18 held load at 57 for hours, beachballing the Mac. Scope it: exact subdirectory, -maxdepth/--max-depth, 'git ls-files' inside a repo, 'axiom-locate' (engine|release|pypkg|corpus-file) for Axiom artifacts, or the interpreter for python package paths (python -c 'import x; print(x.__file__)'). Matched broad root: '${matched}'."
  fi
fi
# <<< unscoped-search: shared region end <<<

# --- codex-lane additions (hf-dest via shell heredoc): begin ---
# A Bash command carrying an apply_patch patch body executes it even when
# Codex does not intercept the heredoc: arg0 (codex-rs/arg0/src/lib.rs) puts
# an apply_patch/applypatch alias of the codex binary on PATH, and the
# standalone executable (apply-patch/src/standalone_executable.rs) applies
# argv[1]-or-stdin against the shell's cwd. So `cd dir; apply_patch <<'EOF'`,
# `pushd dir && apply_patch …`, and `apply_patch <<'EOF' … EOF && git add -A`
# all run for real. Runs AFTER the seven Bash rules and the shared region;
# scans the patch's added lines against every base (payload cwd + every
# cd/pushd target in the command — hf_dest_bases).
if printf '%s' "$command" | grep -qE '\*\*\* Begin Patch'; then
  hf_dest_check "$tool" "$command" "$cwd"
fi
# --- codex-lane additions (hf-dest via shell heredoc): end ---

exit 0
