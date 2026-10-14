#!/bin/zsh -f
# Controlled inspection using shell builtins, without Python startup per call.
# Real kill(pid, 0) in the runner still checks process existence.
zmodload zsh/datetime
if (( ${+FAKE_PS_ETIME} )); then
  print -r -- "$FAKE_PS_ETIME"
  exit 0
fi
case "${FAKE_PS:-}" in
  hang) exec /bin/sleep 30 ;;
  nonzero) print -r -- '00:00'; exit 1 ;;
  empty) exit 1 ;;
  malformed) print -r -- 'not an elapsed time'; exit 0 ;;
esac
integer age
(( age = EPOCHREALTIME - FAKE_PROCESS_STARTED ))
(( age < 0 )) && age=0
printf '%d-%02d:%02d:%02d\n' $(( age / 86400 )) $(( age / 3600 % 24 )) $(( age / 60 % 60 )) $(( age % 60 ))
