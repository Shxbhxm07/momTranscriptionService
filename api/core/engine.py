"""Offline speech → English text, via a local whisper.cpp server.

DERIVED FROM speech-service/core/engine.py::_transcribe_whispercpp. The request shape,
the measured decoding parameters and the verbose_json response parse are carried over
unchanged — that code was tuned against real recordings on this exact hardware and there
was no reason to touch it. What was removed is everything AROUND it: the Groq/faster-
whisper/transformers/whisperx/qwen/Tara engine branches, the model-routing table and the
Devanagari-probe auto-engine selector.

═══ HOW Hindi / Hinglish → English IS HANDLED ═══
Whisper is natively bilingual in a very specific sense: it was trained on two tasks, and
the task is chosen by a token in the decoder prompt, not by a separate model.

    <|transcribe|>  → write down what was said, in the language it was said in
    <|translate|>   → write down what was said, IN ENGLISH

So `translate=true` is not post-hoc machine translation bolted onto a transcript — it is
the model decoding speech directly into English in one pass. That is why this needs no
translation model, no second service and no network: it is the same 3 GB ggml-large-v3
file doing a different task.

Paired with `-l auto` on the server (whisper detects the spoken language itself), the
three required inputs collapse to one code path:

    Hindi audio     → detected hi → translate task → English text
    Hinglish audio  → detected hi (or en) → translate task → English text
    English audio   → detected en → translate task → English text (effectively a copy)

WHY THIS REPLACED THE MoM ROUTING: the MoM service ran Tara first as a language probe and
kept its output for Hinglish, because MoM WANTED the Hindi preserved in Devanagari for a
bilingual transcript. This service wants the opposite, so Tara is not merely unnecessary
here — it is counterproductive, since it has no translate task and its whole purpose is
to emit Devanagari. Dropping it also drops torch + transformers + a 47 GB image.
"""
import logging
import os
from typing import Any, Dict

import requests

from wave import Error as wave_Error

from config import (BEAM_SIZE, LANG_DETECT_MIN_PROB, LANG_DETECT_SECONDS, MAX_CONTEXT,
                    REQUEST_TIMEOUT, TEMPERATURE, WHISPERCPP_URL)
from utils.text_processing import (
    clean_segment_list,
    filter_hallucinated_segments,
    remove_hallucinations,
)

logger = logging.getLogger(__name__)

# whisper.cpp long-form language names → ISO codes. Kept from the MoM engine so the
# reported source language is a stable code rather than whichever spelling whisper used.
_LANG_NAME_TO_CODE = {
    "english": "en", "hindi": "hi", "marathi": "mr", "bengali": "bn",
    "tamil": "ta", "telugu": "te", "gujarati": "gu", "kannada": "kn",
    "malayalam": "ml", "punjabi": "pa", "urdu": "ur", "russian": "ru",
    "arabic": "ar", "french": "fr", "german": "de", "spanish": "es",
    "japanese": "ja", "chinese": "zh", "korean": "ko",
}


