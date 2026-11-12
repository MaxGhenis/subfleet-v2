# --- axiom-brief-regime: every lane brief that can touch RuleSpec carries the encoding regime ---
# Max (2026-09-06): "it must always use the encoder agent … if this isn't basically impossible
# to mess up again we need to fix it." A brief dispatched into the Axiom mirror that mentions
# RuleSpec/rulespec/encoding must carry the ENCODING REGIME block verbatim (ops/il-lane/COMMON-RULES.md
# / TheAxiomFoundation/CLAUDE.md). Lanes do not read CLAUDE.md; the brief is their law.
if printf '%s' "$command" | grep -qE 'subfleet[[:space:]]+run'; then
  seg=$(printf '%s' "$command" | sed -E 's/.*subfleet[[:space:]]+run//')
  wd=$(printf '%s' "$seg" | grep -oE '(^|[[:space:]])-C[[:space:]]+[^[:space:]]+' | head -1 | awk '{print $2}')
  [ -z "$wd" ] && wd="$cwd"
  wd_abs=$(printf '%s' "$wd" | sed "s|^~|$HOME|")
  if printf '%s' "$wd_abs" | grep -q 'TheAxiomFoundation'; then
    pf=$(printf '%s' "$seg" | grep -oE '(^|[[:space:]])-p[[:space:]]+[^[:space:]]+' | head -1 | awk '{print $2}')
    if [ -n "$pf" ]; then
      pf_abs=$(printf '%s' "$pf" | sed "s|^~|$HOME|"); case "$pf_abs" in /*) ;; *) pf_abs="$cwd/$pf_abs" ;; esac
      if [ -f "$pf_abs" ] && grep -qiE 'rulespec|RuleSpec|encod' "$pf_abs" && ! grep -q 'ENCODING REGIME' "$pf_abs"; then
        block "[axiom-brief-regime] This brief touches RuleSpec/encoding but has no ENCODING REGIME block. Modules come only from 'axiom-encode encode --apply' with apply manifests; hand-authored YAML is never a module (Max 9/6). Prepend the block from ~/TheAxiomFoundation/ops/il-lane/COMMON-RULES.md (or TheAxiomFoundation/CLAUDE.md 'Encoding regime') verbatim, then dispatch."
      fi
    fi
  fi
fi

