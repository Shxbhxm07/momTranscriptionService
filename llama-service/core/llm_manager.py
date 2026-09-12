import logging
import httpx
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
import time

from config import (APP_MODE, LLM_MODEL_PATH, VLLM_API_BASE, MAX_INPUT_TOKENS,
                    MAX_NEW_TOKENS_PER_CHUNK, MAX_NEW_TOKENS_SYNTHESIS, MODEL_CONTEXT_LIMIT,
                    MAX_NEW_TOKENS_EXTRACTION, MAX_NEW_TOKENS_CLASSIFY, MOM_MERGE_ITEMS, MOM_VERIFY_DECISIONS,
                    LLM_MODEL_LONG, VLLM_API_BASE_LONG, LLM_LONG_THRESHOLD_TOKENS,
                    MODEL_CONTEXT_LIMIT_LONG, LLM_PROVIDER_ORDER, WATSONX_PROJECT_ID,
                    WATSONX_VERSION, IBM_IAM_URL, LLM_AUTH_MODE, LLM_VERIFY_SSL,
                    CP4D_AUTH_URL, CP4D_USERNAME, CP4D_API_KEY, CP4D_TOKEN_TTL, MOM_WINDOW_KEY_POINTS, WINDOW_CONCURRENCY,
                    LLM_CONCURRENCY)
from core.groq_key_pool import load_pool_from_env
from core.translation_validator import validate_translation
from prompts import MEETING_ANALYSIS_PROMPT, SYNTHESIS_PROMPT, MEETING_ANALYSIS_PROMPT_JSON, SYNTHESIS_PROMPT_JSON, SPEAKER_MAPPING_PROMPT, DECISIONS_EXTRACTION_PROMPT, KEY_POINTS_EXTRACTION_PROMPT, WINDOW_EXTRACTION_PROMPT, SUMMARY_FROM_POINTS_PROMPT, FIGURES_EXTRACTION_PROMPT, TRANSCRIPT_CORRECTION_PROMPT, ITEMS_MERGE_PROMPT, DECISIONS_VERIFY_PROMPT, TRANSLATED_TRANSCRIPT_CORRECTION_PROMPT, ACTION_ITEMS_EXTRACTION_PROMPT, MEETING_TYPE_CLASSIFY_PROMPT, TEMPLATE_FOCUS
from utils.text_utils import chunk_transcript, clean_mom_output, preprocess_transcript
from localization.mom_i18n import parse_and_validate, parse_content, render_mom, GLOSSARY_DNT, GLOSSARY_TERMS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Groq key pool — REQUIRED only when we actually talk to Groq.
#
# config.py already states that cloud-vs-local is "decided PURELY by VLLM_API_BASE";
# this guard predates local vLLM and was unconditional, so llama-service called
# sys.exit(1) without Groq keys EVEN WHEN every request goes to a local model and no
# Groq call is ever made. That made a genuinely air-gapped deployment impossible —
# an offline customer would have had to be shipped cloud API keys, which they can
# neither use nor validate, just to get the service to boot.
#
# Cloud behaviour is unchanged: pointed at Groq, missing keys still fail loudly here.
# ---------------------------------------------------------------------------
_IS_GROQ_BACKEND = "groq" in (VLLM_API_BASE or "").lower()

_key_pool = load_pool_from_env()
if _key_pool is None or len(_key_pool) == 0:
    if _IS_GROQ_BACKEND:
        logger.critical("[KeyPool] FATAL: no API key. The endpoint is Groq (VLLM_API_BASE=%s), which "
                        "needs a key: set WATSONX_API_KEY (or LLM_API_KEY) and restart. If you meant to "
                        "use watsonx, VLLM_API_BASE is wrong — it should point at your watsonx host.",
                        VLLM_API_BASE)
        import sys
        sys.exit(1)
    # Local/offline backend: no keys needed. Requests still send an Authorization
    # header, but a local vLLM ignores it, so a placeholder is sufficient.
    logger.info(f"[GroqPool] No Groq keys set — not required, backend is local ({VLLM_API_BASE}). Running offline.")
    _key_pool = None
else:
    logger.info(f"[GroqPool] Loaded {len(_key_pool)} keys — pool active")

# Sent as the Bearer token when there is no pool. A local vLLM does not authenticate,
# but httpx still needs some value for the header.
_OFFLINE_PLACEHOLDER_KEY = "local-no-auth"


def _pool_status() -> dict:
    """Pool stats for logging. Safe when running keyless against a local backend —
    these are only read inside 429/401 handlers, which a local vLLM should never
    trigger, but a misconfigured endpoint must not turn into an AttributeError."""
    if _key_pool is None:
        return {"available": 0, "total": 0, "cooling": 0}
    return _key_pool.pool_status()

# Estimate system prompt sizes in tokens (chars // 4) for context budget calculation
_ANALYSIS_PROMPT_TOKENS = len(MEETING_ANALYSIS_PROMPT) // 4
_SYNTHESIS_PROMPT_TOKENS = len(SYNTHESIS_PROMPT) // 4

# --- LIVE-1 R4: translation output hardening --------------------------------
# The model sometimes prepends a preamble ("Sure, here's the translation: ...")
# or replies with pure meta-text ("The translation of the NEW TEXT is not needed
# as it's already in English", "Shudh anuvaad ..."). The old 3-phrase startswith
# strip missed inline / single-line preambles. These guards strip a leading
# preamble and, on a pure refusal/meta reply, fall back to the original text so
# no meta-text ever leaks to live viewers.
_TRANSLATION_PREAMBLE_RE = re.compile(
    r'^\s*["\'`]*\s*'                                  # optional opening quote/fence
    r'(?:sure[,!.]?\s*)?'                              # "Sure, "
    r'(?:here(?:\s+is|\'s)\s+(?:the\s+)?)?'            # "here is the" / "here's"
    r'(?:translat(?:ion|ed(?:\s+text)?))'              # "translation" / "translated text"
    r'(?:\s+of\s+(?:the\s+)?new\s+text)?'              # "of the NEW TEXT"
    r'\s*(?:is)?\s*[:\-–—]+\s*',             # "is" / ":" / dashes
    re.IGNORECASE,
)
# QA-LT-2: the "text is already in ..." arm must require an actual LANGUAGE (or "the <target/same>
# language") after it, so a legitimate caption that merely begins "the text is already in the
# database/folder/file" is NOT misclassified as a refusal (which would leak untranslated source text).
_REFUSAL_LANGS = (
    r'english|hindi|hinglish|spanish|french|german|chinese|mandarin|arabic|hebrew|russian|'
    r'japanese|korean|portuguese|italian|urdu|bengali|tamil|telugu|punjabi|marathi|gujarati'
)
_TRANSLATION_REFUSAL_RE = re.compile(
    r'^\s*["\'`(]*\s*(?:the\s+)?(?:'
    r'translation\s+(?:of\s+the\s+new\s+text\s+)?is\s+not\s+(?:needed|required|necessary)|'
    r'no\s+translation\s+(?:is\s+)?(?:needed|required)|'
    r'(?:same\s+)?(?:text|sentence|content|input)\s+is\s+already\s+(?:written\s+)?in\s+'
    r'(?:the\s+(?:target|source|same|original|requested)\s+language\b|(?:' + _REFUSAL_LANGS + r')\b)|'
    r'(?:i\s+)?can(?:\'?t|not)\s+translate|'
    r'shudh\s+anuvaad'
    r')',
    re.IGNORECASE,
)


def _clean_translation_output(translated: str, original: str) -> str:
    """Strip a leading preamble; on a pure meta/refusal reply, return the original text unchanged."""
    if not translated or not translated.strip():
        return original
    cleaned = translated.strip()
    if _TRANSLATION_REFUSAL_RE.match(cleaned):
        return original
    stripped = _TRANSLATION_PREAMBLE_RE.sub('', cleaned, count=1).strip()
    # Drop a wrapping pair of identical quotes if the whole line got quoted.
    if len(stripped) >= 2 and stripped[0] in '"\'`' and stripped[-1] == stripped[0]:
        stripped = stripped[1:-1].strip()
    return stripped or original


