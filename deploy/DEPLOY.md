# MoM + Document Translation — deployment runbook

Two services from one bundle:
- **MoM**: a Kafka job carrying an audio file → minutes of meeting as a `.docx` in MinIO, a
  structured document in Elasticsearch, and an ack on Kafka.
- **Document translation**: `POST /translate-document` (PDF incl. scanned, DOCX, DOC, TXT; Hindi ⇄ English).

Speech runs on your GPUs; the language model runs on IBM watsonx.

## ⚠ Read first — CPU architecture

The images in `images/` are **linux/arm64** (built on an NVIDIA GB10). If your nodes are
**x86_64**, those images will not start: build them with `scripts/build-images.sh` from `source/`
on an x86_64 machine that can reach `nvcr.io` (see step 1b). Nothing else changes.

## What is in this bundle

| path | contents |
|---|---|
| `images/` | 4 container images (`docker save` tars, arm64) + `SHA256SUMS` |
| `models/` | whisper `ggml-large-v3.bin` (3.1 GB), `ggml-silero-v6.2.0.bin`, NeMo `titanet-l.nemo`, `vad_multilingual_marblenet.nemo` + `SHA256SUMS` |
| `deploy/openshift/` | Kustomize, the same pattern as mom-ai: `base/`, `components/gpu-speech/`, two overlays each with its `offline-mom.env`, `secrets.example.env`, `OVERLAYS.md` |
| `source/` | full source and Dockerfiles, for building images on another architecture |
| `scripts/` | `ibm_check.py` (tests the watsonx connection), `build-images.sh` |
| `docs/` | `kafka-contract.md`, `configuration.md`, `ibm-watsonx.md` |

## Components

| Deployment | image | GPU | memory request / limit | port | probe |
|---|---|---|---|---|---|
| whisper-server | offline-mom-whisper | 1 (uses ~4.2 GB) | 4 / 8 Gi | 8080 | TCP |
| nemo-service | offline-mom-nemo | not deployed — diarization is off (`ENABLE_DIARIZATION=false`) | | | |
| llama-service | offline-mom-llama | — | 0.5 / 2 Gi | 8001 | `GET /` |
| transcribe-api | offline-mom-api | — | 1 / 4 Gi | 8000 | `GET /` |
| mom-consumer | offline-mom-api | — | 0.25 / 1 Gi | — | heartbeat file |

Only Whisper needs a GPU, so one is enough. Diarization is off until further notice, which is why
nemo-service is commented out of `components/gpu-speech/kustomization.yaml` and its 48 GB image and
two model files are not needed. To bring it back, uncomment it, set `ENABLE_DIARIZATION=true`, and
give the cluster a second GPU or enable time-slicing, since Whisper and NeMo each request a whole one.
In the `ibm-ocp-to-gb10` shape Whisper is not deployed on OCP at all: it runs on the GB10 and OCP
needs no GPU.

## Prerequisites — from your side

1. Node CPU architecture (see the warning above), and for `ibm-ocp-gpu` the NVIDIA GPU operator
2. An internal registry and pull secret — images total ~69 GB (NeMo alone is 48 GB)
3. For `ibm-ocp-gpu`: a storage class offering ReadWriteMany (or ReadWriteOnce with both GPU pods on
   one node). For `ibm-ocp-to-gb10`: a firewall rule from the OCP pod CIDR to 11.0.0.34 on 8080 and 8003
4. IBM watsonx CP4D: host, username, API key, project id; the Llama 3.3 70B model enabled
5. Kafka brokers + the job and ack topics; MinIO endpoint, keys and buckets; Elasticsearch URL,
   credentials and the agreed index names
6. Network: pods → watsonx, MinIO, Kafka, Elasticsearch

## Step 1a — load and push the images (arm64 nodes)

    cd images && sha256sum -c SHA256SUMS
    for f in *.tar; do docker load -i "$f"; done        # or: podman load -i
    for i in api llama whisper nemo; do
      docker tag offline-mom-$i:1.0.0 <REGISTRY>/offline-mom-$i:1.0.0
      docker push <REGISTRY>/offline-mom-$i:1.0.0
    done

## Step 1b — build instead (x86_64 nodes)

