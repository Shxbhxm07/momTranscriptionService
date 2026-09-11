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
| `manifests/` | OpenShift: ConfigMap, Secret template, PVC, one Deployment + Service per component, Route |
| `source/` | full source and Dockerfiles, for building images on another architecture |
| `scripts/` | `ibm_check.py` (tests the watsonx connection), `build-images.sh` |
| `docs/` | `kafka-contract.md`, `configuration.md`, `ibm-watsonx.md` |

## Components

| Deployment | image | GPU | memory request / limit | port | probe |
|---|---|---|---|---|---|
| whisper-server | offline-mom-whisper | 1 (uses ~4.2 GB) | 4 / 8 Gi | 8080 | TCP |
| nemo-service | offline-mom-nemo | 1 (uses ~0.6 GB) | 6 / 10 Gi | 8003 | `/health` (`nemo_loaded`) |
| llama-service | offline-mom-llama | — | 0.5 / 2 Gi | 8001 | `GET /` |
| transcribe-api | offline-mom-api | — | 1 / 4 Gi | 8000 | `GET /` |
| mom-consumer | offline-mom-api | — | 0.25 / 1 Gi | — | heartbeat file |

Whisper and NeMo each request a whole GPU. They fit together on one GPU (~5 GB) if your cluster
has GPU time-slicing or MIG enabled; otherwise give them one each.

## Prerequisites — from your side

1. Node CPU architecture (see the warning above) and NVIDIA GPU operator on the GPU nodes
2. An internal registry and pull secret — images total ~69 GB (NeMo alone is 48 GB)
3. A storage class offering ReadWriteMany (or ReadWriteOnce with both GPU pods on one node)
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

## Step 2 — namespace, config and secrets

    oc new-project mom            # or your namespace
    # edit every line marked CHANGE:
    vi manifests/01-configmap.yaml
    oc apply -f manifests/01-configmap.yaml
    # fill the template, then:
    oc apply -f manifests/02-secret.example.yaml     # or: oc create secret generic mom-secrets --from-env-file=...

Replace `REGISTRY` in `manifests/1*.yaml` with your registry path.

## Step 3 — model files onto the PVC (one time)

    oc apply -f manifests/03-pvc-models.yaml
    oc run model-loader --image=registry.access.redhat.com/ubi9/ubi-minimal \
       --overrides='{"spec":{"volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"mom-models"}}],
       "containers":[{"name":"model-loader","image":"registry.access.redhat.com/ubi9/ubi-minimal",
       "command":["sleep","3600"],"volumeMounts":[{"name":"m","mountPath":"/models"}]}]}}'
    oc cp models/whisper model-loader:/models/whisper
    oc cp models/nemo    model-loader:/models/nemo
    oc cp models/SHA256SUMS model-loader:/models/
    oc exec model-loader -- sh -c 'cd /models && sha256sum -c SHA256SUMS'
    oc delete pod model-loader

Expected layout: `/models/whisper/ggml-large-v3.bin`, `/models/whisper/ggml-silero-v6.2.0.bin`,
`/models/nemo/titanet-l.nemo`, `/models/nemo/vad_multilingual_marblenet.nemo`.

## Step 4 — check watsonx before deploying

From a machine or pod on the cluster network:

    CP4D_AUTH_URL=https://<cluster>/icp4d-api/v1/authorize \
    CP4D_USERNAME=<user> CP4D_API_KEY=<key> WATSONX_PROJECT_ID=<project> \
    WATSONX_HOST=https://<cluster> MODEL_ID=meta-llama/llama-3-3-70b-instruct VERIFY_SSL=false \
    python3 scripts/ibm_check.py

It prints the `VLLM_API_BASE` to put in the ConfigMap. **Prefer the `/ml/v1/text/chat` endpoint if
it exists** — it keeps structured JSON extraction. Re-apply the ConfigMap if it changes.

## Step 5 — deploy

    oc apply -f manifests/10-whisper.yaml -f manifests/11-nemo.yaml -f manifests/12-llama.yaml
    oc rollout status deploy/whisper-server deploy/nemo-service deploy/llama-service
    oc apply -f manifests/13-api.yaml
    oc rollout status deploy/transcribe-api
    oc apply -f manifests/14-consumer.yaml

Whisper takes about 20 s to load its model, NeMo a few seconds.

## Step 6 — smoke tests

    oc exec deploy/transcribe-api -- curl -s localhost:8000/            # static liveness
    oc exec deploy/transcribe-api -- curl -s localhost:8000/health      # reaches whisper, nemo, llama → watsonx
    oc logs deploy/mom-consumer | grep "newly assigned"                 # joined the Kafka group

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
- **The NeMo image is 48 GB**, almost all of it NVIDIA's base. A 20 GB build on Whisper's base was made and
  tested: it fails on the GB10 (that torch build's cuDNN has no convolution engine for sm_121), so it is not
  shipped. NVIDIA's PyTorch base, with the same torch build as the NeMo image, is the route to a smaller one.
