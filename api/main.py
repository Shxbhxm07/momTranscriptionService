"""Three offline endpoints over the same local models.

    POST /transcribe-and-generate-mom   audio=<file>  → {"success": true, "mom": {...}}
    POST /translate-document            file=<file>   → {"success": true, "translated_text": ...}
    POST /translate-media               file=<file>   → {"success": true, "translated_text": ..., "original_text": ...}

The first turns Hindi/English/Hinglish meeting audio into English Minutes of Meeting; the
second translates PDF / DOCX / DOC / TXT documents between Hindi and English; the third
transcribes an audio or video file in the language spoken and translates it into the other. They share
llama-service and the vLLM behind it, and nothing else — see core/translate_doc.py.

THEIR CONTRACTS ARE OPPOSITE, DELIBERATELY. The MoM endpoint never returns its input text;
the translation endpoint returns nothing but. There is no inconsistency: the transcript
rule protects an internal intermediate the caller never asked for and cannot audit,
whereas a translation IS the deliverable and its source was supplied by the caller.

THE TRANSCRIPT IS INTERNAL. It is produced, filtered and handed to the LLM, and it is
never returned. That is a deliberate contract, not an omission — see the response builder
in transcribe_and_generate_mom() and the note in core/mom.to_mom_response().

100% offline. This process makes exactly two kinds of outbound call, both to model
servers on the local docker network: whisper-server for speech, llama-service for the
minutes. There is no cloud client and no API key anywhere in this service.

    Audio upload
        ↓  ffmpeg — decode any container to 16 kHz mono PCM
    Offline audio preprocessing
        ↓  whisper.cpp large-v3 on CUDA, <|translate|> task
    Offline speech-to-text
        ↓  hallucination / repetition filtering
        ↓  NeMo TitaNet — who spoke when — merged in by timestamp
    English transcription, speaker-labelled  ← internal only, never leaves this process
        ↓  gpt-oss-120b via vLLM, reusing llama-service's MoM prompts
    English MoM
        ↓
    Return ONLY the MoM
"""
import logging
import os
import tempfile
import time
from typing import Dict, Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from config import (
    ENABLE_DIARIZATION,
    ENABLE_LANG_DETECT,
    ENABLE_SPEAKER_NAMING,
    ENABLE_TRANSCRIBE_RETRY,
    ENABLE_TRANSCRIPT_CORRECTION,
    ENABLE_NOISE_REDUCTION,
    ENABLE_OCR,
    FILTER_HALLUCINATIONS,
    LLAMA_URL,
    MAX_DOC_MB,
    MAX_MEDIA_MB,
    MAX_UPLOAD_MB,
    MIN_TRANSCRIPT_CHARS,
    NEMO_URL,
    OCR_LANGS,
    OCR_MAX_PAGES,
    TRANSCRIBE_RETRY_LOST_FRACTION,
    TRANSCRIBE_RETRY_LOST_S,
    TRANSCRIBE_API_URL,
    WHISPERCPP_URL,
)
from core.diarize import Diarizer
from core.engine import TranscriptionEngine
from core.mom import MomGenerator, to_mom_response
from core.translate_doc import (
    OCR_LANG_BY_CANONICAL,
    SUPPORTED_LANGS,
    DocumentTranslator,
    TranslationError,
    detect_language,
    normalize_lang,
)
from utils.audio_processing import media_file_to_wav, normalize_audio
from utils.documents import SUPPORTED_EXTENSIONS, DocumentError, extract_text_blocks
from utils.formatting import label_transcript, apply_speaker_names, speech_in_gaps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Offline Transcription + MoM API",
    description="Hindi / English / Hinglish audio → English transcript → English Minutes of Meeting. Fully offline.",
    version="2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Both are stateless clients built once at import. Neither holds a model: ggml-large-v3
# lives in whisper-server and gpt-oss-120b lives in vLLM, each loaded exactly once at its
# own container's startup and reused for the life of that container.
engine = TranscriptionEngine(WHISPERCPP_URL)
diarizer = Diarizer(NEMO_URL)
mom_generator = MomGenerator(LLAMA_URL)
# Same llama-service, same vLLM, different endpoint — document translation adds no model
# and no container. See core/translate_doc.py.
translator = DocumentTranslator(LLAMA_URL)


