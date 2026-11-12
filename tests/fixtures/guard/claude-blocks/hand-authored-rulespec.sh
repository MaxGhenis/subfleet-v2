# --- hand-authored-rulespec: rule YAML staged in a rulespec-* checkout needs its encoder apply manifest ---
# Companion tests (*.test.yaml) and composed pipelines (…/composed/…) are exempt; everything else under
# <cc>/(statutes|regulations|policies|legislation|programs)/ must have .axiom/encoding-manifests/<mirror>.json
# staged or tracked. The CI guard is the second wall; this is the first.
if printf '%s' "$command" | grep -qE 'git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit'; then
  gdir=$(printf '%s' "$command" | grep -oE 'git[[:space:]]+-C[[:space:]]+[^[:space:]]+' | head -1 | awk '{print $3}')
  [ -z "$gdir" ] && gdir="$cwd"
  gdir=$(printf '%s' "$gdir" | sed "s|^~|$HOME|")
  origin_url=$(git -C "$gdir" remote get-url origin 2>/dev/null)
  if printf '%s' "$origin_url" | grep -qiE 'TheAxiomFoundation/rulespec-'; then
    missing=""
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      case "$f" in
        *.test.yaml|*/composed/*|.axiom/*) continue ;;
      esac
      if printf '%s' "$f" | grep -qE '^[a-z]{2}(-[a-z0-9-]+)?/(statutes|regulations|policies|legislation|programs)/.+\.ya?ml$'; then
        m=".axiom/encoding-manifests/${f%.*}.json"
        if ! git -C "$gdir" cat-file -e ":$m" 2>/dev/null && ! git -C "$gdir" cat-file -e "HEAD:$m" 2>/dev/null; then
          missing="$missing $f"
        fi
      fi
    done < <(git -C "$gdir" diff --cached --name-only --diff-filter=ACMR 2>/dev/null)
    if [ -n "$missing" ]; then
      block "[hand-authored-rulespec] Staged RuleSpec content with no encoder apply manifest:$missing. RuleSpec modules come only from 'axiom-encode encode <corpus citation> --backend codex --apply', which writes .axiom/encoding-manifests/<mirror path>.json; hand-authored YAML is never a module (Max 9/6). Repairs go through --review-findings / --repair-candidate-* replay. Unstage the files and run the encoder."
    fi
  fi
fi

