import re
import logging
from typing import List
from constants import HALLUCINATION_PATTERNS

logger = logging.getLogger(__name__)

def remove_hallucinations(text: str) -> str:
    """Remove hallucinated patterns from text"""
    if not text:
        return text
    cleaned = text
    for pattern in HALLUCINATION_PATTERNS:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def _has_char_loop_word(word: str, min_len: int = 8, max_unique_ratio: float = 0.35) -> bool:
    """Detect a single word that is a character-level repetition loop.

    e.g. 'गूगूगूगूगूगूगू' or 'करेंगूओओगूगूगूगूगूगू' — very few unique chars
    relative to total length.
    """
    if len(word) < min_len:
        return False
    return len(set(word)) / len(word) <= max_unique_ratio


def _words_have_char_loops(words: list, loop_word_ratio: float = 0.5) -> bool:
    """Return True if more than loop_word_ratio of (long) words are char-loop words."""
    long_words = [w for w in words if len(w) >= 8]
    if not long_words:
        return False
    loop_count = sum(1 for w in long_words if _has_char_loop_word(w))
    return loop_count / len(long_words) >= loop_word_ratio


def filter_hallucinated_segments(segments: List[dict]) -> List[dict]:
    """Filter out hallucinated segments, salvaging good content before trailing loops."""
    if not segments:
        return segments

    filtered = []
    for seg in segments:
        text = seg.get('text', '').strip()
        if not text:
            continue

        # Filter short hallucinations
        words = text.split()
        if len(words) <= 2:
            hallucination_words = {'सब्सक्राइब', 'subscribe'}
            if any(w.lower() in hallucination_words for w in words):
                logger.debug(f"Filtering short hallucination: {text}")
                continue

        # Drop any segment whose FIRST word is a known hallucination trigger.
        # Qwen sometimes prepends "सब्सक्राइब" to a garbled sentence —
        # removing just the word leaves a non-sensical fragment.
        _hallucination_starters = {'सब्सक्राइब', 'subscribe'}
        if words and words[0].lower().rstrip('.,!?।') in _hallucination_starters:
            logger.debug(f"Filtering hallucination-starter segment: {text[:60]}")
            continue

        # Character-level loop words (e.g. "गूगूगूगूगूगू") — drop segment
        if _words_have_char_loops(words):
            logger.debug(f"Filtering char-loop-word segment: {text[:50]}...")
            continue

        # Filter extremely repetitive segments — but try to salvage good prefix first
        if len(words) >= 5:
            word_counts = {}
            for w in words:
                word_counts[w.lower()] = word_counts.get(w.lower(), 0) + 1
            max_count = max(word_counts.values()) if word_counts else 0

            # Single-word loop: one word makes up 70% of the segment
            if max_count >= len(words) * 0.70:
                trimmed = remove_trailing_word_loop(text, min_repeats=2)
                trimmed_words = trimmed.split()
                if len(trimmed_words) >= 3 and trimmed != text:
                    logger.debug(f"Salvaged prefix from single-word loop: {trimmed[:50]}...")
                    text = trimmed
                    words = trimmed_words
                else:
                    logger.debug(f"Filtering extremely repetitive segment: {text[:50]}...")
                    continue

            # Bigram loop: e.g. "बहुत में बहुत में बहुत में..." (each word ~38%)
            if len(words) >= 8:
                bigrams = [f"{words[i].lower()}_{words[i+1].lower()}" for i in range(len(words)-1)]
                bigram_counts = {}
                for bg in bigrams:
                    bigram_counts[bg] = bigram_counts.get(bg, 0) + 1
                max_bg = max(bigram_counts.values()) if bigram_counts else 0
                if max_bg >= len(bigrams) * 0.40:
                    # Try to salvage the good content before the loop starts
                    trimmed = remove_trailing_word_loop(text, min_repeats=2)
                    trimmed_words = trimmed.split()
                    if len(trimmed_words) >= 3 and trimmed != text:
                        logger.debug(f"Salvaged prefix from bigram loop: {trimmed[:50]}...")
                        text = trimmed
                        words = trimmed_words
                    else:
                        logger.debug(f"Filtering bigram-loop hallucination: {text[:50]}...")
                        continue

        # Clean hallucination patterns from text first, then decide
        cleaned_text = remove_hallucinations(text)
        if not cleaned_text or len(cleaned_text.split()) < 1:
            logger.debug(f"Filtering hallucinated segment (empty after cleaning): {text[:50]}...")
            continue

        # Check cleaned text for extreme repetitiveness
        cleaned_words = cleaned_text.split()
        if len(cleaned_words) >= 5:
            word_counts = {}
            for w in cleaned_words:
                word_counts[w.lower()] = word_counts.get(w.lower(), 0) + 1
            max_count = max(word_counts.values()) if word_counts else 0
            if max_count >= len(cleaned_words) * 0.70:
                logger.debug(f"Filtering extremely repetitive segment after cleaning: {cleaned_text[:50]}...")
                continue

        if cleaned_text != text:
            logger.debug(f"Cleaned hallucination prefix, kept: {cleaned_text[:50]}...")
        seg['text'] = cleaned_text
        filtered.append(seg)

    # Remove trailing YouTube hallucinations
    if filtered and len(filtered) > 1:
        last_text = filtered[-1].get('text', '').strip().lower()
        youtube_hallucinations = {'subscribe', 'सब्सक्राइब', 'like and subscribe', 'thanks for watching'}
        if last_text in youtube_hallucinations:
            filtered.pop()

    return filtered

