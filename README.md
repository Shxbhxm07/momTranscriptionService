# Offline Minutes-of-Meeting & Document Translation API

Two products over one local LLM, both fully offline — no cloud API, no internet at
inference time:

| | input | output |
|---|---|---|
| **Minutes of Meeting** | Hindi / English / Hinglish audio | English MoM |
| **Document translation** | PDF · DOCX · DOC · TXT | Hindi ⇄ English text |

```
POST /transcribe-and-generate-mom     multipart/form-data, audio=<file>
POST /translate-document              multipart/form-data, file=<file>
                                      [source_lang] [target_lang]   both optional
```

```json
{
  "success": true,
  "mom": {
    "title": "Sprint Review and Deployment Planning",
    "summary": "Rahul opened the sprint review meeting...",
    "key_points": ["Reported API migration is 80% complete", "..."],
    "decisions": ["Production deployment will be postponed until next Friday", "..."],
    "action_items": [
      {"task": "Fix database indexing", "assigned_to": "Priya", "assigned_by": "Rahul", "due": "Wednesday"}
    ],
    "agenda": ["..."], "attendees": [{"name": "...", "role": "..."}],
    "purpose": "...", "formatted": "<rendered minutes document>"
  }
}
```

**The transcript is never returned.** It is produced internally, handed to the LLM, and
discarded — there is no endpoint that serves it, and no transcript-shaped key in any
response. `check_mom.py` asserts this on every test clip.

---

## Layout

```
offline-mom-api/
├── docker-compose.yml     the whole stack
├── .env                   host paths to the model weights (copy from .env.example)
├── api/                   the public API — the only service written for this project
│   ├── core/translate_doc.py  chunking, batching and retry for document translation
│   ├── utils/documents.py     pdf/docx/doc/txt → text blocks (+ OCR for scans)
│   └── make_test_docs.py      generates the translation test fixtures
├── whisper-service/       builds whisper.cpp v1.8.4 from source (CUDA)
├── nemo-service/          speaker diarization        ┐ vendored unmodified from the
├── llama-service/         MoM prompts + JSON repair  ┘ MoM project this was extracted from
└── scripts/
    └── seed-nemo-cache.sh seed TitaNet weights before going air-gapped
```

Self-contained: no path here points outside this directory. The two exceptions are the
model weights — 61 GB and 2.9 GB — which stay on the host and are named in `.env`.

## Quick start

```bash
cp .env.example .env          # point at your model-weight directories
./scripts/seed-nemo-cache.sh  # TitaNet weights, needs a network ONCE
docker compose up -d          # first start loads 61 GB of LLM weights — allow ~8 minutes
curl -F "audio=@meeting.mp3" http://localhost:8000/transcribe-and-generate-mom
curl -F "file=@report.pdf" -F "target_lang=Hindi" http://localhost:8000/translate-document
```

A first build compiles whisper.cpp for six GPU generations and takes tens of minutes;
after that the image is cached.

`GET /health` reports both model servers and, critically, **which LLM backend was actually
resolved** — so "is this really offline?" is one HTTP call, not an audit of container env vars.

---

## Pipeline

```
   Audio upload  (mp3 · m4a · wav · webm · flac · ogg · mp4)
        │
        ▼  ffmpeg → 16 kHz mono PCM                          [transcribe-api]
   Offline audio preprocessing
        │
        ▼  whisper.cpp large-v3, CUDA, <|translate|> task    [whisper-server]
   Offline speech-to-text
        │
        ▼  repetition / hallucination filtering              [transcribe-api]
        ▼  NeMo TitaNet — who spoke when, merged by timestamp [nemo-service]
   English transcription, speaker-labelled  ← internal only, never returned
        │
        ▼  gpt-oss-120b via vLLM, llama-service's MoM prompts [llama-service → vllm]
   English MoM
        │
        ▼
   Return ONLY the MoM
```

### The five services