@app.on_event("startup")
async def startup_event():
    asr_ok = engine.is_ready()
    mom_ok = mom_generator.is_ready()
    dia_ok = diarizer.is_ready() if ENABLE_DIARIZATION else None
    logger.info(f"whisper.cpp : {WHISPERCPP_URL} ({'reachable' if asr_ok else 'NOT REACHABLE'})")
    logger.info(f"diarizer    : {NEMO_URL} ("
                f"{'disabled' if dia_ok is None else 'reachable' if dia_ok else 'NOT REACHABLE'})")
    logger.info(f"MoM writer  : {LLAMA_URL} ({'reachable' if mom_ok else 'NOT REACHABLE'})")
    # Warn, don't crash: compose may still be starting the model servers, and /health
    # reports live state anyway.
    if not (asr_ok and mom_ok):
        logger.warning("a model server is not reachable yet — affected endpoints will 503 until it is")
    logger.info(
        f"✓ Ready | denoise={ENABLE_NOISE_REDUCTION} filter={FILTER_HALLUCINATIONS} "
        f"max_upload={MAX_UPLOAD_MB}MB"
    )


# /health fans out to every dependency, so it is far too expensive to run on every call. A short
# cache makes a burst of probes cost one round trip instead of N — the pile-up that wedged this
# service came from exactly such a burst.
_HEALTH_CACHE: dict = {"at": 0.0, "body": None}
_HEALTH_TTL = 10.0


@app.get("/")
def root():
    """Static liveness route. No dependencies, no I/O — safe to probe every few seconds."""
    return {"service": "offline-mom-api", "status": "up"}


@app.get("/health")
def health():
    now = time.monotonic()
    if _HEALTH_CACHE["body"] is not None and now - _HEALTH_CACHE["at"] < _HEALTH_TTL:
        return _HEALTH_CACHE["body"]
    asr_ok = engine.is_ready()
    llm = mom_generator.health_detail()
    # llama-service reports which backend it RESOLVED to. Surfacing it here is what makes
    # "is the whole pipeline really offline?" answerable with one call — a llama-service
    # accidentally left pointing at Groq would show mode="online" right here instead of
    # being invisible until someone read the container's environment.
    llm_ok = llm.get("status") == "healthy"
    llm_offline = llm.get("mode") == "offline"
    body = {
        "status": "healthy" if (asr_ok and llm_ok) else "degraded",
        "offline": bool(llm_offline),
        "pipeline": {
            "stt": {
                "model": "remote media-transcription service" if TRANSCRIBE_API_URL else "whisper large-v3 (ggml, CUDA)",
                "task": "spoken language (remote service cannot translate)" if TRANSCRIBE_API_URL else "translate → English",
                "url": TRANSCRIBE_API_URL or WHISPERCPP_URL,
                "reachable": asr_ok,
            },
            "diarization": {
                "model": "NeMo TitaNet",
                "enabled": ENABLE_DIARIZATION,
                "url": NEMO_URL,
                # Not part of `status`: diarization improves the MoM but is not required
                # to produce one, so it degrades rather than failing the service.
                "reachable": diarizer.is_ready() if ENABLE_DIARIZATION else False,
            },
            "mom": {
                "model": (llm.get("llm") or {}).get("model"),
                "backend": (llm.get("llm") or {}).get("backend"),
                "url": LLAMA_URL,
                "llm_url": (llm.get("llm") or {}).get("url"),
                "mode": llm.get("mode"),
                "reachable": llm_ok,
            },
            # Document translation shares llama-service with the MoM writer, so it has no
            # reachability of its own — `mom.reachable` above is the same probe. What is
            # worth reporting is which INPUTS this build can actually accept: OCR and the
            # legacy .doc converter are both optional system packages, and whether they
            # made it into the image is otherwise invisible until an upload fails.
            "translation": {
                "languages": list(SUPPORTED_LANGS),
                "formats": list(SUPPORTED_EXTENSIONS),
                "ocr": {"enabled": ENABLE_OCR, "languages": OCR_LANGS},
                "url": LLAMA_URL,
                "reachable": llm_ok,
            },
        },
    }
    _HEALTH_CACHE.update(at=now, body=body)
    return body