class LLMManager:
    """Manages the Llama model via external vLLM API"""

    def __init__(self):
        self.api_base = VLLM_API_BASE
        self._iam_tokens = {}          # api key -> IAMTokenCache (watsonx SaaS only)
        self.model_id = LLM_MODEL_PATH
        # verify=False is needed for an on-prem cluster with a self-signed certificate; the
        # reference IAF integration disables it for both the token call and inference.
        self.client = httpx.Client(timeout=3600.0, verify=LLM_VERIFY_SSL)  # key injected per request
        self.term_corrector = self._load_term_corrector()

    @staticmethod
    def _load_term_corrector():
        """Tier-1 deterministic corrector for the org's core vocabulary. Disabled gracefully on error."""
        try:
            import os
            from core.term_corrector import TermCorrector
            base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lexicon")
            return TermCorrector(os.path.join(base, "core_terms.json"),
                                 os.path.join(base, "common_words.txt"))
        except Exception as e:
            # ERROR, not warning: Tier 1 being off is a real capability loss, and the most likely
            # cause is a missing wordlist — which the corrector now refuses to run without rather
            # than degrading into rewriting ordinary English as product names. The LLM (Tier 2)
            # still corrects transcripts, so this is a safe degradation, but it must be visible.
            logger.error(f"[TERMCORR] Tier-1 deterministic correction DISABLED: {e}")
            return None

    def _get_key(self) -> tuple[str, int]:
        """Acquire next available key from pool. No pool = offline/local backend."""
        if _key_pool is None:
            return _OFFLINE_PLACEHOLDER_KEY, 0
        key, fresh = _key_pool.acquire()
        idx = _key_pool._keys.index(key)
        if not fresh:
            status = _pool_status()
            logger.warning(
                f"[GroqPool] All keys cooling — using soonest available key #{idx} (...{key[-4:]}) "
                f"| pool: {status['available']}/{status['total']} available, {status['cooling']} cooling"
            )
        return key, idx

    def _cooldown(self, key: str, seconds: int = 60) -> None:
        """Mark a key in cooldown after a 429. No-op when there is no pool (local backend)."""
        if _key_pool is None:
            return
        _key_pool.cooldown(key, seconds)

    def _disable(self, key: str) -> None:
        """Drop a revoked/invalid key (401/403) from rotation until the service restarts."""
        if _key_pool is None:
            return
        _key_pool.disable(key)

    def _route(self, estimated_tokens: int) -> dict:
        """Pick the model for a transcript of this size.

        Short transcripts go to the primary model; long ones to LLM_MODEL_LONG when configured.
        The reason is not raw capability, it is CHUNKING: past what one context can hold the pipeline
        splits the transcript, writes a partial MoM per chunk and merges them, and whatever a
        partial leaves out is gone with no trace. A model with a bigger window takes the whole
        meeting in one pass and that failure mode disappears.

        With LLM_MODEL_LONG unset this returns the configured pair for every request, which is
        byte-for-byte today's behaviour.
        """
        if LLM_MODEL_LONG and estimated_tokens >= LLM_LONG_THRESHOLD_TOKENS:
            logger.info(
                f"[MOM] Routing to long-context model '{LLM_MODEL_LONG}' "
                f"({estimated_tokens} tokens >= {LLM_LONG_THRESHOLD_TOKENS})"
            )
            return {"model": LLM_MODEL_LONG, "api_base": VLLM_API_BASE_LONG,
                    "context_limit": MODEL_CONTEXT_LIMIT_LONG}
        return {"model": self.model_id, "api_base": self.api_base,
                "context_limit": MODEL_CONTEXT_LIMIT}

    def load_model(self):
        """Verify API connectivity. FAILS LOUD if the configured model is unavailable —
        a wrong model id must never silently fall back to a different model."""
        health_key = _key_pool._keys[0] if _key_pool else _OFFLINE_PLACEHOLDER_KEY  # fixed key — health checks must not consume pool rotation slots

        # watsonx HAS NO /models ROUTE. Its endpoint is a single POST URL carrying a ?version=
        # query, so "{base}/models" becomes .../text/chat?version=2023-05-29/models and the server
        # answers 405 — which was logged as "vLLM API connection failed" on every start and looked
        # like a broken deployment. What can be verified here is the credential: exchanging it for
        # a token proves the key, the auth mode and the network. The model id cannot be checked,
        # so it is taken as configured, and a wrong one surfaces on the first request instead.
        if self._is_watsonx(self.api_base):
            logger.info(f"Connecting to watsonx at: {self.api_base}")
            try:
                self._bearer(health_key)
            except Exception as e:
                logger.error(f"watsonx credentials could not be exchanged for a token: {e}")
                return
            logger.info(f"✓ watsonx ready. Authenticated; model '{self.model_id}' as configured "
                        f"(watsonx does not list models on this endpoint).")
            return

        try:
            logger.info(f"Connecting to vLLM API at: {self.api_base}")
            response = self.client.get(f"{self.api_base}/models", headers={"Authorization": f"Bearer {health_key}"})
            response.raise_for_status()
            loaded_ids = [m["id"] for m in response.json().get("data", [])]
        except Exception as e:
            # Transient connectivity/auth problem — don't crash on it; is_healthy() keeps re-checking.
            logger.error(f"vLLM API connection failed: {e}")
            return

        # Model validation is OUTSIDE the try ON PURPOSE, so a misconfigured model FAILS LOUD.
        # Silently taking loaded_ids[0] once selected a 512-token prompt-guard classifier and
        # produced junk minutes with no error — never again. Crashing surfaces it immediately.
        if self.model_id in loaded_ids:
            logger.info(f"✓ vLLM API ready. Model '{self.model_id}' is loaded (APP_MODE={APP_MODE}).")
            # A misconfigured LONG model must not stay hidden until the first long meeting
            # arrives, so check it here too — but only WARN, because the primary model can
            # still serve every request; _route falls back to it below.
            if LLM_MODEL_LONG and VLLM_API_BASE_LONG == self.api_base and LLM_MODEL_LONG not in loaded_ids:
                logger.error(
                    f"Long-context model '{LLM_MODEL_LONG}' is NOT available at {self.api_base} "
                    f"(available: {loaded_ids}). Long meetings will fail until this is fixed."
                )
        else:
            raise RuntimeError(
                f"Configured model '{self.model_id}' is NOT available at {self.api_base}. "
                f"Available: {loaded_ids}. Set LLM_MODEL_PATH to one of those "
                f"(e.g. 'openai/gpt-oss-120b' on Groq) — refusing to fall back to a wrong model."
            )
            
    @staticmethod
    def _is_watsonx(base: str) -> bool:
        """watsonx endpoints are a single POST URL under /ml/v1/, with no OpenAI-style routes."""
        return "/ml/v1/" in (base or "")

    def is_healthy(self):
        try:
            health_key = _key_pool._keys[0] if _key_pool else _OFFLINE_PLACEHOLDER_KEY  # fixed key — health checks must not consume pool rotation slots
            if self._is_watsonx(self.api_base):
                # No /models to call: a live token is the only thing that can be proven cheaply.
                return bool(self._bearer(health_key))
            response = self.client.get(f"{self.api_base}/models", headers={"Authorization": f"Bearer {health_key}"})
            return response.status_code == 200
        except Exception:
            return False

    # ── backend shapes ────────────────────────────────────────────────────────────────────────────
    # Three request shapes, chosen by the endpoint path, so switching backend is configuration:
    #   /ml/v1/text/chat        watsonx chat      messages + choices, keeps JSON-schema extraction
    #   /ml/v1/text/generation  watsonx generate  one `input` string, answer in results[0]
    #   anything else           OpenAI-compatible vLLM, OpenRouter, Groq, watsonx model gateway
    @staticmethod
    def _endpoint(base: str) -> str:
        """A watsonx URL is configured COMPLETE — it carries ?version=... — so it is used as given."""
        base = (base or "").rstrip("/")
        if "/ml/v1/" in base:
            return base
        if WATSONX_PROJECT_ID and "ml.cloud.ibm.com" in base:
            return f"{base}/ml/v1/text/chat?version={WATSONX_VERSION}"
        return f"{base}/chat/completions"

    @staticmethod
    def _shape(url: str) -> str:
        if "/text/generation" in url:
            return "generation"
        if "/text/chat" in url:
            return "watsonx_chat"
        return "openai"

    @staticmethod
    def _flatten(messages) -> str:
        """Fold a system+user pair into one prompt for the completion-style API, using the Llama 3
        chat template the reference IAF integration uses. Without the template the model answers the
        instructions conversationally instead of obeying them."""
        parts = ["<|begin_of_text|>"]
        for m in messages:
            role = m.get("role", "user")
            parts.append(f"<|start_header_id|>{role}<|end_header_id|>\n{m.get('content', '')}<|eot_id|>")
        parts.append("<|start_header_id|>assistant<|end_header_id|>\n")
        return "".join(parts)

    @classmethod
    def _shape_payload(cls, payload: dict, url: str) -> dict:
        shape = cls._shape(url)
        if shape == "openai":
            return payload
        p = dict(payload)
        p.pop("provider", None)                     # OpenRouter-only
        if WATSONX_PROJECT_ID:
            p["project_id"] = WATSONX_PROJECT_ID
        if shape == "watsonx_chat":
            p["model_id"] = p.pop("model", None)
            return p
        # completion-style: no messages, no structured output. The JSON then has to survive on the
        # prompt alone, which _parse_points' repair and object-salvage steps already handle.
        fmt = p.pop("response_format", None)
        if fmt:
            logger.debug("[LLM] %s ignores response_format — relying on prompt + JSON repair", url)
        return {
            "input": cls._flatten(p.get("messages") or []),
            "parameters": {
                "decoding_method": "greedy",
                "max_new_tokens": p.get("max_tokens", 512),
                "temperature": p.get("temperature", 0.1),
                "repetition_penalty": 1.1,
            },
            "model_id": p.get("model"),
            "project_id": WATSONX_PROJECT_ID,
        }

    @classmethod
    def _parse_reply(cls, data: dict, url: str):
        """Return (content, finish_reason) from either response shape."""
        if cls._shape(url) == "generation":
            r = (data.get("results") or [{}])[0]
            stop = r.get("stop_reason")
            return r.get("generated_text"), ("length" if stop in ("max_tokens", "token_limit") else stop)
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content"), choice.get("finish_reason")

    @staticmethod
    def _budget(payload: dict):
        return payload.get("max_tokens") or (payload.get("parameters") or {}).get("max_new_tokens")

    @staticmethod
    def _set_budget(payload: dict, n: int) -> None:
        if "parameters" in payload:
            payload["parameters"]["max_new_tokens"] = n
        else:
            payload["max_tokens"] = n

    def _bearer(self, key: str) -> str:
        """The Authorization value for `key`. watsonx credentials are not bearer tokens — they are
        exchanged for one that expires — so the pool still rotates the KEY and this returns a live
        token. Anything else (Zen key, OpenRouter, Groq, local vLLM) is sent as-is."""
        if LLM_AUTH_MODE == "cp4d":
            cache = self._iam_tokens.get("cp4d")
            if cache is None:
                from core.ibm_auth import CP4DTokenCache
                cache = self._iam_tokens["cp4d"] = CP4DTokenCache(
                    CP4D_AUTH_URL, CP4D_USERNAME, CP4D_API_KEY or key,
                    ttl=CP4D_TOKEN_TTL, verify=LLM_VERIFY_SSL)
            return cache.token()
        if LLM_AUTH_MODE == "iam":
            cache = self._iam_tokens.get(key)
            if cache is None:
                from core.ibm_auth import IAMTokenCache
                cache = self._iam_tokens[key] = IAMTokenCache(key, iam_url=IBM_IAM_URL, verify=LLM_VERIFY_SSL)
            return cache.token()
        return key

    def generate(self, system_prompt: str, user_message: str, max_new_tokens: int, temperature: float,
                 frequency_penalty: float = 0.0, presence_penalty: float = 0.0,
                 model: str = None, api_base: str = None, extra: dict = None) -> str:
        """Generate text using vLLM API (OpenAI compatible).
        frequency_penalty/presence_penalty default to 0.0 → unchanged for every existing caller;
        the MoM path sets them >0 for non-English to prevent CJK repetition loops.
        model/api_base default to the configured pair — the MoM path overrides them when a long
        transcript is routed to the big-context model (see _route)."""
        base = api_base or self.api_base
        url = self._endpoint(base)

        payload = {
            "model": model or self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": max_new_tokens,
            "temperature": max(temperature, 0.01),
            "top_p": 0.9,
            "frequency_penalty": frequency_penalty,
            "presence_penalty": presence_penalty,
        }
        # Per-call additions: response_format for grammar-constrained JSON, provider pinning, etc.
        if extra:
            payload.update(extra)
        # PIN THE PROVIDER. OpenRouter load-balances across hosts and each runs its own
        # quantisation: five identical calls hit Parasail, Crusoe, Novita and DeepInfra. That is
        # an uncontrolled variable in both output quality and any accuracy measurement taken from
        # it, so a deployment that cares about consistency names one host and stays on it.
        if LLM_PROVIDER_ORDER and "openrouter" in base.lower():
            payload.setdefault("provider", {"order": LLM_PROVIDER_ORDER, "allow_fallbacks": False})
        payload = self._shape_payload(payload, url)

        RETRY_DELAYS = [3, 8, 20]
        key, key_idx = self._get_key()
        retried_budget = False   # allow ONE extended-budget retry if gpt-oss returns content=None
        for attempt in range(4):
            try:
                logger.info(f"[GroqPool] Using key #{key_idx} (...{key[-4:]})")
                response = self.client.post(url, json=payload,
                                            headers={"Authorization": f"Bearer {self._bearer(key)}"})
                response.raise_for_status()
                content, finish_reason = self._parse_reply(response.json(), url)
                if finish_reason == "length":
                    # A cut-off reply used to pass through as if it were complete, and a JSON caller then
                    # saw only a parse error. Say so here, where the cause is actually known.
                    logger.warning(f"[LLM] reply hit the {self._budget(payload)}-token limit and was cut "
                                   f"off — output is incomplete")
                if content is None:
                    # gpt-oss-120b is a REASONING model: it can spend its ENTIRE output budget on internal
                    # reasoning (sometimes looping) and return {"content": null, "reasoning": "..."} with no
                    # answer. We must NEVER surface that raw chain-of-thought — it once leaked the model's
                    # reasoning + a "done. okay." loop straight into the MoM's ACTION ITEMS. Instead retry
                    # ONCE with more room + repetition penalties so it finishes the answer; if it still can't,
                    # return "" (a clean empty section always beats leaking reasoning).
                    if not retried_budget:
                        retried_budget = True
                        budget = min(int(max_new_tokens) * 2 + 512, 8192)
                        self._set_budget(payload, budget)
                        if "parameters" not in payload:      # penalties are an OpenAI-shape field
                            payload["frequency_penalty"] = max(payload.get("frequency_penalty") or 0.0, 0.4)
                            payload["presence_penalty"] = max(payload.get("presence_penalty") or 0.0, 0.4)
                        logger.warning(
                            "Model returned content=None (budget spent reasoning). Retrying once with a "
                            "%d-token budget + repetition penalties.", budget
                        )
                        continue
                    logger.error(
                        "vLLM still returned content=None after the extended retry — returning empty "
                        "(never leak reasoning into user-facing output)."
                    )
                    return ""
                return content.strip()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 3:
                    wait = RETRY_DELAYS[attempt]
                    self._cooldown(key, 60)
                    status = _pool_status()
                    logger.warning(
                        f"[GroqPool] Cooling key #{key_idx} (...{key[-4:]}) for 60s "
                        f"| pool: {status['available']}/{status['total']} available, {status['cooling']} cooling"
                    )
                    key, key_idx = self._get_key()
                    logger.warning(f"Groq rate limited (429), retrying in {wait}s... (attempt {attempt+1}/4)")
                    time.sleep(wait)
                    continue
                if e.response.status_code in (401, 403):
                    self._disable(key)
                    status = _pool_status()
                    logger.error(
                        f"[GroqPool] Key #{key_idx} (...{key[-4:]}) REVOKED — removed from rotation "
                        f"until restart (HTTP {e.response.status_code}) "
                        f"| pool: {status['available']}/{status['total']} available, "
                        f"{status['disabled']} disabled"
                    )
                    if attempt < 3 and status['available'] > 0:
                        key, key_idx = self._get_key()
                        continue
                    raise
                logger.error(f"vLLM API generation failed: {e}")
                raise
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
                if attempt < 3:
                    wait = RETRY_DELAYS[attempt]
                    logger.warning(f"Groq connection error (attempt {attempt+1}/4): {e}. Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"vLLM API connection failed after retries: {e}")
                raise
            except Exception as e:
                logger.error(f"vLLM API generation failed: {e}")
                raise

    _DECISIONS_EMPTY_RE = re.compile(
        r'DECISIONS TAKEN\s*\n\s*[•\-]?\s*None explicitly stated',
        re.IGNORECASE,
    )
    _ACTION_ITEMS_EMPTY_RE = re.compile(
        r'ACTION ITEMS\s*\n\s*[•\-]?\s*None explicitly stated',
        re.IGNORECASE,
    )
    # Matches trivial closing remarks that are not real decisions
    _DECISIONS_TRIVIAL_RE = re.compile(
        r"(?:end(?:ing)?\s+(?:this\s+)?meeting|meeting\s+(?:end|close)|"
        r"wrap(?:ping)?\s+up|that'?s?\s+all|let'?s?\s+end|adjourn)",
        re.IGNORECASE,
    )

    def _decisions_are_weak(self, analysis: str) -> bool:
        """True when DECISIONS section is empty OR contains only a trivial closing remark."""
        if self._DECISIONS_EMPTY_RE.search(analysis):
            return True
        m = re.search(
            r'DECISIONS TAKEN\s*\n(.*?)(?=_{10,}|\Z)',
            analysis,
            re.DOTALL | re.IGNORECASE,
        )
        if not m:
            return False
        bullets = [b.strip() for b in m.group(1).split('\n')
                   if b.strip().startswith('•')]
        # Only one bullet and it's just "end meeting" → treat as weak
        if len(bullets) == 1 and self._DECISIONS_TRIVIAL_RE.search(bullets[0]):
            return True
        return False

    @staticmethod
    def _is_english(output_lang: str) -> bool:
        return (output_lang or "English").strip().lower() in ("english", "en", "")

    @classmethod
    def _lang_suffix(cls, output_lang: str) -> str:
        """Override directive appended to the MoM system prompts. Returns "" for English so the
        English prompts (and thus existing English MoMs) stay byte-identical."""
        if cls._is_english(output_lang):
            return ""
        lang = output_lang.strip()
        return (
            "\n\n─────────────────────────────────────────────────\n"
            "OUTPUT LANGUAGE — HIGHEST PRIORITY (this OVERRIDES every earlier instruction to write in English):\n"
            f"Write the ENTIRE MoM in {lang}. Translate every section heading (Agenda, Attendees, Summary, "
            f"Speaker-wise Notes, Decisions Taken, Action Items) and every field label (Assigned to, Assigned by, "
            f"Due on, Date, Time, Venue) and ALL content into {lang}. Keep people's names, project names, and "
            "technical/tool names in their original script. Do NOT add English in parentheses. Preserve the exact "
            "structure, section order, bullet formatting, and the ================ separators.\n"
        )

    # The exact 'write in English' OUTPUT directives baked into the MoM prompts. For a non-English
    # MoM we swap "English" → the target language in each so the prompt body no longer contradicts
    # the override above. Input-language descriptions ("transcript may be in English, Hindi…") are
    # deliberately NOT in this list — those describe the input and must stay.
    _ENGLISH_OUTPUT_DIRECTIVES = {
        "write the MoM output in English": "write the MoM output in {lang}",
        "(write in English even if the decision was spoken in Hindi)": "(write in {lang})",
        "(Write the task description in English even if it was spoken in Hindi)": "(Write the task description in {lang})",
        "write output in English": "write output in {lang}",
        "Write task descriptions in English even if spoken in Hindi.": "Write task descriptions in {lang}.",
        # The OUTPUT TEMPLATE shows the section headings/labels (AGENDA, ATTENDEES, Date, …) in
        # English; "copy this structure exactly" made the model echo them untranslated. Tell it the
        # English words are placeholders to translate, so headings come out in the target language.
        "copy this structure exactly": (
            "copy this structure exactly, BUT write every section heading and field label "
            "(AGENDA, ATTENDEES, SUMMARY, SPEAKER-WISE NOTES, DECISIONS TAKEN, ACTION ITEMS, "
            "PURPOSE OF MEETING, Date, Time, Venue, Assigned to, Assigned by, Due on) in {lang} — "
            "the English words shown in the template below are placeholders; NEVER output a heading "
            "or label in English"
        ),
    }

    @classmethod
    def _localize_prompt(cls, prompt: str, output_lang: str) -> str:
        """Build the system prompt for `output_lang`: English → unchanged (byte-identical); otherwise
        rewrite the in-body 'write in English' directives to the target language AND append the
        override. This removes the contradiction that left non-English MoMs coming out in English."""
        if cls._is_english(output_lang):
            return prompt
        lang = output_lang.strip()
        out = prompt
        for english_phrase, template in cls._ENGLISH_OUTPUT_DIRECTIVES.items():
            out = out.replace(english_phrase, template.format(lang=lang))
        return out + cls._lang_suffix(output_lang)

    @classmethod
    def _mom_temperature(cls, output_lang: str, base_temp: float) -> float:
        """The MoM uses temperature 0.05; that low setting triggers repetition loops (comma garbage)
        for non-English. Raise it for non-English to break the loop. We do NOT use a frequency/presence
        penalty — penalties punish the character reuse that CJK languages legitimately need, which
        empties out Chinese. English keeps its exact temperature (unchanged)."""
        return base_temp if cls._is_english(output_lang) else max(base_temp, 0.4)

    @staticmethod
    def _is_degenerate(text: str) -> bool:
        """True if the output looks like a repetition-loop (e.g. a wall of commas): a very long run
        of one character, or one non-space character dominating the text."""
        t = (text or "").strip()
        if len(t) < 80:
            return False
        longest_run = max((len(m.group(0)) for m in re.finditer(r"(.)\1+", t)), default=0)
        non_space = [c for c in t if not c.isspace()]
        if not non_space:
            return True
        from collections import Counter
        top_share = Counter(non_space).most_common(1)[0][1] / len(non_space)
        return longest_run > 40 or top_share > 0.30

    @staticmethod
    def _char_profile(text: str) -> str:
        """TEMP forensic: quantify a raw model output — how much is Han vs comma vs Latin, plus the
        longest single-char run. Lets us prove from logs whether the model returned real Chinese,
        a comma-loop, or a near-empty/Latin skeleton — without dumping the whole payload."""
        t = text or ""
        han = sum(1 for c in t if "一" <= c <= "鿿")
        commas = t.count(",") + t.count("，") + t.count("、")
        latin = sum(1 for c in t if c.isascii() and c.isalpha())
        run = max((len(m.group(0)) for m in re.finditer(r"(.)\1+", t)), default=0)
        return f"len={len(t)} han={han} commas={commas} latin={latin} longest_run={run}"

    def _fill_decisions_if_empty(self, analysis: str, transcript: str, temperature: float, output_lang: str = "English") -> str:
        """If DECISIONS TAKEN is empty or only has a trivial closing remark,
        run a focused second LLM call to fill it."""
        # The weak-section detection + backfill is English-keyed; skip for a localized MoM so we
        # never splice an English-extracted section into a non-English document.
        if not self._is_english(output_lang):
            return analysis
        if not self._decisions_are_weak(analysis):
            return analysis  # already has real decisions

        logger.info("[MOM] DECISIONS TAKEN weak/empty — running focused extraction pass")
        focused = self.generate(
            DECISIONS_EXTRACTION_PROMPT,
            f"TRANSCRIPT:\n{transcript}\n\nList all decisions:",
            400,
            temperature,
        ).strip()

        if not focused or "none explicitly stated" in focused.lower():
            return analysis  # genuinely none

        # Replace the empty/trivial section with the focused result
        analysis = re.sub(
            r'(DECISIONS TAKEN\s*\n)([ \t]*[•\-]?[^\n]*)',
            lambda m: m.group(1) + focused,
            analysis,
            count=1,
            flags=re.IGNORECASE,
        )
        return analysis

    def _fill_action_items_if_empty(self, analysis: str, transcript: str, temperature: float, output_lang: str = "English") -> str:
        """If ACTION ITEMS is empty, run a focused second LLM call to fill it."""
        if not self._is_english(output_lang):
            return analysis   # English-keyed backfill — skip for localized MoMs
        if not self._ACTION_ITEMS_EMPTY_RE.search(analysis):
            return analysis  # already has action items

        logger.info("[MOM] ACTION ITEMS empty — running focused extraction pass")
        focused = self.generate(
            ACTION_ITEMS_EXTRACTION_PROMPT,
            f"TRANSCRIPT:\n{transcript}\n\nList all action items:",
            400,
            temperature,
        ).strip()

        if not focused or "none explicitly stated" in focused.lower():
            return analysis  # genuinely none

        analysis = re.sub(
            r'(ACTION ITEMS\s*\n)(\s*[•\-]?\s*None explicitly stated[^\n]*)',
            lambda m: m.group(1) + focused,
            analysis,
            flags=re.IGNORECASE,
        )
        return analysis

    def generate_stream(self, system_prompt: str, user_message: str, max_new_tokens: int, temperature: float,
                        frequency_penalty: float = 0.0, presence_penalty: float = 0.0):
        """Sync generator yielding text tokens from vLLM streaming API.
        Penalties default to 0.0 (unchanged); MoM sets them >0 for non-English to avoid repetition loops."""
        url = f"{self.api_base}/chat/completions"
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": max_new_tokens,
            "temperature": max(temperature, 0.01),
            "top_p": 0.9,
            "frequency_penalty": frequency_penalty,
            "presence_penalty": presence_penalty,
            "stream": True
        }
        key, key_idx = self._get_key()
        for attempt in range(3):
            try:
                logger.info(f"[GroqPool] Using key #{key_idx} (...{key[-4:]})")
                auth = {"Authorization": f"Bearer {key}"}
                with self.client.stream("POST", url, json=payload, headers=auth) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = chunk["choices"][0]["delta"].get("content", "")
                                if delta:
                                    yield delta
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                return  # success — exit retry loop
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    wait = (attempt + 1) * 15
                    self._cooldown(key, 60)
                    status = _pool_status()
                    logger.warning(
                        f"[GroqPool] Cooling key #{key_idx} (...{key[-4:]}) for 60s "
                        f"| pool: {status['available']}/{status['total']} available, {status['cooling']} cooling"
                    )
                    key, key_idx = self._get_key()
                    logger.warning(f"Groq rate limited (429) on stream, retrying in {wait}s... (attempt {attempt+1}/3)")
                    time.sleep(wait)
                    continue
                if e.response.status_code in (401, 403):
                    self._disable(key)
                    status = _pool_status()
                    logger.error(
                        f"[GroqPool] Key #{key_idx} (...{key[-4:]}) REVOKED — removed from rotation "
                        f"until restart (HTTP {e.response.status_code}) "
                        f"| pool: {status['available']}/{status['total']} available, "
                        f"{status['disabled']} disabled"
                    )
                    if attempt < 2 and status['available'] > 0:
                        key, key_idx = self._get_key()
                        continue
                    raise
                logger.error(f"vLLM streaming failed: {e}")
                raise
            except Exception as e:
                logger.error(f"vLLM streaming failed: {e}")
                raise

    def generate_mom_sse(self, text: str, temperature: float, output_lang: str = "English"):
        """
        Generator yielding SSE-formatted strings for streaming MoM generation.
        Yields: data: {"token": "..."} for each token
                data: {"progress": "..."} for multi-chunk progress
                data: {"done": true, "analysis": "...", "summary": "..."} at the end
        output_lang: language for the WHOLE MoM (default English = unchanged behavior).
        """
        text = preprocess_transcript(text)
        text_len = len(text)
        estimated_tokens = text_len // 4
        logger.info(f"[MOM SSE] Input: {text_len} chars (~{estimated_tokens} tokens), output_lang={output_lang}")

        # Option B: native non-English MoM generation degenerates. For a non-English request we do
        # NOT stream native tokens — we generate the English MoM and translate it (reliable, via
        # generate_mom), emitting progress events then the final translated MoM. English keeps true
        # token streaming below.
        if not self._is_english(output_lang):
            yield f"data: {json.dumps({'progress': 'Generating MoM in English...'})}\n\n"
            yield f"data: {json.dumps({'progress': f'Translating to {output_lang}...'})}\n\n"
            result = self.generate_mom(text, temperature, output_lang)
            analysis = result.get("analysis", "")
            yield f"data: {json.dumps({'done': True, 'analysis': analysis, 'summary': analysis})}\n\n"
            return

        # English path: true token streaming (unchanged behavior).
        _mtype, _focus = self._focus_for(text)   # auto-detected meeting type → template focus (no UI)
        analysis_sys = MEETING_ANALYSIS_PROMPT + _focus
        synthesis_sys = SYNTHESIS_PROMPT + _focus
        fp, pp = 0.0, 0.0
        full_generated = ""

        if estimated_tokens < 5000:
            # Single-pass: true token streaming
            logger.info("[MOM SSE] Mode: single-pass streaming")
            safe_max_output = max(500, min(MAX_NEW_TOKENS_PER_CHUNK, MODEL_CONTEXT_LIMIT - estimated_tokens - _ANALYSIS_PROMPT_TOKENS - 100))
            user_prompt = (
                "Read the following meeting transcript carefully from start to finish, "
                "then generate the complete MoM document.\n\n"
                "TRANSCRIPT:\n"
                "─────────────────────────────────────────────────\n"
                f"{text}\n"
                "─────────────────────────────────────────────────\n\n"
                "Now generate the MoM. Start immediately with ================"
            )
            for token in self.generate_stream(analysis_sys, user_prompt, safe_max_output, temperature, fp, pp):
                full_generated += token
                yield f"data: {json.dumps({'token': token})}\n\n"

        else:
            # Multi-chunk: analyse chunks sync with progress events, then stream synthesis
            logger.info("[MOM SSE] Mode: chunk + synthesise streaming")
            chunks = chunk_transcript(text)
            partial_analyses = []

            for i, chunk in enumerate(chunks):
                yield f"data: {json.dumps({'progress': f'Analyzing part {i+1} of {len(chunks)}...'})}\n\n"
                chunk_prompt = (
                    f"This is section {i+1} of {len(chunks)} of a longer meeting transcript.\n"
                    f"Read it carefully and extract a detailed partial MoM covering only this section.\n\n"
                    f"TRANSCRIPT SECTION {i+1}/{len(chunks)}:\n"
                    f"─────────────────────────────────────────────────\n"
                    f"{chunk}\n"
                    f"─────────────────────────────────────────────────\n\n"
                    f"Generate the partial MoM for this section. Start immediately with ================"
                )
                partial = self.generate(analysis_sys, chunk_prompt, MAX_NEW_TOKENS_PER_CHUNK, temperature, fp, pp)
                partial_analyses.append(partial)

            yield f"data: {json.dumps({'progress': 'Synthesizing final MoM...'})}\n\n"
            combined = self._join_partials(partial_analyses, "\n\n=== CHUNK ===\n\n")

            synthesis_prompt = (
                f"Below are {len(partial_analyses)} partial MoM analyses from consecutive sections of the same meeting.\n"
                f"Merge them into ONE complete, non-redundant MoM document.\n\n"
                f"{'=' * 40} PARTIAL ANALYSES {'=' * 40}\n\n"
                f"{combined}\n\n"
                f"{'=' * 40} END OF PARTIALS {'=' * 40}\n\n"
                f"Now generate the final merged MoM. Start immediately with ================"
            )
            for token in self.generate_stream(synthesis_sys, synthesis_prompt, MAX_NEW_TOKENS_SYNTHESIS, temperature, fp, pp):
                full_generated += token
                yield f"data: {json.dumps({'token': token})}\n\n"

        # Post-process
        cleaned = clean_mom_output(full_generated)
        # TODO(Stage 6): Remove after JSON pipeline is production validated (renderer owns the footer).
        if estimated_tokens >= 5000 and "Regards" not in cleaned[-200:]:
            cleaned += "\n\n" + "=" * 80 + "\n\nRegards,\nGenerated by AngelBot.AI\n"
        filled = self._fill_decisions_if_empty(cleaned, text, temperature)
        filled = self._fill_action_items_if_empty(filled, text, temperature)

        yield f"data: {json.dumps({'done': True, 'analysis': filled, 'summary': filled})}\n\n"

    _PIPELINE_RETRY_WAIT = 20      # seconds; long enough for a provider rate limit to clear

    def generate_mom(self, text: str, temperature: float, output_lang: str = "English", metadata: dict = None) -> dict:
        """Entry point (Stage 3): try the new JSON pipeline; on ANY failure fall back to the legacy
        text pipeline so the MoM endpoint never crashes. `metadata` (date/time/venue) is passed through
        to the renderer; the backend wires real metadata in Stage 5."""
        try:
            return self._generate_mom_json(text, temperature, output_lang, metadata)
        except Exception as e:
            # ONE RETRY BEFORE THE LEGACY PIPELINE. Measured 2026-09-10: a single 429 from the
            # provider threw the entire structured pipeline away, and that run reached the user with
            # no attendees, decisions or action items at all — the legacy path returns rendered prose
            # and no `content`. A transient rate limit should cost a minute, not the whole structured
            # document. The pipeline is not resumable, so this repeats it; that only happens on a
            # failure, and losing every structured field is far more expensive.
            logger.warning(f"[MOM] JSON pipeline failed ({e!r}) — waiting {self._PIPELINE_RETRY_WAIT}s "
                           f"and trying once more before the legacy fallback")
            time.sleep(self._PIPELINE_RETRY_WAIT)
            try:
                return self._generate_mom_json(text, temperature, output_lang, metadata)
            except Exception as e2:
                # ERROR, not WARNING: the legacy pipeline returns no structured `content`, so this
                # degrades the .docx and the Elasticsearch document to prose only. It must stand out.
                logger.error(f"[MOM] Falling back to legacy pipeline (reason={e2!r}) — "
                             f"this MoM will have NO structured decisions/action items")
                return self._generate_mom_legacy(text, temperature, output_lang)

    # ── NEW JSON pipeline ────────────────────────────────────────────────────
    def _generate_mom_json(self, text, temperature, output_lang="English", metadata=None):
        """Structured pipeline: generate JSON content → validate (+1 deterministic repair) → render.
        The renderer (render_mom) is the single owner of headings/labels/separators/footer/speaker labels."""
        if metadata is None:
            metadata = self._fallback_metadata_from_text(text)   # from raw text; TODO(Stage 5): real backend metadata
        text = preprocess_transcript(text)
        text_len = len(text)
        estimated_tokens = text_len // 4
        logger.info(f"[MOM] Input: {text_len} chars (~{estimated_tokens} tokens), output_lang={output_lang} [JSON pipeline]")

        _mtype, _focus = self._focus_for(text)   # auto-detected meeting type → template focus (no UI)
        route = self._route(estimated_tokens)    # one model for the whole request — never switch mid-way
        raw, chunks_used = self._generate_json_raw(text, temperature, estimated_tokens, focus=_focus, route=route)
        logger.info(f"[MOM] JSON generated ({len(raw)} chars, chunks={chunks_used}, type={_mtype})")

        content, ok = parse_and_validate(raw)
        if ok:
            logger.info("[MOM] JSON validated")
        else:
            logger.warning("[MOM] Repair retry")
            raw, _ = self._generate_json_raw(text, 0.0, estimated_tokens, strict=True, focus=_focus, route=route)
            content, ok = parse_and_validate(raw)
            if not ok:
                raise ValueError("JSON invalid after deterministic repair + retry")
            logger.info("[MOM] JSON repair succeeded")

        # Fill passes re-anchored to the PARSED structure (not regex over rendered text).
        # HYBRID EXTRACTION — the method is chosen per FIELD, because measurement showed one
        # method does not win everywhere:
        #   key_points   → overlapping windows. A list of many small facts; a small window is the
        #                  right lens, and it measured 89% precision / 82% recall, the best of any
        #                  run. Whole-transcript passes compress this field away.
        #   decisions    → whole transcript. A motion, its second and "passes unanimously" span
        #   action_items   three windows; no single window contains one. Grounded-only mode
        #                  returned ONE decision and ZERO action items on a meeting that had both.
        if MOM_WINDOW_KEY_POINTS:
            by_type = self._extract_windows(text, temperature, route)
            window_points = [p["text"] for p in by_type.get("key_point", [])]
            # The windows also spot decisions/actions; keep them as extra candidates for the
            # whole-transcript passes to merge with, never as the only source.
            for p in by_type.get("decision", []):
                content.setdefault("decisions", []).append(p["text"])
            for p in by_type.get("action_item", []):
                content.setdefault("action_items", []).append(
                    {"task": p["text"], "assigned_to": (p.get("owner") or "").strip(),
                     "assigned_by": "", "due": (p.get("due") or "").strip()})
            content["key_points"] = window_points

        content = self._fill_key_points_json(content, text, temperature, route)
        content = self._fill_decisions_json(content, text, temperature, route)
        content = self._fill_action_items_json(content, text, temperature, route)
        content = self._fill_figures_json(content, text, temperature, route)

        # Final grounding sweep: a point may quote a real span and still name someone who was
        # never mentioned. See _proper_nouns_supported.
        for f in ("key_points", "decisions", "key_figures"):
            content[f] = [x for x in (self._strip_meta(v) for v in (content.get(f) or [])) if x]
        content["key_points"] = self._drop_repeated_figures(content.get("key_points") or [])
        content["key_points"] = self._drop_unsupported_names(content.get("key_points") or [], text, "KEY POINTS")
        content["decisions"] = self._drop_unsupported_names(content.get("decisions") or [], text, "DECISIONS")
        content["action_items"] = self._drop_unsupported_names(
            content.get("action_items") or [], text, "ACTION ITEMS", get=lambda a: a.get("task", ""))
        content = self._normalise_action_items(content)
        if MOM_MERGE_ITEMS:
            content = self._merge_duplicate_items(content, "action_items", temperature, route)
            content = self._merge_duplicate_items(content, "decisions", temperature, route)
        if MOM_VERIFY_DECISIONS:
            content = self._verify_decisions(content, text, temperature, route)
        content = self._reconcile_attendees(content, text)
        content = self._ground_attendee_roles(content, text)
        content = self._rewrite_summary_json(content, temperature, route)

        # Stage 4: translate CONTENT values, then render in the target language (labels from config).
        # Falls back to whole-doc translation of the English render if content translation shape is off.
        rendered = None
        if not self._is_english(output_lang):
            try:
                translated = self._translate_content(content, output_lang, temperature)
                rendered = render_mom(translated, output_lang, metadata)
                logger.info(f"[MOM] Content translated + localized render → {output_lang}")
            except Exception as e:
                logger.warning(f"[MOM] Content translation failed ({e!r}) — falling back to whole-doc translate")
        if rendered is None:
            rendered = render_mom(content, "English", metadata)
            if not self._is_english(output_lang):
                rendered = self._translate_mom(rendered, output_lang, temperature)
        logger.info(f"[MOM] Rendering complete ({len(rendered)} chars, lang={output_lang})")

        return {
            "analysis": rendered,
            "summary": rendered,
            "content": content,          # English structured base — lets callers cache + localize (Design 2)
            "original_length": text_len,
            "analysis_length": len(rendered),
            "chunks_used": chunks_used,
        }

    def localize_mom(self, content, target_lang, metadata=None, temperature=0.1):
        """Design 2: localize an already-generated English `content` (structured) into target_lang —
        translate the CONTENT VALUES then render with config-driven labels. On translation failure,
        renders the English content in the target-language layout (labels localized, content English)."""
        content = parse_content(content)
        if not self._is_english(target_lang):
            try:
                content = self._translate_content(content, target_lang, temperature)
                logger.info(f"[LOCALIZE] content translated → {target_lang}")
            except Exception as e:
                logger.warning(f"[LOCALIZE] content translation failed ({e!r}) — localized labels + English content")
        return render_mom(content, target_lang, metadata)

    def classify_meeting_type(self, text: str) -> str:
        """Auto-detect the meeting type to pick a template focus (no UI to override, so be
        CONSERVATIVE — default to 'general' on anything unclear or on any error). One cheap LLM
        call on a transcript sample."""
        allowed = ("sales", "standup", "one_on_one", "general")
        try:
            sample = (text or "")[:4000]
            if not sample.strip():
                return "general"
            raw = self.generate(MEETING_TYPE_CLASSIFY_PROMPT, sample, MAX_NEW_TOKENS_CLASSIFY, 0.0).strip().lower()
            word = re.split(r"[^a-z_]+", raw)[0] if raw else "general"
            mtype = word if word in allowed else "general"
            logger.info(f"[MOM] Auto-detected meeting type: {mtype}" + ("" if mtype == word else f" (raw={raw!r})"))
            return mtype
        except Exception as e:
            logger.warning(f"[MOM] Meeting-type classify failed ({e!r}) — using 'general'")
            return "general"

    def _focus_for(self, text: str):
        """(meeting_type, focus_text) for the auto-detected type — focus is appended to the system
        prompt so the SAME schema/layout is emphasised for that meeting type."""
        mtype = self.classify_meeting_type(text)
        return mtype, TEMPLATE_FOCUS.get(mtype, "")

    def _generate_json_raw(self, text, temperature, estimated_tokens, strict=False, focus="", route=None):
        """Run the JSON-emitting prompts (single-pass or chunk+synthesise). Returns (raw_json, chunks).
        `focus` is the auto-detected meeting-type focus appended to the system prompts (schema unchanged)."""
        strict_note = "\n\nReturn ONLY the JSON object — no prose, no code fences." if strict else ""
        analysis_sys = MEETING_ANALYSIS_PROMPT_JSON + focus
        synthesis_sys = SYNTHESIS_PROMPT_JSON + focus
        route = route or self._route(estimated_tokens)
        m, ab, ctx = route["model"], route["api_base"], route["context_limit"]

        # SINGLE-PASS vs CHUNK is decided by what actually fits, not a fixed 5000. That constant
        # was written for a 32k context and silently caps the benefit of any larger model: route a
        # long meeting to a big-context model and it would still be chopped into partials and
        # merged, which is the step that loses whole topics. Fit = context minus the system prompt,
        # the output we intend to ask for, and a small margin.
        single_pass_limit = max(5000, ctx - _ANALYSIS_PROMPT_TOKENS - MAX_NEW_TOKENS_PER_CHUNK - 500)
        if estimated_tokens < single_pass_limit:
            logger.info(f"[MOM] Mode: single-pass [JSON] ({estimated_tokens} < {single_pass_limit} tokens)")
            user_prompt = (
                "Read the following meeting transcript carefully from start to finish, then produce the "
                "MoM as a single JSON object per the schema.\n\n"
                "TRANSCRIPT:\n"
                "─────────────────────────────────────────────────\n"
                f"{text}\n"
                "─────────────────────────────────────────────────\n\n"
                "Now output the JSON object." + strict_note
            )
            safe_max_output = max(500, min(MAX_NEW_TOKENS_PER_CHUNK, ctx - estimated_tokens - _ANALYSIS_PROMPT_TOKENS - 100))
            return self.generate(analysis_sys, user_prompt, safe_max_output, temperature, model=m, api_base=ab), 1

        logger.info("[MOM] Mode: chunk + synthesise [JSON]")
        chunks = chunk_transcript(text)
        partials = []
        for i, chunk in enumerate(chunks):
            logger.info(f"[MOM] Analysing chunk {i+1}/{len(chunks)} [JSON]")
            chunk_prompt = (
                f"This is section {i+1} of {len(chunks)} of a longer meeting transcript.\n"
                f"Extract a partial MoM as a JSON object per the schema, covering only this section.\n\n"
                f"TRANSCRIPT SECTION {i+1}/{len(chunks)}:\n"
                f"─────────────────────────────────────────────────\n"
                f"{chunk}\n"
                f"─────────────────────────────────────────────────\n\n"
                f"Now output the JSON object." + strict_note
            )
            partials.append(self.generate(analysis_sys, chunk_prompt, MAX_NEW_TOKENS_PER_CHUNK, temperature,
                                          model=m, api_base=ab))

        combined = self._join_partials(partials, "\n\n=== PARTIAL JSON ===\n\n")
        synthesis_prompt = (
            f"Below are {len(partials)} partial MoM JSON objects from consecutive sections of the same meeting.\n"
            f"Merge them into ONE complete, non-redundant MoM JSON object per the schema.\n\n"
            f"{'=' * 40} PARTIAL JSON OBJECTS {'=' * 40}\n\n"
            f"{combined}\n\n"
            f"{'=' * 40} END {'=' * 40}\n\n"
            f"Now output the merged JSON object." + strict_note
        )
        return self.generate(synthesis_sys, synthesis_prompt, MAX_NEW_TOKENS_SYNTHESIS, temperature,
                             model=m, api_base=ab), len(chunks)

    _SYNTHESIS_INPUT_CHARS = 16000

    @classmethod
    def _join_partials(cls, partials, separator):
        """Join chunk partials for the synthesis pass, trimming EVERY partial evenly if the
        combined text exceeds the budget.

        The previous `combined[:16000]` cut from the end, which does not shorten the input so much
        as delete the last sections of the meeting outright — a 3-chunk transcript could lose all
        of chunk 3, and the only trace was a WARNING line. Everything after the cut is then absent
        from the merged MoM while the document still looks complete, which is the worst shape a
        truncation can take. Trimming each partial to an equal share keeps every section of the
        meeting represented; a partial already under its share is left whole.
        """
        combined = separator.join(partials)
        if len(combined) <= cls._SYNTHESIS_INPUT_CHARS or not partials:
            return combined
        budget = cls._SYNTHESIS_INPUT_CHARS - len(separator) * (len(partials) - 1)
        share = max(500, budget // len(partials))
        trimmed = [p if len(p) <= share else p[:share] for p in partials]
        logger.warning(
            "[MOM] Synthesis input %d chars > %d — trimming each of %d partials to <=%d chars "
            "(even trim, so no section is dropped)",
            len(combined), cls._SYNTHESIS_INPUT_CHARS, len(partials), share,
        )
        return separator.join(trimmed)

    def _fallback_metadata_from_text(self, text):
        """TODO(Stage 5): replace with metadata passed from the backend.
        Stage-3 bridge — read the FALLBACK DATE/TIME the backend injected into the transcript so the
        renderer has date/time values before backend timestamp generation is wired."""
        date = time = ""
        m = re.search(r'FALLBACK DATE:\s*(.+)', text)
        if m:
            date = m.group(1).strip()
        m = re.search(r'FALLBACK TIME:\s*(.+)', text)
        if m:
            time = m.group(1).strip()
        return {"date": date, "time": time, "venue": ""}

    @staticmethod
    def _bullets_to_list(text):
        """Split a focused-extraction bullet block into a clean list of strings."""
        out = []
        for line in (text or "").splitlines():
            line = line.strip().lstrip("•-*").strip()
            if line and "none explicitly stated" not in line.lower():
                out.append(line)
        return out

    def _decisions_list_weak(self, decisions):
        """True when the decisions list is empty OR every item is only a trivial closing remark."""
        if not decisions:
            return True
        return all(self._DECISIONS_TRIVIAL_RE.search(d) for d in decisions)

    # A key point that names no number, date, amount or outcome is a heading. Used to spot a
    # narrative pass that produced labels instead of content.
    _SPECIFIC_RE = re.compile(r"\d|\$|£|€|₹|%", re.UNICODE)

    # Capitalised tokens that are ordinary English, not names — checking these against the
    # transcript would drop good points for no reason.
    _NOT_A_NAME = frozenset("""the a an and or but if then this that these those we you they i he
        she it our your their his her its is are was were be been being have has had do does did
        will would shall should may might must can could there here what when where who whom which
        why how all any both each few more most other some such no nor not only own same so than
        too very just now also item motion second approved approval meeting minutes agenda council
        town attorney manager mayor member members chair january february march april may june july
        august september october november december monday tuesday wednesday thursday friday
        saturday sunday""".split())

    _CAP_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z'’\-]{2,})")

    @classmethod
    def _proper_nouns_supported(cls, text: str, transcript_lower: str):
        """Return the proper nouns in `text` that do NOT occur in the transcript.

        The quote check proves a REAL SPAN was quoted; it says nothing about the sentence built
        around it. Measured 2026-09-08: a point quoted a genuine phrase and then attached the name
        "Sandra Zersky" to it — the HOA attorney is Stan, and "Sandra" appears nowhere in the
        recording. A name the transcript never contains cannot have been said, so this is checkable
        without a model, exactly like the speaker-naming gate.
        """
        bad = []
        for m in cls._CAP_TOKEN_RE.finditer(text or ""):
            tok = m.group(1)
            if tok.lower() in cls._NOT_A_NAME:
                continue
            # Skip a capital that is only capitalised because it opens the sentence.
            if m.start() == 0 or text[max(0, m.start() - 2):m.start()].strip() in (".", "!", "?"):
                continue
            if tok.lower() not in transcript_lower:
                bad.append(tok)
        return bad

    # A bracket tag opening a line is a diarized speaker: "[Thomas] ..." or "[Speaker_3] ...".
    _LINE_TAG_RE = re.compile(r"(?m)^\s*\[([^\[\]\n]{1,60})\]")

    @staticmethod
    def _attendee_key(name):
        s = (name or "").strip().strip("[]").strip().lower()
        m = re.match(r"speaker[\s_-]*(\d+)$", s)
        return f"speaker_{m.group(1)}" if m else s

    _EVIDENCE_SCHEMA = {
        "type": "object",
        "properties": {"evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "quote": {"type": "string"}},
            "required": ["n", "quote"], "additionalProperties": False}}},
        "required": ["evidence"], "additionalProperties": False,
    }

    def _verify_decisions(self, content, transcript, temperature, route=None):
        """Keep only decisions the transcript can show the group settled.

        Key points are quote-checked and score 92-95; decisions are not and sit at 45-76. Measured
        2026-09-11, what survives in decisions is plain tasks ("The team will review the Issue Herder
        tool") and non-events ("The community contribution for Composer will continue"). Both are phrased
        as group plans, so no wording rule separates them from a real agreement — but neither has a moment
        in the transcript where anyone agreed. The model supplies the evidence; the code checks it appears
        verbatim, exactly as _ground_points does for key points, so the evidence cannot be invented.
        """
        decisions = [d for d in (content.get("decisions") or []) if isinstance(d, str) and d.strip()]
        if not decisions:
            return content
        listing = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(decisions))
        try:
            raw = self.generate(
                DECISIONS_VERIFY_PROMPT, f"TRANSCRIPT:\n{transcript}\n\nSTATEMENTS:\n{listing}",
                MAX_NEW_TOKENS_EXTRACTION, temperature,
                model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
                extra={"response_format": {"type": "json_schema", "json_schema": {
                    "name": "evidence", "strict": True, "schema": self._EVIDENCE_SCHEMA}}},
            )
            evidence = (json.loads(raw) or {}).get("evidence") or []
        except Exception as e:
            logger.warning(f"[MOM] DECISIONS: verification failed ({e!r}) — keeping every decision")
            return content

        hay = self._norm_quote(transcript)
        supported = set()
        for e in evidence:
            if not isinstance(e, dict):
                continue
            n, quote = e.get("n"), self._norm_quote(e.get("quote") or "")
            if isinstance(n, int) and 1 <= n <= len(decisions) and len(quote) >= self._MIN_QUOTE_CHARS and quote in hay:
                supported.add(n - 1)
        dropped = [d for i, d in enumerate(decisions) if i not in supported]
        if not supported:
            # Every decision unsupported means the verification itself misfired, not that the meeting
            # settled nothing. Emptying the section on that basis would be worse than leaving it alone.
            logger.warning(f"[MOM] DECISIONS: no decision could be evidenced — keeping all {len(decisions)}")
            return content
        if dropped:
            logger.info(f"[MOM] DECISIONS: dropped {len(dropped)} without evidence of agreement")
            for d in dropped[:6]:
                logger.info(f"[MOM]   ✗ {d[:80]!r}")
        content["decisions"] = [d for i, d in enumerate(decisions) if i in supported]
        return content

    _MERGE_SCHEMA = {
        "type": "object",
        "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
        "required": ["groups"], "additionalProperties": False,
    }

    def _merge_duplicate_items(self, content, field, temperature, route=None):
        """Merge entries describing the same work. The MODEL only groups numbers; the CODE decides what
        survives, so nothing can be reworded or invented while "merging".

        Word-overlap dedupe cannot see that "Revise and re-record the LEP course" and "The revised script
        will be recorded tomorrow" are one task — they share almost no words. Measured 2026-09-11, LEP came
        back with 19 action items for 12 real ones, the surplus being three wordings of the same work.
        Whether two tasks are the same is a judgement, which is what the model is for and the regexes are not.
        """
        items = [x for x in (content.get(field) or []) if x]
        if len(items) < 2:
            return content
        text_of = (lambda x: x if isinstance(x, str) else (x or {}).get("task", ""))
        owner_of = (lambda x: "" if isinstance(x, str) else (x.get("assigned_to") or ""))
        listing = "\n".join(f"{i + 1}. {text_of(x)}" for i, x in enumerate(items))
        try:
            raw = self.generate(
                ITEMS_MERGE_PROMPT, f"ENTRIES:\n{listing}", MAX_NEW_TOKENS_EXTRACTION, temperature,
                model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
                extra={"response_format": {"type": "json_schema", "json_schema": {
                    "name": "groups", "strict": True, "schema": self._MERGE_SCHEMA}}},
            )
            groups = (json.loads(raw) or {}).get("groups") or []
        except Exception as e:
            logger.warning(f"[MOM] {field.upper()}: duplicate merge failed ({e!r}) — keeping every entry")
            return content

        dropped, notes = set(), []
        for group in groups:
            idx = sorted({i - 1 for i in group if isinstance(i, int) and 1 <= i <= len(items)})
            idx = [i for i in idx if i not in dropped]
            if len(idx) < 2:
                continue
            # Keep the entry that says the most: one with an owner beats one without, then the longest text.
            keep = max(idx, key=lambda i: (bool(owner_of(items[i])), len(text_of(items[i]))))
            for i in idx:
                if i == keep:
                    continue
                if isinstance(items[keep], dict) and isinstance(items[i], dict):
                    for f in ("assigned_to", "assigned_by", "due"):      # a duplicate may carry the detail
                        if not (items[keep].get(f) or "").strip():
                            items[keep][f] = items[i].get(f) or ""
                dropped.add(i)
                notes.append(f"{text_of(items[i])[:60]!r} → {text_of(items[keep])[:60]!r}")
        if dropped:
            logger.info(f"[MOM] {field.upper()}: merged {len(dropped)} duplicate(s) of {len(items)}")
            for n in notes[:6]:
                logger.info(f"[MOM]   {n}")
        content[field] = [x for i, x in enumerate(items) if i not in dropped]
        return content

    def _reconcile_attendees(self, content, transcript):
        """Remove attendees the transcript cannot support — WITHOUT pairing names to speaker labels.

        An earlier version matched each attendee to a speaker label, added labels nobody claimed and
        dropped "duplicates". Measured 2026-09-10 on Town Council it did damage twice: first it added
        four anonymous voices that were council members already listed by name; then, when speaker
        naming put titles into the labels ("Council Member Stephanie Miller"), a label's first word
        matched every council member and three real members plus the Mayor Pro Tem were deleted.
        Titles, spellings ("Laces" / "Lasis" / "Placis") and word order vary too much for a label to
        be paired with a name reliably, and the case it existed for — an introduction Whisper never
        transcribed — is fixed upstream by the re-transcription check. So only rules that need no
        pairing remain:
          • a [Speaker_N] that no transcript line carries is an invented voice — dropped
          • a person whose name occurs nowhere in the transcript — dropped
        """
        tags = {self._attendee_key(t) for t in self._LINE_TAG_RE.findall(transcript or "") if t.strip()}
        if not tags:
            return content                  # no diarization → nothing to check against
        low = (transcript or "").lower()
        kept, dropped = [], []
        for a in content.get("attendees") or []:
            if not isinstance(a, dict):
                continue
            name = (a.get("name") or "").strip()
            k = self._attendee_key(name)
            if not k:
                continue
            if k.startswith("speaker_"):
                (kept if k in tags else dropped).append(a if k in tags else name)
            # ANY word of the name, not the first: in "Council Member Jen Cowish" the first word is a
            # title that is spoken all meeting, which made the check meaningless for titled names. Any
            # word also survives the model repairing a spelling ("Craycraft" for "Kracraft").
            elif any(re.search(rf"\b{re.escape(t)}\b", low) for t in k.split() if len(t) >= 3):
                kept.append(a)
            else:
                dropped.append(name)
        if dropped:
            logger.info(f"[MOM] ATTENDEES: dropped {len(dropped)} unsupported: {dropped}")
        content["attendees"] = kept
        return content

    # Words that make up a job title without saying anything specific about the person.
    _ROLE_GENERIC = frozenset("""team teams member members lead leads leader leaders manager managers management
        engineer engineers engineering staff employee employees participant participants attendee attendees host
        contributor contributors colleague colleagues stakeholder stakeholders representative person individual
        senior junior principal associate specialist unknown none role organization organisation""".split())
    _ROLE_STOP = frozenset("the a an of and for in on at to with from by as".split())

    def _ground_attendee_roles(self, content, transcript):
        """Replace invented job titles with "Unknown".

        The prompt says a role must be stated in the transcript and "Unknown" is otherwise the right
        answer. Llama ignores that: measured 2026-09-10, three runs of one staff meeting all gave
        "Team Lead", "Manager" and "Team Member" to people whose titles are never mentioned, and the
        Package Team run gave "Team Member" to five people. The fabrications are always built only
        from generic title words, so a role part is kept only if it contains a SPECIFIC word
        ("Council", "Marketing", "Security Policies", "Front-end") that occurs in the transcript.
        Checked per comma-separated part: "Engineering Manager, SEC" keeps the real "SEC" and loses
        the invented title. This can only turn a role into "Unknown" — it never adds a claim.
        Known gap: a fabricated title with a specific word that happens to be spoken
        ("Security Engineer" in a security meeting) still passes.
        """
        low = (transcript or "").lower()
        changed = []
        for a in content.get("attendees") or []:
            if not isinstance(a, dict):
                continue
            role = (a.get("role") or "").strip()
            if not role or role.lower() == "unknown":
                continue
            kept = []
            for part in (p.strip() for p in role.split(",")):
                words = [w for w in re.findall(r"[a-z0-9]+", part.lower()) if w not in self._ROLE_STOP]
                specific = [w for w in words if w not in self._ROLE_GENERIC]
                if specific and any(re.search(rf"\b{re.escape(w)}\b", low) for w in specific):
                    kept.append(part)
            new = ", ".join(kept) or "Unknown"
            if new != role:
                changed.append(f"{a.get('name', '')}: {role!r} → {new!r}")
                a["role"] = new
        if changed:
            logger.info(f"[MOM] ATTENDEES: {len(changed)} role(s) not supported by the transcript")
            for c in changed[:8]:
                logger.info(f"[MOM]   {c}")
        return content

    def _drop_unsupported_names(self, items, transcript, field, get=lambda x: x):
        """Remove items naming a person/place/thing the transcript never mentions."""
        low = (transcript or "").lower()
        kept, dropped = [], []
        for it in items:
            bad = self._proper_nouns_supported(get(it), low)
            (dropped if bad else kept).append((it, bad))
        if dropped:
            logger.info(f"[MOM] {field}: dropped {len(dropped)} item(s) naming something never said")
            for it, bad in dropped[:4]:
                logger.info(f"[MOM]   ✗ {get(it)[:64]!r} — not in transcript: {bad}")
        return [it for it, _ in kept]

    # ── High-recall, quote-grounded extraction ───────────────────────────────
    # Windows overlap so a point straddling a boundary is seen whole at least once.
    _WINDOW_CHARS = 1800
    _WINDOW_OVERLAP = 350
    # A quote shorter than this proves nothing — "the" appears in every transcript. 14, not 18:
    # at 18 the gate rejected "passes unanimously" (17 chars) and "I will adjourn at 209" (17),
    # both real quotes of the two facts that keep going missing from the end of a meeting. The
    # proper-noun check is the second line of defence, so this one need only exclude the trivial.
    _MIN_QUOTE_CHARS = 14

    _POINT_SCHEMA = {
        "type": "object",
        "properties": {"points": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "quote": {"type": "string"},
                "type": {"type": "string", "enum": ["key_point", "decision", "action_item"]},
                "owner": {"type": "string"},
                "due": {"type": "string"},
            },
            "required": ["text", "quote", "type", "owner", "due"],
            "additionalProperties": False}}},
        "required": ["points"], "additionalProperties": False,
    }

    @classmethod
    def _windows(cls, text: str):
        """Overlapping windows over the transcript, split on line boundaries where possible."""
        out, i, n = [], 0, len(text)
        while i < n:
            end = min(i + cls._WINDOW_CHARS, n)
            if end < n:                       # prefer a newline, else a sentence end
                cut = text.rfind("\n", i + cls._WINDOW_CHARS // 2, end)
                if cut == -1:
                    cut = text.rfind(". ", i + cls._WINDOW_CHARS // 2, end)
                if cut != -1:
                    end = cut + 1
            out.append(text[i:end])
            if end >= n:
                break
            i = max(i + 1, end - cls._WINDOW_OVERLAP)
        return out

    # Fillers a model silently drops when quoting speech. Removing them from BOTH sides is what
    # lets an honest quote match; they carry no meaning, so nothing can be smuggled in by it.
    _QUOTE_FILLER_RE = re.compile(r"\b(uh|um|erm|ah|mm|hmm|like|you know|i mean|sort of|kind of)\b")

    @classmethod
    def _norm_quote(cls, s: str) -> str:
        """Normalised form for substring checking.

        A character-for-character rule is too strict to be usable: measured 2026-09-08, it deleted
        four TRUE points because the model tidied its own quote — "I will adjourn at 2:09" against a
        transcript reading "adjourn at 209", and "yeah nick i thought you could" against "yeah,
        Nick, uh, I thought you could". Both are honest quotes of real speech. Dropping fillers and
        punctuation keeps the words and their ORDER as the thing being verified, which is what
        actually distinguishes a real quote from an invented one.
        """
        t = (s or "").lower()
        t = cls._QUOTE_FILLER_RE.sub(" ", t)
        return re.sub(r"[^a-z0-9]+", "", t)

    def _ground_points(self, points, transcript):
        """Delete every point whose quote is not verbatim in the transcript.

        THIS IS THE PRECISION MECHANISM, and it is deliberately dumb: a substring test in Python,
        not a model asked whether it told the truth. The literature calls this substring-based
        evidence grounding, and it is the only check in this pipeline that a model cannot talk
        its way past. Measured failures it removes outright: "the next meeting will be after
        Thanksgiving" (nobody said it), "Ensure payment of $150,000 annually" (invented
        obligation), "Council Member Serbu said ..." (real fact, invented attribution) — none of
        them can produce a quote that exists in the transcript.
        """
        hay = self._norm_quote(transcript)
        kept, dropped = [], []
        for p in points:
            if not isinstance(p, dict):
                continue
            text = (p.get("text") or "").strip()
            quote = (p.get("quote") or "").strip()
            if not text:
                continue
            nq = self._norm_quote(quote)
            if len(nq) < self._MIN_QUOTE_CHARS or nq not in hay:
                dropped.append((text, quote))
                continue
            kept.append(p)
        if dropped:
            logger.info(f"[MOM] GROUNDING dropped {len(dropped)} unsupported point(s)")
            for t, q in dropped[:5]:
                logger.info(f"[MOM]   ✗ {t[:70]!r} — quote not in transcript: {q[:50]!r}")
        return kept

    # Second attempt for a window whose schema-constrained reply failed. Strict JSON-schema decoding can
    # trap the model in whitespace — JSON allows it anywhere, so it can emit it until max_tokens. Measured
    # 2026-09-10 on Town Council: two windows returned ~130 characters after spending all 1500 tokens,
    # 3 times out of 3, so repeating the same request never helped and half the meeting's windows were
    # lost. json_object keeps the reply valid JSON without a schema to trap on: on the same two windows
    # it finished normally both times, returning a bare array of complete points (_parse_points takes
    # that shape). Only a window that already failed ever reaches this, so it cannot make a good window
    # worse.
    _WINDOW_FALLBACK_FORMAT = {"response_format": {"type": "json_object"}}

    @staticmethod
    def _parse_points(raw, i, total):
        """Parse a window response, falling back to the deterministic repairs already in the
        renderer before declaring the slice lost."""
        from localization.mom_i18n import _extract_json, _repair_json

        def objects(text):
            # Last resort: every complete {...} carrying a "text" key, wherever it sits. Without the
            # schema the model was measured to answer "Here are the extracted points:\n\n1. {...}\n\n
            # 2. {...}" — each object whole, the document not JSON. Anything invented is still removed
            # by _ground_points, whose quote must occur verbatim in the transcript.
            dec, found, k = json.JSONDecoder(), [], 0
            while (j := text.find("{", k)) >= 0:
                try:
                    obj, k = dec.raw_decode(text, j)
                    if isinstance(obj, dict) and obj.get("text"):
                        found.append(obj)
                except ValueError:
                    k = j + 1
            if not found:
                raise ValueError("no complete point objects")
            return found

        for attempt, parse in (("direct", json.loads), ("extract", _extract_json), ("repair", _repair_json),
                               ("objects", objects)):
            try:
                data = parse(raw)
            except Exception:
                continue
            points = (data.get("points") if isinstance(data, dict)
                      else data if isinstance(data, list) and all(isinstance(x, dict) for x in data)
                      else None)
            if isinstance(points, list):
                if attempt != "direct":
                    logger.info(f"[MOM] window {i+1}/{total} needed {attempt} to parse")
                return points
        raise ValueError("no points array in response")

    def _extract_windows(self, transcript, temperature, route=None):
        """Read the transcript in overlapping windows and return grounded, deduped points.

        WHY WINDOWS: a single pass over a whole meeting is a COMPRESSION task, and the model
        compresses — measured, it returned 5 headings for a 9-minute meeting and dropped 8 of 22
        topics on a 29-minute one. A window is small enough that there is nothing to compress, so
        coverage scales with length instead of collapsing. Overlap means a point that straddles a
        boundary is seen whole in at least one window.
        """
        route = route or {}
        wins = self._windows(transcript)
        logger.info(f"[MOM] WINDOW EXTRACTION — {len(wins)} window(s) of ~{self._WINDOW_CHARS} chars")
        def _one(i_w):
            i, w = i_w
            # TWO ATTEMPTS. A window that returns nothing takes its whole slice of the meeting
            # with it, and until now that showed up as a single WARNING line. Structured outputs
            # are meant to make malformed JSON impossible, but OpenRouter enforces the schema PER
            # PROVIDER and some treat it as a strong hint, so it still arrives. A second sample
            # almost always parses; if it does not, the log says so at ERROR naming the section,
            # because silent partial coverage is the worst outcome here.
            schema = {"response_format": {"type": "json_schema", "json_schema": {
                "name": "points", "strict": True, "schema": self._POINT_SCHEMA}}}
            for attempt, extra in ((1, schema), (2, self._WINDOW_FALLBACK_FORMAT)):
                try:
                    out = self.generate(
                        WINDOW_EXTRACTION_PROMPT,
                        f"SECTION {i+1} OF {len(wins)}:\n─────\n{w}\n─────\n\nExtract every point.",
                        MAX_NEW_TOKENS_EXTRACTION, temperature,
                        model=route.get("model"), api_base=route.get("api_base"),
                        extra=extra,
                    )
                    return self._parse_points(out, i, len(wins))
                except Exception as e:
                    if attempt == 1:
                        logger.warning(f"[MOM] window {i+1}/{len(wins)} failed ({e!r}) — retrying without the JSON schema")
                    else:
                        logger.error(f"[MOM] window {i+1}/{len(wins)} LOST after retry ({e!r}) — "
                                     f"~{len(w)} chars of this meeting are not represented")
            return []

        # Windows are independent, so run them together. Sequentially this pass took 776s on a
        # 9-minute meeting against 189s for the whole previous pipeline; the work is all latency,
        # not compute on this box.
        raw_points = []
        with ThreadPoolExecutor(max_workers=WINDOW_CONCURRENCY) as pool:
            for pts in pool.map(_one, enumerate(wins)):
                raw_points.extend(pts)

        grounded = self._ground_points(raw_points, transcript)

        deduped, seen, seen_terms = [], set(), []
        for p in grounded:
            key, terms = self._task_key(p["text"]), self._task_terms(p["text"])
            if not key or key in seen or self._is_near_duplicate(terms, seen_terms):
                continue
            seen.add(key); seen_terms.append(terms); deduped.append(p)

        by_type = {}
        for p in deduped:
            by_type.setdefault(p.get("type", "key_point"), []).append(p)
        logger.info(
            f"[MOM] WINDOWS: {len(raw_points)} raw → {len(grounded)} grounded → {len(deduped)} unique "
            f"({len(by_type.get('key_point', []))} points, {len(by_type.get('decision', []))} decisions, "
            f"{len(by_type.get('action_item', []))} actions)"
        )
        return by_type

    def _fill_key_points_json(self, content, transcript, temperature, route=None):
        """Run a focused key-points extraction and MERGE it with the main pass's list.

        WHY THIS EXISTS — the same reason _fill_figures_json does, applied to the field the API
        contract actually leads with. The main pass is a NARRATIVE pass, and abstractive
        summarisation reliably drops enumerated content rather than shortening it; the codebase
        already carries three of these passes (decisions, action items, figures) for exactly that.
        key_points is a list with the same failure mode and had no such pass.

        Measured 2026-09-08 on a 9-minute town council meeting via llama-3.3-70b: the narrative
        pass emitted FIVE key points, all of them headings ("Council members' expressions of
        support for the agreement"), and stopped at 3,451 chars against a 4,000-TOKEN budget — so
        this is not truncation, the model simply had no instruction forcing density. The same
        transcript's figures pass returned 7/7 correct amounts and dates. Everything the narrative
        pass dropped — the $150,000 annual fence maintenance, the 14-day termination notice,
        Resolution R-92 — was recoverable; nothing was asking for it.

        Merging rather than replacing keeps the narrative pass's phrasing, which reads better, and
        adds only what it left out. Dedup reuses the same near-duplicate test as decisions.
        """
        existing = [k for k in (content.get("key_points") or []) if isinstance(k, str) and k.strip()]
        thin = sum(1 for k in existing if not self._SPECIFIC_RE.search(k))
        logger.info(
            f"[MOM] KEY POINTS — focused extraction pass [JSON] "
            f"(main pass gave {len(existing)}, {thin} without a specific detail)"
        )
        focused = self.generate(
            KEY_POINTS_EXTRACTION_PROMPT,
            f"TRANSCRIPT:\n{transcript}\n\nList every point discussed:", MAX_NEW_TOKENS_EXTRACTION, temperature,
            model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
        ).strip()

        merged = list(existing)
        seen = {self._task_key(k) for k in existing}
        seen_terms = [self._task_terms(k) for k in existing]
        added = 0
        for item in self._bullets_to_list(focused):
            key = self._task_key(item)
            terms = self._task_terms(item)
            if not key or key in seen or self._is_near_duplicate(terms, seen_terms):
                continue
            seen.add(key)
            seen_terms.append(terms)
            merged.append(item)
            added += 1

        if merged:
            content["key_points"] = merged
        logger.info(f"[MOM] KEY POINTS: {len(existing)} main + {added} recovered = {len(merged)}")
        return content

    def _fill_decisions_json(self, content, transcript, temperature, route=None):
        """Run the focused decisions extraction and MERGE it with the main pass's decisions.

        Was gated on `if not weak: return` — i.e. the fill ran only when the main pass produced
        nothing usable. Same defect as the action-items gate (see _fill_action_items_json): the
        narrative pass compresses a list rather than emptying it, so a couple of surviving
        decisions suppressed the extraction that would have found the rest. Measured 2026-09-07
        on a real 29-minute staff meeting: 2 decisions survived the main pass, the gate skipped
        the fill, and ~5 others never made the record — among them a manager appointment and an
        agreed interim process for performance reviews. Trivial closing remarks are still dropped
        on the way in, which is what _decisions_list_weak was really guarding against.
        """
        existing = [d for d in (content.get("decisions") or []) if isinstance(d, str) and d.strip()]
        logger.info(f"[MOM] DECISIONS — focused extraction pass [JSON] (main pass gave {len(existing)})")
        focused = self.generate(
            DECISIONS_EXTRACTION_PROMPT,
            f"TRANSCRIPT:\n{transcript}\n\nList all decisions:", MAX_NEW_TOKENS_EXTRACTION, temperature,
            model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
        ).strip()

        merged = list(existing)
        seen = {self._task_key(d) for d in existing}
        seen_terms = [self._task_terms(d) for d in existing]
        added = 0
        for item in self._bullets_to_list(focused):
            if self._DECISIONS_TRIVIAL_RE.search(item):
                continue
            key = self._task_key(item)
            terms = self._task_terms(item)
            if not key or key in seen or self._is_near_duplicate(terms, seen_terms):
                continue
            seen.add(key)
            seen_terms.append(terms)
            merged.append(item)
            added += 1

        if merged:
            content["decisions"] = merged
        logger.info(f"[MOM] DECISIONS: {len(existing)} main + {added} recovered = {len(merged)}")
        return content

    def _fill_figures_json(self, content, transcript, temperature, route=None):
        """Populate KEY FIGURES from a focused extraction pass.

        Unlike the decisions/action-item fills, this ALWAYS runs rather than only on an empty
        field, because the main analysis pass has no figures field to leave empty — it drops
        figures into prose and then compresses them away.

        Measured 2026-07-24 on a real 19.7-minute budget meeting: seven figures that WERE
        correctly transcribed (personnel Rs120,000, facilities Rs58,340, utilities Rs14,500,
        warehouse lease Rs51,840, maintenance Rs6,500, supplies Rs28,000, contingency Rs12,000)
        never reached the MoM. The summary rendered them as "A recalculated budget line-item list
        was presented". That is the documented dominant failure of abstractive summarisation —
        omission, not hallucination — and it worsens with length, so prompt tuning alone does not
        fix it. The literature's remedy is to extract facts in a separate pass and let the
        narrative pass stay narrative, which is the pattern this codebase already uses for
        decisions and action items.

        Self-conditional: a meeting with no figures yields "None explicitly stated", the field
        stays empty, and the renderer omits the section entirely.
        """
        if content.get("key_figures"):
            return content
        logger.info("[MOM] KEY FIGURES — running focused extraction pass [JSON]")
        focused = self.generate(
            FIGURES_EXTRACTION_PROMPT,
            f"TRANSCRIPT:\n{transcript}\n\nList every figure stated:", MAX_NEW_TOKENS_EXTRACTION, temperature,
            model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
        ).strip()
        if not focused or "none explicitly stated" in focused.lower():
            return content  # genuinely no figures — section will not be rendered
        items = self._bullets_to_list(focused)
        if items:
            content["key_figures"] = items
        return content

    # "<task> — Owner: <name> — Due: <deadline>" — the shape ACTION_ITEMS_EXTRACTION_PROMPT emits.
    _OWNER_DUE_RE = re.compile(
        r"\s*[—–-]\s*Owner:\s*(?P<owner>.*?)\s*(?:[—–-]\s*Due:\s*(?P<due>.*?))?\s*$",
        re.IGNORECASE,
    )
    # Values the extraction prompt uses when there is no NAMED owner. assigned_to means "a named
    # individual owns this", so every one of these must land as empty rather than be shown to a
    # reader as if it were a person. "Speaker" and "Team" both appeared in real output.
    _UNSET = {"not specified", "none", "n/a", "tbd", "unknown", "team", "speaker", "the speaker",
              "facilitator", "the facilitator", "host", "the host", "group", "the group",
              "everyone", "all", "participants", "attendees", ""}

    @classmethod
    def _parse_action_bullet(cls, bullet: str) -> dict:
        """Turn one extraction bullet into the structured action_item shape.

        The focused pass DOES extract the owner and the deadline; the previous code packed the
        whole bullet into `task` and left assigned_to/due empty, so every consumer reading the
        structured fields — the renderer's "Assigned to:" line included — saw nothing.
        """
        bullet = (bullet or "").strip()
        m = cls._OWNER_DUE_RE.search(bullet)
        if not m:
            return {"task": bullet, "assigned_to": "", "assigned_by": "", "due": ""}
        owner = (m.group("owner") or "").strip()
        due = (m.group("due") or "").strip()
        return {
            "task": bullet[: m.start()].strip(),
            # "Team" is the prompt's placeholder for a group task, not a person — leaving it out
            # keeps assigned_to meaning "a named individual owns this".
            "assigned_to": "" if owner.lower() in cls._UNSET else owner,
            "assigned_by": "",
            "due": "" if due.lower() in cls._UNSET else due,
        }

    _STOPWORDS = frozenset("a an the and or of to for on in at by with within is are be will "
                           "that this it its as from up".split())

    @classmethod
    def _task_key(cls, task: str) -> str:
        """Normalised key for deduping the same task phrased differently across passes."""
        return re.sub(r"[^a-z0-9 ]+", "", (task or "").lower()).strip()

    @staticmethod
    def _stem(w: str) -> str:
        """Crude suffix strip so "act"/"acting" and "policy"/"policies" compare equal."""
        for suf in ("ingly", "edly", "ing", "ies", "ied", "ed", "es", "ly", "s"):
            if len(w) > len(suf) + 2 and w.endswith(suf):
                return w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
        return w

    @classmethod
    def _task_terms(cls, task: str) -> frozenset:
        """Stemmed content words of a task, for near-duplicate detection."""
        return frozenset(cls._stem(w) for w in cls._task_key(task).split() if w not in cls._STOPWORDS)

    @classmethod
    def _is_near_duplicate(cls, terms, seen_terms) -> bool:
        """True when `terms` restates a task already kept.

        Exact-key dedup is not enough across two passes that word things differently: the main
        pass and the focused pass both found the development-vision spreadsheet and it appeared
        twice as "Complete the development vision and mission feedback spreadsheet" and "Complete
        the feedback spreadsheet on development vision and mission within the next week". Those
        share every content word, so containment catches them while leaving genuinely distinct
        tasks alone.
        """
        if not terms:
            return False
        for prev in seen_terms:
            if not prev:
                continue
            overlap = len(terms & prev)
            # Both conditions matter. The ratio alone would merge "Update the yaml files" into
            # "Review the yaml files" (2 of 3 words shared, different task); the absolute floor of
            # 3 shared content words stops that. The ratio then catches the real case, where one
            # pass states a task tersely and the other restates it with its context attached.
            if overlap >= 3 and overlap >= min(len(terms), len(prev)) * 0.6:
                return True
            # CONTAINMENT. The floor of 3 above is deliberately blind to short restatements:
            # "Approved the agenda" {approv, agenda} against "The town council approved the agenda
            # for the meeting" overlaps on only 2 and survived as a second decision. When every
            # content word of one item appears in the other, it adds nothing — and this cannot
            # revive the case the floor guards against, since {updat, yaml, file} is NOT a subset
            # of {review, yaml, file}.
            if len(terms) >= 2 and (terms <= prev or prev <= terms):
                return True
        return False

    def _fill_action_items_json(self, content, transcript, temperature, route=None):
        """Run the focused action-item extraction and MERGE it with the main pass's items.

        This used to be gated on `if content.get("action_items"): return` — the fill ran only when
        the main pass found NOTHING. That gate is wrong for the same reason KEY FIGURES stopped
        using one (see _fill_figures_json): the main pass is a narrative pass, and a narrative pass
        under chunk+synthesise compresses a list down rather than emptying it. One surviving item
        therefore SUPPRESSED the extraction that would have found the rest.

        Measured 2026-09-07 on a real 29-minute engineering staff meeting: the main pass returned
        1 action item, the gate skipped the fill, and 8 assignments went missing — including two
        with an explicit named owner and deadline ("report back by the end of the quarter",
        "run it by David de Santo tomorrow"). Running the focused pass unconditionally and merging
        recovered them. Dedup is on normalised task text, and the main pass wins on a collision
        because its items carry assigned_by, which the bullet format cannot express.
        """
        existing = [a for a in (content.get("action_items") or []) if isinstance(a, dict)]
        logger.info(f"[MOM] ACTION ITEMS — focused extraction pass [JSON] (main pass gave {len(existing)})")
        focused = self.generate(
            ACTION_ITEMS_EXTRACTION_PROMPT,
            f"TRANSCRIPT:\n{transcript}\n\nList all action items:", MAX_NEW_TOKENS_EXTRACTION, temperature,
            model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
        ).strip()

        merged = list(existing)
        seen = {self._task_key(a.get("task", "")) for a in existing}
        seen_terms = [self._task_terms(a.get("task", "")) for a in existing]
        added = 0
        for bullet in self._bullets_to_list(focused):
            item = self._parse_action_bullet(bullet)
            key = self._task_key(item["task"])
            terms = self._task_terms(item["task"])
            if not key or key in seen or self._is_near_duplicate(terms, seen_terms):
                continue
            seen.add(key)
            seen_terms.append(terms)
            merged.append(item)
            added += 1

        if merged:
            content["action_items"] = merged
        logger.info(f"[MOM] ACTION ITEMS: {len(existing)} main + {added} recovered = {len(merged)}")
        return content

    # Words a model writes into a field it has nothing to put in. They render as though a real
    # person or date were recorded — "Assigned to: none" reads like an assignee called None.
    _PLACEHOLDER_VALUES = frozenset({
        "none", "null", "n/a", "na", "nil", "tbd", "unknown", "not specified", "unspecified",
        "no one", "nobody", "not applicable", "team", "-", "--", "",
    })

    # An action item is a task someone will DO. These are records of something that already
    # happened — a motion made, a vote taken — which belong in DECISIONS and were being copied
    # into ACTION ITEMS as well.
    _PAST_RECORD_RE = re.compile(
        r"\b(was|were)\s+(made|seconded|approved|passed|carried|adopted|held|taken|read|"
        r"introduced|opened|closed|adjourned|asked|requested|invited|thanked|noted)\b|"
        r"\bpassed\s+unanimously\b|\b(motion|vote|roll\s*call)\s+(was|passed|carried)\b",
        re.IGNORECASE,
    )
    # ...but "was asked" is only a record when nothing is still outstanding. "Nick was asked to
    # give an overview" happened during the meeting; "X was asked to report back by Friday" is a
    # real task. A deadline or a forward-looking word is what separates them.
    # A bare 4-digit number is NOT a deadline. "Resolution Number R-92, series 2025" matched the
    # old \d{4} and kept a past record alive as an action item. A year only counts as a due date
    # when a date word introduces it.
    _FUTURE_MARKER_RE = re.compile(
        r"\b(by|before|within|no later than|upcoming|will|shall|due|deadline)\b"
        r"|\bnext\s+(week|month|quarter|year|meeting)\b"
        r"|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
        r"|\b(january|february|march|april|june|july|august|september|october|november|december)\b",
        re.IGNORECASE)

    # Phrases a model writes when it narrates its own reasoning into the answer. Adding the
    # NEVER-INVENT rules to the extraction prompts produced exactly this: a key figure reading
    # "after Thanksgiving (NO: this is an inference. The correct output is None ...)". The
    # judgement is right and it does not belong in minutes a client reads.
    _META_MARKER_RE = re.compile(
        r"\b(this is an inference|not explicitly stated|the correct output|implied[,;]|"
        r"cannot be determined|no specific date (?:was )?mentioned|inferred from|"
        r"assuming|presumably|it is unclear|note:|NO:)\b", re.IGNORECASE)
    _PARENTHETICAL_RE = re.compile(r"\s*\(([^()]*)\)")

    @classmethod
    def _strip_meta(cls, text: str) -> str:
        """Remove parentheticals that explain the model's reasoning, keep the ones that inform.

        "(R-92, Series 2025)" is content. "(implied, not explicitly stated as start date)" is the
        model thinking out loud. Only the second kind matches a meta marker.
        """
        def repl(m):
            return "" if cls._META_MARKER_RE.search(m.group(1)) else m.group(0)
        out = cls._PARENTHETICAL_RE.sub(repl, text or "")
        return re.sub(r"\s{2,}", " ", out).strip(" .,;:") or ""

    _NUMBER_RE = re.compile(r"\d[\d,.]*")

    @classmethod
    def _drop_repeated_figures(cls, points):
        """Drop a point that repeats a figure another point already made.

        Measured: "$150,000 a year on fence maintenance" and "roughly $150,000 annually on fence
        maintenance" both survived as separate key points. Sharing a NUMBER is not enough on its
        own — "starts November 19, 2025" and "ends December 1, 2026" are two different facts about
        two different numbers — so a point is only dropped when it repeats the same number AND
        most of the same words.
        """
        kept, seen = [], []          # [(numbers, terms)]
        for p in points:
            nums = frozenset(cls._NUMBER_RE.findall(p))
            terms = cls._task_terms(p)
            dup = any(
                nums and nums & n and terms and
                len(terms & t) >= max(2, int(min(len(terms), len(t)) * 0.6))
                for n, t in seen
            )
            if dup:
                logger.info(f"[MOM] dropped repeated figure: {p[:64]!r}")
                continue
            seen.append((nums, terms))
            kept.append(p)
        return kept

    # CROSS-FIELD duplicate detection has to be much stricter than within-field. A task and a
    # decision about the same subject share every word of that subject's name: "Continue
    # discussions with the Rock Creek Homeowners Association" and "The council approved the
    # tolling agreement with the Rock Creek Homeowners Association" overlap on four tokens of a
    # four-word proper noun and nothing else, which the 0.6 within-field ratio reads as a
    # duplicate. It is not — one is a future task, the other a past decision. 0.85 requires them
    # to share almost everything, not just who they are about.
    _RESTATEMENT_RATIO = 0.85

    @classmethod
    def _is_restatement(cls, terms, others):
        """True when `terms` says essentially the same thing as one of `others`."""
        if not terms:
            return False
        for other in others:
            if not other:
                continue
            overlap = len(terms & other)
            # Against the LONGER text, and rounded UP. Measured against the shorter one, a short task
            # sitting inside a longer decision counted as a restatement and was deleted: 2026-09-10
            # this threw away 7 of 19 real LEP tasks ("Send the I Speak guides to Culp", "Remediate
            # the website and forms by April") and 4 of 7 on Package Team. Minutes legitimately carry
            # a decision and the task it creates; only a near word-for-word repeat is a restatement.
            if overlap >= max(3, math.ceil(max(len(terms), len(other)) * cls._RESTATEMENT_RATIO)):
                return True
        return False

    @classmethod
    def _clean_placeholder(cls, value: str) -> str:
        """'' for anything that is a stand-in rather than an answer."""
        v = (value or "").strip()
        return "" if v.lower().strip(" .") in cls._PLACEHOLDER_VALUES else v

    # Two forms, and neither may swallow the spaces around it — a bare \s* on the outside turns
    # "and Speaker 4 replied" into "anda participantreplied". The single-letter variant requires a
    # CAPITAL so the plain word "speakers" is not mangled.
    # No IGNORECASE: it defeats the capital-letter requirement below and turns the ordinary word
    # "speakers" into "a participant".
    _SPEAKER_TAG_RE = re.compile(
        r"\[\s*[Ss]peaker[\s_-]*(?:\d+|[A-Za-z])\s*\]"   # [Speaker_2] — bracketed, any letter
        r"|\b[Ss]peaker[\s_-]*(?:\d+|[A-Z])\b")           # Speaker 4 / Speaker A — bare, needs a capital

    def _rewrite_summary_json(self, content, temperature, route=None):
        """Rewrite the summary from the VERIFIED record instead of the raw transcript.

        The narrative pass reads the transcript directly, which is why the summary has been the
        weakest field in every scored run: it can state things no extraction ever verified. That is
        where "[Speaker_2] asked about new judicial appointments" and "the next meeting set for
        September 28th" came from — a wrong date invented in prose while the extraction had the
        right one. Handing it only the checked facts makes an ungrounded summary structurally
        impossible, and its coverage inherits key_points', which measures ~91%.

        Falls back to the existing summary on any failure: a narrative summary with a flaw beats no
        summary at all.
        """
        points = [p for p in (content.get("key_points") or []) if isinstance(p, str)]
        if len(points) < 3:
            return content                      # too little verified material to improve on
        record = "\n".join(f"- {p}" for p in points)
        for d in (content.get("decisions") or []):
            record += f"\n- DECIDED: {d}"
        for a in (content.get("action_items") or []):
            if isinstance(a, dict) and a.get("task"):
                who = f" (owner: {a['assigned_to']})" if a.get("assigned_to") else ""
                record += f"\n- TASK: {a['task']}{who}"
        try:
            out = self.generate(
                SUMMARY_FROM_POINTS_PROMPT,
                f"VERIFIED RECORD OF THE MEETING:\n{record}\n\nWrite the summary.",
                MAX_NEW_TOKENS_EXTRACTION, temperature,
                model=(route or {}).get("model"), api_base=(route or {}).get("api_base"),
            ).strip()
        except Exception as e:
            logger.warning(f"[MOM] summary rewrite failed ({e!r}) — keeping the narrative summary")
            return content
        # A speaker tag in the summary is the single most visible defect in these minutes, and the
        # prompt has been ignored on it before. Remove it here rather than asking again.
        out = self._SPEAKER_TAG_RE.sub("a participant", out).strip()
        if len(out) < 120:
            logger.warning(f"[MOM] summary rewrite too short ({len(out)} chars) — keeping the original")
            return content
        logger.info(f"[MOM] SUMMARY rewritten from {len(points)} verified points "
                    f"({len(content.get('summary') or '')} → {len(out)} chars)")
        content["summary"] = out
        return content

    @staticmethod
    def _normalise_action_items(content):
        """Enforce the action-item invariants the PROMPT states but the model does not keep.

        Both prompts say "NEVER put the same person in both fields", and both models do it
        anyway: 5 of 10 items on a real committee meeting came back Speaker_1 / Speaker_1.
        That is the model describing a SELF-COMMITMENT ("I'll close the issue") and filling
        the assigner slot with the owner because the schema gives it nowhere else to put that.

        Clearing the duplicate is the right call here rather than a schema change: the
        assigner field exists to answer "who delegated this", and for a self-commitment the
        answer is nobody. The renderer already omits an empty assigner line, so the minutes
        simply stop claiming Rich assigned a task to Rich.
        """
        items = content.get("action_items")
        if not isinstance(items, list):
            return content

        cleared = placeholders = 0
        for ai in items:
            if not isinstance(ai, dict):
                continue
            for field in ("assigned_to", "assigned_by", "due"):
                before = ai.get(field) or ""
                after = LLMManager._clean_placeholder(before)
                if before != after:
                    ai[field] = after
                    placeholders += 1
            to = (ai.get("assigned_to") or "").strip().lower()
            by = (ai.get("assigned_by") or "").strip().lower()
            if to and to == by:
                ai["assigned_by"] = ""
                cleared += 1

        # Drop entries that are RECORDS, not tasks. Two ways in: the text reads as something
        # already done, or it simply restates a decision. Measured on a council meeting, the
        # action list contained "A motion was made to approve the resolution ..." beside the
        # decision saying the same thing — nothing to assign, nothing to chase.
        decisions = [d for d in (content.get("decisions") or []) if isinstance(d, str)]
        dec_terms = [LLMManager._task_terms(d) for d in decisions]
        kept, dropped = [], []
        for ai in items:
            task = (ai.get("task") or "") if isinstance(ai, dict) else ""
            if not task.strip():
                continue
            has_future = bool((ai.get("due") or "").strip()) or bool(LLMManager._FUTURE_MARKER_RE.search(task))
            if LLMManager._PAST_RECORD_RE.search(task) and not has_future:
                dropped.append((task, "records something already done"))
                continue
            if LLMManager._is_restatement(LLMManager._task_terms(task), dec_terms):
                dropped.append((task, "restates a decision"))
                continue
            kept.append(ai)

        content["action_items"] = kept
        if placeholders:
            logger.info(f"[MOM] ACTION ITEMS: blanked {placeholders} placeholder value(s)")
        if cleared:
            logger.info(f"[MOM] ACTION ITEMS: cleared {cleared} self-referential assigned_by")
        for t, why in dropped:
            logger.info(f"[MOM] ACTION ITEMS: dropped {t[:60]!r} — {why}")
        return content

    # ── Stage 4: translate CONTENT values only; labels come from the renderer's config table ──
    def _collect_translatable(self, content):
        """Return a list of (container, key) slots holding NON-EMPTY prose to translate.
        Name/speaker/assignee/date fields are deliberately excluded — they never reach the model."""
        slots = []
        def add(container, key):
            v = container[key]
            if isinstance(v, str) and v.strip():
                slots.append((container, key))
        add(content["header"], "topic")
        for i in range(len(content["agenda"])):
            add(content["agenda"], i)
        for a in content["attendees"]:
            add(a, "role")                              # name kept
        add(content, "summary")
        for i in range(len(content.get("key_points") or [])):
            add(content["key_points"], i)
        # key_figures is deliberately NOT translated — those strings carry amounts and units, and
        # the prompts go to some length to stop the model restating a magnitude.
        for sn in content["speaker_notes"]:             # speaker kept
            for i in range(len(sn["points"])):
                add(sn["points"], i)
        for i in range(len(content["decisions"])):
            add(content["decisions"], i)
        for ai in content["action_items"]:              # assigned_to / assigned_by kept
            add(ai, "task")
            add(ai, "due")
        add(content, "purpose")
        return slots

    def _translate_string_array(self, strings, target_lang, temperature=0.1):
        """Translate a list of strings into target_lang in ONE call; returns a same-length list."""
        system = (
            f"You are a professional translator. You translate the string values of a Minutes-of-Meeting into "
            f"{target_lang}. You receive a JSON array of strings and return a JSON array of the SAME length and "
            f"order, each translated into {target_lang}.\nRULES:\n"
            f"- Translate every DESCRIPTIVE phrase FULLY into {target_lang}, including short, capitalized, or "
            "title-like phrases (meeting topics, agenda items, headings). Capitalization alone does NOT mean keep "
            "it in English.\n"
            "- The ⟦0⟧, ⟦1⟧, … tokens are placeholders for names/terms already extracted — copy each one EXACTLY "
            "as-is and translate the ordinary words around it.\n"
            "- Keep people's names, company/brand/product names, technical terms and acronyms (e.g. DevOps, GPU, "
            "API, Backend, Frontend, Docker, Neo4j, LangChain), file names, URLs, email addresses, version numbers, "
            "IDs, and all numbers EXACTLY as they appear — never translate or transliterate them. If a SINGLE word "
            f"is a technical term or proper noun and you are unsure of an established {target_lang} equivalent, keep "
            "that single word as-is; but never keep a whole descriptive phrase in English.\n"
            "- Do NOT add, remove, reorder, split, or merge array items. Output ONLY the JSON array."
        )
        payload = json.dumps(strings, ensure_ascii=False)
        max_tokens = min(4000, max(800, len(payload)))
        arr = self._parse_json_array(self.generate(system, payload, max_tokens, temperature))
        return [str(x) for x in arr]

    def _translate_content(self, content, target_lang, temperature=0.1):
        """Translate content values in place (on a copy). Identifiers/names are masked before
        translation and restored after (do-not-translate protection), so tech terms and names are
        never transliterated. Raises on a shape mismatch so the caller can fall back."""
        import copy
        content = copy.deepcopy(content)
        slots = self._collect_translatable(content)
        if not slots:
            return content
        strings = [c[k] for (c, k) in slots]
        masked, restore = self._mask_terms(strings, self._term_restore_map(content, target_lang))
        translated = self._translate_string_array(masked, target_lang, temperature)
        if len(translated) != len(strings):
            raise ValueError(f"translation length mismatch: {len(translated)} != {len(strings)}")
        for (c, k), t in zip(slots, translated):
            c[k] = self._unmask(t, restore)
        return content

    _GENERIC_SPEAKER = re.compile(r'^\[?\s*speaker', re.IGNORECASE)

    # Identifier patterns kept verbatim even when not in the glossary (safety net). Case-sensitive.
    _PATTERNS = [
        r"https?://\S+",                                     # URLs
        r"[\w.+-]+@[\w-]+\.[\w.-]+",                          # emails
        r"\b\w+\.(?:py|js|ts|json|ya?ml|md|txt|csv|sql|sh|env|docx?|pdf)\b",   # file names
        r"\bv?\d+\.\d+(?:\.\d+)?\b",                          # version numbers
        r"\b[A-Za-z]*\d[\w-]*\b",                             # tokens with digits (800ms, BM25, Neo4j)
        r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z0-9]*\b",              # CamelCase/mixed (DevOps, LangChain)
        r"\b[A-Z]{2,}\b",                                     # ALL-CAPS acronyms (GPU, API)
    ]

    def _content_names(self, content):
        """Real names from the structured fields (generic [Speaker_N] excluded — renderer handles those)."""
        names = set()
        for a in content["attendees"]:
            names.add(a["name"])
        for sn in content["speaker_notes"]:
            names.add(sn["speaker"])
        for ai in content["action_items"]:
            names.add(ai["assigned_to"]); names.add(ai["assigned_by"])
        return {n for n in names if n and not self._GENERIC_SPEAKER.match(n.strip())}

    def _term_restore_map(self, content, target_lang):
        """Build {term: restore_value} for `target_lang`, from the glossary + content names:
          - glossary TERMS  → the approved translation for target_lang (else the source term)
          - glossary DNT    → the canonical term, kept verbatim
          - content names   → kept verbatim
        Precedence: approved translation > DNT > name."""
        m = {}
        for name in self._content_names(content):
            m[name] = name
        for t in GLOSSARY_DNT:
            m[t] = t
        for src, langs in GLOSSARY_TERMS.items():
            m[src] = (langs or {}).get(target_lang, src)
        return m

    def _mask_terms(self, strings, term_map):
        """Single-pass mask: glossary/name terms (case-insensitive, restore=approved translation or
        verbatim) + identifier patterns (verbatim). ONE pass so sentinels are never re-scanned/
        corrupted. Returns (masked_strings, restore{sentinel: value})."""
        restore, seen = {}, {}
        def sentinel(value):
            if value not in seen:
                s = f"⟦{len(seen)}⟧"
                seen[value] = s
                restore[s] = value
            return seen[value]
        lookup = {k.lower(): v for k, v in term_map.items()}
        parts = []
        if term_map:
            alt = "|".join(re.escape(t) for t in sorted(term_map, key=len, reverse=True))
            parts.append(r"\b(?i:" + alt + r")\b")   # curated terms — case-insensitive (scoped flag)
        parts += self._PATTERNS                       # identifier patterns — case-sensitive
        rx = re.compile("|".join(parts))
        return [rx.sub(lambda m: sentinel(lookup.get(m.group(0).lower(), m.group(0))), s) for s in strings], restore

    @staticmethod
    def _unmask(text, restore):
        """Restore ⟦n⟧ sentinels (tolerant of added spaces); drop any that couldn't be mapped."""
        return re.sub(r"⟦\s*(\d+)\s*⟧", lambda m: restore.get(f"⟦{m.group(1)}⟧", ""), text)

    # ── LEGACY text pipeline ─────────────────────────────────────────────────
    # TODO(Stage 6): Remove after JSON pipeline is production validated.
    def _generate_mom_legacy(self, text: str, temperature: float, output_lang: str = "English") -> dict:
        """Orchestrate Minutes of Meeting generation (handles chunking).
        output_lang: language for the WHOLE MoM (default English = unchanged behavior)."""
        text = preprocess_transcript(text)

        text_len = len(text)
        estimated_tokens = text_len // 4
        logger.info(f"[MOM] Input: {text_len} chars (~{estimated_tokens} tokens), output_lang={output_lang}")

        # Option B: ALWAYS generate the MoM in English (reliable), then translate it below for a
        # non-English request. Native non-English generation degenerates (see investigation), so we
        # never do it — we translate the finished English MoM instead.
        analysis_sys = MEETING_ANALYSIS_PROMPT
        synthesis_sys = SYNTHESIS_PROMPT
        fp, pp = 0.0, 0.0

        # ── Short transcript: single pass ──────────────────────────────────
        # 8B has ~8k context — single-pass up to ~6k tokens
        if estimated_tokens < 5000:
            logger.info("[MOM] Mode: single-pass")
            user_prompt = (
                "Read the following meeting transcript carefully from start to finish, "
                "then generate the complete MoM document.\n\n"
                "TRANSCRIPT:\n"
                "─────────────────────────────────────────────────\n"
                f"{text}\n"
                "─────────────────────────────────────────────────\n\n"
                "Now generate the MoM. Start immediately with ================"
            )
            # Dynamically cap output tokens so input + output never exceeds model context
            safe_max_output = max(500, min(MAX_NEW_TOKENS_PER_CHUNK, MODEL_CONTEXT_LIMIT - estimated_tokens - _ANALYSIS_PROMPT_TOKENS - 100))
            analysis = self.generate(analysis_sys, user_prompt, safe_max_output, temperature)
            analysis = clean_mom_output(analysis)
            chunks_used = 1

        # ── Long transcript: chunk → analyse → synthesise ──────────────────
        else:
            logger.info("[MOM] Mode: chunk + synthesise")
            chunks = chunk_transcript(text)
            partial_analyses = []

            for i, chunk in enumerate(chunks):
                logger.info(f"[MOM] Analysing chunk {i+1}/{len(chunks)}")
                chunk_prompt = (
                    f"This is section {i+1} of {len(chunks)} of a longer meeting transcript.\n"
                    f"Read it carefully and extract a detailed partial MoM covering only this section.\n\n"
                    f"TRANSCRIPT SECTION {i+1}/{len(chunks)}:\n"
                    f"─────────────────────────────────────────────────\n"
                    f"{chunk}\n"
                    f"─────────────────────────────────────────────────\n\n"
                    f"Generate the partial MoM for this section. Start immediately with ================"
                )
                partial = self.generate(analysis_sys, chunk_prompt, MAX_NEW_TOKENS_PER_CHUNK, temperature, fp, pp)
                partial_analyses.append(partial)

            logger.info("[MOM] Synthesising chunks...")
            combined = "\n\n=== CHUNK ===\n\n".join(partial_analyses)

            # Guard against context overflow
            max_chars = 16000  # 8B context limit — keep synthesis input small
            if len(combined) > max_chars:
                logger.warning(f"[MOM] Truncating synthesis input to {max_chars} chars")
                combined = combined[:max_chars]

            synthesis_prompt = (
                f"Below are {len(partial_analyses)} partial MoM analyses from consecutive sections of the same meeting.\n"
                f"Merge them into ONE complete, non-redundant MoM document.\n\n"
                f"{'=' * 40} PARTIAL ANALYSES {'=' * 40}\n\n"
                f"{combined}\n\n"
                f"{'=' * 40} END OF PARTIALS {'=' * 40}\n\n"
                f"Now generate the final merged MoM. Start immediately with ================"
            )
            analysis = self.generate(synthesis_sys, synthesis_prompt, MAX_NEW_TOKENS_SYNTHESIS, temperature, fp, pp)
            analysis = clean_mom_output(analysis)
            chunks_used = len(chunks)

            # TODO(Stage 6): Remove after JSON pipeline is production validated (renderer owns the footer).
            if "Regards" not in analysis[-200:]:
                analysis += "\n\n" + "=" * 80 + "\n\nRegards,\nGenerated by AngelBot.AI\n"

        # ── Focused fallback passes (run when sections are empty/weak) ──────
        # The MoM is still English here — fills always run in English, then we translate below.
        analysis = self._fill_decisions_if_empty(analysis, text, temperature)
        analysis = self._fill_action_items_if_empty(analysis, text, temperature)

        # ── Option B: translate the finished English MoM into the requested language ──
        if not self._is_english(output_lang):
            logger.info(f"[MOM] Translating English MoM → {output_lang}")
            analysis = self._translate_mom(analysis, output_lang, temperature)

        logger.info(f"[MOM] ✓ Complete ({len(analysis)} chars, lang={output_lang})")

        return {
            "analysis": analysis,
            "summary": analysis,
            "original_length": text_len,
            "analysis_length": len(analysis),
            "chunks_used": chunks_used,
        }

    def translate_mom(self, english_mom: str, target_lang: str, temperature: float = 0.1) -> str:
        """Public entry: translate a FINISHED English MoM into `target_lang` (structure-preserving).
        Returns the English MoM unchanged for an English/empty target. Used by the participant-MoM
        path to translate the cached English base without re-generating it."""
        if self._is_english(target_lang):
            return english_mom
        return self._translate_mom(english_mom, target_lang, temperature)

    def _translate_mom(self, english_mom: str, target_lang: str, temperature: float = 0.1) -> str:
        """Option B: translate a FINISHED English MoM into `target_lang`, preserving structure.

        Native non-English MoM generation degenerates, but translating the completed English MoM is
        reliable. This is a structure-preserving translation: prose and section headings/labels are
        translated, while separators, bullets, indentation, proper nouns, tech terms, numbers, and
        dates are kept intact. On any failure we fall back to the English MoM (never return empty)."""
        if not english_mom or not english_mom.strip():
            return english_mom
        lang = (target_lang or "").strip()

        system_prompt = (
            f"You are a professional document translator. Translate the Minutes of Meeting (MoM) below "
            f"into {lang}. This is a STRUCTURE-PRESERVING translation, not a rewrite.\n\n"
            "RULES:\n"
            f"1. Translate ALL prose, sentences, and bullet content into {lang}.\n"
            f"2. Translate EVERY section heading (AGENDA, ATTENDEES, SUMMARY, SPEAKER-WISE NOTES, "
            f"DECISIONS TAKEN, ACTION ITEMS, PURPOSE OF MEETING) and EVERY field label "
            f"(Date, Time, Venue, Assigned to, Assigned by, Due on) into {lang}.\n"
            "3. Keep the EXACT layout: preserve every ==== and ____ separator line unchanged, keep the "
            "• bullets, keep indentation and blank lines, keep the same section order.\n"
            "4. Keep people's names, project/product names, and technical or tool names (e.g. DevOps, "
            "GPU, Neo4j, AngelBot.AI) in their original Latin script. Keep numbers, dates, and times as-is.\n"
            "5. Do NOT add English in parentheses. Do NOT add notes, explanations, or a preamble.\n"
            f"6. Output ONLY the translated MoM in {lang}."
        )

        try:
            translated = self.generate(system_prompt, english_mom, MAX_NEW_TOKENS_SYNTHESIS, max(temperature, 0.1))
        except Exception as e:
            logger.warning(f"[MOM] Translation to {lang} failed ({e}) — returning English MoM")
            return english_mom
        translated = (translated or "").strip()
        if not translated:
            logger.warning(f"[MOM] Translation to {lang} returned empty — returning English MoM")
            return english_mom
        return translated

    # A correction may legitimately shorten a chunk a little — it removes stutters and repair
    # loops; the conservative prompt measured 99% retention on real meeting audio. Losing a
    # TENTH of it is not correction, it is truncation, refusal or paraphrase, and the
    # damage is invisible downstream because what comes back still looks like a transcript.
    _CORRECTION_MIN_RETENTION = 0.90

    @classmethod
    def _corrected_or_original(cls, corrected: str, original: str, where: str) -> str:
        """Accept a correction only if it still contains the meeting.

        This pass REWRITES the evidence every later stage is scored against, so its failure
        mode is the worst kind: a truncated or empty answer silently deletes what was said and
        nothing downstream can tell. Falling back to the original text makes the worst case a
        no-op (garble survives) instead of data loss.
        """
        corrected = (corrected or "").strip()
        if not corrected:
            logger.warning("[CORRECT] %s came back empty — keeping the original text", where)
            return original
        if len(corrected) < len(original) * cls._CORRECTION_MIN_RETENTION:
            logger.warning(
                "[CORRECT] %s shrank %d -> %d chars (< %.0f%% retained) — keeping the original text",
                where, len(original), len(corrected), cls._CORRECTION_MIN_RETENTION * 100,
            )
            return original
        return corrected

    def correct_transcript(self, text: str, temperature: float = 0.1, mode: str = "hinglish", known_names: list = None) -> str:
        """Fix ASR errors in a diarized transcript using context. Chunks long transcripts to avoid truncation.

        known_names: people confirmed present (voice-ID / named speaker labels). Appended to the prompt so
        mangled proper nouns get fixed to the right person, without inventing names."""
        logger.info(f"[CORRECT] Correcting transcript ({len(text)} chars, mode={mode})")
        # Tier 1 (deterministic): fix the org's core vocabulary BEFORE the LLM, so the LLM can't turn
        # a hard mishearing into a DIFFERENT real product ("Vsperge"->"Vespa"). Bounded to our terms;
        # general jargon is left to the LLM (Tier 2).
        _tier1_input = None
        if getattr(self, "term_corrector", None) is not None:
            text, _tfixes = self.term_corrector.correct(text)
            _tier1_input = text  # LLM input — diffed against LLM output to auto-learn new aliases
            if _tfixes:
                logger.info(f"[CORRECT] Tier-1 term fixes ({len(_tfixes)}): {_tfixes[:15]}")
        prompt = TRANSLATED_TRANSCRIPT_CORRECTION_PROMPT if mode == "translated" else TRANSCRIPT_CORRECTION_PROMPT
        roster = ", ".join(sorted({n.strip() for n in (known_names or []) if n and n.strip()}))
        if roster:
            prompt = f"{prompt}\n\nKNOWN PEOPLE in this meeting (fix mis-transcribed names to these; do NOT invent others): {roster}"
            logger.info(f"[CORRECT] name roster: {roster}")

        CHUNK_CHARS = 4000
        # A correction pass must REPRODUCE its input, so its output is at least as long as the
        # chunk it was given (~1000 tokens for 4000 chars) — and on a reasoning model the
        # thinking comes out of the same allowance. 2000 was not enough for both, so chunks
        # came back truncated, which in a correction pass means DELETED TRANSCRIPT. The guard
        # below is the real protection; this is the budget that stops it firing constantly.
        # translated: Chinese→English can expand 3-5x — give more output budget
        max_output = max(4000 if mode == "translated" else 2500, MAX_NEW_TOKENS_EXTRACTION)

        lines = text.split('\n')
        chunks: list[str] = []
        current_lines: list[str] = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1  # +1 for the newline
            if current_len + line_len > CHUNK_CHARS and current_lines:
                chunks.append('\n'.join(current_lines))
                current_lines = [line]
                current_len = line_len
            else:
                current_lines.append(line)
                current_len += line_len

        if current_lines:
            chunks.append('\n'.join(current_lines))

        if len(chunks) == 1:
            corrected = self._corrected_or_original(
                self.generate(prompt, f"TRANSCRIPT:\n\n{text}", max_output, temperature), text, "single-pass"
            )
            self._maybe_learn_terms(_tier1_input, corrected)
            logger.info(f"[CORRECT] ✓ Done single-pass ({len(corrected)} chars)")
            return corrected

        # RUN THE CHUNKS TOGETHER. They are independent slices of one transcript and are rejoined
        # in order, so nothing about correcting them depends on sequence. Sequentially this stage
        # was both the slowest in the pipeline and a latent failure: at ~60s per 4000-char chunk a
        # 90-minute meeting needs ~15 chunks, which exceeds the caller's 900s REFINE_TIMEOUT — and
        # because correction degrades silently to the raw transcript, the only symptom would be
        # ASR garble surviving into the minutes of exactly the longest meetings.
        # pool.map preserves input order, so the rejoin below is unaffected.
        logger.info(f"[CORRECT] Chunked: {len(chunks)} chunks, {LLM_CONCURRENCY} at a time")

        def _one(i_chunk):
            i, chunk = i_chunk
            logger.info(f"[CORRECT] Chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
            return self._corrected_or_original(
                self.generate(prompt, f"TRANSCRIPT:\n\n{chunk}", max_output, temperature),
                chunk, f"chunk {i+1}")

        with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as pool:
            corrected_chunks = list(pool.map(_one, enumerate(chunks)))

        corrected = '\n'.join(corrected_chunks)
        self._maybe_learn_terms(_tier1_input, corrected)
        logger.info(f"[CORRECT] ✓ Done chunked ({len(corrected)} chars, {len(chunks)} chunks)")
        return corrected

    def _maybe_learn_terms(self, before, corrected):
        """Self-learning: let the LLM's corrections teach the lexicon new aliases. Non-critical."""
        if before is None or getattr(self, "term_corrector", None) is None:
            return
        try:
            learned = self.term_corrector.learn_from_correction(before, corrected)
            if learned:
                logger.info(f"[TERMCORR] auto-learned aliases from LLM: {learned}")
        except Exception as e:
            logger.warning(f"[TERMCORR] alias learning failed (non-critical): {e}")

    def generate_translation(self, text: str, source_lang: str, target_lang: str, temperature: float = 0.0, context: str = "") -> str:
        """Translate `text` into target_lang — output only, deterministic, no meta-text.

        LIVE-1 R4: `context` is accepted for backward-compat but intentionally NOT
        injected into the prompt. The old CONTEXT/NEW-TEXT framing was the main trigger
        for the model emitting meta-text ("translation not needed ...") and it made the
        output non-deterministic / uncacheable. Per-line caption translation does not
        need it. `temperature` is likewise ignored — translation runs deterministically.
        """
        logger.info(f"[TRANSLATE] {source_lang} → {target_lang}")
        logger.info(f"[TRANSLATE] Input: {len(text)} chars")

        # Trivial input — nothing to translate.
        if not text or not text.strip():
            return text
        # Already in the requested language — return unchanged, no model call.
        if source_lang == target_lang:
            return text

        system_prompt = (
            f"You are a translation engine. Translate the user's text into {target_lang}. "
            f"Output ONLY the translated text — no notes, no explanations, no preamble, no quotation marks. "
            f"If the text is already in {target_lang}, output it unchanged. "
            f"Never say things like 'no translation needed' or 'already in {target_lang}'."
        )

        estimated_tokens = max(len(text) // 2, 500)
        max_tokens = min(estimated_tokens * 2, 3000)

        translated = self.generate(
            system_prompt,
            text,
            max_tokens,
            0.0,  # deterministic for caption translation (generate() clamps to >= 0.01)
        )

        translated = _clean_translation_output(translated, text)

        logger.info(f"[TRANSLATE] ✓ Done ({len(translated)} chars)")
        return translated

    @staticmethod
    def _parse_json_array(raw: str):
        """Parse a JSON array out of a model response, tolerating markdown fences and
        surrounding prose. Raises if no array can be parsed."""
        s = (raw or "").strip()
        if "```" in s:                      # strip ```json ... ``` fences
            parts = s.split("```")
            if len(parts) >= 3:
                s = parts[1]
            if s.lstrip().lower().startswith("json"):
                s = s.lstrip()[4:]
        start, end = s.find("["), s.rfind("]")
        if start != -1 and end != -1 and end > start:
            s = s[start:end + 1]
        return json.loads(s)

    def generate_translation_batch(self, texts: list, source_lang: str, target_lang: str):
        """Translate many short texts in ONE model call to slash per-segment Groq calls
        (the main cause of 429 rate-limiting under live load).

        Returns (results, reasons), both aligned to `texts`:
          results[i] = translated string, or None if that item could not be translated.
          reasons[i] = None when results[i] is usable (success or trivial pass-through),
                       else WHY it is None — 'transport' (network/429/5xx: retry generously)
                       or a validation reason ('echo' | 'wrong_script' | 'mixed_script' |
                       'empty': retry sparingly — re-asking a deterministic model is low-yield).
        The caller uses `reasons` to pick a retry budget; it does NOT need a second pipeline.

        Alignment is guaranteed by a strict JSON-array contract + length check. On a shape
        mismatch (API worked, model mis-formatted) we drop to per-item translation. On a
        transport/429 error we return None for the pending items WITHOUT per-item retries,
        so a saturated key pool is not amplified — the caller retries the whole batch later.
        """
        n = len(texts)
        results = [None] * n
        reasons = [None] * n
        if n == 0:
            return results, reasons

        pending_idx = []
        for i, t in enumerate(texts):
            if not t or not t.strip() or source_lang == target_lang:
                results[i] = t                      # trivial → unchanged, no model call
            else:
                pending_idx.append(i)
        if not pending_idx:
            return results, reasons

        items = [texts[i] for i in pending_idx]
        # NOTE: the previous prompt ended with "If an element is already in {target_lang}, return
        # it unchanged." For a code-mixed source (Hinglish = Devanagari + English) the model read
        # that as "this is already Hindi" and echoed the line verbatim (sim=1.00), so a Hindi
        # viewer kept seeing Hinglish. The source is ALWAYS code-mixed here, so instead we state
        # explicitly that a partially-target-script input is NOT already translated.
        system_prompt = (
            f"You are a translation engine. You receive a JSON array of strings. "
            f"Translate EACH element into {target_lang}. Respond with ONLY a JSON array of the "
            f"SAME length and SAME order, each element being the translation of the input at the "
            f"same index. No notes, no preamble, no markdown fences.\n"
            f"The input is code-mixed: it may already contain some {target_lang} words mixed with "
            f"English (and English words may be spelled phonetically). This does NOT mean it is "
            f"already translated. You MUST produce output that is fully and naturally in "
            f"{target_lang}: translate the English words too, and convert any phonetically-spelled "
            f"words into proper {target_lang} words and script. Never echo an element back "
            f"unchanged unless it is genuinely 100% correct {target_lang} already. "
            # Names are not words. Measured 2026-09-09: "Superior Town Council" came back as
            # "उच्च शहर परिषद" — "उच्च" means "high/superior", so the TOWN'S NAME was rendered as
            # the adjective it happens to look like. The reverse direction gets this right, so
            # this only needs saying on the way out of English.
            f"NAMES ARE NOT WORDS. For the name of a person, place, organisation, product or "
            f"brand, write how it SOUNDS in the {target_lang} script — never what it would mean "
            f"if it were an ordinary word. 'Superior Town Council' is the name of a town, so it "
            f"becomes the {target_lang} spelling of the sounds 'Superior', not the {target_lang} "
            f"word for 'better'. Ordinary words around the name are still translated normally. "
            f"Keep proper nouns, acronyms (AI, GPU, API), numbers and URLs as-is."
        )
        user_message = json.dumps(items, ensure_ascii=False)
        total_chars = sum(len(x) for x in items)
        max_tokens = min(max(total_chars // 2, 500) * 3, 6000)

        try:
            raw = self.generate(system_prompt, user_message, max_tokens, 0.0)
        except Exception as e:
            logger.warning(f"[TRANSLATE-BATCH] batch call failed ({e}); leaving {len(items)} item(s) for caller retry")
            for idx in pending_idx:
                reasons[idx] = "transport"           # whole batch call failed → transport, retry generously
            return results, reasons

        arr = None
        try:
            arr = self._parse_json_array(raw)
        except Exception:
            arr = None

        if isinstance(arr, list) and len(arr) == len(items):
            valid = 0
            for j, idx in enumerate(pending_idx):
                val = arr[j]
                cleaned = _clean_translation_output(val, texts[idx]) if isinstance(val, str) and val.strip() else None
                if cleaned is None:
                    reasons[idx] = "empty"
                    continue
                # Validate against the target language/script. Invalid (echo / wrong_script /
                # mixed_script) → None + reason so the caller retries it on the small budget.
                ok, reason = validate_translation(cleaned, texts[idx], target_lang)
                if ok:
                    results[idx] = cleaned
                    valid += 1
                else:
                    reasons[idx] = reason
            logger.info(f"[TRANSLATE-BATCH] ✓ {valid}/{len(items)} valid in one call → {target_lang}")
            return results, reasons

        # API call worked but the model returned the wrong shape → per-item is safe (won't 429
        # any worse than the call that just succeeded) and preserves alignment.
        logger.warning(f"[TRANSLATE-BATCH] shape mismatch (got {type(arr).__name__}); per-item fallback for {len(pending_idx)} item(s)")
        for idx in pending_idx:
            try:
                cleaned = self.generate_translation(texts[idx], source_lang, target_lang)
                ok, reason = validate_translation(cleaned, texts[idx], target_lang)
                if ok:
                    results[idx] = cleaned
                else:
                    reasons[idx] = reason or "empty"
            except Exception:
                reasons[idx] = "transport"           # per-item network/429 failure
        return results, reasons

    def identify_speakers(self, text: str, temperature: float) -> dict:
        """Identify speaker names and roles from transcript"""
        import json
        logger.info(f"[SPEAKER_ID] Analyzing transcript ({len(text)} chars)")
        
        # Names arrive whenever someone introduces themselves, which is often NOT early: in a
        # 29-minute staff meeting the technical-marketing attendee introduced himself at
        # minute 23. The old 12k cap silently put those speakers out of reach. Budgets now
        # allow the whole transcript; the cap remains only as a context-limit backstop.
        sample_text = text[:24000]
        
        # 1000 tokens was the same starved budget the extraction passes had: gpt-oss spends
        # its output allowance reasoning before it writes, so a speaker map for a real meeting
        # never finished and generate() returned "" -> {} -> no names, silently.
        result_json = self.generate(
            SPEAKER_MAPPING_PROMPT,
            f"Transcript:\n\n{sample_text}",
            MAX_NEW_TOKENS_EXTRACTION,
            temperature
        )
        
        try:
            # Clean up potential markdown formatting and conversational filler
            import re
            json_block = result_json
            if "```json" in result_json:
                json_block = result_json.split("```json")[1].split("```")[0].strip()
            elif "```" in result_json:
                json_block = result_json.split("```")[1].split("```")[0].strip()
            else:
                # Search for the first { and last }
                match = re.search(r"(\{.*\})", result_json, re.DOTALL)
                if match:
                    json_block = match.group(1)
                
            mapping = json.loads(json_block)
            logger.info(f"[SPEAKER_ID] ✓ Identified {len(mapping)} speakers")
            return mapping
        except Exception as e:
            logger.error(f"[SPEAKER_ID] Failed to parse JSON: {e}")
            logger.error(f"Raw output: {result_json}")
            return {}
