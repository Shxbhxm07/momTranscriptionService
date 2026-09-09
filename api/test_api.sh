#!/usr/bin/env bash
# Smoke test: Hindi / English / Hinglish audio must all produce an English MoM,
# and the response must NOT carry the transcript. Checks live in check_mom.py.
#
#   ./test_api.sh [base_url] [audio_dir]
set -uo pipefail

BASE="${1:-http://localhost:8000}"
AUDIO_DIR="${2:-./samples}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

printf '── health ──────────────────────────────────────────\n'
curl -sf "$BASE/health" || { echo "API not reachable at $BASE"; exit 1; }
printf '\n\n── audio → English MoM ─────────────────────────────\n'

fail=0
shopt -s nullglob
for f in "$AUDIO_DIR"/*; do
  [ -f "$f" ] || continue
  printf '%-34s ' "$(basename "$f")"
  curl -s -m 1800 -F "audio=@$f" "$BASE/transcribe-and-generate-mom" \
    | python3 "$HERE/check_mom.py" || fail=1
done

printf '────────────────────────────────────────────────────\n'
[ "$fail" -eq 0 ] && echo "PASS — every clip produced an English MoM, no transcript leaked" \
                  || echo "FAIL — see above"
exit "$fail"