On an x86_64 machine with Docker and access to `nvcr.io`:

    cd source && REGISTRY=<REGISTRY> ../scripts/build-images.sh

Whisper is compiled for GPU generations sm_80/86/89/90/120/121 (A100, A10, L40S, H100, Blackwell),
so it runs on any of those without changes.

## Step 2 — choose a shape, then namespace, secrets and configuration

Every setting is an environment variable in one file per shape, exactly as in mom-ai
(`deploy/openshift/OVERLAYS.md` has the details):

- `ibm-ocp-gpu`: Whisper and NeMo run as pods on OCP GPUs.
- `ibm-ocp-to-gb10`: the app runs on OCP and speech runs on the GB10 over the LAN (mom-ai's current
  split). No GPU is needed on OCP.

    # A DEDICATED namespace, never mom-ai's: llama-service, nemo-service and whisper-server are its
    # names too, and applying there would REPLACE the running mom-ai app.
    oc new-project offline-mom

    # Credentials, kept out of every file that is committed
    cp deploy/openshift/secrets.example.env secrets.env          # fill it in
    oc create secret generic offline-mom-secrets --from-env-file=secrets.env -n offline-mom

    # Configuration: every CHANGE_ME in the env file, and the registry in kustomization.yaml
    vi deploy/openshift/overlays/<shape>/offline-mom.env
    vi deploy/openshift/overlays/<shape>/kustomization.yaml
    grep -rn CHANGE_ME deploy/openshift/overlays/<shape>/        # must print nothing

## Step 3 — model files onto the PVC (one time, `ibm-ocp-gpu` only)

    oc apply -n offline-mom -f deploy/openshift/components/gpu-speech/pvc-models.yaml
    oc run model-loader -n offline-mom --image=registry.access.redhat.com/ubi9/ubi-minimal \
       --overrides='{"spec":{"volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"offline-mom-models"}}],
       "containers":[{"name":"model-loader","image":"registry.access.redhat.com/ubi9/ubi-minimal",
       "command":["sleep","3600"],"volumeMounts":[{"name":"m","mountPath":"/models"}]}]}}'
    oc cp models/whisper    offline-mom/model-loader:/models/whisper
    oc cp models/nemo       offline-mom/model-loader:/models/nemo
    oc cp models/SHA256SUMS offline-mom/model-loader:/models/
    oc exec -n offline-mom model-loader -- sh -c 'cd /models && sha256sum -c SHA256SUMS'
    oc delete pod -n offline-mom model-loader

Expected layout: `/models/whisper/ggml-large-v3.bin`, `/models/whisper/ggml-silero-v6.2.0.bin`,
`/models/nemo/titanet-l.nemo`, `/models/nemo/vad_multilingual_marblenet.nemo`. The paths are set in
the overlay's `offline-mom.env`.

## Step 4 — check watsonx before deploying

From a machine or pod on the cluster network:

    CP4D_AUTH_URL=https://<cluster>/icp4d-api/v1/authorize \
    CP4D_USERNAME=<user> CP4D_API_KEY=<key> WATSONX_PROJECT_ID=<project> \
    WATSONX_HOST=https://<cluster> MODEL_ID=meta-llama/llama-3-3-70b-instruct VERIFY_SSL=false \
    python3 scripts/ibm_check.py

It prints the `VLLM_API_BASE` to put in the overlay's `offline-mom.env`. **Prefer the `/ml/v1/text/chat`
endpoint if it exists**: it keeps structured JSON extraction.

## Step 5 — deploy

For `ibm-ocp-to-gb10`, bring speech up on the GB10 first (from this repository, on the GB10):

    docker compose build whisper-server nemo-service && docker compose up -d whisper-server nemo-service

Then, on OCP:

    oc kustomize deploy/openshift/overlays/<shape>        # dry render: review it
    oc apply -k  deploy/openshift/overlays/<shape>
    oc rollout status deployment -n offline-mom

Whisper takes about 20 s to load its model, NeMo a few seconds.

**Changing a setting later:** edit `offline-mom.env` and run `oc apply -k` again. The ConfigMap's name
carries a hash of its contents, so the pods roll by themselves; `oc rollout restart deployment -n
offline-mom` forces it.

