# OCP deployment: offline MoM + document translation

The same pattern as mom-ai's `deploy/openshift`: one ConfigMap, `offline-mom-config`, built from the
overlay's `offline-mom.env`, holds every setting; everything else is written once in `base/`.
Credentials never go in it: they live in the `offline-mom-secrets` Secret. Changing configuration
means editing the env file and re-applying. No image rebuild.

## The two shapes

| | `ibm-ocp-gpu` | `ibm-ocp-to-gb10` |
|---|---|---|
| Where speech runs | Whisper as a pod, processor build, no GPU (`components/speech`) | the GB10 (`11.0.0.34`), over the LAN |
| `WHISPERCPP_URL` | `http://whisper-server:8080` | `http://11.0.0.34:8080` |
| `NEMO_URL` | `http://nemo-service:8003` | `http://11.0.0.34:8003` |
| Diarization | **off** (`ENABLE_DIARIZATION=false`) — nemo-service is not deployed | **off**, same setting |
| Model paths, `SPEAKER_CLEANUP` | in the env file (read by the pods) | set on the GB10, in its compose |
| GPU on OCP | none — Whisper runs on the processor, ~8 min per 9-minute meeting. A GPU cluster can build `Dockerfile.gpu` instead and get 40 s | none |
| PVC for model files | `offline-mom-models`, ~3.1 GB (Whisper only) | none |
| Extra requirement | GPU operator on the nodes | firewall rule: OCP pod CIDR → 11.0.0.34 on 8080, 8003 |
| LLM | IBM watsonx (CP4D) | IBM watsonx (CP4D) |

`ibm-ocp-to-gb10` matches mom-ai's current split plan and needs no GPU on OCP.

## Layout

    base/                    transcribe-api, llama-service, mom-consumer, Route    (no GPU)
    components/speech/   whisper-server, nemo-service, the models PVC          (no GPU needed)
    overlays/ibm-ocp-gpu/      base + speech + offline-mom.env
    overlays/ibm-ocp-to-gb10/  base only        + offline-mom.env
    secrets.example.env      the credential keys, never the values

## Apply (ops step, needs cluster access)

    # 1. A DEDICATED namespace. Not mom-ai's: llama-service, nemo-service and whisper-server are its
    #    names too, and applying there would REPLACE the running mom-ai app.
    oc new-project offline-mom

    # 2. Credentials
    cp deploy/openshift/secrets.example.env secrets.env      # fill it in; never commit it
    oc create secret generic offline-mom-secrets --from-env-file=secrets.env -n offline-mom

    # 3. Configuration: set every CHANGE_ME, and the registry in kustomization.yaml
    grep -rn CHANGE_ME deploy/openshift/overlays/<shape>/

    # 4. Preview, then apply
    oc kustomize deploy/openshift/overlays/<shape>           # dry render, review it
    oc apply -k  deploy/openshift/overlays/<shape>
    oc rollout status deployment -n offline-mom

## Changing a setting later

Edit `overlays/<shape>/offline-mom.env` and run `oc apply -k` again. The ConfigMap's name carries a
hash of its contents, so the Deployments see a new name and roll by themselves. To force a restart
anyway: `oc rollout restart deployment -n offline-mom`.

Testing without IBM: point three lines at OpenRouter and add its key to the Secret as `GROQ_API_KEYS`:

    VLLM_API_BASE=https://openrouter.ai/api/v1
    LLM_MODEL_PATH=meta-llama/llama-3.3-70b-instruct
    LLM_AUTH_MODE=bearer        # and remove WATSONX_PROJECT_ID / CP4D_AUTH_URL
    LLM_PROVIDER_ORDER=DeepInfra

## Checking it worked

    oc exec deploy/llama-service  -n offline-mom -- curl -s localhost:8001/health   # which LLM backend, URL and model resolved
    oc exec deploy/transcribe-api -n offline-mom -- curl -s localhost:8000/health   # whisper, NeMo and llama reachable?

A setting that did not take effect is otherwise invisible until minutes come back from the wrong
engine, so check these after every change.

## For `ibm-ocp-to-gb10`: the GB10 side

The GB10 must run the 1.0.0 speech images. From this repository on the GB10:

    docker compose build whisper-server nemo-service && docker compose up -d whisper-server nemo-service

The July NeMo image still running there works, but it logs a Qdrant "Startup error" on every start.
