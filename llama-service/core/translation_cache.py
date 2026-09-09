"""
LIVE-1 Phase 1 — in-memory translation cache + single-flight + concurrency cap.

Why this exists (see BUGS_AND_FIXES.md):
  • C1 cache       — read-through: identical (line, source, target) is translated once, then reused
                     (fixes the redundant ru→he→ru re-fetch + cross-viewer duplication).
  • C2 single-flight — N concurrent viewers asking for the SAME key collapse to ONE model call
                       (the 8×→1× win). A failed/in-flight result is NEVER persisted, so one failure
                       cannot poison every viewer.
  • C3 off-loop    — the blocking LLM call runs via asyncio.to_thread (done in the /translate route),
                     and a Semaphore caps concurrent Groq calls so we don't trade serialization for a
                     429 storm.

It is deliberately small and async behind a tiny interface so an in-memory store can later be swapped
for Redis (multi-pod) without touching the /translate route. In-memory is correct for a single pod.
"""
import asyncio
import hashlib
import logging
import os
import re
import time
import unicodedata

logger = logging.getLogger(__name__)

# --- config (env-overridable) ----------------------------------------------
_MAX_ENTRIES = int(os.getenv("TRANSLATION_CACHE_MAX", "5000"))         # bounded: cap memory
_TTL_SECONDS = float(os.getenv("TRANSLATION_CACHE_TTL", "3600"))       # ~one meeting's lifetime
_MAX_CONCURRENCY = int(os.getenv("TRANSLATION_MAX_CONCURRENCY", "4"))  # cap concurrent Groq calls

_WS_RE = re.compile(r"\s+")
# Strip a single leading "[speaker_N]" / "[Name]" label so a line translated live (bare) and the same
# line after diarization ("[speaker_2] ...") share one cache key. (Full per-line keying is Phase 2.)
_SPEAKER_PREFIX_RE = re.compile(r"^\s*\[[^\]]+\]\s*")


def normalize_key_text(text: str) -> str:
    """Stable cache-key normalization: drop a leading [speaker] label, NFC-normalize, collapse
    internal whitespace, trim. Case + punctuation are PRESERVED (they carry meaning) — this must
    stay separate from any lossy dedup normalizer."""
    t = _SPEAKER_PREFIX_RE.sub("", text or "")
    t = unicodedata.normalize("NFC", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def make_translation_key(text: str, source_lang: str, target_lang: str) -> str:
    h = hashlib.sha1(normalize_key_text(text).encode("utf-8")).hexdigest()
    return f"{source_lang}|{target_lang}|{h}"


class TranslationCache:
    """In-memory read-through cache + single-flight. Async interface so a Redis backend can be
    dropped in later behind the same methods."""

    def __init__(self):
        self._store = {}        # key -> (value, expires_at_monotonic)
        self._inflight = {}     # key -> asyncio.Future   (single-flight)
        self._lock = asyncio.Lock()                 # guards _store / _inflight bookkeeping only
        self._sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    def _get_fresh(self, key):
        item = self._store.get(key)
        if not item:
            return None
        value, expires_at = item
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def _set(self, key, value):
        # bounded: evict the oldest (insertion-ordered) entry when at capacity
        if key not in self._store and len(self._store) >= _MAX_ENTRIES:
            try:
                self._store.pop(next(iter(self._store)))
            except StopIteration:
                pass
        self._store[key] = (value, time.monotonic() + _TTL_SECONDS)

    async def get_or_translate(self, key, work):
        """Return (translated_text, was_cache_hit).

        `work` is a zero-arg SYNC callable returning the translated string; it is run off the event
        loop via asyncio.to_thread under a concurrency cap. Read-through + single-flight: concurrent
        callers for the same key share one `work` invocation.
        """
        async with self._lock:
            cached = self._get_fresh(key)
            if cached is not None:
                return cached, True
            fut = self._inflight.get(key)
            owner = fut is None
            if owner:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut

        if not owner:
            # Someone else is already translating this exact key — share their result.
            return await fut, False

        # We own the translation for this key.
        try:
            async with self._sem:                       # C3: cap concurrent Groq calls
                value = await asyncio.to_thread(work)    # C3: run the blocking LLM call off the loop
            async with self._lock:
                self._set(key, value)                    # C1: cache only successful results
            fut.set_result(value)
            return value, False
        except BaseException as e:
            # C2: propagate to any followers, but DO NOT cache — a failure must not poison the key.
            # BaseException (not Exception) so a CANCELLED owner — CancelledError is a BaseException,
            # which an `except Exception` would miss — still resolves the Future. Otherwise a follower
            # awaiting it (e.g. a viewer who closes the tab mid-translate) would hang forever.
            if not fut.done():
                fut.set_exception(e if isinstance(e, Exception) else RuntimeError("Translation cancelled"))
            raise
        finally:
            async with self._lock:
                if self._inflight.get(key) is fut:       # C2: ALWAYS clear the in-flight marker
                    self._inflight.pop(key, None)
            # Mark a stored exception as retrieved (avoids asyncio "never retrieved" warning if no
            # follower awaited it).
            if fut.done() and not fut.cancelled() and fut.exception() is not None:
                pass

    def stats(self):
        return {"entries": len(self._store), "inflight": len(self._inflight),
                "max_entries": _MAX_ENTRIES, "ttl_s": _TTL_SECONDS, "max_concurrency": _MAX_CONCURRENCY}


# Module-level singleton (single pod). Swap for a Redis-backed impl behind the same interface later.
translation_cache = TranslationCache()