def _refine(transcript: str, known_names: Dict[str, str]) -> str:
    """Last step before the transcript leaves for the MoM writer: repair ASR garble.

    Kept separate from _audio_to_english's happy path because it must run on BOTH the
    diarized and the un-diarized branch, and because it is the one stage here that REWRITES
    what was said rather than adding to it. Everything it can do wrong, it does silently, so
    both this call and llama-service itself fall back to the untouched text on any doubt.
    """
    if not (ENABLE_TRANSCRIPT_CORRECTION and transcript.strip()):
        return transcript
    corrected = mom_generator.correct_transcript(transcript, sorted(set(known_names.values())))
    if corrected != transcript:
        logger.info(f"[REQ] ✓ transcript corrected: {len(transcript)} → {len(corrected)} chars")
    return corrected


def _audio_to_english(audio: UploadFile) -> str:
    """Upload → preprocess → English transcript, speaker-labelled where possible.

    Returns the text that goes to the LLM and nowhere else. When diarization succeeds the
    text is turn-structured:

        [Speaker_1] Hello everyone, my name is Rahul...
        [Speaker_2] Yes Rahul, our API migration is 80% complete...

    and when it does not, the same plain paragraph as before. Both are valid input to
    llama-service's prompt — it reads bracket tags when present and copes without them.
    """
    if not engine.is_ready():
        raise HTTPException(
            status_code=503,
            detail=f"Local whisper server not reachable at {WHISPERCPP_URL}",
        )

    # .file, not `await audio.read()` — this function is sync so it reads the underlying
    # spooled file directly (same reason as translate_document).
    raw = audio.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio file")

    size_mb = len(raw) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"Audio is {size_mb:.0f}MB, limit is {MAX_UPLOAD_MB}MB")

    logger.info(f"[REQ] {audio.filename} ({size_mb:.1f}MB)")

    try:
        wav_bytes = normalize_audio(raw, denoise=ENABLE_NOISE_REDUCTION)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp.write(wav_bytes)
            temp_file = tmp.name

        # Pick the decode task from the actual language. English goes through <|transcribe|>,
        # everything else through <|translate|> exactly as before.
        translate = True
        if ENABLE_LANG_DETECT:
            lang = engine.detect_language(temp_file)
            if lang == "en":
                translate = False
                logger.info("[REQ] English detected — using the transcribe task (no language switch)")
        result = engine.transcribe_to_english(temp_file, translate=translate)
        result = engine.clean(result, FILTER_HALLUCINATIONS)
        transcription = result["text"].strip()
        logger.info(
            f"[REQ] ✓ transcribed {audio.filename}: {len(transcription)} chars from "
            f"{result['duration']:.1f}s of {result['source_language']} audio"
        )

        if not (ENABLE_DIARIZATION and transcription):
            return _refine(transcription, {})

        # Diarize the SAME normalised wav the transcript came from, so both sets of
        # timestamps share one clock — the merge below is pure timestamp overlap, and it
        # would silently mis-assign speakers if the two stages saw different audio.
        #
        # Sequential, not concurrent: whisper-server and TitaNet contend for the same GPU,
        # so overlapping them buys nothing and risks memory pressure alongside vLLM.
        speaker_segments = diarizer.diarize(temp_file)
        if not speaker_segments:
            logger.info("[REQ] no speaker segments — using untagged transcript")
            return transcription

        # WHISPER SOMETIMES SKIPS WHOLE STRETCHES OF SPEECH, and nothing downstream can recover
        # words that were never written. Measured 2026-09-10: the same 29-minute recording, four
        # runs, identical bytes — three were fine, one fell into a degenerate state from ~1085 s
        # and left a 10 s hole in every 30 s window, swallowing an attendee introducing himself.
        # That is why the same meeting named a speaker "Amar" on one run and "Fernando" on the next.
        # Turning off temperature fallback was measured and is WORSE (45.7% duplicate lines), so
        # the fix is to detect the bad run and do it again. A whole second pass rather than
        # patching the holes, because the text between the holes is degraded too.
        if ENABLE_TRANSCRIBE_RETRY:
            limit = max(TRANSCRIBE_RETRY_LOST_S, TRANSCRIBE_RETRY_LOST_FRACTION * float(result.get("duration") or 0.0))
            lost = speech_in_gaps(result["segments"], speaker_segments)
            if lost > limit:
                logger.warning(f"[REQ] transcript skipped {lost:.0f}s of speech the diarizer heard "
                               f"(limit {limit:.0f}s) — re-transcribing once")
                try:
                    retry = engine.clean(engine.transcribe_to_english(temp_file, translate=translate),
                                         FILTER_HALLUCINATIONS)
                    lost_retry = speech_in_gaps(retry["segments"], speaker_segments)
                    if lost_retry < lost and retry["text"].strip():
                        result, transcription = retry, retry["text"].strip()
                        logger.info(f"[REQ] ✓ retry kept — {lost_retry:.0f}s skipped (was {lost:.0f}s), "
                                    f"{len(transcription)} chars")
                    else:
                        logger.warning(f"[REQ] retry no better ({lost_retry:.0f}s skipped) — keeping the first")
                except Exception as e:
                    logger.warning(f"[REQ] re-transcription failed ({e}) — keeping the first transcript")

        labelled, n_speakers = label_transcript(result["segments"], speaker_segments)
        if not labelled.strip():
            return transcription
        logger.info(f"[REQ] ✓ speaker-labelled transcript: {n_speakers} speakers")

        # Name the clusters BEFORE correcting, so the corrector gets a roster to anchor
        # mangled proper nouns against — that ordering is why correct_transcript takes a
        # known_names argument at all.
        names = {}
        if ENABLE_SPEAKER_NAMING:
            named, names = apply_speaker_names(labelled, mom_generator.identify_speakers(labelled))
            if names:
                labelled = named
        return _refine(labelled, names)
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except OSError as e:
                logger.warning(f"Could not delete temp file: {e}")


