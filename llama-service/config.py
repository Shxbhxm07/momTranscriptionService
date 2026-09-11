import os

# Cloud (Groq) vs local (vLLM / gpt-oss) is decided PURELY by VLLM_API_BASE — the codebase has
# no separate LLM_ENGINE switch (llm_manager reads VLLM_API_BASE directly). We detect it here so
# the token budgets below auto-adjust per deployment from this SAME shared file:
#   • OCP / cloud   → VLLM_API_BASE points at api.groq.com  → original Groq-tuned budgets
#   • GB10 / local  → VLLM_API_BASE points at the local vLLM → larger gpt-oss budgets
# APP_MODE is the single product-wide switch; see backend/config.py for the full note. This
# service derives its own defaults from it, so nothing here needs to know about ASR or TTS.
#
# Explicit VLLM_API_BASE still wins, which is what keeps the existing compose overlay (pointing at
# host.docker.internal:8004) working untouched, and lets a deliberate hybrid be configured.
APP_MODE      = os.getenv("APP_MODE", "online").strip().lower()
IS_OFFLINE    = APP_MODE == "offline"
LOCAL_AI_HOST = os.getenv("LOCAL_AI_HOST", "").strip()

VLLM_API_BASE  = os.getenv("VLLM_API_BASE") or (
    f"http://{LOCAL_AI_HOST or 'vllm'}:8004/v1" if IS_OFFLINE else "https://api.groq.com/openai/v1"
)
LLM_MODEL_PATH = os.getenv("LLM_MODEL_PATH") or (
    "gpt-oss-120b" if IS_OFFLINE else "openai/gpt-oss-120b"
)
# Budgets key on the MODEL, not the backend. Both modes now run gpt-oss-120b — locally via vLLM
# ("gpt-oss-120b") and on Groq ("openai/gpt-oss-120b"; llama-3.3-70b-versatile is not on our plan).
# gpt-oss is a REASONING model: it spends tokens "thinking" from the SAME output budget before it
# writes, so a low cap truncates long MoM mid-word (exactly what the old Groq-tuned 3000 did). Give
# any reasoning model the larger budget in BOTH modes; a plain model set via env gets the smaller one.
# LONG-MEETING ROUTE. Chunking is where topics get lost: the per-chunk pass writes a partial
# MoM and the synthesis pass merges them, and anything a partial drops is gone with no trace.
# A model with a bigger context can take the whole transcript in ONE pass and skip that risk
# entirely, so long transcripts are routed to it. Leave LLM_MODEL_LONG empty to disable routing
# and send everything to LLM_MODEL_PATH, which is exactly today's behaviour.
LLM_MODEL_LONG = os.getenv("LLM_MODEL_LONG", "").strip()
# Defaults to the same endpoint — set only if the long model is served somewhere else.
VLLM_API_BASE_LONG = os.getenv("VLLM_API_BASE_LONG", "").strip() or VLLM_API_BASE
# Route to the long model when the transcript alone exceeds this many tokens. 5000 matches the
# point where _generate_json_raw stops doing a single pass and starts chunking.
LLM_LONG_THRESHOLD_TOKENS = int(os.getenv("LLM_LONG_THRESHOLD_TOKENS", "5000"))

# Comma-separated OpenRouter provider names, most preferred first, e.g. "DeepInfra,Together".
# Empty = let OpenRouter route freely, which measurably sends identical requests to different
# hosts running different quantisations. Ignored for a local vLLM.
LLM_PROVIDER_ORDER = [p.strip() for p in os.getenv("LLM_PROVIDER_ORDER", "").split(",") if p.strip()]