class TranscriptionEngine:
    """Thin, stateless client for the whisper.cpp /inference endpoint.

    Stateless is the point: the model lives in the whisper-server process, so this class
    holds nothing but a connection pool and can be shared across workers freely.
    """

    def __init__(self, base_url: str = WHISPERCPP_URL):
        self.base_url = base_url.rstrip("/")
        # One pooled Session for the process. Reuses the TCP connection across requests
        # instead of a fresh handshake per upload.
        self.session = requests.Session()

    # ── health ───────────────────────────────────────────────────────────────
    def is_ready(self) -> bool:
        """whisper.cpp loads the model BEFORE it binds the port, so a successful connect
        is a genuine 'model is on the GPU' signal, not just 'process started'."""
        try:
            # No /health route on whisper-server; any answered request proves the bind.
            self.session.get(f"{self.base_url}/", timeout=5)
            return True
        except requests.RequestException:
            return False

    # ── language detection ───────────────────────────────────────────────────
    @staticmethod
    def _head_wav(audio_path: str, seconds: int = 30) -> bytes:
        """First `seconds` of a 16 kHz mono WAV, as a valid standalone WAV."""
        import io
        import wave
        with wave.open(audio_path, "rb") as src:
            frames = src.readframes(min(src.getnframes(), int(src.getframerate() * seconds)))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as dst:
                dst.setnchannels(src.getnchannels())
                dst.setsampwidth(src.getsampwidth())
                dst.setframerate(src.getframerate())
                dst.writeframes(frames)
            return buf.getvalue()

    def detect_language(self, audio_path: str) -> str:
        """Detect the spoken language from a short head slice. Returns "" if undetermined.

        Deliberately a SEPARATE short call rather than reusing the main pass: the task token
        has to be chosen before the full decode runs, so the language has to be known first.
        30 seconds is enough for Whisper's language ID and costs a few seconds.
        """
        try:
            head = self._head_wav(audio_path, LANG_DETECT_SECONDS)
            r = self.session.post(
                f"{self.base_url}/inference",
                data={"response_format": "verbose_json", "translate": "false",
                      "beam_size": "1", "temperature": "0.0"},
                files={"file": ("head.wav", head, "audio/wav")},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                return ""
            body = r.json()
            lang = str(body.get("detected_language") or body.get("language") or "").lower()
            lang = _LANG_NAME_TO_CODE.get(lang, lang)
            prob = float(body.get("detected_language_probability") or 0.0)
            logger.info(f"[WHISPER] language probe: {lang or 'unknown'} (p={prob:.2f})")
            # Only act on a confident answer. An unsure probe falls through to translate,
            # which is today's behaviour and is never wrong for a non-English meeting.
            return lang if prob >= LANG_DETECT_MIN_PROB else ""
        except (requests.RequestException, ValueError, OSError, wave_Error) as e:
            logger.warning(f"[WHISPER] language probe failed ({e}) — defaulting to translate")
            return ""

    # ── transcription ────────────────────────────────────────────────────────
    def transcribe_to_english(self, audio_path: str, translate: bool = True) -> Dict[str, Any]:
        """Decode `audio_path` (16 kHz mono WAV) to English text.

        `translate=False` selects Whisper's <|transcribe|> task, which is correct ONLY when
        the audio is already English. Sending <|translate|> on English is not the no-op the
        old comment claimed: measured 2026-09-07 on a 3-minute slice of a real committee
        meeting, the two tasks agreed on only 90.2% of words, and translate DROPPED the
        proper nouns "CULP" and "Harford" plus several ordinary words that transcribe kept.
        Words lost here can never be recovered — the LLM cannot repair what was never written.

        The trade is that transcribe lower-cases proper nouns where translate capitalises
        them, which the downstream correction pass largely repairs ("C ulp." -> "Culp.").
        """
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        # Cap beam_size at 5. Measured 2026-07-22 on this stack: whisper.cpp large-v3
        # degenerates into a "सब्सक्राइब" (subscribe) hallucination loop at beam >= 7.
        # beam 5 is the sweep-validated safe+best value. Never raise this above 5.
        safe_beam = min(int(BEAM_SIZE), 5)

        data = {
            "response_format": "verbose_json",
            # THE TRANSLATION SWITCH. Selects Whisper's <|translate|> task, so the decoder
            # emits English regardless of the spoken language. No `language` field is sent
            # on purpose: whisper-server runs with `-l auto`, and letting it detect the
            # language is load-bearing. Measured 2026-07-23 (A/B, identical audio):
            # forcing language="hi" on English speech transliterated it INTO Devanagari
            # and triggered a hallucination loop. Auto-detect is what avoids that.
            "translate": "true" if translate else "false",
            "beam_size": str(safe_beam),
            "temperature": str(float(TEMPERATURE)),
            # BOUNDED context — this value has a failure mode on EITHER side of it, both
            # measured on real recordings. Do not change it without re-running both:
            #
            #   max_context=0 (too little): the decoder loses its anchor on a long window.
            #     A 54s clip came back as ONE 21s segment containing a YouTube artifact
            #     instead of the speech — ~45% of the content silently lost.
            #
            #   max_context=64+ (too much): the decoder echoes. A 19.7-minute session
            #     produced 60.9% duplicate lines, one line repeated 290 times, and the loop
            #     BLOCKED progress — the entire second half was never transcribed at all.
            #
            #   max_context=32: clean on BOTH. 19.7-min → 0% duplicates; 54s → no
            #     hallucination, every phrase recovered.
            "max_context": str(MAX_CONTEXT),
        }

        # NOTE: no `prompt` / initial_prompt is sent, and that is deliberate. The MoM
        # service's HINGLISH_PROMPT existed to make whisper PRESERVE code-switching
        # (Hindi in Devanagari, English in Latin) — directly at odds with translating to
        # English. It also had a documented failure mode: whisper echoes its initial
        # prompt back as false content on unclear audio, which once put a phantom
        # attendee into a MoM. Sending nothing removes both problems at once.

        files = {"file": (os.path.basename(audio_path), audio_data, "audio/wav")}

        logger.info(
            f"[WHISPER] POST {self.base_url}/inference — task={'translate' if translate else 'transcribe'}, lang=auto→en, "
            f"beam={safe_beam}, temp={TEMPERATURE}, max_context={MAX_CONTEXT}"
        )

        try:
            response = self.session.post(
                f"{self.base_url}/inference",
                data=data,
                files=files,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(
                f"Cannot reach the local whisper server at {self.base_url}: {e}"
            ) from e

        if response.status_code != 200:
            raise RuntimeError(
                f"whisper-server error {response.status_code}: {response.text[:300]}"
            )

        return self._parse(response.json())

    # ── response parsing (verbose_json, OpenAI-audio shaped) ─────────────────
    @staticmethod
    def _parse(result: Dict[str, Any]) -> Dict[str, Any]:
        """Carried over from the MoM whispercpp parser, unchanged."""
        detected = result.get("language", "unknown")
        detected = _LANG_NAME_TO_CODE.get(str(detected).lower(), detected)

        segments = []
        text_parts = []
        for seg in result.get("segments", []):
            seg_text = (seg.get("text") or "").strip()
            if not seg_text:
                continue
            # WORD TIMINGS are carried through, not dropped. whisper-server already returns
            # them on every segment and they cost nothing extra; without them the speaker
            # merge can only assign a WHOLE segment to one speaker, which mis-attributes
            # every segment that straddles a speaker change. See label_transcript().
            words = []
            for w in (seg.get("words") or []):
                token = w.get("word")
                if not token:
                    continue
                words.append({
                    "word": token,
                    "start": round(float(w.get("start", 0.0)), 3),
                    "end": round(float(w.get("end", 0.0)), 3),
                })
            segments.append({
                "text": seg_text,
                "start": round(float(seg.get("start", 0.0)), 3),
                "end": round(float(seg.get("end", 0.0)), 3),
                "words": words,
            })
            text_parts.append(seg_text)

        # Some responses carry only a flat `text` (e.g. very short clips with no segment
        # breakdown). Synthesise one segment so downstream filtering has something to work on.
        if not segments and result.get("text"):
            flat = result["text"].strip()
            segments = [{"text": flat, "start": 0.0, "end": float(result.get("duration", 0.0))}]
            text_parts = [flat]

        duration = float(result.get("duration", segments[-1]["end"] if segments else 0.0))
        logger.info(
            f"[WHISPER] ✓ {len(segments)} segments, detected source lang={detected}, "
            f"audio duration={duration:.1f}s"
        )

        return {
            "text": " ".join(text_parts),
            "segments": segments,
            "source_language": detected,
            "duration": round(duration, 2),
        }

    # ── cleanup ──────────────────────────────────────────────────────────────
    @staticmethod
    def clean(result: Dict[str, Any], enable_filter: bool) -> Dict[str, Any]:
        """Whisper's repetition/YouTube-artifact scrubbing.

        Same three-stage pass as the MoM engine's process_transcription(), minus the
        speaker/segment bookkeeping the MoM pipeline needed downstream.
        """
        if not enable_filter:
            return result

        original_count = len(result["segments"])
        text = remove_hallucinations(result["text"])
        segments = filter_hallucinated_segments(result["segments"])
        segments = clean_segment_list(segments)

        # Rebuild the flat text FROM the surviving segments, so a segment dropped as a
        # hallucination cannot survive inside the joined string.
        text = " ".join(s.get("text", "").strip() for s in segments if s.get("text")) if segments else ""

        dropped = original_count - len(segments)
        if dropped:
            logger.info(f"[FILTER] Dropped {dropped} hallucinated segment(s)")

        result["text"] = text
        result["segments"] = segments
        result["segments_filtered"] = dropped
        return result
