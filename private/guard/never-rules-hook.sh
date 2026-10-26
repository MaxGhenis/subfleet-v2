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
#     and a glob word that can match a root is judged by its matches. Change
#     a root on both sides in one reviewed step: here, and in
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
# codex addition that lets the fenced hf-dest heredoc scan below run.
printf '%s' "$command" | grep -qE 'stash|AXIOM_|getUpdates|get_updates|dump-keychain|checkout[[:space:]]+-b|switch[[:space:]]+-c|pr[[:space:]]+merge|git[[:space:]]+push|(^|[[:space:];&|(])(find|rg|grep)[[:space:]]' \
  || printf '%s' "$command" | grep -qE '\*\*\* Begin Patch' \
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
#   escape     any `-maxdepth` / `--max-depth` in the command disarms the rule
#              (a depth-bounded search is fine).
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
#              while any searcher is present; and `.`/`./` when the cwd itself
#              is a broad root. A root in another command of the pipeline
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
# Path words judged on disk (D-LANE-SCOPE1 r3, 2026-10-25; r3b, 2026-10-26,
# for parity with snapshot-5 of the Claude half's unscoped_search.py). Before
# the text rules below, every path word of the command is judged once. A path
# word is a run of characters other than whitespace and | ; & ( ) < >, where a
# quoted string counts as part of its run (`"/Volumes/Macintosh HD"` is one
# word), its quote marks dropped, that starts with a root spelling: ~, $HOME,
# $SUBFLEET_ROOT or $SUBFLEET_HOME (each alone or before a /), or /. A run
# that is no path word has the words of each of its quoted strings judged in
# turn (`bash -c 'cd ~/x && rg y'`).
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
#               different path that is (`rg x ~/link-to-home`). A path under a
#               mount that may stall (/Volumes, /Network, /net, /home and
#               anything under them) is never resolved or tested on disk: it
#               reads as no file, and as leading nowhere else.
#   walks       A root followed by a trailing run of bare `*` or `.*`
#               components (`rg foo ~/.subfleet/jobs/*`, `~/*/*`), or by a `**/`
#               component, is that root: the shell hands the searcher every
#               entry of it, and zsh walks it for `**/`. Inside a script run by
#               bash, sh, dash or ksh -c, `**` is `*` (no globstar). `/*`
#               itself stays the text rule's root.
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
#               A glob word holding $, a backtick or { is judged by its text.
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
# --- the file system: P is an absolute path, judged under the seam ---
# pw_islink P: P is a symlink (not followed).
pw_islink() { [ -L "$pw_fs$1" ]; }
# pw_lexists P: something is at P, a dangling link included.
pw_lexists() { [ -e "$pw_fs$1" ] || [ -L "$pw_fs$1" ]; }
# pw_nondir P: P exists and is not a directory, links followed.
pw_nondir() { [ -e "$pw_fs$1" ] && [ ! -d "$pw_fs$1" ]; }
# pw_readlink P: pw_rl is the target of the link at P.
pw_readlink() { pw_rl=$(readlink "$pw_fs$1" 2>/dev/null) || pw_rl=""; }
# --- names ---
# pw_abs WORD: pw_p is the absolute path a root spelling names, or empty.
pw_abs() {
  pw_p=""
  case "$1" in
    '~'|'$HOME') pw_p=$HOME ;;
    '~/'*) pw_p="$HOME/${1#\~/}" ;;
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
# pw_normalize PATH: pw_norm, then the data volume folded to / and /private/tmp,
# /private/var and /private/etc to /tmp, /var and /etc (any letter case).
pw_normalize() {
  local nc=""
  pw_norm "$1"
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
# pw_child S: S (a path below a home, or below /Users/<any>) is one of its
# broad children, or a folder directly in its .subfleet. Caller has nocasematch.
pw_child() {
  case "$1" in
    theaxiomfoundation|rulesfoundation|policyengine|library|library/caches|.cache|.local|.subfleet) return 0 ;;
    .subfleet/*) case "${1:10}" in */*) return 1 ;; esac; return 0 ;;
  esac
  return 1
}
# pw_isbroad P: P (normalized) is a broad root by name.
pw_isbroad() {
  local p=$1 rest="" nc="" r=1
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  case "$p" in
    /|/users|/volumes|/system|/system/volumes|/private|/tmp|/var|/var/folders|/opt|/opt/homebrew|/library|/applications|/usr) r=0 ;;
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
# --- where links lead ---
# pw_isfile P: P (normalized) exists and is no directory.
pw_isfile() { pw_nondir "$1"; }
# pw_broad P: P (normalized) is a broad root by name and no file.
pw_broad() { pw_isbroad "$1" && ! pw_isfile "$1"; }
# --- globs (r3's whole-word expansion) ---
# pw_listed DIR: whether the normalized DIR can hold a broad root by name.
pw_listed() {
  case "$1" in
    /|/Users|/private|"$pw_home"|"$pw_home/Library"|"$pw_home/.subfleet") return 0 ;;
  esac
  local u=${1#/Users/}
  [ "$u" != "$1" ] || return 1
  case "$u" in
    */*/*) return 1 ;;
    */Library|*/.subfleet) return 0 ;;
    */*) return 1 ;;
  esac
  return 0
}
# pw_glob DIR PATTERN: status 0 iff one of the first pw_cap matches of
# DIR/PATTERN is a broad root. Bash's own pathname expansion of an unquoted
# variable, which never runs a substitution; every setting it changes is put
# back before it judges a match.
pw_glob() {
  local f="" n="" d="" c="" e="" g="" ifs=${IFS-} ifs_set=${IFS+x} gi=${GLOBIGNORE-} gi_set=${GLOBIGNORE+x} m=""
  case $- in *f*) f=1 ;; esac
  if shopt -q nullglob; then n=1; fi
  if shopt -q dotglob; then d=1; fi
  if shopt -q nocaseglob; then c=1; fi
  if shopt -q extglob; then e=1; fi
  if shopt -q failglob; then g=1; fi
  unset GLOBIGNORE
  set +f
  shopt -s nullglob
  shopt -u dotglob nocaseglob extglob failglob
  IFS=
  pw_m=( "$pw_fs$1"/$2 )
  if [ -n "$ifs_set" ]; then IFS=$ifs; else unset IFS; fi
  if [ -n "$gi_set" ]; then GLOBIGNORE=$gi; fi
  if [ -n "$n" ]; then shopt -s nullglob; else shopt -u nullglob; fi
  if [ -n "$d" ]; then shopt -s dotglob; else shopt -u dotglob; fi
  if [ -n "$c" ]; then shopt -s nocaseglob; else shopt -u nocaseglob; fi
  if [ -n "$e" ]; then shopt -s extglob; else shopt -u extglob; fi
  if [ -n "$g" ]; then shopt -s failglob; else shopt -u failglob; fi
  if [ -n "$f" ]; then set -f; fi
  [ "${#pw_m[@]}" -gt 0 ] && [ "$pw_cap" -gt 0 ] || return 1
  # Each match by name (the seam folder taken off, //, . and .. resolved, the
  # home spelled ~), then on disk: a broad root by name that is an existing
  # non-directory is not broad (decision 1).
  printf '%s\n' "${pw_m[@]:0:$pw_cap}" | PW_FS="$pw_fs" PW_HOME="$pw_home" PW_ROOT="$pw_root_re" LC_ALL=C awk '
    function norm(p,   n, i, parts, k, r, st) {
      n = split(p, parts, "/"); k = 0
      for (i = 1; i <= n; i++) {
        if (parts[i] == "" || parts[i] == ".") continue
        if (parts[i] == "..") { if (k > 0) k--; continue }
        st[++k] = parts[i]
      }
      r = ""
      for (i = 1; i <= k; i++) r = r "/" st[i]
      return (r == "") ? "/" : r
    }
    BEGIN { fs = ENVIRON["PW_FS"]; home = ENVIRON["PW_HOME"]; re = ENVIRON["PW_ROOT"] }
    {
      p = $0
      if (fs != "") { if (index(p, fs "/") != 1) next; p = substr(p, length(fs) + 1) }
      p = norm(p)
      if (home != "" && home != "/") {
        if (p == home) p = "~"
        else if (index(p, home "/") == 1) p = "~" substr(p, length(home) + 1)
      }
      if (p ~ re) print $0
    }' 2>/dev/null | {
    while IFS= read -r m; do
      if [ -e "$m" ] && [ ! -d "$m" ]; then continue; fi
      exit 0
    done
    exit 1
  }
}
# pw_word_glob WORD: decisions 2 and 3 for a word with a glob character. pw_t
# is a placeholder root (broad), F (judged on disk, not broad), or empty (the
# text rule's).
pw_word_glob() {
  local b=$1 walk="" lit="" dir="" rest=""
  pw_t=""
  case "/$b" in
    */'**'/*) b=${b%%/'**'/*}; walk=1 ;;
    *) [ "$b" = "/" ] || b=${b%/}
       while :; do
         case "$b" in */'*'|*/'.*'|*/'**') b=${b%/*}; walk=1 ;; *) break ;; esac
       done ;;
  esac
  [ -n "$b" ] || b=/
  case "$b" in
    *[*?[]*) ;;
    *) if [ -n "$walk" ] && [[ $b =~ $pw_root_re ]]; then pw_sentinel "$1" " (glob)"; fi
       return 0 ;;
  esac
  pw_abs "$b"
  case "$pw_p" in ''|*'$'*|*'`'*|*'{'*) return 0 ;; esac
  lit=${pw_p%%[*?[]*}; dir=${lit%/*}; rest=${pw_p:${#dir}+1}
  pw_norm "${dir:-/}"
  pw_listed "$pw_n" || return 0
  if pw_glob "$dir" "$rest"; then pw_sentinel "$1" " (glob)"; else pw_t=F; fi
}
# --- words ---
# pw_sentinel WORD [SUFFIX]: pw_t is a new placeholder root, named WORD SUFFIX.
pw_sentinel() {
  pw_nt=$((pw_nt + 1))
  pw_t="/Users/@sfglob${pw_nt}@"
  pw_shown[$pw_nt]="$1${2-}"
}
# pw_word WORD CONTEXT: pw_t is a placeholder root (broad), F (not broad), or
# empty (the text rules' verdict stands). CONTEXT b: the word is in a script
# run by bash, sh, dash or ksh -c, where `**` is `*`. QUOTED q: a quote mark
# was part of the word, so a broad one is placed even where the text rules
# read it as a root by name (they would see a quoted part as Q).
pw_word() {
  local nc=""
  shopt -q nocasematch && nc=1
  shopt -u nocasematch
  pw_word_judge "$@"
  [ -z "$nc" ] || shopt -s nocasematch
}
pw_word_judge() {
  local w=$1 b="" walk=""
  pw_t=""
  case "$w" in
    '/*') return 0 ;;
    *[*?[]*) ;;
    *) pw_abs "$w"
       case "$pw_p" in ''|*'$'*|*'`'*) return 0 ;; esac
       pw_normalize "$pw_p"
       if pw_broad "$pw_n"; then
         if [ "${3-}" = q ] || ! [[ $w =~ $pw_root_re ]]; then pw_sentinel "$w"; fi
       elif [[ $w =~ $pw_root_re ]]; then
         pw_t=F
       fi
       return 0 ;;
  esac
  pw_word_glob "$w"
}
# <<< r3 path words: end <<<

searcher=""
if printf '%s' "$command" | grep -qE '(^|[[:space:];&|(])find[[:space:]]'; then searcher="find"
elif printf '%s' "$command" | grep -qE '(^|[[:space:];&|(])rg[[:space:]]'; then searcher="rg"
elif printf '%s' "$command" | grep -qE '(^|[[:space:];&|(])grep[[:space:]]' \
  && printf '%s' "$command" | grep -qE '(^|[[:space:]])(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive)([[:space:]]|$)'; then searcher="recursive grep"
fi
if [ -n "$searcher" ] && ! printf '%s' "$command" | grep -qE -- '--?max-?depth'; then
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
  # (and `${SUBFLEET_ROOT}`/`${SUBFLEET_HOME}` without braces too).
  # The searcher detection above and the cwd rule below keep looking at the raw
  # command.
  judged=$(printf '%s' "$command" | LC_ALL=C awk '{ if (sub(/\\$/, "")) printf "%s", $0; else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
  [ -n "$judged" ] || judged=$command
  # Two trailing backslashes escape each other, so the following newline is a
  # real command separator. The mandated one-backslash join above cannot tell
  # that case apart; retain the raw newline when an even run is present so an
  # old blocked crawl on the next line cannot disappear during normalization.
  if printf '%s' "$command" | LC_ALL=C awk 'match($0, /\\+$/) && RLENGTH % 2 == 0 { even=1 } END { exit !even }' 2>/dev/null; then
    judged=$(printf '%s' "$command" | LC_ALL=C awk '{ if (match($0, /\\+$/) && RLENGTH % 2 == 1) printf "%s", substr($0, 1, length($0) - 1); else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
    [ -n "$judged" ] || judged=$command
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
  pw_nt=0 pw_nd=0 pw_dec=""
  pw_awk='
    function ctx_of(pre, ctx) {
      if (pre ~ /(^|[[:space:];&|(\/])(sh|bash|dash|ksh)([[:space:]]+-[^[:space:]]+)*[[:space:]]+-[A-Za-z]*c[A-Za-z]*[[:space:]]*$/) return "b"
      if (pre ~ /(^|[[:space:];&|(\/])zsh([[:space:]]+-[^[:space:]]+)*[[:space:]]+-[A-Za-z]*c[A-Za-z]*[[:space:]]*$/) return "z"
      return ctx
    }
    function walk(s, ctx,   out, orig, run, orun, b, qc, rest, piece, opiece, q, inner, nctx) {
      out = ""; orig = ""
      while (match(s, /([^[:space:]|;&()<>"\047]|"[^"]*"|\047[^\047]*\047)+/)) {
        out = out substr(s, 1, RSTART - 1); orig = orig substr(s, 1, RSTART - 1)
        run = substr(s, RSTART, RLENGTH); s = substr(s, RSTART + RLENGTH); orun = run
        b = run; gsub(/["\047]/, "", b)
        if (b ~ /^(~|\$HOME|\$SUBFLEET_(ROOT|HOME))(\/|$)/ || b ~ /^\//) {
          k++
          if (pass == 1) printf "%d\t%s\t%s\t%s\n", k, ctx, (run == b) ? "u" : "q", b
          else if (k in tok) {
            qc = ""
            if (match(run, /["\047]/)) qc = substr(run, RSTART, 1)
            run = qc tok[k] qc
          }
          out = out run
        } else {
          rest = run; piece = ""; opiece = ""
          while (match(rest, /"[^"]*"|\047[^\047]*\047/)) {
            piece = piece substr(rest, 1, RSTART - 1); opiece = opiece substr(rest, 1, RSTART - 1)
            q = substr(rest, RSTART, RLENGTH); rest = substr(rest, RSTART + RLENGTH)
            inner = substr(q, 2, length(q) - 2)
            nctx = ctx_of(orig opiece, ctx)
            if (nctx == "b" && inner ~ /globstar/) nctx = "z"
            piece = piece substr(q, 1, 1) walk(inner, nctx) substr(q, 1, 1); opiece = opiece q
          }
          out = out piece rest
        }
        orig = orig orun
      }
      return out s
    }
    pass == 2 && NR <= nb { split($0, f, "\t"); tok[f[1]] = f[2]; next }
    { line = walk($0, "z"); if (pass == 2) print line }'
  while IFS=$'\t' read -r pw_k pw_c pw_q pw_b; do
    pw_word "$pw_b" "$pw_c" "$pw_q"
    if [ -n "$pw_t" ]; then pw_dec="$pw_dec$pw_k	$pw_t
"; pw_nd=$((pw_nd + 1)); fi
  done < <(printf '%s\n' "$judged" | LC_ALL=C awk -v pass=1 "$pw_awk" 2>/dev/null)
  if [ "$pw_nd" -gt 0 ]; then
    pw_j=$( { printf '%s' "$pw_dec"; printf '%s\n' "$judged"; } | LC_ALL=C awk -v pass=2 -v nb="$pw_nd" "$pw_awk" 2>/dev/null) && [ -n "$pw_j" ] && judged=$pw_j
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
  # cwd rule: `find .` / `rg pat .` executed while sitting IN a broad root.
  if [ -z "$hit" ]; then
    case "$cwd" in
      "$HOME"|"$HOME/TheAxiomFoundation"|"$HOME/RulesFoundation"|"$HOME/PolicyEngine"|/tmp|/private/tmp|/|/Users)
        if printf '%s' "$command" | grep -qE "(^|[[:space:];&|(])(find|rg|grep)[[:space:]]+([^|;&]*[[:space:]])?\.(/)?([[:space:]]|\$)"; then hit=1; matched=". (cwd $cwd)"; fi ;;
    esac
  fi
  if [ -n "$hit" ]; then
    [ -n "$matched" ] || matched="?"
    # >>> r3 path words: name >>>
    pw_i=1
    while [ "$pw_i" -le "$pw_nt" ]; do
      pw_t="/Users/@sfglob${pw_i}@"
      case "$matched" in *"$pw_t"*) matched="${matched%%"$pw_t"*}${pw_shown[$pw_i]}${matched#*"$pw_t"}" ;; esac
      pw_i=$((pw_i + 1))
    done
    # <<< r3 path words: name <<<
    block "[unscoped-search] Whole-tree ${searcher} over a broad root (/, /Users, ~, ~/TheAxiomFoundation, ~/PolicyEngine, ~/.subfleet, ~/.subfleet/<child>, /tmp, caches) — 8 of these ran in parallel on 8/8 and drove load to 47, and ~22 sol-lane finds on 8/18 held load at 57 for hours, beachballing the Mac. Scope it: exact subdirectory, -maxdepth/--max-depth, 'git ls-files' inside a repo, 'axiom-locate' (engine|release|pypkg|corpus-file) for Axiom artifacts, or the interpreter for python package paths (python -c 'import x; print(x.__file__)'). Matched broad root: '${matched}'."
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
