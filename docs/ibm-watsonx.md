# Running the MoM writer on IBM watsonx

Whisper and NeMo stay local. Only the LLM endpoint moves.

Modelled on the working reference integration in `query_enhancement 2.py`: the IAF deployment is
**Cloud Pak for Data on OpenShift**, not IBM Cloud SaaS, which changes three things — the token
issuer, the certificate, and the request shape.

## Step 1 — find out which endpoint the cluster has

    CP4D_AUTH_URL=https://cpd-watsonx-imir.apps.ocp4.iaf.in/icp4d-api/v1/authorize \
    CP4D_USERNAME=<user> CP4D_API_KEY=<key> WATSONX_PROJECT_ID=<project> \
    WATSONX_HOST=https://cpd-watsonx-imir.apps.ocp4.iaf.in \
    MODEL_ID=meta-llama/llama-3-3-70b-instruct VERIFY_SSL=false \
    python3 scripts/ibm_check.py

It authenticates, probes **both** `/ml/v1/text/chat` and `/ml/v1/text/generation`, tests JSON-schema
output, checks the truncation signal and two concurrent calls, then prints the configuration to use.

**This is the question that matters.** `/text/chat` takes messages and supports `response_format`,
so windowed extraction keeps its JSON schema — the mechanism behind key points scoring 92-95.
`/text/generation` takes one prompt string and has no structured output, so the JSON has to survive
on the prompt plus our repair and object-salvage parsing. Both work; the first is better.

## Step 2 — configure

Chat endpoint (preferred):

    VLLM_API_BASE=https://cpd-watsonx-imir.apps.ocp4.iaf.in/ml/v1/text/chat?version=2023-05-29

Generation endpoint (what the reference script uses):

    VLLM_API_BASE=https://cpd-watsonx-imir.apps.ocp4.iaf.in/ml/v1/text/generation?version=2023-05-29

Then, for either:

    WATSONX_PROJECT_ID=<project id>
    LLM_MODEL_PATH=meta-llama/llama-3-3-70b-instruct
    LLM_AUTH_MODE=cp4d                 # auto-selected when CP4D_AUTH_URL is set
    CP4D_AUTH_URL=https://cpd-watsonx-imir.apps.ocp4.iaf.in/icp4d-api/v1/authorize
    CP4D_USERNAME=<user>
    CP4D_API_KEY=<key>
    CP4D_TOKEN_TTL=3600                # how often to re-authorize
    LLM_VERIFY_SSL=false               # self-signed cluster certificate
    LLM_PROVIDER_ORDER=                # OpenRouter-only, leave empty

The watsonx URL is configured COMPLETE, including `?version=`, and the request shape is read from
its path. Nothing else in the service changes.

## What each shape sends

| | chat | generation |
|---|---|---|
| model field | `model_id` | `model_id` |
| prompt | `messages` | one `input` string, Llama 3 chat template |
| budget | `max_tokens` | `parameters.max_new_tokens` |
| answer | `choices[0].message.content` | `results[0].generated_text` |
| structured output | `response_format` honoured | not available |

## Authentication

A CP4D credential is **not** a bearer token. `POST {username, api_key}` to `/icp4d-api/v1/authorize`
returns `{"token": ...}`, and that token expires. `core/ibm_auth.py` caches it and re-authorizes
`CP4D_TOKEN_TTL` seconds after issue, refreshing 5 minutes early — the margin must exceed the
slowest single call, or a token can expire during a 60-90 s extraction and fail it. The response
carries no expiry, which is why the TTL is configured rather than read.

`LLM_AUTH_MODE=bearer` skips all of it, for a long-lived Zen key or a plain vLLM.

## After switching, before trusting any number

- **Re-measure the four test meetings** (`scratchpad/m3/MEASURE.md`). Every score so far came from
  Llama 3.3 70B on DeepInfra via OpenRouter; another host may serve a different quantisation.
- **Check the model is actually served.** The reference script carries a note that
  `meta-llama/llama-3-1-8b-instruct` was rejected as "not supported" on this cluster, so confirm the
  70B id before planning around it.
- **Expect lower structured-output reliability on `/text/generation`** and watch the logs for
  `needed repair to parse` and `needed objects to parse`.
