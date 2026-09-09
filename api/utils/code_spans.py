"""Hide code from the translator, then put it back.

A translation model cannot tell a shell command from a sentence. Asked to translate
`docker logs llama-summarizer | tail -3` into Hindi it does exactly what it was told and
returns `डॉकर लॉग्स लामा-समराइज़र | पूंछ -3` — where `पूंछ` is an animal's tail. The output
reads as Hindi and is unrunnable. MEASURED on a real technical document (HVT-I1911.docx,
83 blocks): file paths were renamed, two shell commands were destroyed, four CONSTANT_CASE
identifiers were translated, and ASCII digits came back as Devanagari (०.८६ for 0.86).

The fix is not a better prompt — it is to never show the model the code at all:

    curl -s localhost:8001/health   →   ⟦0⟧          (masked)
                                    →   ⟦0⟧          (model returns it untouched)
                                    →   curl -s localhost:8001/health   (restored)

A masked span is inert: it holds no letters, so the model has nothing to translate and
llama-service's script validator ignores it (its _content_chars drops digit-bearing
tokens, so placeholders never count against a chunk's Devanagari ratio).

WHAT THIS DOES NOT FIX, stated plainly: a term-as-term written in ordinary letters. The
same document has a table of "we said X / it heard Y" — `Diarization | "Divide"` — whose
two sides are plain English words. Both were translated to the same Hindi word and the row
became meaningless. Single-token quoted strings are protected below, which catches
`"Divide"` and `"Quadrant"`, but an unquoted example word is indistinguishable from prose
and is left to the model.
"""
import re
from typing import List, Tuple

# ⟦ ⟧ (U+27E6/27E7) are not on any keyboard, carry no meaning in either language, and are
# absent from ordinary prose — so the model has no reason to translate, quote or reflow
# them. A commoner delimiter like [0] risks colliding with the document's own text.
_OPEN, _CLOSE = "⟦", "⟧"

# Tolerant on the way back: the model sometimes pads the brackets, and it has been
# observed converting ASCII digits to Devanagari (०) mid-sentence — which would otherwise
# orphan every placeholder in that chunk.
_RESTORE_RE = re.compile(r"[⟦〖]\s*([0-9०-९]+)\s*[⟧〗]")

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Shell commands, matched at the start of a line and stopped before a trailing ` #`
# comment — the command must survive verbatim, but its comment is prose and translating it
# is the useful thing to do.
_COMMANDS = (
    "curl|wget|docker|docker-compose|kubectl|python3?|pip3?|git|npm|npx|yarn|make|"
    "bash|sh|zsh|cd|ls|cat|grep|egrep|awk|sed|jq|tail|head|sort|uniq|wc|find|chmod|"
    "sudo|apt|apt-get|systemctl|uvicorn|pytest|export|source|echo|mkdir|rm|cp|mv|diff|ssh|scp"
)

