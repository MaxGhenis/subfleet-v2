# --- axiom-brief-regime: every lane brief that can touch RuleSpec carries the encoding regime ---
# Max (2026-09-06): "it must always use the encoder agent … if this isn't basically impossible
# to mess up again we need to fix it." A brief dispatched into the Axiom mirror that mentions
# RuleSpec/rulespec/encoding must carry the ENCODING REGIME block verbatim (ops/il-lane/COMMON-RULES.md
# / TheAxiomFoundation/CLAUDE.md). Lanes do not read CLAUDE.md; the brief is their law.
# Guard sync fix r1 (2026-11-15): every `subfleet run` in the command is judged, from each folder the shell
# can be in there (the payload cwd and every cd/pushd target before it). Every -C and -p counts, attached
# (-Cdir, -pfile, -p=file) or not, and a relative one resolves against that folder, as subfleet resolves
# it; more than 16 briefs to read is refused.
# Guard sync fix r2 (2026-11-17): a path's `.` and repeated `/` collapse, and each brief's folder is
# resolved (cd -P, pwd -P) before the brief is read, so every spelling that resolves to one brief is read
# once. More than 64 paths to resolve, or more than 16 distinct briefs, is refused.
# Guard sync fix r3 (2026-11-26): each folder stays in that form as the words are read, so a cd costs its own
# words and one copy of its folder, never a new split of it. A cd/pushd target's `..` takes off the word before
# it, as bash and zsh do by default; where its physical spelling (`..` kept, as cd -P, set -P or zsh's
# CHASE_LINKS resolve it) differs, that folder is read from too, and both count toward the 64. -C and -p keep
# `..` as written, as subfleet resolves them. More than 4096 folders to examine (each folder for each
# subfleet run after it, once more for each -C and -p) is refused.
if printf '%s' "$command" | grep -qE 'subfleet[[:space:]]+run'; then
  ab_list=$(printf '%s' "$command" | LC_ALL=C awk -v cwd="$cwd" -v home="$HOME" '
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
    # norm(base "/" p, file) for a folder base already in the form norm gives, or norm(p, file) for an absolute p:
    # only the words of p are split, so a stored folder is copied once and never split again
    function join(base, p, file,   a, n, i, r) {
      p = expand(p)
      if (substr(p, 1, 1) == "/") base = "/"
      n = split(p, a, "/"); r = ""
      for (i = 1; i <= n; i++) if (a[i] != "" && a[i] != ".") r = r "/" a[i]
      if (r != "") base = (base == "/" ? "" : base) r
      if (file && base != "/" && ("/" p) ~ /\/\.?$/) base = base "/"
      return base
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
      nd = 1; dirs[1] = cwd; known[cwd] = 1; cur = cwd; prev = cwd; pcur = cwd; pprev = cwd; no = 0; wk = 0
      for (i = 1; i <= n; i++) {
        t = w[i]
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
        if ((t != "subfleet" && t !~ /\/subfleet$/) || w[i + 1] != "run") continue
        nc = 0; np = 0
        for (j = i + 2; j <= n && w[j] != ";"; j++) {
          o = w[j]
          if (o == "-C" || o == "-p") { v = w[++j]; if (v == ";") break }
          else if (o ~ /^-[Cp]=/) v = substr(o, 4)
          else if (o ~ /^-[Cp]./) v = substr(o, 3)
          else continue
          if (substr(o, 2, 1) == "C") cs[++nc] = v; else ps[++np] = v
        }
        for (k = 1; k <= nd && np; k++) {
          if ((wk += 1 + nc + np) > 4096) { print "TOOMANY"; exit }
          ax = !nc && index(dirs[k], "TheAxiomFoundation")
          for (c = 1; c <= nc; c++) if (index(join(dirs[k], cs[c]), "TheAxiomFoundation")) ax = 1
          if (!ax) continue
          for (q = 1; q <= np; q++) {
            f = join(dirs[k], ps[q], 1)
            if (f in seen) continue
            seen[f] = 1
            if (++no > 64) { print "TOOMANY"; exit }
            out[no] = f
          }
        }
      }
      for (k = 1; k <= no; k++) print out[k]
    }')
  ab_many="[axiom-brief-regime] This command names more than 16 briefs to read, or more than 64 paths to them (each -p from each folder the shell can be in), or more than 4096 folders to examine (each folder for each subfleet run after it, once more for each -C and -p); the guard cannot read them all in time. Dispatch one subfleet run per command."
  [ "$ab_list" != "TOOMANY" ] || block "$ab_many"
  # Each brief is keyed by its folder's physical path (the trailing x keeps a newline that ends it) and
  # its name; a brief that is not a file is not read, as before.
  ab_n=0
  while IFS= read -r ab_p; do
    [ -n "$ab_p" ] || continue
    ab_dir=${ab_p%/*}
    pf_abs=$(CDPATH= cd -P -- "${ab_dir:-/}" 2>/dev/null || exit 1; { pwd -P || printf '%s\n' "${ab_dir:-/}"; } 2>/dev/null; echo x) || continue
    pf_abs=${pf_abs%$'\n'x}
    pf_abs=${pf_abs%/}/${ab_p##*/}
    [ -f "$pf_abs" ] || continue
    ab_i=0
    while [ "$ab_i" -lt "$ab_n" ] && [ "${ab_files[ab_i]}" != "$pf_abs" ]; do ab_i=$((ab_i + 1)); done
    [ "$ab_i" -eq "$ab_n" ] || continue
    [ "$ab_n" -lt 16 ] || block "$ab_many"
    ab_files[ab_n]=$pf_abs; ab_n=$((ab_n + 1))
  done <<< "$ab_list"
  ab_i=0
  while [ "$ab_i" -lt "$ab_n" ]; do
    pf_abs=${ab_files[ab_i]}; ab_i=$((ab_i + 1))
    if grep -qiE 'rulespec|RuleSpec|encod' "$pf_abs" && ! grep -q 'ENCODING REGIME' "$pf_abs"; then
      block "[axiom-brief-regime] This brief touches RuleSpec/encoding but has no ENCODING REGIME block. Modules come only from 'axiom-encode encode --apply' with apply manifests; hand-authored YAML is never a module (Max 9/6). Prepend the block from ~/TheAxiomFoundation/ops/il-lane/COMMON-RULES.md (or TheAxiomFoundation/CLAUDE.md 'Encoding regime') verbatim, then dispatch."
    fi
  done
fi

