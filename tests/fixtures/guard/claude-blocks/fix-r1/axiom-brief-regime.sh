# --- axiom-brief-regime: every lane brief that can touch RuleSpec carries the encoding regime ---
# Max (2026-09-06): "it must always use the encoder agent … if this isn't basically impossible
# to mess up again we need to fix it." A brief dispatched into the Axiom mirror that mentions
# RuleSpec/rulespec/encoding must carry the ENCODING REGIME block verbatim (ops/il-lane/COMMON-RULES.md
# / TheAxiomFoundation/CLAUDE.md). Lanes do not read CLAUDE.md; the brief is their law.
# Guard sync fix r1 (2026-11-15): every `subfleet run` in the command is judged, from each folder the shell
# can be in there (the payload cwd and every cd/pushd target before it). Every -C and -p counts, attached
# (-Cdir, -pfile, -p=file) or not, and a relative one resolves against that folder, as subfleet resolves
# it; more than 16 briefs to read is refused.
if printf '%s' "$command" | grep -qE 'subfleet[[:space:]]+run'; then
  ab_list=$(printf '%s' "$command" | LC_ALL=C awk -v cwd="$cwd" -v home="$HOME" '
    function expand(p) {
      if (p == "~" || p == "$HOME") return home
      if (substr(p, 1, 2) == "~/") return home substr(p, 2)
      if (substr(p, 1, 6) == "$HOME/") return home substr(p, 6)
      return p
    }
    function join(base, p) { p = expand(p); return substr(p, 1, 1) == "/" ? p : base "/" p }
    { text = text $0 "\n" }
    END {
      if (cwd == "") cwd = "."
      gsub(/\\\n/, "", text); gsub(/\$\{HOME\}/, "$HOME", text); gsub(/["'\''\\]/, "", text)
      gsub(/[;&|()`{}<>\n]/, " ; ", text)
      n = split(text, w, /[ \t]+/)
      nd = 1; dirs[1] = cwd; known[cwd] = 1; cur = cwd; prev = cwd; no = 0
      for (i = 1; i <= n; i++) {
        t = w[i]
        if (t == "cd" || t == "pushd" || t == "popd") {
          for (k = i + 1; k <= n && (w[k] ~ /^-[LPe@]+$/ || w[k] == "--"); k++) ;
          v = (t == "popd") ? "-" : w[k]
          if (v == "-") { v = cur; cur = prev; prev = v; continue }
          prev = cur; cur = (k > n || v == ";" || v == "") ? home : join(cur, v)
          if (!(cur in known)) { known[cur] = 1; dirs[++nd] = cur }
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
          ax = !nc && index(dirs[k], "TheAxiomFoundation")
          for (c = 1; c <= nc; c++) if (index(join(dirs[k], cs[c]), "TheAxiomFoundation")) ax = 1
          if (!ax) continue
          for (q = 1; q <= np; q++) {
            f = join(dirs[k], ps[q])
            if (f in seen) continue
            seen[f] = 1
            if (++no > 16) { print "TOOMANY"; exit }
            out[no] = f
          }
        }
      }
      for (k = 1; k <= no; k++) print out[k]
    }')
  if [ "$ab_list" = "TOOMANY" ]; then
    block "[axiom-brief-regime] This command names more than 16 briefs to read (each -p from each folder the shell can be in); the guard cannot read them all in time. Dispatch one subfleet run per command."
  fi
  while IFS= read -r pf_abs; do
    if [ -n "$pf_abs" ] && [ -f "$pf_abs" ] && grep -qiE 'rulespec|RuleSpec|encod' "$pf_abs" && ! grep -q 'ENCODING REGIME' "$pf_abs"; then
      block "[axiom-brief-regime] This brief touches RuleSpec/encoding but has no ENCODING REGIME block. Modules come only from 'axiom-encode encode --apply' with apply manifests; hand-authored YAML is never a module (Max 9/6). Prepend the block from ~/TheAxiomFoundation/ops/il-lane/COMMON-RULES.md (or TheAxiomFoundation/CLAUDE.md 'Encoding regime') verbatim, then dispatch."
    fi
  done <<< "$ab_list"
fi