# Order matters: the widest, most specific patterns run FIRST so a broad match (a whole
# command line) is taken before a narrow one (an identifier inside it) can fragment it.
_PATTERNS = [
    # ``code`` and `code` — an explicit author instruction that this is not prose.
    re.compile(r"`{1,3}[^`\n]+`{1,3}"),
    # A whole command line, up to any trailing comment.
    re.compile(rf"^[ \t]*(?:{_COMMANDS})\b[^\n#]*[^\s#]", re.MULTILINE),
    re.compile(r"https?://\S+"),
    # host:port and host:port/path — localhost:8001/health
    re.compile(r"\b[\w.-]+:\d{2,5}(?:/[^\s,;)]*)?"),
    # File paths. An extension is REQUIRED (or an explicit ./ or / prefix) so that ordinary
    # prose containing a slash — "and/or", "he/she" — is never mistaken for a path.
    #
    # THE EXTENSION RULE MUST RUN FIRST. With the leading-slash rule ahead of it,
    # "llama-service/core/term_corrector.py" matched only from its first slash, masking
    # "/core/term_corrector.py" and leaving "llama-service" behind to be translated — the
    # path was protected in halves, which protects nothing.
    re.compile(
        r"\b[\w.-]+(?:/[\w.-]+)*\."
        r"(?:py|js|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|conf|txt|md|sh|sql|html?|css|"
        r"go|rs|java|cpp?|hpp?|rb|php|env|lock|log)\b"
    ),
    # (?<![\w]) is what makes the comment above TRUE. Without it a bare "/" matches anywhere,
    # including inside a word: "friends/family" masked "/family", the model did not reproduce the
    # placeholder, and the validator threw away the whole translated line as lost_code_span.
    re.compile(r"(?:\.{1,2}/|(?<![\w])/)[\w.-]+(?:/[\w.-]+)*"),
    # key=Value flags and kwargs — correct=False, --gpu-memory-utilization=0.60
    re.compile(r"\B--[\w-]+(?:=[^\s,;)]+)?"),
    re.compile(r"\b[A-Za-z_]\w*=[^\s,;)]+"),
    # dotted attribute chains and calls — orchestrator._confirmed_names(), core.speaker_db.THRESHOLD
    re.compile(r"\b\w+(?:\.\w+)+\(\s*\)"),
    re.compile(r"\b\w+\(\s*\)"),
    re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,}\b"),
    # CONSTANT_CASE and snake_case — identifiers, never words.
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),
    re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"),
    # An internal capital marks a product or type name: DocuTalk, TitaNet, vLLM, MinIO.
    # These are exactly the words the document is about and must survive verbatim.
    # ...but [a-z0-9]* matches EMPTY, so this also matched every ALL-CAPS word: AGENDA,
    # ATTENDEES and DECISIONS were protected as product names and came back untranslated in the
    # middle of a Hindi document. Short all-caps tokens are still protected — API, HTTP, JSON,
    # PDF are acronyms a translation should keep — but anything longer than four letters is a
    # word, and words are what a translator is for.
    re.compile(r"\b(?![A-Z0-9]{5,}\b)[A-Za-z][a-z0-9]*[A-Z][A-Za-z0-9]*\b"),
    # A quoted SINGLE token is a term being named rather than used — "Quadrant", "Vsperge".
    # Multi-word quotes are left alone: those are ordinary quoted prose.
    re.compile(r"[\"“]([A-Za-z][\w.-]*)[\"”]"),
    # Emoji survive as-is; the model has been seen dropping them mid-line.
    re.compile(r"[\U0001F000-\U0001FAFF☀-➿️←-⇿⬀-⯿]"),
]


def mask(text: str) -> Tuple[str, List[str]]:
    """Replace every code-like span with ⟦n⟧. Returns (masked_text, originals)."""
    spans: List[str] = []

    def take(match: "re.Match") -> str:
        value = match.group(0)
        # A span that is only whitespace would consume a placeholder for nothing.
        if not value.strip():
            return value
        spans.append(value)
        return f"{_OPEN}{len(spans) - 1}{_CLOSE}"

    masked = text
    for pattern in _PATTERNS:
        masked = pattern.sub(take, masked)
    return masked, spans


def unmask(text: str, spans: List[str]) -> Tuple[str, int]:
    """Put the originals back. Returns (restored_text, number_of_spans_not_found).

    The miss count is the caller's correctness signal: a placeholder the model deleted or
    mangled beyond recognition means a command or path is GONE from that chunk, and the
    honest response is to fall back to the untranslated source rather than to publish a
    paragraph with a hole in it. See DocumentTranslator._store.
    """
    if not spans:
        return text, 0

    seen = set()

    def put(match: "re.Match") -> str:
        index = int(match.group(1).translate(_DEVANAGARI_DIGITS))
        if 0 <= index < len(spans):
            seen.add(index)
            return spans[index]
        return match.group(0)

    restored = _RESTORE_RE.sub(put, text)
    return restored, len(spans) - len(seen)


def has_translatable_text(masked: str) -> bool:
    """Is there anything left for the model to do?

    A chunk that is nothing but a command or a file path masks down to placeholders and
    punctuation. Sending it costs a share of a ~43 chars/s budget to be told what we
    already know, so the caller returns it unchanged instead — which also makes such lines
    exactly, rather than probably, verbatim.
    """
    return any(ch.isalpha() for ch in _RESTORE_RE.sub(" ", masked))


def normalize_digits(text: str) -> str:
    """Devanagari digits → ASCII. Applied to model output before placeholders are restored.

    gpt-oss renders numbers in Devanagari intermittently — measured in one document as
    `०.८६` for a 0.86 threshold and section headings running `1. 2. 3. ४. ५. 6.`. Modern
    Hindi writing uses ASCII digits, so this is a consistency fix rather than a
    translation choice, and doing it BEFORE unmask also repairs any placeholder index the
    model converted on its way through.
    """
    return text.translate(_DEVANAGARI_DIGITS)
