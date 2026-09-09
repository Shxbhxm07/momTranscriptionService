# YouTube test material

Audio is NOT committed here — only the IDs and a fetch command, so nothing copyrighted is
redistributed with this repo. Downloading needs internet; it is test-data acquisition, not
inference, so it does not affect the offline guarantee.

```bash
pip install yt-dlp
yt-dlp "https://youtu.be/<ID>" -f "bestaudio[ext=m4a]/bestaudio" -o "<ID>.%(ext)s"
```

m4a is deliberate — whisper-server cannot decode it, so these clips also exercise the
ffmpeg normalisation path.

## Verified against this pipeline

| ID | length | what it is | why it is useful |
|---|---|---|---|
| `JcpIxGF_Meg` | 25:55 | "Fluent English Conversation Practice \| Ram & Rajni" | **The regression case.** Two speakers, rapid turn-taking, names said once at the start. This is the file that exposed the no-diarization failure. Diarization finds 2 speakers / 110 turns. |
| `xCXVRiQ5hnI` | 27:05 | "Panel Discussion: Impact of Government Policies on Indian Startups" | Multi-speaker panel, Indian English with Hindi code-switching, real policy discussion. |

## Further candidates (found, not yet run)

| ID | length | what it is |
|---|---|---|
| `vpCwYpZoFxo` | 48:37 | Group Discussion — Corporate Communication (structured, many speakers) |
| `lJ3Wt-ng1Ls` | 1:24:04 | WTFund Founders — 17 young founders pitching, heavy Hinglish |
| `HVkSGvZi-WY` | 48:29 | Founders vs VCs discussion — conversational Hinglish |
| `ISVrekq2lLM` | 33:10 | "Managing People / Managing Teams" — meeting-shaped content |

## A note on what makes GOOD test material

The samples in the parent directory are TTS-generated, which means **one synthetic voice**.
They verify the plumbing but cannot test diarization — one voice is one cluster. Real
recordings with genuinely different speakers are the only way to exercise it.

For a rigorous benchmark rather than spot checks, the standard corpora are purpose-built
for this and come with ground-truth speaker labels:

- **AMI Meeting Corpus** — 100 h of 4-person meetings, https://groups.inf.ed.ac.uk/ami/download/
- **ICSI Meeting Corpus** — 75 meetings with word-level timing and speaker labels

Public-body meetings (council/committee streams) are also strong candidates: genuinely
meeting-shaped, with real agendas, motions and action items, and they are public record.