# ── IBM watsonx ───────────────────────────────────────────────────────────────────────────────────
# Two deployments and three request shapes, so VLLM_API_BASE carries the FULL endpoint for watsonx
# (it already includes ?version=...), and the shape is read from its path:
#   .../ml/v1/text/chat        → messages + choices, like OpenAI. Keeps JSON-schema extraction.
#   .../ml/v1/text/generation  → a single `input` string, answer in results[0].generated_text.
#                                No structured output: the JSON has to survive on prompt + repair.
#   anything else              → OpenAI-compatible /chat/completions (vLLM, OpenRouter, Groq, and
#                                watsonx's own model gateway).
# The IAF cluster is Cloud Pak for Data with a self-signed certificate, hence LLM_VERIFY_SSL.
WATSONX_PROJECT_ID = os.getenv("WATSONX_PROJECT_ID", "").strip()
WATSONX_VERSION    = os.getenv("WATSONX_VERSION", "2023-05-29").strip()
IBM_IAM_URL        = os.getenv("IBM_IAM_URL", "https://iam.cloud.ibm.com/identity/token").strip()
CP4D_AUTH_URL      = os.getenv("CP4D_AUTH_URL", "").strip()
CP4D_USERNAME      = os.getenv("CP4D_USERNAME", "").strip()
CP4D_API_KEY       = os.getenv("CP4D_API_KEY", "").strip()
CP4D_TOKEN_TTL     = int(os.getenv("CP4D_TOKEN_TTL", "3600"))
# Self-signed certs are normal on an on-prem OpenShift cluster; the reference integration disables
# verification for both the token and the inference call.
LLM_VERIFY_SSL     = os.getenv("LLM_VERIFY_SSL", "true").lower() == "true"
# bearer: send the key as-is (vLLM, OpenRouter, Groq, a CP4D Zen key)
# cp4d:   username + api_key -> /icp4d-api/v1/authorize   (the IAF cluster)
# iam:    IBM Cloud apikey grant                          (watsonx SaaS)
LLM_AUTH_MODE      = (os.getenv("LLM_AUTH_MODE", "").strip().lower()
                      or ("cp4d" if CP4D_AUTH_URL else
                          "iam" if "ml.cloud.ibm.com" in (VLLM_API_BASE or "") else "bearer"))

# REASONING MODELS spend part of the output allowance "thinking" before they write, so every
# budget below has to cover the thinking AND the answer. This was inferred from the model NAME,
# which is a trap the moment the model changes: swapping to Llama silently flips it to False and
# every budget collapses — 700 tokens for the extraction passes is what produced empty
# action_items in the first place. Make it an explicit setting, defaulting to the name check so
# existing gpt-oss deployments behave identically.
_REASONING_ENV = os.getenv("LLM_IS_REASONING", "").strip().lower()
_IS_REASONING = (
    _REASONING_ENV in ("1", "true", "yes") if _REASONING_ENV in ("1", "true", "yes", "0", "false", "no")
    else "gpt-oss" in LLM_MODEL_PATH.lower()
)

# 32768 was a Groq TPM-rate-limit guard, not a model limit. Llama 3.3 70B handles 128k and Scout
# far more, and a bigger window means fewer chunks — which is the main source of dropped topics.
# Raise it per deployment; the code clamps output against whatever is set here.
MODEL_CONTEXT_LIMIT       = int(os.getenv("MODEL_CONTEXT_LIMIT", "32768"))
MODEL_CONTEXT_LIMIT_LONG  = int(os.getenv("MODEL_CONTEXT_LIMIT_LONG", str(MODEL_CONTEXT_LIMIT)))
MAX_INPUT_TOKENS          = 20000  # generous input budget for long meetings
CHUNK_SIZE_CHARS          = 20000  # larger chunks — gpt-oss-120b handles them well

# The MoM generation budget must cover the model's REASONING **and** the JSON it then writes.
# 4000/6000 was too tight once the schema grew a key_points entry per topic. Measured 2026-09-07
# on a 17-minute, 6-speaker committee meeting (12.7k chars, single-pass): the model spent ~2800
# tokens reasoning, began emitting JSON, and hit the 4000 cap mid-object. Truncated JSON fails
# validation, the strict repair retry then spent its ENTIRE budget reasoning and returned
# content=None, and the whole request fell through to the legacy text pipeline — which returns no
# structured `content` at all, so the API reported "no meeting content" for a meeting that had
# transcribed perfectly. One tight cap, three failures deep, and a wrong answer at the end.
#
# Headroom check against vLLM's --max-model-len 32768, worst case in each mode:
#   single-pass  6.1k prompt + 5.0k transcript +  8k output = 19.1k
#   per-chunk    6.1k prompt + 5.0k chunk      +  8k output = 19.1k
#   synthesis    3.0k prompt + 4.0k partials   + 10k output = 17.0k
# _generate_json_raw still clamps the single-pass value to real remaining context, so a long
# transcript shrinks this rather than overflowing.
# The non-reasoning numbers are NOT the old 2000/3000. Those predate the key_points field, which
# adds one entry per topic discussed. Measured 2026-09-07: the finished MoM JSON for a 17-minute
# meeting was 10,388 chars ~ 2,600 tokens of pure output, so a 2000 cap truncates it mid-object
# even for a model that does no thinking at all. A truncated object fails validation and falls
# through to the legacy text pipeline, which returns no structured fields.
MAX_NEW_TOKENS_PER_CHUNK  = 8000 if _IS_REASONING else 4000   # per-chunk MoM output
MAX_NEW_TOKENS_SYNTHESIS  = 10000 if _IS_REASONING else 5000  # final merged MoM (also used by translate_mom)