@app.post("/transcribe-and-generate-mom")
def transcribe_and_generate_mom(audio: UploadFile = File(...)):
    """Hindi/English/Hinglish audio → English Minutes of Meeting.

    The English transcript is generated internally and passed to the LLM, but is not part
    of the response.

    SYNC (`def`), NOT `async def` — load-bearing, and it was a real outage. Every call this
    pipeline makes is blocking `requests` I/O: whisper, the diarizer, the speaker-namer and
    the MoM writer. On the event loop that blocks the WHOLE process for the length of the
    job, which for a 9-minute recording is several minutes. MEASURED 2026-09-08: while one
    such upload ran, /health stopped answering entirely (90 s, no response) and an OPTIONS
    preflight from a browser returned nothing at all — so a frontend calling any endpoint
    saw only "NetworkError when attempting to fetch resource", and the container went on
    reporting healthy from its last successful probe. A sync handler runs in FastAPI's
    threadpool instead, so uploads no longer block each other or /health.
    """
    if not mom_generator.is_ready():
        raise HTTPException(status_code=503, detail=f"Local MoM service not reachable at {LLAMA_URL}")

    try:
        transcription = _audio_to_english(audio)

        # Guard the LLM against input it cannot make minutes from. Without this a silent
        # or one-sentence clip still gets a full MoM, and every field in it is invented.
        if len(transcription) < MIN_TRANSCRIPT_CHARS:
            logger.warning(f"[REQ] transcript too short for a MoM ({len(transcription)} chars)")
            # Report the LENGTH, never the text — the caller needs to know why no minutes
            # came back, and a character count says that without returning the transcript.
            return {
                "success": True,
                "mom": None,
                "note": (
                    f"Audio produced only {len(transcription)} characters of speech, below the "
                    f"{MIN_TRANSCRIPT_CHARS}-character minimum for MoM generation. "
                    "Minutes were not generated because there is not enough content to summarise."
                ),
            }

        mom = to_mom_response(mom_generator.generate(transcription))

        # An all-empty MoM means the model found no meeting content to report — correct
        # behaviour on audio that is not a meeting (a speech, a song, one stray sentence),
        # since the prompt forbids inventing anything the transcript does not state. But
        # returning success with every field blank tells the caller nothing about WHY, so
        # say it explicitly instead. Mirrors the too-short guard above.
        if not any((mom["summary"].strip(), mom["key_points"], mom["decisions"], mom["action_items"])):
            # Empty STRUCTURED fields do not by themselves mean the audio had no meeting. When
            # llama-service's JSON pipeline fails it falls back to a legacy TEXT pipeline that
            # returns no `content` object at all — only rendered minutes. Reporting that as "no
            # meeting content" is simply false, and it throws away a complete document: measured
            # 2026-09-07, a 17-minute committee meeting transcribed cleanly (12.3k chars, 6
            # speakers), rendered 3,931 chars of minutes, and was returned to the caller as null.
            # So distinguish the two cases by what actually came back.
            if mom["formatted"].strip():
                logger.warning(
                    "[REQ] structured MoM fields empty but %d chars rendered — returning the "
                    "document; llama-service fell back to its legacy text pipeline",
                    len(mom["formatted"]),
                )
                return {
                    "success": True,
                    "mom": mom,
                    "note": (
                        "Minutes were generated, but only as a rendered document: the structured "
                        "fields (key_points, decisions, action_items) could not be extracted for "
                        "this recording. Read `mom.formatted` for the full minutes."
                    ),
                }
            logger.warning("[REQ] MoM came back empty — audio has no meeting content")
            return {
                "success": True,
                "mom": None,
                "note": (
                    "No minutes could be generated: the audio was transcribed successfully but "
                    "contains no meeting content (no discussion, decisions or action items) to "
                    "summarise."
                ),
            }

        logger.info(
            f"[REQ] ✓ MoM: {len(mom['key_points'])} key points, "
            f"{len(mom['decisions'])} decisions, {len(mom['action_items'])} action items"
        )
        return {"success": True, "mom": mom}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── document translation ─────────────────────────────────────────────────────