| service | device | role | model |
|---|---|---|---|
| `whisper-server` | GPU | speech → English text | `ggml-large-v3` (2.9 GB) |
| `nemo-service` | GPU | who spoke when | NeMo TitaNet (~150 MB) |
| `vllm` | GPU | serves the LLM | `gpt-oss-120b`, mxfp4 (61 GB) |
| `llama-service` | CPU | transcript → structured MoM | *(reused unmodified)* |
| `transcribe-api` | CPU | the one public endpoint | — |

**GPU memory is the binding constraint, and the working range is narrow.** On the GB10
(121 GB unified) the LLM weights alone are 61 GB, so `--gpu-memory-utilization` is squeezed
from both sides — measured, not guessed:

| value | result |
|---|---|
| `0.62` (the MoM stack's) | vLLM 76.3 GB + whisper 4.2 GB → the first diarization dies with `CUDA error: out of memory`, while every container still reports healthy |
| `0.55` | vLLM will not start: `No available memory for the cache blocks` |
| **`0.60`** ← shipped | vLLM gets a 68k-token KV cache; whisper and NeMo both fit |

**Startup order is also load-bearing.** vLLM sizes its KV cache from memory that is FREE
when it starts, so whisper and NeMo must come up *after* it — `docker-compose.yml` enforces
this with `depends_on`. Without it, vLLM loses the race and fails to start.

Both model servers load their weights **once** at container startup and hold them for the
life of the container. `transcribe-api` and `llama-service` are stateless clients, so they
restart in a second without touching 64 GB of weights.

The MoM stack this was cut down from ran ten services (backend gateway, speech router,
tara, nemo, llama, vllm, chatterbox, postgres, qdrant, frontend).

---

## Offline models

**STT — Whisper large-v3 via whisper.cpp (CUDA).** Whisper was trained on two tasks,
selected by a decoder token: `<|transcribe|>` writes down what was said in the language it
was said in; `<|translate|>` writes it **in English**. This service always sends
`translate=true`, so the model decodes speech *directly into English in one pass*. There is
no translation model and no second hop — it is the same 3 GB file doing a different task.

Combined with `-l auto` (whisper detects the language itself), all three inputs collapse to
one code path:

```
Hindi audio     → detected hi   → translate → English
Hinglish audio  → detected hi/en → translate → English
English audio   → detected en   → translate → English (effectively pass-through)
```

There is no language parameter on the API, deliberately: forcing a language code was
measured on this stack to make things *worse* — English speech forced to `hi` came back
transliterated into Devanagari with a hallucination loop. Auto-detect is load-bearing.

**Diarization — NeMo TitaNet.** Labels each turn `[Speaker_1]`, `[Speaker_2]`, … which is
the exact form llama-service's prompt is written against. Called with `identify=false`, so
no enrolled-voice lookup happens, no Qdrant is needed and no voiceprints are ever stored —
clusters stay anonymous. Whisper's timed segments and NeMo's speaker turns are merged by
maximum time overlap (`utils/formatting.py`). Measured: a 15.6-minute meeting diarized in
33 s; a 26-minute two-person interview gave 2 speakers and 110 turns, correctly.

**MoM — gpt-oss-120b via vLLM**, driven by `../llama-service`, which is reused **unmodified**:
same prompts, same JSON schema, same validate-and-repair retry, same fill passes that
re-query the model when decisions or action items come back empty. The only thing this
stack changes about it is one environment variable — `VLLM_API_BASE` pointed at the local
vLLM instead of Groq, because that service picks cloud-vs-local purely from that URL.

---

## Document translation

```
POST /translate-document   file=<pdf|docx|doc|txt>  [source_lang] [target_lang]
```

```json
{
  "success": true,
  "source_lang": "Hindi", "target_lang": "English", "source_lang_detected": true,
  "translated_text": "The Ministry of Finance has issued new guidelines...",
  "document": {"filename": "circular.pdf", "format": "pdf-mixed", "pages": 12,
               "ocr_pages": 4, "text_layer_pages": 8, "blocks": 96, "characters": 41203},
  "stats": {"chunks": 104, "translated": 104, "untranslated": 0, "model_calls": 12,
            "repeated_chunks_reused": 11, "failure_reasons": {}, "untranslated_samples": []},
  "note": "Source language was not specified; detected as Hindi from the document's script."
}
```

**Both language parameters are optional.** The source is detected from the document's
script and the target defaults to the other language, so `file=@x.pdf` alone is a complete
request. Either can be given as `Hindi`/`English` or `hi`/`en`.

```
   Upload  (pdf · docx · doc · txt)
        │
        ▼  pypdfium2 text layer · python-docx · LibreOffice (.doc)   [transcribe-api]
   Text blocks
        │  └─ PDF pages with no text layer → tesseract hin+eng OCR at 300 dpi
        ▼  split on sentence boundaries, batched to the model's output budget
        ▼  llama-service /translate_batch → vLLM → gpt-oss-120b
   Translated text
```

**No new model and no new container.** llama-service already had `/translate_batch`,
written for Hindi⇄English with a deterministic script validator; this endpoint reuses it
over the same vLLM that writes the minutes, exactly as `core/mom.py` reuses `/summarize`.

### Four things worth knowing

**Code is hidden from the model, not explained to it.** A translation model cannot tell a
shell command from a sentence. MEASURED on a real technical document (83 blocks): `tail -3`
came back as `पूंछ -3` — "tail" as in an animal's — `llama-service/core/term_corrector.py`
was renamed into Hindi, four `CONSTANT_CASE` identifiers were translated, and `0.86` came
back as `०.८६`. No prompt fixes this reliably, so `utils/code_spans.py` masks code-like
spans before the model sees them and restores them verbatim afterwards:

```
curl -s localhost:8001/health   →   ⟦0⟧   →   ⟦0⟧   →   curl -s localhost:8001/health
                                  masked    model      restored
```

Protected: backtick spans, shell command lines, URLs, `host:port/path`, file paths,
`snake_case` / `CONSTANT_CASE` / `CamelCase` identifiers, `foo()`, `key=Value`, `--flags`,
single-token quoted terms and emoji. A chunk that masks down to nothing but placeholders
skips the model entirely — that line is then verbatim by construction rather than by luck.
**If the model loses a placeholder, the chunk fails** (`lost_code_span`) and falls back to
its source, because Hindi prose with a command silently deleted from the middle is worse
than an untranslated paragraph: nothing about it looks wrong.

**It reports what it could not translate.** llama-service's single `/translate` endpoint
returns *the original text* when the model refuses or comes back empty — correct for live
captions, silently wrong for a document, because untranslated paragraphs are then
indistinguishable from translated ones. `/translate_batch` instead validates every item
against the target script and says *why* each failure failed. Untranslated chunks still
fall back to their source text (a hole where a paragraph was is worse), but they are
counted in `stats.untranslated`, explained in `note`, and sampled in
`untranslated_samples` — so a partial result can never pass for a complete one.

**Scanned pages are decided one at a time.** A page whose text layer holds fewer than
`MIN_PAGE_TEXT_CHARS` characters is treated as a scan and sent to OCR; the rest use their
text layer. Deciding per document instead would get mixed files wrong in both directions —
a scanned appendix on a born-digital report is the common shape for Hindi government PDFs.
`format` comes back as `pdf-text`, `pdf-ocr` or `pdf-mixed` to say what actually happened.

**pypdfium2 was chosen over pypdf on a measurement.** On the same Hindi PDF, pypdf
returned mangled Devanagari with substituted characters and no word breaks
(`वित्तमंत्रालयनेसभीकेंद्रीयसरकेंरविभीगों…`); pypdfium2 returned correct text whose only defect was an
occasional missing space. Those missing spaces were then measured not to matter: the
mis-spaced extraction and a clean copy of the same sentence produced **character-identical
English translations**, because the model absorbs the spacing.

### Measured on this stack

| | |
|---|---|
| throughput | **~43 chars/s**, unchanged from 6 to 10 blocks per call — bound by output tokens, not round trips |
| a 400-character document | ~10 s, one model call |
| a 40,000-character document | ~15 min (extrapolated from the above) |
| batch budget | llama-service allots `min(max(chars/2, 500)·3, 6000)` tokens and gpt-oss-120b spends the same budget thinking — so `DOC_BATCH_CHARS` stays at 2500, a third under the cap |

Repeated chunks are translated once and reused (`repeated_chunks_reused`), which matters
most on OCR'd scans where a running header reappears on every page.

---

## Concurrency: every handler is sync, on purpose

Both upload endpoints are declared `def`, not `async def`. This looks like a style choice
and is not one.

Every call these pipelines make is **blocking** `requests` I/O — whisper, the diarizer, the
speaker-namer, the MoM writer, the translator. On the asyncio event loop that blocks the
entire process for the length of the job, and these jobs are minutes long.

**MEASURED 2026-09-08, as a live outage.** While one 9-minute audio upload was being
processed by an `async def` handler:

```
GET /health                        no response after 90 s
OPTIONS /translate-document        no response at all
```

The container went on reporting `healthy` the whole time, because Docker's healthcheck was
still showing its last successful probe. A browser frontend calling any endpoint saw only:

```
TypeError: NetworkError when attempting to fetch resource.
```

— which reads like a CORS or network fault and is neither. The server was simply not
answering anyone.

Declaring the handlers `def` moves them to FastAPI's threadpool. Same measurement after
the change, with a translation running:

```
GET /health                        200 in 0.015 s
OPTIONS /translate-document        200 in 0.003 s
```

The consequence to remember: a sync handler must not `await`, so both read their upload
with `file.file.read()` rather than `await file.read()`. GPU work still serialises
downstream — this fixes who can *reach* the API, not how many jobs the GPU runs at once.

---

## Verifying it is really offline

All four services were run on a docker network with `internal: true` (no route to the
internet) and the full pipeline still worked:

```bash
docker exec transcribe-llama-service python -c \
  "import socket; socket.gethostbyname('api.groq.com')"     # gaierror — cannot resolve
docker exec transcribe-api sh -c \
  'curl -F "audio=@/tmp/meeting.wav" http://localhost:8000/transcribe-and-generate-mom'
# → full English MoM with decisions and action items
```

With `internal: true` the published host ports stop routing, so test from inside the
container as above. `transcribe-api`'s own source contains no cloud endpoint and no API
key. The **only** step needing a network is `pip install` at image **build** time.

One caveat stated plainly: `llama-service` is reused as-is and still carries a Groq code
path. `docker-compose.yml` pins it to the local vLLM; run it some other way and forget
that variable and it defaults to Groq. That is why `/health` echoes the resolved backend
(`"backend": "vllm"`, `"mode": "offline"`) rather than asserting offline-ness.

---

## Why ffmpeg is in the image

`whisper-server` decodes with **miniaudio**. Measured against this exact build:

| format | direct to whisper-server | through this API |
|---|---|---|
| WAV, MP3, FLAC | ✅ | ✅ |
| **m4a / AAC** | ❌ `Invalid request` | ✅ |
| **webm / opus** | ❌ `Invalid request` | ✅ |

m4a is what phones record and webm/opus is what browser `MediaRecorder` produces, so
without ffmpeg normalisation those uploads simply fail.

---

## Configuration

| env | default | notes |
|---|---|---|
| `WHISPERCPP_URL` | `http://whisper-server:8080` | STT server |
| `LLAMA_URL` | `http://llama-service:8001` | MoM writer |
| `NEMO_URL` | `http://nemo-service:8003` | diarizer |
| `ENABLE_DIARIZATION` | `true` | off = plain transcript, degraded attribution |
| `MOM_TEMPERATURE` | `0.05` | minutes should be reproducible, not creative |
| `MIN_TRANSCRIPT_CHARS` | `80` | below this, no MoM is attempted |
| `FILTER_HALLUCINATIONS` | `true` | Whisper repetition / YouTube-artifact scrubbing |
| `ENABLE_NOISE_REDUCTION` | `false` | ffmpeg denoise + gate; quality trade, costs CPU |
| `BEAM_SIZE` | `5` | **do not raise above 5** — see below |
| `MAX_CONTEXT` | `32` | **do not change** — see below |
| `MAX_UPLOAD_MB` | `500` | audio |

Document translation adds its own:

| env | default | notes |
|---|---|---|
| `MAX_DOC_MB` | `50` | documents are small; OCR makes big scans expensive in CPU, not bytes |
| `DOC_BATCH_CHARS` | `2500` | source chars per model call — **do not raise above 4000**, see below |
| `DOC_BATCH_ITEMS` | `10` | blocks per model call |
| `DOC_CHUNK_CHARS` | `1200` | longer blocks are split on sentence boundaries first |
| `DOC_TRANSPORT_RETRIES` | `3` | network/5xx retries; validation failures get one solo retry instead |
| `ENABLE_OCR` | `true` | off = scanned PDFs return a 422 explaining why |
| `MIN_PAGE_TEXT_CHARS` | `40` | below this a PDF page counts as scanned and goes to OCR |
| `OCR_DPI` | `300` | **do not lower** — see below |
| `OCR_LANGS` | `hin+eng` | narrowed automatically when `source_lang` is given |
| `OCR_MAX_PAGES` | `200` | pages past this are skipped rather than holding a worker for an hour |
| `SOFFICE_TIMEOUT` | `180` | LibreOffice builds its profile on first run, which dominates a small file |
| `TRANSLATE_TIMEOUT` | `3600` | a long document is minutes of model time |
| `PROTECT_CODE_SPANS` | `true` | mask code before translating — off reproduces the failures above |
| `NORMALIZE_DIGITS` | `true` | Devanagari digits → ASCII in the output |

Two of these carry the same kind of measured failure mode as `BEAM_SIZE` above:

- **`DOC_BATCH_CHARS` > 4000** — llama-service sizes its output budget from the batch's
  total length and caps it at 6000 tokens, so past 4000 characters a longer batch gets no
  more room to answer in. gpt-oss-120b is a reasoning model spending that same budget on
  its own thinking first; overrun it and the call returns `content=None`.
- **`OCR_DPI` < 300** — Devanagari needs the resolution more than Latin does. The matras
  that separate ो from ा are one or two pixels tall at 150 dpi, so a lower setting does not
  blur the text, it changes the words.

Two carry measured failure modes on this stack (commented in `core/engine.py`):

- **`BEAM_SIZE` > 5** — large-v3 degenerates into a `सब्सक्राइब` hallucination loop at beam ≥ 7.
- **`MAX_CONTEXT`** fails on *both* sides. At `0` the decoder loses its anchor and silently
  drops ~45 % of a clip; at `64+` it echoes (60.9 % duplicate lines on a 19.7-minute
  session, the loop blocking the entire second half). `32` is clean on both.

---

## Testing

```bash
cd api && ./test_api.sh                       # every sample → English MoM, no transcript leaked
python3 make_test_docs.py samples/documents   # generate the translation fixtures (once)
./test_translate.sh                           # every format, both directions
```

`check_mom.py` enforces the contract: the five required keys with correct types, zero
Devanagari anywhere in the MoM, and none of `transcription` / `raw_transcript` /
`segments` / `speaker_transcript` / `transcript` at any level.

Measured end-to-end on the GB10:
- 59 s Hinglish meeting → full MoM in **~87 s**
- 26 min YouTube interview (m4a) → full MoM in **~8 min** (~4 min transcribe + diarize,
  ~4 min LLM, since a long transcript is chunked and synthesised)

---

## Limitations

- **Diarization tells you HOW MANY voices, not WHOSE.** It separates speakers reliably, but
  binding a cluster to a name is still the LLM's job from context, and it can get that
  backwards. Measured on a 26-minute two-person interview: diarization correctly found 2
  speakers, the previously-empty `attendees` list came back populated, and per-speaker key
  points separated cleanly — but one action item ("buy a camera") was assigned to the host
  when the transcript has the guest saying it. Enrolled voice profiles would fix this, at
  the cost of the speaker-enrollment database this service deliberately omits.
- **The attendee list can over-count.** The same run listed three attendees for two
  speakers — a named entry plus both `[Speaker_N]` tags, instead of merging the name into
  its tag. The prompt asks for one attendee per tag; it does not always comply.
- **`key_points` is derived, not prompted.** llama-service's schema has no such field, so
  it is built from `speaker_notes[].points` (substantive per-speaker bullets), falling back
  to `agenda` when there are none. On genuine multi-speaker meetings the first source
  populates; on single-speaker audio the fallback makes `key_points` mirror `agenda`.
  Forking llama-service's prompt to add a real field was rejected as the more invasive fix.
- **Non-meeting audio returns `mom: null`** with an explanatory `note`, rather than an
  invented MoM. An 11-second political speech legitimately produces no minutes.
- **Whisper's English is faithful but plain**, and proper nouns degrade across the language
  switch — Indian names especially ("QA lead" → "K-lead" was observed). The LLM often
  repairs these from context, but not always.
- **One GPU-bound request at a time is the sane assumption.** whisper-server serialises on
  the GPU, and vLLM is capped at `--max-num-seqs 4`.
- **`ggml-large-v3.bin` must be the multilingual model.** The `.en` variants cannot
  translate — whisper.cpp silently disables the translate task on them, and Hindi would
  come back as Hindi.
- **Cold start is ~8 minutes**, dominated by loading 61 GB of LLM weights off disk.

### Document translation

- **Romanized Hindi is read as English.** The source language is detected by SCRIPT, so
  "aap kaise hain" is Latin text and is reported as English. Detection cannot see the
  difference; pass `source_lang=Hindi` explicitly for transliterated documents.
- **Layout is not preserved — the output is plain text.** Paragraph breaks survive and
  DOCX table rows keep their cells (joined with ` | `), but fonts, columns, images,
  headers and footers do not. A PDF's table loses its cell boundaries entirely: measured
  on the test fixture, a three-column table came back as one run-on line
  (`Category Threshold Approver Goods 5,00,000 Joint Secretary …`), because a PDF text
  layer stores no table structure to recover. DOCX and DOC keep theirs.
- **PDF line rejoining is a heuristic.** A PDF stores visual lines, not paragraphs, so
  lines are rejoined into sentences before translating (`utils/documents.py::_reflow`).
  It occasionally merges a short heading into the paragraph below it. That costs
  formatting fidelity in the output, not translation quality — which is the trade, since
  the alternative is translating half-sentences.
- **Reading order was measured correct on a two-column PDF** (column one in full, then
  column two) but this depends on the PDF's own content order, which varies by producer.
  A PDF whose content stream disagrees with its visual layout will extract out of order.
- **OCR is only as good as the scan.** Skewed, low-resolution or handwritten pages
  degrade, and OCR errors are invisible to the translator — it faithfully translates
  whatever tesseract read. `format: "pdf-ocr"` / `"pdf-mixed"` and `ocr_pages` in the
  response say when this applies, so an odd translation can be traced to its source.
- **A term-as-term in plain letters is still translated.** Code protection keys off
  *shape* — a path, an identifier, a command. A document containing a table of "we said X /
  it heard Y" where both sides are ordinary English words has nothing to key off: measured
  on one such table, `Diarization | "Divide"` became the same Hindi word on both sides and
  the row lost its meaning. Quoting the term (`"Divide"`) protects it; leaving it bare does not.
- **Untranslated chunks are returned in the source language**, never dropped. They are
  counted in `stats.untranslated` with reasons and samples, and `note` says so — but a
  caller that ignores those fields will see a document that looks complete.
- **~43 chars/s** means a long document is genuinely slow: budget ~15 minutes for 40,000
  characters. One GPU-bound request at a time remains the sane assumption.
