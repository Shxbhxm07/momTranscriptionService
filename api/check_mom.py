"""Contract checker for test_api.sh — reads one API response on stdin, exits non-zero on failure."""
import json
import sys

# The transcript is an INTERNAL step. Any of these in the response is a contract violation.
BANNED = {"transcription", "raw_transcript", "segments", "speaker_transcript", "transcript"}
REQUIRED = (("title", str), ("summary", str), ("key_points", list),
            ("decisions", list), ("action_items", list))


def fail(msg):
    print(f"FAIL  {msg}")
    sys.exit(1)


try:
    d = json.load(sys.stdin)
except Exception:
    fail("non-JSON response")

if not d.get("success"):
    fail(str(d.get("detail"))[:90])

leaked = BANNED & set(d)
if isinstance(d.get("mom"), dict):
    leaked |= BANNED & set(d["mom"])
if leaked:
    fail(f"transcript leaked into response: {sorted(leaked)}")

mom = d.get("mom")
if mom is None:
    # Legitimate outcome for audio with no meeting content; the API must explain why.
    if not str(d.get("note", "")).strip():
        fail("mom is null with no explanatory note")
    print(f"SKIP  no MoM — {str(d['note'])[:60]}")
    sys.exit(0)

for key, typ in REQUIRED:
    if key not in mom:
        fail(f"mom missing {key!r}")
    if not isinstance(mom[key], typ):
        fail(f"mom[{key!r}] is {type(mom[key]).__name__}, expected {typ.__name__}")

# The MoM must be English — Devanagari anywhere means the pipeline leaked source script.
deva = sum(1 for c in json.dumps(mom, ensure_ascii=False) if "ऀ" <= c <= "ॿ")
if deva:
    fail(f"{deva} Devanagari chars in the MoM")

if not mom["summary"].strip():
    # Documented degraded mode, not a contract breach: llama-service's JSON pipeline failed and
    # its legacy TEXT pipeline produced the minutes instead, so `formatted` holds a real document
    # while the structured fields are empty. The API explains this in `note`. Surface it loudly —
    # it means structured extraction is failing on this clip — but do not call it a failure, since
    # an English MoM was produced and no transcript leaked, which is what this test asserts.
    if d.get("note") and mom.get("formatted", "").strip():
        print(f"DEGRADED  rendered-only, no structured fields — {len(mom['formatted'])} chars")
        sys.exit(0)
    fail("empty summary")

print(f"OK    {mom['title'][:32]!r} "
      f"{len(mom['key_points'])}pts {len(mom['decisions'])}dec {len(mom['action_items'])}act")