def clean_transcription_text(text: str) -> str:
    """Clean up transcription text (remove word/phrase repetitions)"""
    if not text:
        return text
    cleaned = text
    # Remove longer phrase repetitions first (4-8 words) — catches Hinglish mid-segment loops
    for n in range(8, 3, -1):
        phrase = r'(?:\w+\s+)' * (n - 1) + r'\w+'
        pattern = r'(' + phrase + r')(?:\s+\1)+'
        cleaned = re.sub(pattern, r'\1', cleaned, flags=re.IGNORECASE)
    # Remove single word repetitions
    cleaned = re.sub(r'\b(\w+)\s+\1\b', r'\1', cleaned, flags=re.IGNORECASE)
    # Remove 2-word phrase repetitions
    cleaned = re.sub(r'\b(\w+\s+\w+)\s+\1\b', r'\1', cleaned, flags=re.IGNORECASE)
    # Remove 3-word phrase repetitions
    cleaned = re.sub(r'\b(\w+\s+\w+\s+\w+)\s+\1\b', r'\1', cleaned, flags=re.IGNORECASE)
    # Normalize whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned)
    # Fix punctuation spacing
    cleaned = re.sub(r'\s*([,;:।])\s*', r'\1 ', cleaned)
    cleaned = re.sub(r'\s*([.!?])\s*', r'\1 ', cleaned)
    return cleaned.strip()