## Step 6 — smoke tests

    oc exec -n offline-mom deploy/transcribe-api -- curl -s localhost:8000/        # static liveness
    oc exec -n offline-mom deploy/transcribe-api -- curl -s localhost:8000/health  # whisper, NeMo, llama reachable?
    oc exec -n offline-mom deploy/llama-service  -- curl -s localhost:8001/health  # which LLM backend and model resolved
    oc logs -n offline-mom deploy/mom-consumer | grep "newly assigned"             # joined the Kafka group

Then publish one real job (format in `docs/kafka-contract.md`) and expect, within ~5-20 minutes:
a `[JOB <id>] acked SUCCESS, offset committed` line in the consumer log, the `.docx` in the summary
bucket, the document in Elasticsearch, and the ack on the ack topic.

Document translation:

    curl -F file=@sample.pdf -F target_lang=hi https://<route>/translate-document

## Troubleshooting

| symptom | cause |
|---|---|
| pod `exec format error` | arm64 image on x86_64 nodes — step 1b |
| NeMo tries to download a model | `DIARIZATION_MODEL_PATH` / `VAD_MODEL_PATH` not set or files missing on the PVC |
| `SSL: CERTIFICATE_VERIFY_FAILED` to watsonx | set `LLM_VERIFY_SSL=false` (self-signed cluster certificate) |
| 401 from watsonx | CP4D username / API key, or `CP4D_AUTH_URL` wrong |
| 504 on the Route after 30 s | the timeout annotation on the Route is missing |
| a setting did not take effect | re-run `oc apply -k`; check `/health` on llama-service shows the expected backend |
| mom-ai stopped working after applying | the overlay was applied into mom-ai's namespace: the names overlap — restore mom-ai and use `offline-mom` |
| every ack is FAILURE with `no file_urls` | the job used `file_fids` only — that path is not built yet |
| permission denied writing files | grant the service account the `anyuid` SCC; the NVIDIA base images expect it |
| MoM empty or "legacy pipeline" in llama logs | the LLM call failed twice; check watsonx quota and connectivity |
| NeMo logs `Not able to download url` | a model is being loaded by NAME: check `DIARIZATION_MODEL_PATH` and `VAD_MODEL_PATH` point at files on the PVC |

## Known limitations

- **"Already ingested" files** (`file_fids` without `file_urls`) are acknowledged as FAILURE on
  purpose until the backend team confirms how a file id resolves to a readable object.
- **Accuracy** was measured on Llama 3.3 70B via another host; re-measure after switching to watsonx.
- **One meeting at a time.** Throughput is bounded by the GPU, roughly 3-6 meetings per hour.
- **NeMo's cluster clean-up (`SPEAKER_CLEANUP`) is off.** The current code can merge over-split speakers
  and drop noise clusters, but every accuracy figure was measured without it, so it ships off. Turning it
  on changes speaker counts and, through them, attendees and speaker naming — A/B it on the test meetings first.
- **No GPU on the cluster?** `whisper-service/Dockerfile.cpu` builds a 107 MB processor-only Whisper
  image: no CUDA, no NVIDIA base, runs on any node. Measured on the GB10: 7.6 min for a 9.3-minute
  meeting on 8 threads (the GPU takes 40 s), same model and settings, so accuracy is comparable.
  The alternative is the `ibm-ocp-to-gb10` shape, where OCP calls a GPU box over the LAN.
- **The Whisper image is ~1 GB** (2026-09-12). It builds on NVIDIA's vLLM base and runs on plain
  Ubuntu, carrying only whisper-server and the three CUDA libraries it links against. The previous
  19.8 GB image ran on the build base itself. Transcripts are byte-identical; a 9-minute meeting takes
  41 s instead of 35 s, because the old image also carried a newer GPU driver that the container
  runtime used in place of the node's.
- **The NeMo image is 48 GB**, almost all of it NVIDIA's base. A 20 GB build on Whisper's base was made and
  tested: it fails on the GB10 (that torch build's cuDNN has no convolution engine for sm_121), so it is not
  shipped. NVIDIA's PyTorch base, with the same torch build as the NeMo image, is the route to a smaller one.