# A SEPARATE product from the MoM pipeline above, sharing its LLM. Note the contracts are
# opposite by design: the MoM endpoint never returns its input text, while this one
# returns nothing else. There is no conflict — the transcript rule protects an internal
# intermediate the caller never asked for, whereas here the translated text IS the
# deliverable and the caller supplied the source themselves.


def _resolve_languages(text: str, source_lang: Optional[str], target_lang: Optional[str]):
    """Decide the (source, target) pair, filling in whatever the caller left out.

    Both parameters are optional because the overwhelmingly common request is "translate
    this document" — the language is a property of the file, not a decision the caller
    should have to make. Naming only one is enough, since with two supported languages the
    other is implied; naming neither detects the source by script and flips it.
    """
    source = normalize_lang(source_lang, "source_lang")
    target = normalize_lang(target_lang, "target_lang")

    detected = False
    if source is None:
        source = detect_language(text)
        detected = True
    if target is None:
        target = "English" if source == "Hindi" else "Hindi"
    return source, target, detected


# ── /translate-media ──────────────────────────────────────────────────────────────────────────────
# Audio or video in, translated text out: Hindi speech becomes English, English speech becomes Hindi.
#
# ONE ROUTE FOR BOTH DIRECTIONS: Whisper transcribes in the language that was SPOKEN, then the same
# document translator turns that text into the other language. Whisper's own translate task was the
# tempting shortcut for Hindi → English, but it only ever translates INTO English and never returns
# the original words, and the caller wants both.
from core.translate_doc import (TranslationError, describe_media_translation,  # noqa: E402
                                detect_language, normalize_lang)

# Whisper's language id regularly labels Hindi speech as Urdu, and an unpinned transcribe task then
# writes URDU SCRIPT — which the Hindi translator's script checks cannot read. Both are Hindi here.
_PROBE_TO_LANG = {"hi": "Hindi", "ur": "Hindi", "en": "English"}
_WHISPER_CODE = {"Hindi": "hi", "English": "en"}