def remove_segment_trailing_repetition(text: str, min_chars: int = 15) -> str:
    """Remove trailing repetitions in segments"""
    if not text or len(text) < min_chars * 2:
        return text
    for length in range(len(text) // 2, min_chars - 1, -5):
        trailing = text[-length:].strip().lower()
        rest = text[:-length].strip()
        if len(rest) > 0 and trailing in rest.lower():
            logger.debug(f"Removed trailing repetition: ...{trailing[:30]}")
            return rest
    return text


_TRAILING_CONJUNCTIONS = {
    'और', 'or', 'but', 'तो', 'कि', 'for', 'of', 'to', 'with', 'in', 'on', 'at', 'by', 'the', 'a', 'an',
}


def _strip_trailing_conjunctions(words: list) -> list:
    """Remove trailing pure conjunction/preposition words from a word list."""
    while words and words[-1].lower() in _TRAILING_CONJUNCTIONS:
        words = words[:-1]
    return words


def remove_trailing_word_loop(text: str, min_repeats: int = 3, max_phrase_words: int = 6) -> str:
    """Remove trailing word-level repetition loops.

    Catches patterns like:
      "good text और वार्ट और वार्ट और वार्ट और व"  → "good text और वार्ट"
      "good text और देखते हैं और देखते हैं और देखते हैं" → "good text और देखते हैं"

    The existing bigram filter operates on the whole segment and misses loops that
    only affect the tail (e.g. 3 repeats of a 2-word phrase = only ~17% of total bigrams).
    This function scans the tail for consecutive exact phrase repetitions.
    """
    words = text.split()
    n = len(words)
    if n < max_phrase_words * min_repeats:
        return text

    best_first_start = None
    best_kept_end = None

    for phrase_words in range(1, max_phrase_words + 1):
        # tail_skip: words to skip at end (incomplete last repetition, including garbled tokens)
        for tail_skip in range(phrase_words + 1):
            end = n - tail_skip
            if end < phrase_words * min_repeats:
                continue

            phrase = tuple(words[end - phrase_words:end])

            # Count consecutive backwards occurrences of this phrase
            pos = end - phrase_words
            count = 1
            while pos >= phrase_words:
                if tuple(words[pos - phrase_words:pos]) == phrase:
                    count += 1
                    pos -= phrase_words
                else:
                    break

            if count >= min_repeats and pos > 0:
                # pos = first word index where the loop begins
                kept_end = pos + phrase_words  # keep one copy of the phrase
                # Prefer earliest loop start (most aggressive trim)
                if best_first_start is None or pos < best_first_start:
                    best_first_start = pos
                    best_kept_end = kept_end

    if best_kept_end is not None:
        kept_words = _strip_trailing_conjunctions(words[:best_kept_end])
        result = ' '.join(kept_words)
        if len(result) < len(text) - 5:
            logger.debug(
                f"Removed trailing word loop at word {best_first_start}: "
                f"...{' '.join(words[best_first_start:best_first_start + 6])}..."
            )
            return result
    return text

def trim_garbled_tail(text: str, tail_words: int = 8, max_unique_ratio: float = 0.5) -> str:
    """Trim trailing garbled repetitions where last N words have too few unique words.

    Catches patterns like: "...दर्शाए गाई तर्शाए तर्शाए गाई तर्शाए"
    where the tail is mostly recycled words (garbled Whisper output near segment end).
    """
    words = text.split()
    if len(words) < tail_words * 2:
        return text
    tail = words[-tail_words:]
    unique_ratio = len(set(w.lower() for w in tail)) / tail_words
    if unique_ratio <= max_unique_ratio:
        # Walk backwards to find where garbling starts
        for start in range(len(words) - tail_words, max(0, len(words) - tail_words * 2), -1):
            chunk = words[start:start + tail_words]
            if len(set(w.lower() for w in chunk)) / tail_words > max_unique_ratio:
                trimmed = ' '.join(words[:start + 1])
                logger.debug(f"Trimmed garbled tail at word {start}: ...{' '.join(tail[:4])}...")
                return trimmed
        # Entire tail is garbled — drop tail_words
        return ' '.join(words[:len(words) - tail_words])
    return text


def clean_segment_list(segments: list) -> list:
    """Clean all segments (remove repetitions, clean text, drop consecutive duplicates)"""
    if not segments:
        return segments
    cleaned = []
    last_text = None
    for seg in segments:
        text = seg.get('text', '').strip()
        if not text:
            continue
        # Remove trailing repetitions (character-level)
        text = remove_segment_trailing_repetition(text)
        # Remove trailing word-level loops (e.g. "phrase phrase phrase partial")
        text = remove_trailing_word_loop(text)
        # Trim garbled tails where the last N words have too few unique words
        text = trim_garbled_tail(text)
        # Clean text
        text = clean_transcription_text(text)
        if not text:
            continue
        # Drop consecutive identical segments (e.g. "don't drink alcohol" x2)
        if text.lower() == last_text:
            logger.debug(f"Dropping consecutive duplicate segment: {text[:50]}")
            continue
        # Drop near-duplicate consecutive segments (>70% word overlap)
        if last_text:
            cur_words = set(text.lower().split())
            prev_words = set(last_text.split())
            if cur_words and prev_words:
                overlap = len(cur_words & prev_words) / max(len(cur_words), len(prev_words))
                if overlap >= 0.70:
                    logger.debug(f"Dropping near-duplicate segment ({overlap:.0%} overlap): {text[:50]}")
                    continue
        seg['text'] = text
        cleaned.append(seg)
        last_text = text.lower()
    return cleaned
