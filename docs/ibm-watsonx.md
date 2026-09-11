# Running the MoM writer on IBM watsonx.ai

Whisper and NeMo stay local. Only the LLM endpoint moves.

## Which of the two paths you are on

watsonx offers an **OpenAI-compatible `/chat/completions`** through its *model gateway*, and a
**native API** at `/ml/v1/text/chat`. `WATSONX_PROJECT_ID` is the switch: set it and the service
talks native, leave it empty and it talks OpenAI-compatible (the same code path as OpenRouter,
Groq and a local vLLM).

Run this first — it answers the question in about a minute:

    IBM_BASE=https://us-south.ml.cloud.ibm.com IBM_APIKEY=<key> \
    IBM_PROJECT_ID=<project> IBM_MODEL=meta-llama/llama-3-3-70b-instruct \
    python3 scripts/ibm_check.py

It checks authentication, which chat URL answers, JSON-schema output, the `json_object` fallback,
truncation reporting (`finish_reason`), and two concurrent calls.

## A. Model gateway (OpenAI-compatible) — configuration only

    VLLM_API_BASE=https://<gateway host>/v1
    LLM_MODEL_PATH=meta-llama/llama-3-3-70b-instruct
    GROQ_API_KEYS=<ibm api key>          # the variable is historical; it is just the bearer key
    LLM_PROVIDER_ORDER=                  # OpenRouter-only, leave empty

## B. Native watsonx API

    VLLM_API_BASE=https://us-south.ml.cloud.ibm.com
    WATSONX_PROJECT_ID=<project id>      # setting this selects the native shape
    WATSONX_VERSION=2024-10-08
    LLM_MODEL_PATH=meta-llama/llama-3-3-70b-instruct
    GROQ_API_KEYS=<ibm cloud api key>
    LLM_AUTH_MODE=iam                    # auto-selected for *.ml.cloud.ibm.com

`model` becomes `model_id`, `project_id` is added, and the OpenRouter-only `provider` field is
dropped. Everything else — messages, max_tokens, temperature, top_p, response_format — keeps the
same names, so JSON-schema extraction is unchanged.

### Authentication

A SaaS IBM Cloud API key is **not** a bearer token: it is exchanged for an IAM token that expires,
usually hourly. `core/ibm_auth.py` caches the token and refreshes it 5 minutes before expiry — the
margin has to exceed the slowest single call, or a token can expire during a 60-90 s extraction and
fail it. On-prem Cloud Pak for Data issues a long-lived **Zen key**: set `LLM_AUTH_MODE=bearer` and
the ordinary key path is used, with no token exchange.

## After switching, before trusting any number

- **Re-measure the four test meetings.** Every accuracy figure recorded so far came from Llama 3.3
  70B on DeepInfra via OpenRouter. A different host may serve a different quantisation of the same
  weights, so scores can move. `scratchpad/m3/MEASURE.md` has the procedure.
- **Confirm you are pinned to one model build.** OpenRouter silently rotated us across four
  providers of differing quality until `LLM_PROVIDER_ORDER` pinned one; that variable made accuracy
  measurements meaningless until it was found.
- **Check the context limit** on your plan against `MODEL_CONTEXT_LIMIT` (default 32768). The
  windowed extraction needs roughly 8k in and 1.5k out per call.
