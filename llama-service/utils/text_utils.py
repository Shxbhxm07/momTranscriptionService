import re
import difflib
import logging
from config import CHUNK_SIZE_CHARS

logger = logging.getLogger(__name__)

# =============================================================================
# PREPROCESSING
# =============================================================================

def _strip_timestamps(text: str) -> str:
    """Remove inline timestamp artifacts produced by transcription tools.

    Handles formats like:
      0:077 seconds          → ''
      1:141 minute, 14 seconds → ''
      10:0110 minutes, 1 second → ''
      2:002 minutes          → ''
    """
    pattern = (
        r'\d{1,2}:\d{2}'
        r'\d*'
        r'\s*(?:'
            r'\d+\s*minutes?,?\s*\d*\s*seconds?'
            r'|\d+\s*minutes?'
            r'|\d+\s*seconds?'
        r')'
    )
    return re.sub(pattern, '', text)


def _remove_fillers(text: str) -> str:
    """Remove common transcription filler artifacts."""
    # Repeated words: "where where where" → "where"
    text = re.sub(r'\b(\w+)(\s+\1){2,}', r'\1', text, flags=re.IGNORECASE)
    # Doubled words: "I I", "the the". A handful of English words legitimately double, and
    # collapsing those changes meaning rather than removing a stutter — measured against this
    # function: "he had had enough" -> "he had enough" (tense lost), "the fact that that
    # happened" -> "the fact that happened" (clause broken). Everything else still collapses,
    # because in speech a doubled word is almost always a stutter.
    text = re.sub(
        r'\b(?!(?:had|that)\b)(\w+)\s+\1\b', r'\1', text, flags=re.IGNORECASE
    )
    # Filler sounds
    text = re.sub(r'\b(uh|um|uh uh)\b', '', text, flags=re.IGNORECASE)
    # Clean up extra spaces
    text = re.sub(r'  +', ' ', text)
    return text


def _deduplicate_sentences(text: str) -> str:
    """Remove repeated consecutive sentences within each speaker turn.

    Catches Qwen3-ASR hallucination loops where the same sentence is
    emitted 2-8 times in a row after a quiet or garbled audio segment.
    Works on both Devanagari (Hindi) and Latin (English/Hinglish) text.
    """
    def _norm(s: str) -> str:
        return re.sub(r'[^\w\u0900-\u097f]', '', s, flags=re.UNICODE).lower()

    lines = text.split('\n')
    result = []

    for line in lines:
        # Preserve speaker label prefix ([speaker_1], Name:, etc.)
        m = re.match(r'^(\[?[^\]\n]{1,40}[\]:]?\s+)', line)
        if m:
            label = m.group(1)
            content = line[len(label):]
        else:
            label = ''
            content = line

        # Split on sentence-ending punctuation (English + Hindi दण्ड)
        sentences = re.split(r'(?<=[.!?।])\s+', content.strip())
        sentences = [s.strip() for s in sentences if s.strip()]

        if not sentences:
            result.append(line)
            continue

        # Remove consecutive identical sentences (normalised comparison)
        deduped = []
        for s in sentences:
            if deduped and _norm(s) == _norm(deduped[-1]):
                continue
            deduped.append(s)

        result.append(label + ' '.join(deduped))

    return '\n'.join(result)


def _deduplicate_speaker_names(text: str) -> str:
    """Fuzzy-match speaker names and normalise variants to one canonical name.

    Extracts all 'Name:' style speaker labels, groups names that are
    sufficiently similar (difflib ratio >= 0.75), then replaces every
    variant in the text with the longest/most-complete form.
    """
    raw_names = list(dict.fromkeys(
        m.group(1).strip()
        for m in re.finditer(r'^([A-Za-z][A-Za-z\s\-\.]{1,30}):', text, re.MULTILINE)
        if len(m.group(1).strip()) > 1
    ))

    if not raw_names:
        return text

    visited: set = set()
    clusters: list = []

    for name in raw_names:
        if name in visited:
            continue
        cluster = [name]
        visited.add(name)
        for other in raw_names:
            if other in visited:
                continue
            ratio = difflib.SequenceMatcher(None, name.lower(), other.lower()).ratio()
            if ratio >= 0.75:
                cluster.append(other)
                visited.add(other)
        clusters.append(cluster)

    for cluster in clusters:
        if len(cluster) == 1:
            continue
        canonical = max(cluster, key=len)
        for variant in cluster:
            if variant != canonical:
                text = re.sub(
                    rf'\b{re.escape(variant)}\b',
                    canonical,
                    text
                )
        logger.info(f"[PREPROCESS] Merged names {cluster} → '{canonical}'")

    return text


def preprocess_transcript(transcript: str) -> str:
    """Full pre-processing pipeline applied before sending to the LLM."""
    transcript = _strip_timestamps(transcript)
    transcript = _remove_fillers(transcript)
    transcript = _deduplicate_sentences(transcript)
    transcript = _deduplicate_speaker_names(transcript)
    # Collapse blank lines created by removals
    transcript = re.sub(r'\n{3,}', '\n\n', transcript)
    transcript = transcript.strip()
    logger.info(f"[PREPROCESS] Done — {len(transcript)} chars after cleaning")
    return transcript


# =============================================================================
# CHUNKING
# =============================================================================

def chunk_transcript(transcript: str) -> list:
    """Chunk transcript at natural boundaries (line breaks or sentences)"""

    if len(transcript) > 0 and transcript.count('\n') < len(transcript) / 1000:
        logger.info("[CHUNK] Splitting by sentences due to lack of line breaks")
        lines = re.split(r'(?<=[.!?])\s+', transcript)
        join_char = ' '
    else:
        lines = transcript.split('\n')
        join_char = '\n'

    chunks = []
    current_chunk = []
    current_length = 0

    for line in lines:
        line_length = len(line)

        if current_length + line_length > CHUNK_SIZE_CHARS and current_chunk:
            chunks.append(join_char.join(current_chunk))
            current_chunk = [line]
            current_length = line_length
        else:
            current_chunk.append(line)
            current_length += line_length + 1

    if current_chunk:
        chunks.append(join_char.join(current_chunk))

    logger.info(f"[CHUNK] Created {len(chunks)} chunks")
    return chunks


# =============================================================================
# OUTPUT CLEANING
# =============================================================================

def clean_mom_output(text: str) -> str:
    """Clean MoM output — strip LLM preamble artifacts and normalise formatting."""
    preambles = [
        r'^here\s+is\s+the\s+(synthesized\s+)?minutes',
        r'^here\s+is\s+the\s+mom',
        r'^minutes\s+of\s+meeting[:\s]*\n',
        r'^i\s+have\s+(generated|created)',
        r'^based\s+on\s+(the\s+)?',
    ]

    for pattern in preambles:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)

    # Normalise bullets
    text = re.sub(r'^\s*\+\s+', '  • ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*-\s+([^S])', r'  • \1', text, flags=re.MULTILINE)

    # Remove empty bullet lines (LLM sometimes generates "• \n" with no content)
    text = re.sub(r'\n[ \t]*•[ \t]*\n', '\n', text)
    text = re.sub(r'\n[ \t]*•[ \t]*$', '', text, flags=re.MULTILINE)

    # Collapse excessive blank lines
    text = re.sub(r'\n{4,}', '\n\n\n', text)

    return text.strip()
