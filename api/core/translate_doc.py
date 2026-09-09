"""Document text → translated text, Hindi ⇄ English, via the local LLM.

Like core/mom.py, this module owns NO prompt and NO model. Translation already exists,
tested, in ../llama-service: a translation prompt written against code-mixed Hindi input,
a deterministic script-based output validator, and a read-through cache. It is reused as a
service over the same vLLM that writes the minutes.

    transcribe-api ──POST /translate_batch──▶ llama-service ──▶ vLLM ──▶ gpt-oss-120b

WHAT THIS MODULE OWNS is everything between a document and that endpoint: deciding which
language the text is in, cutting it into pieces the model can answer in one budget,
grouping those pieces into calls, retrying the ones that come back wrong, and putting the
answers back together in order.

WHY /translate_batch AND NOT /translate. The single endpoint is the obvious choice and it
is the wrong one here, for a reason worth stating: on a refusal or an empty completion,
llama-service's _clean_translation_output returns THE ORIGINAL TEXT. For live captions
that is the right call — a caption that briefly shows the source beats a blank one. For a
document it is the worst possible failure, because untranslated paragraphs come back
indistinguishable from translated ones and the caller has no way to tell. /translate_batch
instead validates every item against the target script and returns None plus a REASON for
anything that failed, which is what lets this module retry deliberately and report what it
could not translate. Untranslated text still falls back to the source — see _finalise —
but it is counted, listed and named in the response rather than hidden.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from config import (
    DOC_BATCH_CHARS,
    DOC_BATCH_ITEMS,
    DOC_CHUNK_CHARS,
    DOC_TRANSPORT_RETRIES,
    LLAMA_URL,
    NORMALIZE_DIGITS,
    PROTECT_CODE_SPANS,
    TRANSLATE_TIMEOUT,
)
from utils.code_spans import has_translatable_text, mask, normalize_digits, unmask

logger = logging.getLogger(__name__)


# ── languages ────────────────────────────────────────────────────────────────
# Canonical names, not ISO codes, and this is load-bearing in TWO places inside
# llama-service:
#
#   1. the prompt interpolates the value literally — "Translate the text into {target}" —
#      so "hi" asks the model to translate into something called "hi";
#   2. its validator resolves a language by SUBSTRING against the keys of LANG_CONFIG
#      ("hindi", "english", ...), and "hi" matches none of them. An unmatched language
#      silently disables the script checks, so an echoed, untranslated paragraph would
#      come back marked valid.
#
# (2) is the dangerous one: passing the ISO code does not fail loudly, it turns off the
# only thing standing between the caller and silently untranslated output. So the API
# accepts the codes for convenience and normalises here, once.
CANONICAL_LANGS = {
    "hi": "Hindi", "hin": "Hindi", "hindi": "Hindi", "devanagari": "Hindi",
    "en": "English", "eng": "English", "english": "English",
}
SUPPORTED_LANGS = ("Hindi", "English")

# tesseract language data installed in the image, per canonical language.
OCR_LANG_BY_CANONICAL = {"Hindi": "hin", "English": "eng"}

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")

# Above this share of Devanagari among the letters, the text is Hindi. Set low on purpose:
# real Hindi documents carry English proper nouns, acronyms and numerals throughout, and a
# majority test would call a Hindi page with an English letterhead "English". The reverse
# error is far less likely — English documents do not carry stray Devanagari.
_HINDI_LETTER_RATIO = 0.15


class TranslationError(Exception):
    """A translation that could not be completed. Message is caller-facing."""


def normalize_lang(value: Optional[str], field_name: str) -> Optional[str]:
    """'hi'/'HINDI'/'Hindi' → 'Hindi'. None/'' → None (meaning: decide automatically)."""
    if value is None:
        return None
    key = value.strip().lower()
    if not key or key == "auto":
        return None
    if key not in CANONICAL_LANGS:
        raise TranslationError(
            f"Unsupported {field_name} '{value}'. This API translates between Hindi and "
            f"English only — use 'Hindi' or 'English' (or 'hi'/'en')."
        )
    return CANONICAL_LANGS[key]


def detect_language(text: str) -> str:
    """Hindi or English, by script. Deterministic, offline, no model call.

    Script detection cannot see romanized Hindi ("aap kaise hain"), which is Latin text
    and is reported as English. That is a real limitation of measuring script rather than
    language, and the reason source_lang is an explicit override on the endpoint.
    """
    deva = len(_DEVANAGARI.findall(text))
    latin = len(_LATIN.findall(text))
    if deva + latin == 0:
        return "English"
    return "Hindi" if deva / (deva + latin) >= _HINDI_LETTER_RATIO else "English"


# ── chunking ─────────────────────────────────────────────────────────────────

# Sentence boundary for both scripts: a terminator plus optional closing quote/bracket,
# followed by whitespace. Devanagari's danda (।) and double danda (॥) are included — a
# Hindi paragraph contains no full stops at all, so without them long Hindi blocks would
# only ever be split mid-clause by the character-count fallback below.
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?।॥])["\'”’)\]]*\s+')


def _split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text)]
    return [p for p in parts if p]


def _hard_split(text: str, limit: int) -> List[str]:
    """Last-resort split for a single 'sentence' longer than the limit — a table row, a
    list with no terminators, or OCR output that lost its punctuation. Breaks on word
    boundaries so no word is cut in half.

    The character-slice fallback is not hypothetical: OCR on a dense or skewed scan
    regularly emits long runs with no spaces at all, and splitting only on spaces would
    return that run whole — handing the model a chunk far past the batch budget it was
    sized for. A mid-word cut mistranslates one word; an unbounded chunk loses the call.
    """
    out, current = [], ""
    for word in text.split(" "):
        while len(word) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(word[:limit])
            word = word[limit:]
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def chunk_blocks(blocks: List[str], limit: int = DOC_CHUNK_CHARS) -> Tuple[List[str], List[int]]:
    """Blocks → (chunks, owner_index).

    Returns the chunks to translate and, for each, the index of the block it came from,
    so _finalise can rebuild the document in order. A block short enough to translate
    whole yields exactly one chunk — the common case, since most paragraphs are shorter
    than the limit.

    Sentences are packed greedily up to the limit rather than sent one per chunk: a lone
    sentence gives the model no surrounding context for pronouns or gender agreement,
    both of which Hindi needs and English does not mark.
    """
    chunks: List[str] = []
    owners: List[int] = []
    for block_index, block in enumerate(blocks):
        if len(block) <= limit:
            chunks.append(block)
            owners.append(block_index)
            continue
        current = ""
        for sentence in _split_sentences(block):
            pieces = [sentence] if len(sentence) <= limit else _hard_split(sentence, limit)
            for piece in pieces:
                candidate = f"{current} {piece}".strip()
                if current and len(candidate) > limit:
                    chunks.append(current)
                    owners.append(block_index)
                    current = piece
                else:
                    current = candidate
        if current:
            chunks.append(current)
            owners.append(block_index)
    return chunks, owners


def batch_chunks(chunks: List[str],
                 max_items: int = DOC_BATCH_ITEMS,
                 max_chars: int = DOC_BATCH_CHARS,
                 indices: Optional[List[int]] = None) -> List[List[int]]:
    """Group chunk indices into model calls, bounded by both count and total characters.

    The character bound is the one that matters — llama-service sizes the output budget
    from the batch's total length and caps it at 6000 tokens, so an oversized batch is one
    the model has no room to finish. See DOC_BATCH_CHARS in config.py for the measurement.
    """
    batches: List[List[int]] = []
    current: List[int] = []
    current_chars = 0
    # `indices` restricts batching to a subset without renumbering it — the caller holds
    # parallel arrays keyed by the ORIGINAL index, so the returned batches must speak in
    # those same indices or every translation lands on the wrong chunk.
    for i in (range(len(chunks)) if indices is None else indices):
        size = len(chunks[i])
        if current and (len(current) >= max_items or current_chars + size > max_chars):
            batches.append(current)
            current, current_chars = [], 0
        current.append(i)
        current_chars += size
    if current:
        batches.append(current)
    return batches


# ── result ───────────────────────────────────────────────────────────────────

@dataclass
class TranslationResult:
    text: str = ""
    blocks: int = 0
    chunks: int = 0
    model_calls: int = 0
    translated: int = 0
    # Chunks the model never translated acceptably. Their SOURCE text is kept in `text`
    # so the document stays complete and readable, which makes counting them here the
    # only way a caller can tell. Never let this be silent.
    untranslated: int = 0
    failure_reasons: Dict[str, int] = field(default_factory=dict)
    untranslated_samples: List[str] = field(default_factory=list)
    cached_duplicates: int = 0
    # Chunks that were nothing but code once masked — a shell command, a file path — and
    # so were returned verbatim without a model call. Reported because "not translated" and
    # "had nothing to translate" are different outcomes and only one is a problem.
    code_only_chunks: int = 0
    protected_spans: int = 0


class DocumentTranslator:
    """Thin client for llama-service's translation endpoints. Stateless."""

    def __init__(self, base_url: str = LLAMA_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def is_ready(self) -> bool:
        """Probe '/' rather than '/health' — same reason as MomGenerator.is_ready:
        llama-service's /health round-trips to the LLM, so using it as a liveness check
        would put a model call in front of every upload."""
        try:
            return self.session.get(f"{self.base_url}/", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    def _post_batch(self, texts: List[str], source_lang: str, target_lang: str):
        payload = {"texts": texts, "source_lang": source_lang, "target_lang": target_lang}
        r = self.session.post(
            f"{self.base_url}/translate_batch", json=payload, timeout=TRANSLATE_TIMEOUT
        )
        if r.status_code != 200:
            raise TranslationError(f"llama-service error {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data.get("translations") or [], data.get("reasons") or []

    def translate_blocks(self, blocks: List[str], source_lang: str, target_lang: str) -> TranslationResult:
        """Translate paragraph blocks and reassemble them into one document.

        Same-language requests short-circuit without a model call: llama-service would
        return the text unchanged anyway, and paying minutes of GPU time to learn that is
        not a useful default.
        """
        result = TranslationResult(blocks=len(blocks))
        if source_lang == target_lang:
            result.text = "\n\n".join(blocks)
            result.chunks = result.translated = len(blocks)
            logger.info("[XLATE] source and target are both %s — returning text unchanged", source_lang)
            return result

        chunks, owners = chunk_blocks(blocks)
        result.chunks = len(chunks)
        if not chunks:
            return result

        # Translate each DISTINCT chunk once. Documents repeat themselves heavily —
        # letterheads, running headers, table labels, and above all OCR'd page furniture
        # that reappears on every single page — and each duplicate would otherwise cost a
        # full share of the ~43 chars/s budget.
        unique: Dict[str, List[int]] = {}
        for i, text in enumerate(chunks):
            unique.setdefault(text, []).append(i)
        distinct = list(unique.keys())
        result.cached_duplicates = len(chunks) - len(distinct)
        if result.cached_duplicates:
            logger.info(
                "[XLATE] %d of %d chunks are repeats — translating %d distinct",
                result.cached_duplicates, len(chunks), len(distinct),
            )

        translations: List[Optional[str]] = [None] * len(distinct)
        reasons: List[Optional[str]] = [None] * len(distinct)

        # Hide code from the model before it can translate it. A shell command or a file
        # path is not prose, and a translator asked to render one produces fluent Hindi
        # that no longer runs — see utils/code_spans.py for the measured failures.
        payloads: List[str] = []
        spans: List[List[str]] = []
        for text in distinct:
            if PROTECT_CODE_SPANS:
                masked, found = mask(text)
            else:
                masked, found = text, []
            payloads.append(masked)
            spans.append(found)
            result.protected_spans += len(found)

        # A chunk that masks down to nothing but placeholders has no words in it. Sending
        # it would spend model time to be handed back what we already hold, and the answer
        # is verbatim by construction rather than by luck.
        pending_indices = []
        for i, payload in enumerate(payloads):
            if spans[i] and not has_translatable_text(payload):
                translations[i] = distinct[i]
                result.code_only_chunks += 1
            else:
                pending_indices.append(i)

        batches = batch_chunks(payloads, indices=pending_indices)
        total_chars = sum(len(payloads[i]) for i in pending_indices)
        logger.info(
            "[XLATE] %s → %s | %d blocks, %d chunks (%d distinct, %d chars) in %d call(s); "
            "%d code span(s) protected, %d chunk(s) code-only",
            source_lang, target_lang, len(blocks), len(chunks), len(distinct), total_chars,
            len(batches), result.protected_spans, result.code_only_chunks,
        )

        for batch_number, batch in enumerate(batches, start=1):
            calls = self._run_batch(
                batch, distinct, payloads, spans, translations, reasons, source_lang, target_lang
            )
            result.model_calls += calls
            done = sum(1 for t in translations if t is not None)
            logger.info(
                "[XLATE] batch %d/%d done — %d/%d chunks translated",
                batch_number, len(batches), done, len(distinct),
            )

        return self._finalise(result, chunks, owners, unique, distinct, translations, reasons)

    @staticmethod
    def _store(chunk_index, value, distinct, spans, translations, reasons) -> bool:
        """Accept one model answer: normalise digits, restore code, verify nothing was lost.

        Returns True when the chunk is done. Returns False — leaving `reasons` set — when
        the answer is unusable, which puts it back in front of the retry logic.

        THE PLACEHOLDER CHECK IS THE POINT. A model that drops a ⟦n⟧ has deleted a command
        or a file path from the middle of a paragraph, and the result would read as clean,
        confident Hindi with the one unrecoverable part silently missing. That is worse
        than an untranslated paragraph, because nothing about it looks wrong. So a lost
        span fails the chunk and it falls back to its source text like any other failure.
        """
        if not isinstance(value, str) or not value.strip():
            return False

        text = normalize_digits(value) if NORMALIZE_DIGITS else value
        restored, missing = unmask(text, spans[chunk_index])
        if missing:
            logger.warning(
                "[XLATE] %d of %d protected span(s) lost by the model — keeping source for this chunk",
                missing, len(spans[chunk_index]),
            )
            reasons[chunk_index] = "lost_code_span"
            return False

        translations[chunk_index] = restored
        reasons[chunk_index] = None
        return True

    def _run_batch(self, batch, distinct, payloads, spans, translations, reasons,
                   source_lang, target_lang) -> int:
        """Translate one batch, retrying by failure class. Returns the number of calls made.

        The two classes get different budgets deliberately. A 'transport' failure means the
        call never produced an answer (network, 5xx, a saturated backend), so the same
        request is worth repeating. A validation failure means the model DID answer and the
        answer was wrong — echoed, wrong script, or half-translated — and asking a
        deterministic model the identical question again mostly returns the identical wrong
        answer. What does help is asking it alone, without the JSON-array framing and with
        the whole output budget to itself, so that is the one retry it gets.
        """
        calls = 0
        pending = list(batch)

        for attempt in range(DOC_TRANSPORT_RETRIES + 1):
            if not pending:
                break
            texts = [payloads[i] for i in pending]
            try:
                got, why = self._post_batch(texts, source_lang, target_lang)
                calls += 1
            except (requests.RequestException, TranslationError) as e:
                if attempt >= DOC_TRANSPORT_RETRIES:
                    for i in pending:
                        reasons[i] = "transport"
                    logger.error("[XLATE] batch failed after %d attempts: %s", attempt + 1, e)
                    break
                wait = 2 ** attempt
                logger.warning("[XLATE] batch call failed (%s) — retrying in %ds", e, wait)
                time.sleep(wait)
                continue

            # A short or malformed response would silently misalign every translation with
            # the wrong source chunk. Treat it as a failed call rather than trusting it.
            if len(got) != len(texts):
                logger.error(
                    "[XLATE] llama-service returned %d translations for %d texts — discarding batch",
                    len(got), len(texts),
                )
                for i in pending:
                    reasons[i] = "misaligned"
                break

            still_pending = []
            for position, chunk_index in enumerate(pending):
                value = got[position]
                if self._store(chunk_index, value, distinct, spans, translations, reasons):
                    continue
                reason = reasons[chunk_index] or (why[position] if position < len(why) else None) or "empty"
                reasons[chunk_index] = reason
                if reason == "transport":
                    still_pending.append(chunk_index)

            pending = still_pending
            if pending and attempt < DOC_TRANSPORT_RETRIES:
                wait = 2 ** attempt
                logger.warning(
                    "[XLATE] %d chunk(s) hit transport errors — retrying in %ds", len(pending), wait
                )
                time.sleep(wait)

        # One solo retry for everything that failed validation.
        retryable = [
            i for i in batch
            # "misaligned" is included deliberately: a batch discarded for a length
            # mismatch tells us nothing about the individual chunks in it, and asking for
            # them one at a time removes the array contract that was mis-formatted.
            if translations[i] is None
            and reasons[i] in ("echo", "wrong_script", "mixed_script", "empty", "misaligned",
                               "lost_code_span")
        ]
        for chunk_index in retryable:
            try:
                got, why = self._post_batch([payloads[chunk_index]], source_lang, target_lang)
                calls += 1
            except (requests.RequestException, TranslationError) as e:
                logger.warning("[XLATE] solo retry failed: %s", e)
                continue
            if self._store(chunk_index, got[0] if got else None, distinct, spans, translations, reasons):
                logger.info("[XLATE] solo retry recovered a chunk")
            elif why:
                reasons[chunk_index] = why[0] or reasons[chunk_index]

        return calls

    @staticmethod
    def _finalise(result, chunks, owners, unique, distinct, translations, reasons) -> TranslationResult:
        """Map distinct translations back onto every chunk and rebuild the document.

        A chunk with no acceptable translation keeps its SOURCE text. That is the right
        choice for a document — a hole where a paragraph was is worse than a paragraph in
        the wrong language — but it is only defensible because the counts and samples
        below travel back with it, so the caller learns exactly what happened.
        """
        final: List[Optional[str]] = [None] * len(chunks)
        for distinct_index, text in enumerate(distinct):
            for chunk_index in unique[text]:
                final[chunk_index] = translations[distinct_index]
                if translations[distinct_index] is None:
                    reason = reasons[distinct_index] or "empty"
                    result.failure_reasons[reason] = result.failure_reasons.get(reason, 0) + 1

        rendered: List[str] = []
        current_block = -1
        for chunk_index, value in enumerate(final):
            if value is None:
                result.untranslated += 1
                value = chunks[chunk_index]
                if len(result.untranslated_samples) < 5:
                    result.untranslated_samples.append(value[:160])
            else:
                result.translated += 1
            if owners[chunk_index] != current_block:
                rendered.append(value)
                current_block = owners[chunk_index]
            else:
                # Same source paragraph, split for the model — rejoin it into one.
                rendered[-1] = f"{rendered[-1]} {value}"

        result.text = "\n\n".join(rendered)
        if result.untranslated:
            logger.warning(
                "[XLATE] %d/%d chunks kept as source text — reasons: %s",
                result.untranslated, len(chunks), result.failure_reasons,
            )
        return result
