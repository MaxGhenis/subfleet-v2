# --- hand-authored-rulespec: rule YAML staged in a rulespec-* checkout needs its encoder apply manifest ---
# Companion tests (*.test.yaml) and composed pipelines (…/composed/…) are exempt; everything else under
# <cc>/(statutes|regulations|policies|legislation|programs)/ must have .axiom/encoding-manifests/<mirror>.json
# staged or tracked. The CI guard is the second wall; this is the first.
# Guard sync fix r1 (2026-11-15): every `git … commit` in the command is judged. git's global options (-C, -c,
# --no-pager, --git-dir, …) may come before `commit`, and GIT_DIR=… before git. Its folder is each one the
# shell can be in there: the payload cwd and every cd/pushd target before it (a relative one resolved
# against the folder before it), with each -C applied. One cat-file --batch-check reads every manifest, so a
# large index costs a few processes, not four per file; a folder that is not there costs none.
# Guard sync fix r2 (2026-11-17): a path's `.` and repeated `/` collapse, and each folder is resolved (cd -P,
# pwd -P) before git runs, so every spelling that resolves to one folder is checked once (a subfolder of a
# checkout is a folder of its own); more than 64 paths to resolve is refused. The git work is bounded by time:
# before each git call for a third or later folder, the block denies once 15 s have passed since it began,
# and never allows (the hook's 60 s timeout allows). The first two folders are checked in full: the first two
# distinct (resolved folder, --git-dir) keys that exist, in the awk's order (each git … commit in command
# order, from each folder met before it, the payload cwd first and then each cd/pushd target, with that
# commit's -C applied); a folder that is not there is skipped and not counted. So `cd a && cd b && git commit`
# can be refused under load: b is the third. Staged names are read raw (-z, --no-relative), so
# each name under the anchor is looked up with its own bytes, and one holding a newline is denied.
# Guard sync fix r3 (2026-11-26): each folder stays in that form as the words are read, so a cd costs its own
# words and one copy of its folder, never a new split of it. A cd/pushd target's `..` takes off the word before
# it, as bash and zsh do by default; where its physical spelling (`..` kept, as cd -P, set -P or zsh's
# CHASE_LINKS resolve it) differs, that folder is checked too, and both count toward the 64. -C, --git-dir and
# GIT_DIR= keep `..` as written, as git resolves them. More than 4096 folders to examine (each folder for each
# git … commit after it, once more for each -C) is refused.
if printf '%s' "$command" | grep -qE 'git[[:space:]].*(commit|\\$)'; then
  hr_t0=$SECONDS
  hr_list=$(printf '%s' "$command" | LC_ALL=C awk -v cwd="$cwd" -v home="$HOME" '
    function expand(p) {
      if (p == "~" || p == "$HOME") return home
      if (substr(p, 1, 2) == "~/") return home substr(p, 2)
      if (substr(p, 1, 6) == "$HOME/") return home substr(p, 6)
      return p
    }
    # the same path with each `.` and empty component dropped; `..` stays for the shell to resolve, and a
    # file path keeps a trailing `/`, which only a folder passes
    function norm(p, file,   a, n, i, o) {
      n = split(p, a, "/"); o = ""
      for (i = 1; i <= n; i++) if (a[i] != "" && a[i] != ".") o = o "/" a[i]
      if (substr(p, 1, 1) != "/") o = "." o
      else if (o == "") o = "/"
      if (file && o != "/" && p ~ /\/\.?$/) o = o "/"
      return o
    }
    # norm(base "/" p) for a folder base already in the form norm gives, or norm(p) for an absolute p: only the
    # words of p are split, so a stored folder is copied once and never split again
    function join(base, p,   a, n, i, r) {
      p = expand(p)
      if (substr(p, 1, 1) == "/") base = "/"
      n = split(p, a, "/"); r = ""
      for (i = 1; i <= n; i++) if (a[i] != "" && a[i] != ".") r = r "/" a[i]
      return r == "" ? base : (base == "/" ? r : base r)
    }
    # where a logical cd or pushd (the default in bash and zsh) lands: join, but each `..` takes off the word
    # before it, by name, as the shell does before it enters the folder; at `/` it does nothing, after `.` or `..`
    # it stays
    function cdl(base, p,   a, n, i, st, t, e, s, w, r) {
      p = expand(p)
      if (substr(p, 1, 1) == "/") base = "/"
      n = split(p, a, "/"); t = 0; e = length(base)
      for (i = 1; i <= n; i++) {
        w = a[i]
        if (w == "" || w == ".") continue
        if (w != "..") { st[++t] = w; continue }
        if (t && st[t] != "..") { t--; continue }
        if (!t) {
          for (s = e; s > 0 && substr(base, s, 1) != "/"; s--) ;
          w = substr(base, s + 1, e - s)
          if (w == "") continue
          if (w != "." && w != "..") { e = s > 1 ? s - 1 : 1; continue }
        }
        st[++t] = ".."
      }
      base = substr(base, 1, e); r = ""
      for (i = 1; i <= t; i++) r = r "/" st[i]
      return r == "" ? base : (base == "/" ? r : base r)
    }
    { text = text $0 "\n" }
    END {
      if (cwd == "") cwd = "."
      cwd = norm(cwd); if (home != "") home = norm(home)
      gsub(/\\\n/, "", text); gsub(/\$\{HOME\}/, "$HOME", text); gsub(/["'\''\\]/, "", text)
      gsub(/[;&|()`{}<>\n]/, " ; ", text)
      n = split(text, w, /[ \t]+/)
      nd = 1; dirs[1] = cwd; known[cwd] = 1; cur = cwd; prev = cwd; pcur = cwd; pprev = cwd; gd_env = ""; no = 0; wk = 0
      for (i = 1; i <= n; i++) {
        t = w[i]
        if (t == ";") { gd_env = ""; continue }
        if (t == "cd" || t == "pushd" || t == "popd") {
          for (k = i + 1; k <= n && (w[k] ~ /^-[LPe@]+$/ || w[k] == "--"); k++) ;
          v = (t == "popd") ? "-" : w[k]
          if (v == "-") { v = cur; cur = prev; prev = v; v = pcur; pcur = pprev; pprev = v; continue }
          # cur: where the shell lands resolving `..` by name; pcur: resolving links first, `..` kept for cd -P
          prev = cur; pprev = pcur
          if (k > n || v == ";" || v == "") cur = pcur = home
          else { cur = cdl(cur, v); pcur = join(pcur, v) }
          if (!(cur in known)) { known[cur] = 1; dirs[++nd] = cur }
          if (!(pcur in known)) { known[pcur] = 1; dirs[++nd] = pcur }
          continue
        }
        if (t ~ /^GIT_DIR=/) { gd_env = substr(t, 9); continue }
        if (t != "git" && t !~ /\/git$/) continue
        nc = 0; gd = gd_env
        for (j = i + 1; j <= n; j++) {
          o = w[j]
          if (o == "-C") { cs[++nc] = w[++j]; continue }
          if (o == "--git-dir") { gd = w[++j]; continue }
          if (o ~ /^--git-dir=/) { gd = substr(o, 11); continue }
          if (o == "-c" || o == "--work-tree" || o == "--namespace" || o == "--config-env" || o == "--attr-source") { j++; continue }
          if (o !~ /^-/) break
        }
        if (w[j] != "commit") continue
        if (gd != "") gd = norm(expand(gd), 1)
        for (k = 1; k <= nd; k++) {
          if ((wk += 1 + nc) > 4096) { print "TOOMANY"; exit }
          d = dirs[k]
          for (c = 1; c <= nc; c++) if (cs[c] != "" && cs[c] != ";") d = join(d, cs[c])
          key = d "\037" gd
          if (key in seen) continue
          seen[key] = 1
          if (++no > 64) { print "TOOMANY"; exit }
          out[no] = key
        }
      }
      for (k = 1; k <= no; k++) print out[k]
    }')
  if [ "$hr_list" = "TOOMANY" ]; then
    block "[hand-authored-rulespec] This command names more than 64 folders to commit from (each cd/pushd target, with each git -C applied), or more than 4096 folders to examine (each folder for each git commit after it, once more for each -C); the guard cannot check them all in time. Commit from one checkout per command."
  fi
  hr_late="[hand-authored-rulespec] The guard's 15 s budget for checking staged indexes ran out before it checked every folder this command commits from; it does not let an unchecked commit through. Commit from one checkout per command, or retry when the host is less busy."
  # Each folder is keyed by its physical path (the trailing x keeps a newline that ends it) and its git dir,
  # compared whole; a folder that is not there costs no git process.
  hr_n=0
  while IFS=$'\037' read -r hr_d hr_gd; do
    [ -n "$hr_d" ] || continue
    gdir=$(CDPATH= cd -P -- "$hr_d" 2>/dev/null || exit 1; { pwd -P || printf '%s\n' "$hr_d"; } 2>/dev/null; echo x) || continue
    gdir=${gdir%$'\n'x}
    hr_i=0
    while [ "$hr_i" -lt "$hr_n" ] && { [ "${hr_dirs[hr_i]}" != "$gdir" ] || [ "${hr_gds[hr_i]}" != "$hr_gd" ]; }; do hr_i=$((hr_i + 1)); done
    [ "$hr_i" -eq "$hr_n" ] || continue
    hr_dirs[hr_n]=$gdir; hr_gds[hr_n]=$hr_gd; hr_n=$((hr_n + 1))
    [ "$hr_n" -le 2 ] || [ $((SECONDS - hr_t0)) -lt 15 ] || block "$hr_late"
    origin_url=$(git -C "$gdir" ${hr_gd:+"--git-dir=$hr_gd"} remote get-url origin 2>/dev/null)
    printf '%s' "$origin_url" | grep -qiE 'TheAxiomFoundation/rulespec-' || continue
    [ "$hr_n" -le 2 ] || [ $((SECONDS - hr_t0)) -lt 15 ] || block "$hr_late"
    hr_files=$(git -C "$gdir" ${hr_gd:+"--git-dir=$hr_gd"} diff --cached --name-only -z --no-relative --diff-filter=ACMR 2>/dev/null \
      | LC_ALL=C tr '\n\0' '\001\n' \
      | LC_ALL=C awk '/\.test\.yaml$/ || /\/composed\// || /^\.axiom\// { next }
          /^[a-z][a-z](-[a-z0-9-]+)?\/(statutes|regulations|policies|legislation|programs)\/.+\.ya?ml$/')
    [ -n "$hr_files" ] || continue
    [ "$hr_n" -le 2 ] || [ $((SECONDS - hr_t0)) -lt 15 ] || block "$hr_late"
    missing=$( { printf '%s\n--\n' "$hr_files"
                 printf '%s\n' "$hr_files" \
                   | LC_ALL=C awk '{ sub(/\.[^.]*$/, ""); print ":.axiom/encoding-manifests/" $0 ".json"; print "HEAD:.axiom/encoding-manifests/" $0 ".json" }' \
                   | git -C "$gdir" ${hr_gd:+"--git-dir=$hr_gd"} cat-file --batch-check 2>/dev/null; } \
      | LC_ALL=C awk '!s { if ($0 == "--") s = 1; else f[++n] = $0; next } { r[++k] = $0 }
          END { for (i = 1; i <= n; i++) if (f[i] ~ /\001/ || (r[2 * i - 1] !~ /^[0-9a-f]+ / && r[2 * i] !~ /^[0-9a-f]+ /)) { g = f[i]; gsub(/[\001-\037\177]/, "?", g); printf " %s", g } }')
    if [ -n "$missing" ]; then
      block "[hand-authored-rulespec] Staged RuleSpec content with no encoder apply manifest:$missing. RuleSpec modules come only from 'axiom-encode encode <corpus citation> --backend codex --apply', which writes .axiom/encoding-manifests/<mirror path>.json; hand-authored YAML is never a module (Max 9/6). Repairs go through --review-findings / --repair-candidate-* replay. Unstage the files and run the encoder."
    fi
  done <<< "$hr_list"
fi

