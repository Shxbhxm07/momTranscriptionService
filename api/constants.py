"""Hallucination patterns for the offline English transcription API.

Carried over from the MoM speech-service (speech-service/constants.py). Whisper's
failure modes are a property of the MODEL, not of the product built on top of it, so
these patterns stay relevant even though every MoM feature is gone.

WHAT WAS DROPPED vs the original:
  • HINGLISH_PROMPT and the four "initial-prompt echo" patterns that existed only to
    scrub that prompt back out of the transcript. This service never sends an
    initial_prompt — the prompt existed to PRESERVE Hindi/Devanagari code-switching,
    which is the exact opposite of what a Hindi→English translator wants — so both the
    prompt and its echo-scrubbers are dead weight here.

WHY THE DEVANAGARI PATTERNS STAY: whisper's translate task is not a guarantee. On
silence or unintelligible audio it can still fall back to emitting source-script junk
(the "सब्सक्राइब" YouTube loop is the classic one), so the Hindi patterns are still
load-bearing on a Hindi/Hinglish input path.
"""

HALLUCINATION_PATTERNS = [
    # Hindi YouTube/filler hallucinations — still reachable: translate=true is a decoding
    # task, not a hard constraint, so Hindi junk can still surface on unclear audio.
    r'(सब्सक्राइब\s*){2,}',
    r'सब्सक्राइब',
    r'(\bहमेशा करने के लिए\s*){2,}',
    r'(\bउपयोग करने के लिए\s*){2,}',
    r'(\bकर सकते हैं\s*){3,}',
    r'(\bधन्यवाद\s*){3,}',
    r'(\bनमस्ते\s*){3,}',
    # English YouTube hallucinations
    r'(subscribe\s*){2,}',
    r'(thanks for watching\s*){2,}',
    r'(thank you for watching\s*){2,}',
    r'(please subscribe\s*){2,}',
    r'(like and subscribe\s*){2,}',
    r'(click the bell\s*){2,}',
    r'(see you in the next\s*){2,}',
    r'(bye bye\s*){3,}',
    # Common Whisper meeting hallucinations
    r'(thank you\.\s*){3,}',
    r'(thank you\s*){4,}',
    r'(you\.\s*){4,}',
    r'(\bi see\b\s*){3,}',
    r'(\buh+\s*){4,}',
    r'(\bum+\s*){4,}',
    r'(\bhmm+\s*){3,}',
    r'^(\.?\s*thank you\.?\s*)+$',
    r'^(\.?\s*okay\.?\s*)+$',
    r'^(\.?\s*yes\.?\s*)+$',
    r'(Transcribed by\s+https?://\S+)',
    r'(Subtitles by\s+\S+)',
    r'\[silence\]',
    r'\[inaudible\]',
    r'\[crosstalk\]',
    r'\[background noise\]',
    # Hyphenated word-repetition loop: Whisper renders confusing audio as "एक-एक-एक-एक..."
    # The repetition is internal to one space-split token, so word-loop filters miss it.
    r'(\w{1,8}-){5,}\w{0,8}',
    # Punctuation noise
    r'(\.{3,})',
    r'(\s*\.\s*){5,}',
    r'^[\s\.\,\-]+$',
    # Music / sound effects
    r'\[.*?music.*?\]',
    r'\[.*?applause.*?\]',
    r'\[.*?laughter.*?\]',
    r'♪+',
]
