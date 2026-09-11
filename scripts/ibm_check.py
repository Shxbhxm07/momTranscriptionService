#!/usr/bin/env python3
"""Check an IBM watsonx endpoint against what this pipeline actually needs, before wiring it in.

Cloud Pak for Data (the IAF cluster):
    CP4D_AUTH_URL=https://cpd-watsonx-imir.apps.ocp4.iaf.in/icp4d-api/v1/authorize \
    CP4D_USERNAME=... CP4D_API_KEY=... WATSONX_PROJECT_ID=... \
    WATSONX_HOST=https://cpd-watsonx-imir.apps.ocp4.iaf.in \
    MODEL_ID=meta-llama/llama-3-3-70b-instruct VERIFY_SSL=false python3 scripts/ibm_check.py

IBM Cloud SaaS: set IBM_APIKEY instead of the CP4D_* trio.

The important question it answers: does /ml/v1/text/chat exist? With it we keep JSON-schema
extraction, which is what makes key points score 92-95. Without it we fall back to
/ml/v1/text/generation, where the JSON must survive on the prompt and our repair steps alone.
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

import httpx

HOST = os.getenv("WATSONX_HOST", "").rstrip("/")
VERSION = os.getenv("WATSONX_VERSION", "2023-05-29")
PROJECT = os.getenv("WATSONX_PROJECT_ID", "")
MODEL = os.getenv("MODEL_ID", "meta-llama/llama-3-3-70b-instruct")
VERIFY = os.getenv("VERIFY_SSL", "false").lower() == "true"
if not HOST:
    sys.exit("set WATSONX_HOST (e.g. https://cpd-watsonx-imir.apps.ocp4.iaf.in)")

client = httpx.Client(timeout=120, verify=VERIFY)
ok = lambda label, good, note="": print(f"  {'PASS' if good else 'FAIL'}  {label}" + (f"  — {note}" if note else ""))
results = {}

print(f"\n1. AUTHENTICATION   (TLS verification {'on' if VERIFY else 'OFF'})")
token = None
if os.getenv("CP4D_AUTH_URL"):
    try:
        r = client.post(os.getenv("CP4D_AUTH_URL"),
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                        json={"username": os.getenv("CP4D_USERNAME"), "api_key": os.getenv("CP4D_API_KEY")})
        token = r.json().get("token") if r.status_code == 200 else None
        ok("CP4D /icp4d-api/v1/authorize", bool(token), f"HTTP {r.status_code}" + ("" if token else f": {r.text[:120]}"))
    except Exception as e:
        ok("CP4D authorize", False, repr(e))
elif os.getenv("IBM_APIKEY"):
    try:
        r = client.post("https://iam.cloud.ibm.com/identity/token",
                        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": os.getenv("IBM_APIKEY")},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = r.json().get("access_token") if r.status_code == 200 else None
        ok("IBM Cloud IAM token", bool(token), f"expires_in={r.json().get('expires_in')}" if token else r.text[:120])
    except Exception as e:
        ok("IAM token", False, repr(e))
else:
    sys.exit("set CP4D_AUTH_URL (+CP4D_USERNAME/CP4D_API_KEY) or IBM_APIKEY")
if not token:
    sys.exit("\nNo token — nothing else can be tested.")
H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

print("\n2. WHICH ENDPOINTS EXIST   (decides whether we keep structured extraction)")
chat_url = f"{HOST}/ml/v1/text/chat?version={VERSION}"
gen_url = f"{HOST}/ml/v1/text/generation?version={VERSION}"
chat_body = {"model_id": MODEL, "project_id": PROJECT, "max_tokens": 20,
             "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}
gen_body = {"model_id": MODEL, "project_id": PROJECT,
            "input": "<|begin_of_text|><|start_header_id|>user<|end_header_id|>\nReply with the single word: ok<|eot_id|>"
                     "<|start_header_id|>assistant<|end_header_id|>\n",
            "parameters": {"decoding_method": "greedy", "max_new_tokens": 20, "temperature": 0.1}}
for label, url, body, key in (("chat  /ml/v1/text/chat", chat_url, chat_body, "chat"),
                              ("gen   /ml/v1/text/generation", gen_url, gen_body, "gen")):
    try:
        r = client.post(url, headers=H, json=body)
        good = r.status_code == 200
        results[key] = good
        text = ""
        if good:
            d = r.json()
            text = (d.get("choices", [{}])[0].get("message", {}).get("content")
                    if "choices" in d else d.get("results", [{}])[0].get("generated_text", ""))
        ok(label, good, (repr((text or "")[:40]) if good else f"HTTP {r.status_code}: {r.text[:120]}"))
    except Exception as e:
        results[key] = False
        ok(label, False, repr(e))

if results.get("chat"):
    print("\n3. STRUCTURED OUTPUT on the chat endpoint   (window extraction depends on this)")
    schema = {"type": "object", "properties": {"points": {"type": "array", "items": {
        "type": "object", "properties": {"text": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["text", "quote"], "additionalProperties": False}}},
        "required": ["points"], "additionalProperties": False}
    for label, fmt in (("response_format=json_schema", {"type": "json_schema", "json_schema": {
                            "name": "points", "strict": True, "schema": schema}}),
                       ("response_format=json_object (fallback)", {"type": "json_object"})):
        try:
            r = client.post(chat_url, headers=H, json={**chat_body, "max_tokens": 200, "response_format": fmt,
                "messages": [{"role": "system", "content": "Extract points as JSON: {'points':[{'text','quote'}]}"},
                             {"role": "user", "content": "The council approved the budget. Nick called it straightforward."}]})
            good = r.status_code == 200
            note = ""
            if good:
                c = r.json()["choices"][0]["message"].get("content") or ""
                try: note = f"parsed, {len(json.loads(c).get('points', []))} points"
                except Exception: note = f"not valid JSON: {c[:50]!r}"
            ok(label, good, note or f"HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            ok(label, False, repr(e))
else:
    print("\n3. STRUCTURED OUTPUT — skipped, no chat endpoint")

print("\n4. TRUNCATION SIGNAL   (we log and recover from cut-off replies)")
try:
    if results.get("chat"):
        r = client.post(chat_url, headers=H, json={**chat_body, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Count from 1 to 200, one per line."}]})
        fr = r.json()["choices"][0].get("finish_reason")
    else:
        b = json.loads(json.dumps(gen_body)); b["parameters"]["max_new_tokens"] = 16
        b["input"] = b["input"].replace("Reply with the single word: ok", "Count from 1 to 200, one per line.")
        r = client.post(gen_url, headers=H, json=b)
        fr = r.json()["results"][0].get("stop_reason")
    ok("reports why it stopped", fr in ("length", "max_tokens", "token_limit"), f"got {fr!r}")
except Exception as e:
    ok("truncation signal", False, repr(e))

print("\n5. CONCURRENCY   (we run 2 calls at once)")
url, body = (chat_url, chat_body) if results.get("chat") else (gen_url, gen_body)
def one(_):
    t = time.time(); r = client.post(url, headers=H, json=body); return r.status_code, time.time() - t
with ThreadPoolExecutor(max_workers=2) as ex: res = list(ex.map(one, range(2)))
ok("2 concurrent calls", all(s == 200 for s, _ in res), ", ".join(f"{s} in {d:.1f}s" for s, d in res))

print("\nCONFIGURE THIS")
if results.get("chat"):
    print(f"  VLLM_API_BASE={chat_url}          ← keeps JSON-schema extraction")
elif results.get("gen"):
    print(f"  VLLM_API_BASE={gen_url}     ← no structured output; JSON rests on prompt + repair")
else:
    print("  neither endpoint answered — send me the exact URL and a sample response")
print(f"  WATSONX_PROJECT_ID={PROJECT or '<project id>'}\n  LLM_MODEL_PATH={MODEL}\n"
      f"  LLM_AUTH_MODE={'cp4d' if os.getenv('CP4D_AUTH_URL') else 'iam'}\n  LLM_VERIFY_SSL={'true' if VERIFY else 'false'}")