def _mostly_other_script(text: str) -> bool:
    """True when most letters are neither Devanagari nor Latin — speech we cannot translate.

    detect_language() only weighs Devanagari against Latin, so an Urdu or Tamil transcript, with
    neither, would fall through to "English" and be sent to the model as English. This catches it.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    known = sum(1 for ch in letters if "\u0900" <= ch <= "\u097f" or (ch.isascii()))
    return known / len(letters) < 0.5


def _transcript_blocks(segments, limit: int = 600):
    """Consecutive Whisper segments joined into paragraph-sized blocks.

    A segment is a few seconds of speech, often half a sentence. Translated one by one, each loses
    the context that decides its meaning; joined into ~600-character paragraphs the model sees whole
    thoughts, and the original and the translation stay paragraph-for-paragraph comparable.
    """
    blocks, current = [], ""
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if current and len(current) + 1 + len(text) > limit:
            blocks.append(current)
            current = text
        else:
            current = f"{current} {text}".strip()
    if current:
        blocks.append(current)
    return blocks


@app.post("/translate-media")
def translate_media(
    file: UploadFile = File(...),
    source_lang: Optional[str] = Form(None),
    target_lang: Optional[str] = Form(None),
):
    """Audio or video → transcript in the spoken language → the other language. Hindi ⇄ English.

    Both language fields are optional: the spoken language is detected, and the target is the other
    one. SYNC on purpose, like every handler here — each step blocks for minutes.
    """
    import shutil

    if not engine.is_ready():
        raise HTTPException(status_code=503, detail=f"Local whisper server not reachable at {WHISPERCPP_URL}")
    if not translator.is_ready():
        raise HTTPException(status_code=503, detail=f"Local translation service not reachable at {LLAMA_URL}")
    try:
        requested_source = normalize_lang(source_lang, "source_lang")
        requested_target = normalize_lang(target_lang, "target_lang")
    except TranslationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    filename = file.filename or "media"
    upload_path = wav_path = None
    try:
        # To disk, never into memory: a video can be gigabytes. Starlette has already spooled large
        # uploads to a temp file; this copies it in 1 MB pieces rather than reading it whole.
        suffix = os.path.splitext(filename)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp, length=1024 * 1024)
            upload_path = tmp.name
        size_mb = os.path.getsize(upload_path) / (1024 * 1024)
        if size_mb == 0:
            raise HTTPException(status_code=400, detail="Empty file")
        if size_mb > MAX_MEDIA_MB:
            raise HTTPException(status_code=413, detail=f"File is {size_mb:.0f}MB, limit is {MAX_MEDIA_MB}MB")
        logger.info(f"[MEDIA] {filename} ({size_mb:.1f}MB)")

        try:
            wav_path = media_file_to_wav(upload_path, denoise=ENABLE_NOISE_REDUCTION)
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        os.unlink(upload_path)          # a large video's disk is freed as soon as its audio is out
        upload_path = None

        # 1. Which language was spoken. A caller who knows can say; otherwise Whisper probes.
        source, probe = requested_source, ""
        if source is None and ENABLE_LANG_DETECT:
            probe = engine.detect_language(wav_path)
            source = _PROBE_TO_LANG.get(probe)
            if probe and source is None:
                raise HTTPException(status_code=422, detail=(
                    f"The speech was detected as {probe!r}. Only Hindi and English are supported."))

        # 2. Transcribe IN that language — pinned, so Hindi comes back in Devanagari, not Urdu script.
        result = engine.transcribe_to_english(wav_path, translate=False, language=_WHISPER_CODE.get(source))
        result = engine.clean(result, FILTER_HALLUCINATIONS)
        blocks = _transcript_blocks(result.get("segments")) or [result["text"].strip()]
        original = "\n\n".join(b for b in blocks if b)
        if not original.strip():
            raise HTTPException(status_code=422, detail="No speech could be transcribed from this file.")

        # 3. An unsure probe leaves the language to the transcript's own script.
        detected = requested_source is None
        if source is None:
            if _mostly_other_script(original):
                raise HTTPException(status_code=422, detail=(
                    "The speech is in neither Hindi nor English. Only Hindi and English are supported."))
            source = detect_language(original)
        target = requested_target or ("English" if source == "Hindi" else "Hindi")

        # 4. Translate, with the same script checks, retries and honest reporting as documents.
        try:
            translated = translator.translate_blocks(blocks, source, target)
        except TranslationError as e:
            raise HTTPException(status_code=502, detail=str(e))

        notes = []
        if detected:
            notes.append(f"The spoken language was detected as {source}.")
        if translated.untranslated:
            notes.append(
                f"{translated.untranslated} of {translated.chunks} passage(s) could not be translated and "
                f"are given in {source} ({', '.join(f'{k}: {v}' for k, v in sorted(translated.failure_reasons.items()))}).")
        logger.info(f"[MEDIA] ✓ {filename}: {source} → {target}, {result.get('duration', 0):.0f}s of audio, "
                    f"{translated.translated}/{translated.chunks} passages, {translated.untranslated} untranslated")
        response = {
            "success": True,
            "source_lang": source,
            "target_lang": target,
            "language_detected": detected,
            "duration_s": result.get("duration"),
            "original_text": original,
            "translated_text": translated.text,
            "notes": notes,
            "stats": {"passages": translated.chunks, "translated": translated.translated,
                      "untranslated": translated.untranslated, "model_calls": translated.model_calls},
        }
        # The chat reply, in the language of the translation — see describe_media_translation.
        response["description"] = describe_media_translation(filename, response)
        return response
    finally:
        for path in (upload_path, wav_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


@app.post("/translate-document")
def translate_document(
    file: UploadFile = File(...),
    source_lang: Optional[str] = Form(None),
    target_lang: Optional[str] = Form(None),
):
    """PDF / DOCX / DOC / TXT → translated text. Hindi ⇄ English, fully offline.

    Both language parameters are optional: the source is detected from the document's
    script and the target defaults to the other language.

    DEFINED WITH `def`, NOT `async def`, AND THAT IS DELIBERATE. Everything this handler
    does is blocking — extraction, OCR, and minutes of waiting on the model — so on the
    event loop it would stall every other request in the process. Nothing would notice
    except the one thing that matters: the container's HEALTHCHECK curls /health every 30s
    with a 10s timeout, so a document taking longer than that would mark this service
    unhealthy while it is working perfectly. A sync handler runs in FastAPI's threadpool
    and keeps /health answering.
    """
    if not translator.is_ready():
        raise HTTPException(
            status_code=503, detail=f"Local translation service not reachable at {LLAMA_URL}"
        )

    # .file rather than `await file.read()` — this handler is sync (see the docstring), so
    # it reads the underlying spooled file directly.
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    size_mb = len(raw) / (1024 * 1024)
    if size_mb > MAX_DOC_MB:
        raise HTTPException(
            status_code=413, detail=f"Document is {size_mb:.0f}MB, limit is {MAX_DOC_MB}MB"
        )

    filename = file.filename or "document"
    extension = os.path.splitext(filename)[1].lower()
    logger.info(f"[DOC] {filename} ({size_mb:.2f}MB)")

    # Validate the language arguments BEFORE extracting, so a typo in source_lang fails in
    # milliseconds instead of after a multi-minute OCR pass.
    try:
        requested_source = normalize_lang(source_lang, "source_lang")
        requested_target = normalize_lang(target_lang, "target_lang")
    except TranslationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if extension and extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{extension}'. This endpoint accepts "
                f"{', '.join(SUPPORTED_EXTENSIONS)}."
            ),
        )

    # A declared source language narrows OCR to one tesseract model, which is more
    # accurate than the combined hin+eng. Undeclared, OCR has to consider both.
    ocr_lang = OCR_LANG_BY_CANONICAL.get(requested_source or "", OCR_LANGS)

    try:
        extraction = extract_text_blocks(raw, filename, ocr_lang=ocr_lang)
    except DocumentError as e:
        # 422, not 400: the request was well-formed and the file arrived intact — this
        # service simply cannot get text out of it. The message says which case it is.
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"[DOC] extraction failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not read {filename}: {e}")

    # PDF extraction raises on empty output itself (it knows whether OCR was tried), but a
    # .docx or .txt holding nothing but images, or nothing at all, arrives here with zero
    # blocks. Translating that would return success and an empty string.
    if not extraction.blocks:
        raise HTTPException(
            status_code=422,
            detail=f"No text could be extracted from {filename} — the document appears to be empty.",
        )

    logger.info(
        f"[DOC] ✓ extracted {extraction.chars} chars in {len(extraction.blocks)} blocks "
        f"({extraction.source}"
        + (f", {extraction.ocr_pages} page(s) OCR'd" if extraction.ocr_pages else "")
        + ")"
    )

    sample = " ".join(extraction.blocks[:40])
    source, target, detected = _resolve_languages(sample, source_lang, target_lang)

    try:
        result = translator.translate_blocks(extraction.blocks, source, target)
    except TranslationError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"[DOC] translation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    # Notes are how this endpoint stays honest about partial results. An untranslated
    # chunk is returned AS ITS SOURCE TEXT — a hole in the document would be worse — which
    # means the response is otherwise indistinguishable from a complete translation. Say so.
    notes = []
    if detected:
        notes.append(f"Source language was not specified; detected as {source} from the document's script.")
    if source == target:
        notes.append(
            f"Source and target are both {source}, so the text was returned unchanged. "
            "Pass target_lang explicitly to translate."
        )
    if extraction.ocr_pages:
        notes.append(
            f"{extraction.ocr_pages} of {extraction.pages} page(s) had no text layer and were "
            f"read with OCR, which is less accurate than extracted text."
        )
    # Pages that were neither read nor OCR'd are pages whose content is NOT in this
    # response — the OCR budget ran out, or OCR is off and they were scans. Nothing else
    # in the payload would reveal that, so it cannot be left to the log.
    if extraction.pages:
        skipped = extraction.pages - extraction.text_layer_pages - extraction.ocr_pages
        if skipped > 0:
            notes.append(
                f"{skipped} page(s) were skipped and are NOT included: they had no text layer "
                + ("and OCR is disabled on this service."
                   if not ENABLE_OCR else
                   f"and the {OCR_MAX_PAGES}-page OCR budget was already spent. Raise "
                   "OCR_MAX_PAGES to include them.")
            )
    if result.untranslated:
        notes.append(
            f"{result.untranslated} of {result.chunks} text chunk(s) could not be translated "
            f"and are returned in the original language "
            f"({', '.join(f'{k}: {v}' for k, v in sorted(result.failure_reasons.items()))})."
        )

    logger.info(
        f"[DOC] ✓ {filename}: {source} → {target}, {result.translated}/{result.chunks} chunks "
        f"in {result.model_calls} model call(s), {result.untranslated} untranslated"
    )

    return {
        "success": True,
        "source_lang": source,
        "target_lang": target,
        "source_lang_detected": detected,
        "translated_text": result.text,
        "document": {
            "filename": filename,
            "format": extraction.source,
            "pages": extraction.pages,
            "ocr_pages": extraction.ocr_pages,
            "text_layer_pages": extraction.text_layer_pages,
            "blocks": result.blocks,
            "characters": extraction.chars,
        },
        "stats": {
            "chunks": result.chunks,
            "translated": result.translated,
            "untranslated": result.untranslated,
            "model_calls": result.model_calls,
            "repeated_chunks_reused": result.cached_duplicates,
            # Code spans (paths, commands, identifiers, URLs) hidden from the model and
            # restored verbatim afterwards, and the chunks that turned out to be nothing
            # but code and so never needed a model call at all.
            "protected_code_spans": result.protected_spans,
            "code_only_chunks": result.code_only_chunks,
            "failure_reasons": result.failure_reasons,
            # Up to five samples of what came back untranslated, so a caller can see WHICH
            # parts to check rather than diffing the whole document against its source.
            "untranslated_samples": result.untranslated_samples,
        },
        **({"note": " ".join(notes)} if notes else {}),
    }


# NOTE: there is deliberately no transcription-returning endpoint. An earlier revision
# exposed POST /transcribe; it was removed because "the transcript is internal" is not a
# property you can claim while also serving it on a public route.


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
