#!/usr/bin/env python3
"""Check an IBM watsonx endpoint against everything this pipeline actually needs.

    IBM_BASE=https://us-south.ml.cloud.ibm.com IBM_APIKEY=... IBM_PROJECT_ID=... \
    IBM_MODEL=meta-llama/llama-3-3-70b-instruct python3 ibm_check.py

Reports, in order: auth, whether an OpenAI-compatible /chat/completions exists, JSON-schema
structured output, the json_object fallback, output-limit behaviour, and 2-way concurrency.
Nothing here is specific to our prompts — it tests the contract llm_manager.generate() relies on.
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import httpx

BASE = os.getenv("IBM_BASE", "").rstrip("/")
APIKEY = os.getenv("IBM_APIKEY", "")
PROJECT = os.getenv("IBM_PROJECT_ID", "")
MODEL = os.getenv("IBM_MODEL", "meta-llama/llama-3-3-70b-instruct")
ZEN = os.getenv("IBM_ZEN_APIKEY", "")
if not BASE or not (APIKEY or ZEN):
    sys.exit("set IBM_BASE and IBM_APIKEY (or IBM_ZEN_APIKEY)")

ok = lambda label, good, note="": print(f"  {'PASS' if good else 'FAIL'}  {label}" + (f"  — {note}" if note else ""))
client = httpx.Client(timeout=120)

print("\n1. AUTHENTICATION")
token, mode = None, None
if ZEN:
    token, mode = ZEN, "Zen API key (long-lived — our static key pool works as-is)"
    ok("Zen key supplied", True, mode)
else:
    try:
        r = client.post("https://iam.cloud.ibm.com/identity/token",
                        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": APIKEY},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code == 200:
            j = r.json(); token = j["access_token"]; exp = j.get("expires_in", "?")
            mode = f"IAM token, expires in {exp}s — NEEDS A REFRESHER, our pool assumes static keys"
            ok("IAM token exchange", True, mode)
        else:
            ok("IAM token exchange", False, f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        ok("IAM token exchange", False, repr(e))
    if not token:      # some gateways take the raw API key as the bearer
        token, mode = APIKEY, "raw API key as bearer (untested above)"

H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
body = lambda **kw: {"model": MODEL, "messages": [{"role": "system", "content": "Reply exactly as asked."},
                                                  {"role": "user", "content": "Reply with the single word: ok"}],
                     "max_tokens": 20, "temperature": 0.01, **({"project_id": PROJECT} if PROJECT else {}), **kw}

print("\n2. OPENAI-COMPATIBLE ENDPOINT  (decides zero-code vs adapter)")
url, reply = None, None
for cand in (f"{BASE}/v1/chat/completions", f"{BASE}/ml/v1/text/chat?version=2024-10-08", f"{BASE}/chat/completions"):
    try:
        r = client.post(cand, headers=H, json=body())
        if r.status_code == 200:
            url, reply = cand, r.json()
            ok(f"POST {cand}", True, "200")
            break
        ok(f"POST {cand}", False, f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        ok(f"POST {cand}", False, repr(e))
if not url:
    sys.exit("\nNo working chat endpoint — send me the exact URL from your IBM console.")
shape = "choices[0].message.content" if "choices" in (reply or {}) else list((reply or {}).keys())
ok("OpenAI response shape", "choices" in (reply or {}), f"got {shape}")

print("\n3. STRUCTURED OUTPUT  (window extraction depends on this)")
schema = {"type": "object", "properties": {"points": {"type": "array", "items": {
    "type": "object", "properties": {"text": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["text", "quote"], "additionalProperties": False}}},
    "required": ["points"], "additionalProperties": False}
for label, extra in (("response_format=json_schema", {"response_format": {"type": "json_schema", "json_schema": {
                        "name": "points", "strict": True, "schema": schema}}}),
                     ("response_format=json_object (fallback)", {"response_format": {"type": "json_object"}})):
    try:
        r = client.post(url, headers=H, json={**body(max_tokens=200), "messages": [
            {"role": "system", "content": "Extract points as JSON with a 'points' array of {text, quote}."},
            {"role": "user", "content": "The council approved the budget. Nick said it was straightforward."}], **extra})
        good = r.status_code == 200
        parsed = ""
        if good:
            c = r.json()["choices"][0]["message"].get("content") or ""
            try: parsed = f"parsed, {len(json.loads(c).get('points', []))} points"
            except Exception: parsed = f"not valid JSON: {c[:60]!r}"
        ok(label, good, parsed if good else f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        ok(label, False, repr(e))

print("\n4. OUTPUT LIMIT BEHAVIOUR  (we rely on finish_reason)")
try:
    r = client.post(url, headers=H, json={**body(max_tokens=16), "messages": [
        {"role": "user", "content": "Count slowly from 1 to 200, one number per line."}]})
    fr = r.json()["choices"][0].get("finish_reason")
    ok("finish_reason reported on truncation", fr == "length", f"got {fr!r}")
except Exception as e:
    ok("finish_reason", False, repr(e))

print("\n5. CONCURRENCY  (we run 2 calls at once; more is faster if allowed)")
def one(_):
    t = time.time(); r = client.post(url, headers=H, json=body()); return r.status_code, time.time() - t
with ThreadPoolExecutor(max_workers=2) as ex: res = list(ex.map(one, range(2)))
ok("2 concurrent calls", all(s == 200 for s, _ in res), ", ".join(f"{s} in {d:.1f}s" for s, d in res))

print(f"\nSUMMARY\n  endpoint: {url}\n  auth: {mode}\n  model: {MODEL}")
print("  → zero-code path if section 2 and 3 passed: set VLLM_API_BASE, LLM_MODEL_PATH, GROQ_API_KEYS and restart.")
