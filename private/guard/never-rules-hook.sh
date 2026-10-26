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
# unscoped-search rule.
printf '%s' "$command" | grep -qE 'stash|AXIOM_|getUpdates|get_updates|dump-keychain|checkout[[:space:]]+-b|switch[[:space:]]+-c|pr[[:space:]]+merge|git[[:space:]]+push|(^|[[:space:];&|(])(find|rg|grep)[[:space:]]' \
  || printf '%s' "$command" | grep -qE '\*\*\* Begin Patch' \
  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(/\\\\'\"\`])(find|rg|[efru]?grep)[\`'\")]?([[:space:]]|\$)" \
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
#              line, to depth 4; a searcher deeper is denied (r3c). A root in
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
  local nc="" r=1
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  case "$1" in /volumes|/volumes/*|/network|/network/*|/net|/net/*|/home|/home/*) r=0 ;; esac
  [ -n "$nc" ] || shopt -u nocasematch
  return $r
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
# a mount that may stall, or when the links loop.
pw_physical() {
  local path="" rest="" name="" hops=0
  pw_r=$1
  pw_stall "$1" && return 0
  rest=${1#/}
  while [ -n "$rest" ]; do
    case "$rest" in */*) name=${rest%%/*}; rest=${rest#*/} ;; *) name=$rest; rest="" ;; esac
    case "$name" in ''|.) continue ;; ..) path=${path%/*}; continue ;; esac
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
  local LC_ALL=C s=$1 n=${#1} k=0 j=0 ch="" lead="" body="" rest="" cls="" end=0 bl=0
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
    while [ "$j" -lt "$n" ] && [ "${s:j:1}" != "]" ]; do
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
    else pw_pat="$pw_pat\\["; k=$((k + 1))
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
  local rest="$1/" logical=${2-} c="" nc=0 first=-1 k=0 text="" glob="" pat="" p="" q="" l="" i=0 budget=$pw_cap last=0 m=0 dot=""
  local -a comps cp cl np nl
  while [ -n "$rest" ]; do
    c=${rest%%/*}; rest=${rest#*/}
    [ -n "$c" ] || continue
    comps[$nc]=$c
    case "$c" in *[*?[]*) [ "$first" -ge 0 ] || first=$nc ;; esac
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
      cp=(); cl=(); m=0
      while [ "$m" -lt "${#np[@]}" ]; do
        if pw_isbroad "${np[$m]}" && ! pw_isfile "${np[$m]}"; then cp[${#cp[@]}]=${np[$m]}; cl[${#cl[@]}]=${nl[$m]}; fi
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
pw_brackets() {
  local LC_ALL=C s=$1 n=${#1} k=0 j=0 out="" body="" rest="" cls=""
  while [ "$k" -lt "$n" ]; do
    if [ "${s:k:1}" != "[" ]; then out="$out${s:k:1}"; k=$((k + 1)); continue; fi
    j=$((k + 1))
    case "${s:j:1}" in '!'|'^') j=$((j + 1)) ;; esac
    [ "${s:j:1}" != "]" ] || j=$((j + 1))
    while [ "$j" -lt "$n" ] && [ "${s:j:1}" != "]" ]; do
      if [ "${s:j:1}" = "[" ] && [ "${s:j+1:1}" = ":" ]; then
        rest=${s:j+2}; cls=${rest%%:]*}
        if [ "$cls" != "$rest" ]; then j=$((j + ${#cls} + 4)); continue; fi
      fi
      j=$((j + 1))
    done
    if [ "$j" -lt "$n" ]; then body=${s:k:j-k+1}; out="$out${body//\//$'\007'}"; k=$((j + 1))
    else out="$out["; k=$((k + 1))
    fi
  done
  pw_w=$out
}
# pw_bscan WORD FROM: the first brace group at or after FROM that is no ${:
# pw_bs and pw_be its { and }, pw_bk its kind (comma, nested, seq or lit), pw_bn
# its alternatives; status 1 when there is none.
pw_bscan() {
  local LC_ALL=C s=$1 n=${#1} k=$2 j=0 d=0 nested="" commas=0
  while [ "$k" -lt "$n" ]; do
    if [ "${s:k:1}" = "{" ] && { [ "$k" -eq 0 ] || [ "${s:k-1:1}" != '$' ]; }; then
      j=$((k + 1)); d=1; nested=""; commas=0
      while [ "$j" -lt "$n" ]; do
        case "${s:j:1}" in
          '{') d=$((d + 1)); nested=1 ;;
          '}') d=$((d - 1)); [ "$d" -gt 0 ] || break ;;
          ',') [ "$d" -ne 1 ] || commas=$((commas + 1)) ;;
        esac
        j=$((j + 1))
      done
      if [ "$j" -lt "$n" ]; then
        pw_bs=$k; pw_be=$j; pw_bn=$((commas + 1))
        if [ -n "$nested" ]; then pw_bk=nested
        elif [ "$commas" -gt 0 ]; then pw_bk=comma
        else case "${s:k+1:j-k-1}" in *..*) pw_bk=seq ;; *) pw_bk=lit ;; esac
        fi
        return 0
      fi
    fi
    k=$((k + 1))
  done
  return 1
}
# pw_expand WORD CONTEXT: pw_alts is every word WORD can be after brace expansion
# and, outside CONTEXT b, zsh's (a|b) alternation (r3c decision 3): each comma
# group expanded, 64 words at most; a nested group, a {x..y} sequence or more
# than 64 words reads each group as *, and an alternation that is not a list of
# plain names, or would pass 64, reads its component as * (pw_grp set then). A
# quoted or escaped { } , ( | ) arrives as \001-\006, and is itself again at the
# end.
pw_expand() {
  local LC_ALL=C w=$1 ctx=${2-z} k=0 prod=1 star="" x="" pre="" post="" inner="" a="" found="" one="" lb='{' rb='}' cm=',' lp='(' vb='|' rp=')'
  local -a todo out
  pw_grp=""
  while pw_bscan "$w" "$k"; do
    case "$pw_bk" in nested|seq) star=1 ;; comma) prod=$((prod * pw_bn)) ;; esac
    [ "$prod" -le 64 ] || star=1
    k=$((pw_be + 1))
  done
  if [ -n "$star" ]; then
    k=0
    while pw_bscan "$w" "$k"; do
      if [ "$pw_bk" = lit ]; then k=$((pw_be + 1)); continue; fi
      w="${w:0:pw_bs}*${w:pw_be+1}"; k=$((pw_bs + 1))
    done
    pw_alts=( "$w" ); pw_grp=1
  else
    todo=( "$w" ); pw_alts=()
    while [ "${#todo[@]}" -gt 0 ]; do
      x=${todo[0]}; todo=( ${todo[@]+"${todo[@]:1}"} )
      k=0; found=""
      while pw_bscan "$x" "$k"; do
        if [ "$pw_bk" = comma ]; then found=1; break; fi
        k=$((pw_be + 1))
      done
      if [ -z "$found" ]; then pw_alts[${#pw_alts[@]}]=$x; continue; fi
      pre=${x:0:pw_bs}; post=${x:pw_be+1}; inner="${x:pw_bs+1:pw_be-pw_bs-1},"
      while [ -n "$inner" ]; do a=${inner%%,*}; inner=${inner#*,}; todo[${#todo[@]}]="$pre$a$post"; done
    done
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
            a=${inner//[!|]/}; [ $(( ${#todo[@]} + ${#out[@]} + ${#a} + 1 )) -le 64 ] && one=1 ;; esac ;;
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
    x=${x//$'\001'/$lb}; x=${x//$'\002'/$rb}; x=${x//$'\003'/$cm}
    x=${x//$'\004'/$lp}; x=${x//$'\005'/$vb}; x=${x//$'\006'/$rp}
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
  shopt -q nocasematch && nc=1
  shopt -q nocaseglob && ng=1
  shopt -u nocasematch nocaseglob
  pw_word_judge "$@"
  [ -z "$nc" ] || shopt -s nocasematch
  [ -z "$ng" ] || shopt -s nocaseglob
}
pw_word_judge() {
  local w=$1 ctx=${2-z} q=${3-} f=${4-p} shown=${5-$1} a="" broad="" glob=""
  pw_t=""
  pw_expand "$w" "$ctx"
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
# pw_alt WORD CONTEXT CD: pw_av is how one alternative of a word (no group
# left) is judged: B broad, N not broad, G broad as a glob, NG not broad as a
# glob, T left to the text rules (it still holds $ or a backtick, or is /*).
pw_alt() {
  local w=$1 ctx=$2 f=$3 b="" walk="" x="" c=""
  pw_av=T
  case "$w" in
    '/*') return 0 ;;
    *[*?[]*) ;;
    *) pw_abs "$w"
       case "$pw_p" in ''|*'$'*|*'`'*) return 0 ;; esac
       [ "$f" = c ] || { pw_physdots "$pw_p"; pw_p=$pw_pd; }
       pw_normalize "$pw_p"
       if pw_broad "$pw_n"; then pw_av=B; else pw_av=N; fi
       return 0 ;;
  esac
  pw_stars "$w" "$ctx"; b=$pw_w
  if [ "$ctx" != b ]; then
    pw_brackets "$b"; b=$pw_w
    case "/$b" in */'**'/*) b=${b%%/'**'/*}; walk=1 ;; esac
  fi
  # r3c 1(a) and 1(b): trailing slashes, /. components and <glob>/.. go first;
  # then a trailing run of components made only of wildcards (or .* or **) is
  # the folder they sit in, whether or not it is ever listed.
  if [ -z "$walk" ]; then
    while :; do
      case "$b" in
        /|'') break ;;
        */) b=${b%/} ;;
        */.) b=${b%/.} ;;
        */..) x=${b%/..}; c=${x##*/}
          case "$c" in *[*?[]*) b=${x%/*}; walk=1 ;; *) break ;; esac ;;
        */*) c=${b##*/}
          if [ "$c" = '.*' ] || [ "$c" = '**' ] || pw_wild "$c"; then b=${b%/*}; walk=1; else break; fi ;;
        *) break ;;
      esac
      [ -n "$b" ] || b=/
    done
  fi
  [ -n "$b" ] || b=/
  case "$b" in
    *[*?[]*) ;;
    *) pw_abs "$b"
       case "$pw_p" in ''|*'$'*|*'`'*) if [[ $b =~ $pw_root_re ]]; then pw_av=G; fi; return 0 ;; esac
       [ "$f" = c ] || { pw_physdots "$pw_p"; pw_p=$pw_pd; }
       pw_normalize "$pw_p"
       if pw_broad "$pw_n"; then pw_av=G; else pw_av=NG; fi
       return 0 ;;
  esac
  pw_abs "$b"
  case "$pw_p" in ''|*'$'*|*'`'*) return 0 ;; esac
  if pw_globbroad "$pw_p" "$f"; then pw_av=G; else pw_av=NG; fi
}
# --- the command as the rules read it (r3c) ---
# pw_prep_awk, mode prep: the command with, in every simple command and in every
# quoted string holding a searcher's name, (1) a searcher named by a path, a
# backslash or a quote, or as egrep, fgrep or ugrep, made find, rg or grep (rgrep
# grep -r), as is $(which X), $(command -v X) and `which X` (decision 5); (2) a
# simple command that holds a searcher and its own unquoted depth bound
# (-maxdepth, --max-depth, -d N) made `:`, the bound exempting only that
# command (decision 6); (3) a bare `cd` or `cd --` made `cd ~`; (4) a comment
# dropped. Every other byte is kept. Mode nopath prints 1 when some simple
# command is a recursive searcher given no path, so it walks ".": find with an
# expression first, rg with no path and nothing on its stdin (no pipe into it,
# no < redirection; --files lists "." whatever its stdin), grep -r with none.
pw_prep_awk='
  function base(v,   x) { x = v; sub(/.*\//, "", x); return x }
  function isname(v) { return v ~ /^(find|rg|grep|egrep|fgrep|rgrep|ugrep)$/ }
  function canon(v) { if (v ~ /^(egrep|fgrep|ugrep)$/) return "grep"; if (v == "rgrep") return "grep -r"; return v }
  function value(t,   out, i, n, c, j) {
    out = ""; n = length(t); i = 1
    while (i <= n) {
      c = substr(t, i, 1)
      if (c == "\\") { out = out substr(t, i + 1, 1); i += 2; continue }
      if (c == "\047") { j = index(substr(t, i + 1), "\047"); if (!j) j = n - i + 1; out = out substr(t, i + 1, j - 1); i += j + 1; continue }
      if (c == "\"") { i++; while (i <= n && substr(t, i, 1) != "\"") { c = substr(t, i, 1); if (c == "\\" && i < n) { i++; c = substr(t, i, 1) }; out = out c; i++ }; i++; continue }
      out = out c; i++
    }
    return out
  }
  function dqend(t, i,   n, c) {
    n = length(t); i++
    while (i <= n) { c = substr(t, i, 1); if (c == "\\") { i += 2; continue }; if (c == "\"") return i; i++ }
    return 0
  }
  function subst(t,   out, m, w) {
    out = ""
    while (match(t, /\$\((which|command[[:space:]]+-v)[[:space:]]+[^[:space:]()`]+[[:space:]]*\)|`(which|command[[:space:]]+-v)[[:space:]]+[^[:space:]()`]+[[:space:]]*`/)) {
      m = substr(t, RSTART, RLENGTH); w = m
      sub(/^(\$\(|`)(which|command[[:space:]]+-v)[[:space:]]+/, "", w); sub(/[[:space:]]*(\)|`)$/, "", w)
      out = out substr(t, 1, RSTART - 1) (isname(base(w)) ? w : m); t = substr(t, RSTART + RLENGTH)
    }
    return out t
  }
  function inner(q, depth) { return (mode == "prep" && depth < 6 && q ~ SRCH) ? prep(q, depth + 1) : q }
  function simple(seg, depth, piped,   nt, i, n, c, j, t, b, hs, hb, out, w, tk, val, sp, qd, stdin, npos, hasE, rec, k, files) {
    nt = 0; i = 1; n = length(seg); sp[0] = ""
    while (i <= n) {
      c = substr(seg, i, 1)
      if (c ~ /[[:space:]]/) { sp[nt] = sp[nt] c; i++; continue }
      nt++; tk[nt] = ""; sp[nt] = ""
      while (i <= n) {
        c = substr(seg, i, 1)
        if (c ~ /[[:space:]]/) break
        if (c == "\\") { tk[nt] = tk[nt] substr(seg, i, 2); i += 2; continue }
        if (c == "\047") {
          j = index(substr(seg, i + 1), "\047")
          if (!j) { tk[nt] = tk[nt] substr(seg, i); i = n + 1; continue }
          tk[nt] = tk[nt] "\047" inner(substr(seg, i + 1, j - 1), depth) "\047"; i += j + 1; continue
        }
        if (c == "\"") {
          j = dqend(seg, i)
          if (!j) { tk[nt] = tk[nt] substr(seg, i); i = n + 1; continue }
          tk[nt] = tk[nt] "\"" inner(substr(seg, i + 1, j - i - 1), depth) "\""; i = j + 1; continue
        }
        tk[nt] = tk[nt] c; i++
      }
    }
    for (t = 1; t <= nt; t++) { val[t] = value(tk[t]); qd[t] = (tk[t] ~ /["\047\\]/) }
    t = 1
    while (t <= nt && val[t] ~ /^[A-Za-z_][A-Za-z0-9_]*=/) t++
    while (t <= nt && val[t] ~ /^(env|command|builtin|exec|nice|nohup|time|timeout|xargs|sudo|noglob|stdbuf|caffeinate)$/) {
      t++
      while (t <= nt && (val[t] ~ /^-/ || val[t] ~ /^[0-9][0-9.]*[smhd]?$/ || val[t] ~ /^[A-Za-z_][A-Za-z0-9_]*=/)) t++
    }
    if (t <= nt) { b = base(val[t]); if (isname(b) && tk[t] != canon(b)) { tk[t] = canon(b); val[t] = tk[t] } }
    hs = 0; hb = 0
    for (k = 1; k <= nt; k++) {
      if (isname(base(val[k])) || val[k] == "grep -r") hs = 1
      if (!qd[k] && (tk[k] ~ /^--?max-?depth(=.*)?$/ || tk[k] ~ /^-d[0-9]+$/ || (tk[k] == "-d" && k < nt && tk[k + 1] ~ /^[0-9]+$/))) hb = 1
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
        if (w ~ /^[0-9]*<<?<?$/) { stdin = 1; k++; continue }
        if (w ~ /^[0-9]*</) { stdin = 1; continue }
        if (w ~ /^[0-9&]*>>?&?$/) { k++; continue }
        if (w ~ /^[0-9&]*>/) continue
        if (w == "--") { for (k++; k <= nt; k++) npos++; break }
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
    if (hs && hb) return sp[0] ":" sp[nt]
    if (nt == 1 && tk[1] == "cd") tk[1] = "cd ~"
    else if (nt == 2 && tk[1] == "cd" && tk[2] == "--") tk[2] = "~"
    out = sp[0]
    for (t = 1; t <= nt; t++) out = out tk[t] sp[t]
    return out
  }
  function prep(t, depth,   n, i, c, seg, res, j, sep, piped, run) {
    n = length(t); i = 1; seg = ""; res = ""; piped = 0
    while (i <= n) {
      if (match(substr(t, i, 512), /^[^\\\047"#;&|()\n]+/)) { seg = seg substr(t, i, RLENGTH); i += RLENGTH; continue }
      c = substr(t, i, 1)
      if (c == "\\") { seg = seg substr(t, i, 2); i += 2; continue }
      if (c == "\047") { j = index(substr(t, i + 1), "\047"); if (!j) j = n - i; seg = seg substr(t, i, j + 1); i += j + 1; continue }
      if (c == "\"") { j = dqend(t, i); if (!j) j = n; seg = seg substr(t, i, j - i + 1); i = j + 1; continue }
      if (c == "#" && (seg == "" || seg ~ /[[:space:]]$/)) { while (i <= n && substr(t, i, 1) != "\n") i++; continue }
      if (c == "#") { seg = seg c; i++; continue }
      sep = c
      if ((c == "|" || c == "&") && substr(t, i + 1, 1) == c) sep = c c
      res = res simple(seg, depth, piped) sep; seg = ""; i += length(sep)
      piped = (sep == "|")
    }
    return res simple(seg, depth, piped)
  }
  BEGIN { SRCH = "(^|[[:space:];&|(\047\"\\\\/`])(find|rg|grep|egrep|fgrep|rgrep|ugrep)([[:space:]\047\")`]|$)" }
  mode == "prep" { printf "%s%s", (NR > 1 ? "\001" : ""), prep(subst($0), 0) }
  mode == "nopath" { prep($0, 0) }
  END { if (mode == "nopath") print (found ? 1 : 0) }'
# pw_nopath CMD: CMD (prepped) holds a recursive searcher given no path.
pw_nopath() { [ "$(printf '%s' "$1" | LC_ALL=C awk -v RS=$'\001' -v mode=nopath "$pw_prep_awk" 2>/dev/null)" = 1 ]; }
pw_cmd=$command
if [ "${#command}" -le 65536 ] && printf '%s' "$command" | grep -qE "(^|[[:space:];&|(/\\\\'\"\`])(find|rg|[efru]?grep)[\`'\")]?([[:space:]]|\$)"; then
  pw_cmd=$(printf '%s' "$command" | LC_ALL=C awk -v RS=$'\001' -v mode=prep "$pw_prep_awk" 2>/dev/null) || pw_cmd=""
  [ -n "$pw_cmd" ] || pw_cmd=$command
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
  # quote, or the end.
  root_core="/|/\\*|/Users/?|(${broad_re})/?"
  root_re="(${root_core})"
  root_end="['\"]?([[:space:]|;&)]|\$)"
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
  # -c without globstar, z otherwise) and whether a quote mark was part of it
  # (q, else u), each is judged here, and pass 2
  # rewrites the ones whose verdict moves (F, or a placeholder root, inside
  # the word's first quote mark when it has one), so every rule below reads
  # the rewritten command.
  pw_root_re="^(${root_core})\$"
  pw_normalize "$HOME"; pw_home=$pw_n
  pw_nt=0 pw_nd=0 pw_dec="" pw_deep="" pw_unwrap=""
  pw_awk='
    function ctx_of(pre, ctx) {
      if (pre ~ /(^|[[:space:];&|(\/])(sh|bash|dash|ksh)([[:space:]]+-[^[:space:]]+)*[[:space:]]+-[A-Za-z]*c[A-Za-z]*[[:space:]]*$/) return "b"
      if (pre ~ /(^|[[:space:];&|(\/])zsh([[:space:]]+-[^[:space:]]+)*[[:space:]]+-[A-Za-z]*c[A-Za-z]*[[:space:]]*$/) return "z"
      return ctx
    }
    function isc(pre) {
      return pre ~ /(^|[[:space:];&|(\/])(sh|bash|dash|ksh|zsh)([[:space:]]+-[^[:space:]]+)*[[:space:]]+-[A-Za-z]*c[A-Za-z]*[[:space:]]*$/ || pre ~ /(^|[[:space:];&|(])eval[[:space:]]+$/
    }
    function lit(c) {
      if (c == "{") return "\001"; if (c == "}") return "\002"; if (c == ",") return "\003"
      if (c == "(") return "\004"; if (c == "|") return "\005"; if (c == ")") return "\006"
      return c
    }
    function sform(run, mode,   out, i, n, c, j, seg, k) {
      out = ""; n = length(run); i = 1
      while (i <= n) {
        c = substr(run, i, 1)
        if (c == "\\") { if (i < n) out = out (mode ? lit(substr(run, i + 1, 1)) : substr(run, i + 1, 1)); i += 2; continue }
        if (c == "$" && substr(run, i + 1, 1) == "\047") {
          j = index(substr(run, i + 2), "\047")
          if (j) { out = out (mode ? "*" : substr(run, i + 2, j - 1)); i += j + 2; continue }
        }
        if (c == "\047") {
          j = index(substr(run, i + 1), "\047"); if (!j) j = n - i + 1
          seg = substr(run, i + 1, j - 1)
          if (mode) for (k = 1; k <= length(seg); k++) out = out lit(substr(seg, k, 1)); else out = out seg
          i += j + 1; continue
        }
        if (c == "\"") {
          i++
          while (i <= n && substr(run, i, 1) != "\"") {
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
    function walk(s, ctx, depth, prose,   out, orig, run, orun, b, sf, qc, rest, piece, opiece, q, inner, nctx, gap, first, cd, cdp, val) {
      out = ""; orig = ""; first = 1; cd = 0; cdp = 0
      while (match(s, /(\\.|[^[:space:]|;&()<>"\047\\]|"([^"\\]|\\.)*"|\047[^\047]*\047|\(([^[:space:]()<>;&"\047\\]|\\.)*\))+/)) {
        gap = substr(s, 1, RSTART - 1)
        if (gap ~ /[;&|()]/) { first = 1; cd = 0; cdp = 0 }
        out = out gap; orig = orig gap
        run = substr(s, RSTART, RLENGTH); s = substr(s, RSTART + RLENGTH); orun = run
        b = run; gsub(/["\047]/, "", b)
        sf = sform(run, 1)
        if (sf ~ /^(~[A-Za-z0-9._-]*|\$HOME|\$SUBFLEET_(ROOT|HOME))(\/|$)/ || sf ~ /^\// || (sf ~ /^\{/ && sf ~ /,|\.\./)) {
          k++
          if (pass == 1) printf "%d\t%s\t%s\t%s\t%s\t%s\n", k, ctx, (run == sf) ? "u" : "q", (cd && !cdp) ? "c" : "p", sf, b
          else if (k in tok) {
            qc = ""
            if (match(run, /["\047]/)) qc = substr(run, RSTART, 1)
            run = qc tok[k] qc
          }
          out = out run
        } else {
          rest = run; piece = ""; opiece = ""
          while (match(rest, /"([^"\\]|\\.)*"|\047[^\047]*\047/)) {
            piece = piece substr(rest, 1, RSTART - 1); opiece = opiece substr(rest, 1, RSTART - 1)
            q = substr(rest, RSTART, RLENGTH); rest = substr(rest, RSTART + RLENGTH)
            inner = substr(q, 2, length(q) - 2)
            nctx = ctx_of(orig opiece, ctx)
            if (nctx == "b" && inner ~ /globstar/) nctx = "z"
            piece = piece substr(q, 1, 1) walk(inner, nctx, depth, 1) substr(q, 1, 1); opiece = opiece q
          }
          out = out piece rest
          # A -c or eval argument holding a searcher is the script a shell runs:
          # judged again as its own line, to depth 4 (r3c decision 6).
          if (!prose && isc(orig)) {
            val = sform(orun, 0)
            if (val ~ /(^|[[:space:];&|(\047"\\\/`])(find|rg|grep|egrep|fgrep|rgrep|ugrep)([[:space:]\047")`]|$)/) {
              nctx = ctx_of(orig, ctx)
              if (nctx == "b" && val ~ /globstar/) nctx = "z"
              if (depth >= 4) deep = 1
              else { val = walk(val, nctx, depth + 1, 0); extra = extra val "\n" }
            }
          }
        }
        val = sform(orun, 0)
        if (first) { cd = (val == "cd" || val == "pushd"); first = (val == "builtin" || val == "command") }
        else if (cd && val == "-P") cdp = 1
        orig = orig orun
      }
      return out s
    }
    pass == 2 && NR <= nb { split($0, f, "\t"); tok[f[1]] = f[2]; next }
    { line = walk($0, "z", 0, 0); if (pass == 2) print line }
    END {
      if (pass == 1 && deep) print "0\tdeep"
      if (pass == 1 && extra != "") print "0\tunwrap"
      if (pass == 2) printf "%s", extra
    }'
  while IFS=$'\t' read -r pw_k pw_c pw_q pw_f pw_b pw_s; do
    if [ "$pw_k" = 0 ]; then
      case "$pw_c" in deep) pw_deep=1 ;; unwrap) pw_unwrap=1 ;; esac
      continue
    fi
    pw_word "$pw_b" "$pw_c" "$pw_q" "$pw_f" "$pw_s"
    if [ -n "$pw_t" ]; then pw_dec="$pw_dec$pw_k	$pw_t
"; pw_nd=$((pw_nd + 1)); fi
  done < <(printf '%s\n' "$judged" | LC_ALL=C awk -v pass=1 "$pw_awk" 2>/dev/null)
  if [ "$pw_nd" -gt 0 ] || [ -n "$pw_unwrap" ]; then
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
  if printf '%s' "$stripped" | grep -qE "$xargs_re"; then
    if printf '%s' "$stripped" | grep -qE "$xargs_feed_re"; then
      hit=1; matched=$(first_match "$stripped" "$xargs_feed_re" | sed 's/[[:space:]]*$//')
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