# Focused single-purpose extraction passes (decisions / action items / figures).
#
# These were 400-700 and that was silently BROKEN on a reasoning model, for the same reason
# documented above but never applied here. Measured 2026-09-07 against the local gpt-oss-120b
# on a 24k-char meeting transcript, calling the shipped ACTION_ITEMS_EXTRACTION_PROMPT:
#
#   max_tokens=400  → finish_reason "length", content=None, 1518 chars of reasoning and no answer
#   max_tokens=1312 → finish_reason "stop", 6 action items   (1251 of 1312 tokens consumed — marginal)
#   max_tokens=3000 → finish_reason "stop", 7 action items
#
# So the pass burned its whole budget thinking and returned nothing; llm_manager's content=None
# retry doubles the cap (400 → 1312), which only just fits and tips back into failure on a longer
# or more crowded transcript. That is why ACTION ITEMS came back empty on real meetings that
# plainly contained them. The budget is an upper bound, not a target — the model still stops at
# ~1400 tokens on this transcript, so raising it costs nothing when the answer is short.
MAX_NEW_TOKENS_EXTRACTION = 3000 if _IS_REASONING else 1500

# Meeting-type classification asks for ONE word, and 8 tokens is enough to WRITE one word but not
# enough for a reasoning model to think first — so this call returned content=None on every single
# request and only survived via the doubling retry, costing a wasted round trip per MoM.
MAX_NEW_TOKENS_CLASSIFY   = 512 if _IS_REASONING else 16


# GROUNDED MODE. When on, key_points / decisions / action_items are built ONLY from window
# extraction points whose verbatim quote was located in the transcript; a point the model cannot
# quote is deleted rather than reported. Trades a little polish for the property that the minutes
# cannot contain a statement the meeting did not make. Set to false to restore the previous
# narrative-plus-fills behaviour.
MOM_WINDOW_KEY_POINTS = os.getenv("MOM_WINDOW_KEY_POINTS", "true").lower() == "true"

# Ask the model which extracted entries describe the same work, then merge them in code. Lexical dedupe
# cannot see that "revise and re-record the LEP course" and "the revised script will be recorded
# tomorrow" are one task — measured 2026-09-11, LEP carried 19 action items for 12 real ones.
MOM_MERGE_ITEMS = os.getenv("MOM_MERGE_ITEMS", "true").lower() == "true"

# Require each decision to carry verbatim evidence that the group settled it, and drop the ones that cannot.
# OFF until measured: quote-grounding these fields shipped once inside a four-change bundle that scored 78
# against 84, and nobody could tell which change did the damage. Measured on its own, it is the only lever
# left for decisions — measured 2026-09-11 the model still records plain tasks ("The team will review the
# Issue Herder tool") and non-events ("The community contribution will continue") as decisions, and no
# wording rule separates those from a real group agreement.
MOM_VERIFY_DECISIONS = os.getenv("MOM_VERIFY_DECISIONS", "false").lower() == "true"
# Windows are independent HTTP calls; the limit is provider rate limiting, not this box.
# 2, not 4: four concurrent window calls tripped OpenRouter's rate limit, the 429 exhausted its
# retries, and the whole request fell through to the legacy pipeline — which returns no
# structured content, so every field came back empty. Throughput is not worth that failure mode.
# How many model calls may be in flight at once. The limit is the PROVIDER'S rate limit, not this
# box — four concurrent calls tripped OpenRouter's 429, the retries were exhausted and the whole
# request fell through to the legacy pipeline. Shared by every independent-call stage: window
# extraction and transcript correction.
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", os.getenv("WINDOW_CONCURRENCY", "2")))
WINDOW_CONCURRENCY = LLM_CONCURRENCY   # kept for the existing name
