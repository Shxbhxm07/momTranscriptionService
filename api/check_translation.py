"""Contract checker for test_translate.sh — reads one API response on stdin, exits non-zero on failure.

    python3 check_translation.py <expected_target_lang>

THE ASSERTION THAT MATTERS is the script check. Everything else here is shape validation
that a typo would catch anyway; the script check is the one that catches the failure this
endpoint is actually prone to. llama-service returns the SOURCE text when the model
refuses or comes back empty, so a response that is well-formed, `success: true`, and
entirely untranslated is a real and reachable outcome. Measuring the script of the output
is what tells the two apart from outside the service.
"""
import json
import sys

REQUIRED = (("source_lang", str), ("target_lang", str), ("translated_text", str),
            ("document", dict), ("stats", dict))


def fail(msg):
    print(f"FAIL  {msg}")
    sys.exit(1)


def script_ratio(text):
    """(Devanagari, Latin) letter counts. Digits, punctuation and whitespace are ignored —
    they are script-neutral and a table of figures would otherwise read as 'no language'."""
    deva = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return deva, latin


expected_target = sys.argv[1] if len(sys.argv) > 1 else None

try:
    d = json.load(sys.stdin)
except Exception:
    fail("non-JSON response")

if not d.get("success"):
    fail(str(d.get("detail"))[:110])

for key, typ in REQUIRED:
    if key not in d:
        fail(f"missing {key!r}")
    if not isinstance(d[key], typ):
        fail(f"{key!r} is {type(d[key]).__name__}, expected {typ.__name__}")

text = d["translated_text"]
if not text.strip():
    fail("translated_text is empty")

target = d["target_lang"]
if expected_target and target != expected_target:
    fail(f"target_lang is {target!r}, expected {expected_target!r}")

deva, latin = script_ratio(text)
letters = deva + latin
if not letters:
    fail("translated_text contains no letters in either script")

# Thresholds are deliberately loose in one direction only. Hindi output legitimately keeps
# English acronyms, proper nouns and units ("API", "Joint Secretary", "GPU"), so a Hindi
# translation is not expected to be pure Devanagari — a clear majority is enough. English
# output has no such excuse: correct English contains NO Devanagari at all, so any
# meaningful amount of it means part of the document came back untranslated.
if target == "Hindi":
    if deva / letters < 0.55:
        fail(f"target was Hindi but only {deva / letters:.0%} of letters are Devanagari "
             f"— text likely came back untranslated")
elif target == "English":
    if deva / letters > 0.05:
        fail(f"target was English but {deva / letters:.0%} of letters are Devanagari "
             f"— text likely came back untranslated")

stats = d["stats"]
untranslated = stats.get("untranslated", 0)
if untranslated and not str(d.get("note", "")).strip():
    fail(f"{untranslated} chunk(s) untranslated but no explanatory note")

doc = d["document"]
detail = (f"{doc.get('format')} · {doc.get('blocks')} blocks · {doc.get('characters')} chars "
          f"· {stats.get('model_calls')} call(s)")
if doc.get("ocr_pages"):
    detail += f" · {doc['ocr_pages']} OCR page(s)"
if stats.get("repeated_chunks_reused"):
    detail += f" · {stats['repeated_chunks_reused']} repeat(s) reused"

if untranslated:
    print(f"DEGRADED  {d['source_lang']}→{target}  {untranslated}/{stats.get('chunks')} untranslated "
          f"({stats.get('failure_reasons')})  {detail}")
    sys.exit(0)

print(f"OK    {d['source_lang']}→{target}  {detail}")
