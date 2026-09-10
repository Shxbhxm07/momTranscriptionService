import asyncio
import json
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import uvicorn
from models.schemas import SummarizeRequest, TranslateRequest, TranslateBatchRequest, TranslateMomRequest, LocalizeMomRequest, SpeakerMapRequest, CorrectTranscriptRequest
from core.llm_manager import LLMManager
from core.translation_cache import translation_cache, make_translation_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Llama Meeting Intelligence Service",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

llm_manager = LLMManager()

@app.on_event("startup")
async def startup_event():
    try:
        llm_manager.load_model()
        logger.info("✓ Service ready (vLLM API mode)")
    except Exception as e:
        logger.error(f"Startup error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise

@app.get("/")
async def root():
    return {
        "service": "Llama Meeting Intelligence",
        "version": "2.0",
        "model": "vLLM API Proxy",
        "features": {
            "summarization": True,
            "translation": True,
        }
    }

def _backend_name(api_base: str) -> str:
    """Name the remote provider so /health says WHO is receiving the transcripts."""
    from urllib.parse import urlparse
    host = (urlparse(api_base or "").hostname or "").lower()
    for vendor in ("groq", "openrouter", "together", "fireworks", "anthropic", "openai"):
        if vendor in host:
            return vendor
    return host or "remote"


def _is_remote(api_base: str) -> bool:
    """True when the LLM endpoint is somewhere other than this machine/compose network.

    This decides what /health reports, and it used to be `"groq" in api_base` — a substring
    check that answers "is this Groq", not "does audio leave this box". Point the service at
    ANY other provider (OpenRouter, Together, a hosted vLLM) and /health would cheerfully
    report mode=offline while shipping every transcript to a third party. For a product whose
    entire promise is that recordings never leave the machine, that is the one thing /health
    must never get wrong, so decide it from the HOST instead of a vendor name.
    """
    from urllib.parse import urlparse
    host = (urlparse(api_base or "").hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return False
    # Compose service names ("vllm") and private ranges are the local stack.
    if "." not in host:
        return True if host in ("api", "openrouter") else False
    import ipaddress
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True          # a public DNS name → remote


# WHY THESE HANDLERS ARE SYNC `def`, NOT `async def`
# Every llm_manager call below is blocking: httpx sync client, plus ThreadPoolExecutor joins for
# the windowed passes. In FastAPI a blocking call inside `async def` runs ON the event loop and
# freezes the whole process — measured 2026-09-10, an 18-minute /summarize made the container
# answer nothing at all, and its own "/" healthcheck timed out 16 times in a row and marked it
# unhealthy while the job was in fact running fine.
# A plain `def` handler is run by FastAPI in the anyio worker threadpool instead, so the loop
# stays free to serve "/" and the probes. This matters most in the client's OpenShift deployment:
# Docker's `restart: unless-stopped` ignores health, but a Kubernetes liveness probe does not —
# it would kill the pod mid-meeting, every meeting.
# Keep `async def` only for handlers that await (`/translate`, `/translate_batch`) or do no
# blocking work (`/`, `/summarize/stream`, which hands Starlette a sync generator).

@app.get("/health")
def health():
    # Reports WHICH backend this service resolved to, not just whether it answered. Cloud-vs-local
    # is derived from VLLM_API_BASE alone (config.py), so there is no flag to read back — without
    # this, "am I on Groq or the local vLLM?" can only be answered by reading pod environment
    # variables, which is exactly what made a mode mismatch invisible on OpenShift.
    #
    # llm_manager.model_id is used rather than the configured LLM_MODEL_PATH because load_model()
    # falls back to whatever the server actually serves when the configured name is absent. Showing
    # the effective model means that silent substitution is visible here too.
    from config import VLLM_API_BASE, LLM_MODEL_PATH
    healthy = llm_manager.is_healthy()
    is_cloud = _is_remote(VLLM_API_BASE)
    return {
        "status": "healthy" if healthy else "loading",
        "api_connected": healthy,
        "mode": "online" if is_cloud else "offline",
        "llm": {
            "backend": _backend_name(VLLM_API_BASE) if is_cloud else "vllm",
            "url": VLLM_API_BASE,
            "model": getattr(llm_manager, "model_id", LLM_MODEL_PATH),
            "model_configured": LLM_MODEL_PATH,
        },
    }

@app.post("/summarize")
def summarize(request: SummarizeRequest):
    try:
        return llm_manager.generate_mom(request.text, request.temperature, request.output_lang, request.metadata)
    except Exception as e:
        logger.error(f"[MOM] Failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/localize-mom")
def localize_mom(request: LocalizeMomRequest):
    """Design 2: localize an already-generated English MoM `content` (structured) into target_lang —
    translate content values + render config-driven labels. Lets the backend cache the English content
    once and localize per language."""
    try:
        mom = llm_manager.localize_mom(request.content, request.target_lang, request.metadata, request.temperature)
        return {"mom": mom}
    except Exception as e:
        logger.error(f"[LOCALIZE] Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/summarize/stream")
async def summarize_stream(request: SummarizeRequest):
    def generate():
        try:
            for sse_chunk in llm_manager.generate_mom_sse(request.text, request.temperature, request.output_lang):
                yield sse_chunk
        except Exception as e:
            logger.error(f"[MOM STREAM] Failed: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/translate-mom")
def translate_mom(request: TranslateMomRequest):
    """Translate a FINISHED English MoM into target_lang (structure-preserving). Lets the caller
    translate a cached English base into any language without re-generating the MoM."""
    try:
        translated = llm_manager.translate_mom(request.text, request.target_lang, request.temperature)
        return {"mom": translated}
    except Exception as e:
        logger.error(f"[TRANSLATE-MOM] Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/summarize_test")
def summarize_test(text:str,temperature: float = 0.05):
    try:
        return llm_manager.generate_mom(text, temperature)
    except Exception as e:
        logger.error(f"[MOM] Failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/identify-speakers")
def identify_speakers(request: SpeakerMapRequest):
    try:
        return llm_manager.identify_speakers(request.text, request.temperature)
    except Exception as e:
        logger.error(f"[SPEAKER_ID] Failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/correct-transcript")
def correct_transcript(request: CorrectTranscriptRequest):
    try:
        corrected = llm_manager.correct_transcript(request.text, request.temperature, request.mode, request.known_names)
        return {"corrected_text": corrected}
    except Exception as e:
        logger.error(f"[CORRECT] Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/translate")
async def translate(request: TranslateRequest):
    try:
        text = request.text or ""
        # LIVE-1 Phase 1: trivial / same-language → no cache, no model call.
        if not text.strip() or request.source_lang == request.target_lang:
            return {
                "translated_text": text,
                "source_lang": request.source_lang,
                "target_lang": request.target_lang,
            }

        key = make_translation_key(text, request.source_lang, request.target_lang)

        def _work():
            # context/temperature intentionally ignored (LIVE-1 R4) → deterministic & cacheable.
            return llm_manager.generate_translation(text, request.source_lang, request.target_lang)

        # C1 read-through + C2 single-flight + C3 off-loop (asyncio.to_thread inside the cache).
        translated_text, was_hit = await translation_cache.get_or_translate(key, _work)
        logger.info(f"[TRANSLATE] {'cache HIT' if was_hit else 'MISS'} {request.source_lang} → {request.target_lang}")
        return {
            "translated_text": translated_text,
            "source_lang": request.source_lang,
            "target_lang": request.target_lang,
        }
    except Exception as e:
        logger.error(f"[TRANSLATE] Failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/translate_batch")
async def translate_batch(request: TranslateBatchRequest):
    """Translate many segments in ONE model call (live-viewer fan-out). Returns
    `translations` aligned to `texts` (null when an item couldn't be translated) plus
    `reasons` aligned the same way: null on success, else 'transport' (retry generously)
    or a validation reason 'echo'/'wrong_script'/'mixed_script'/'empty' (retry sparingly),
    so the caller can size each item's retry budget without a second pipeline."""
    try:
        texts = request.texts or []
        if not texts:
            return {"translations": [], "reasons": [], "source_lang": request.source_lang, "target_lang": request.target_lang}
        # Run the blocking model call off the event loop.
        translations, reasons = await asyncio.to_thread(
            llm_manager.generate_translation_batch, texts, request.source_lang, request.target_lang
        )
        hits = sum(1 for t in translations if t is not None)
        logger.info(f"[TRANSLATE-BATCH] {hits}/{len(texts)} translated {request.source_lang} → {request.target_lang}")
        return {
            "translations": translations,
            "reasons": reasons,
            "source_lang": request.source_lang,
            "target_lang": request.target_lang,
        }
    except Exception as e:
        logger.error(f"[TRANSLATE-BATCH] Failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
