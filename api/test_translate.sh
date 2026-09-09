#!/usr/bin/env bash
# Smoke test: every supported document format, both directions, must come back translated
# into the target language. Checks live in check_translation.py — the important one is
# that the OUTPUT SCRIPT matches the requested language, which is what catches text that
# came back untranslated behind a successful-looking response.
#
#   ./test_translate.sh [base_url] [doc_dir]
#
# Fixtures are not committed. Generate them with:  python3 make_test_docs.py <doc_dir>
# A file is translated to English if its name contains "hindi", otherwise to Hindi.
set -uo pipefail

BASE="${1:-http://localhost:8000}"
DOC_DIR="${2:-./samples/documents}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

printf '── health ──────────────────────────────────────────\n'
curl -sf "$BASE/health" -o /dev/null || { echo "API not reachable at $BASE"; exit 1; }
curl -s "$BASE/health" | python3 -c "
import json,sys
t=json.load(sys.stdin)['pipeline']['translation']
print(f\"translation: {'/'.join(t['languages'])} · {' '.join(t['formats'])} · OCR={t['ocr']['enabled']} ({t['ocr']['languages']}) · reachable={t['reachable']}\")
"

printf '\n── document → translation ──────────────────────────\n'
fail=0
shopt -s nullglob
for f in "$DOC_DIR"/*; do
  [ -f "$f" ] || continue
  case "$(basename "$f")" in
    *hindi*) target=English ;;
    *)       target=Hindi   ;;
  esac
  printf '%-26s → %-8s ' "$(basename "$f")" "$target"
  curl -s -m 3600 -F "file=@$f" -F "target_lang=$target" "$BASE/translate-document" \
    | python3 "$HERE/check_translation.py" "$target" || fail=1
done

printf '────────────────────────────────────────────────────\n'
[ "$fail" -eq 0 ] && echo "PASS — every document translated into the requested language" \
                  || echo "FAIL — see above"
exit "$fail"
