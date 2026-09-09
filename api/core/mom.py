"""Transcript → structured English Minutes of Meeting, via the local LLM.

This module deliberately contains NO prompt and NO parsing logic of its own. All of that
already exists, tested, in ../llama-service — the MoM prompts, the JSON schema, the
validate-and-repair retry, and the "fill decisions / action items / figures" passes that
re-query the model when a section comes back empty. Reimplementing any of it here would
have meant maintaining a second copy of a prompt that took a lot of measurement to get
right (see llama-service/prompts.py).

So llama-service is reused as a SERVICE, exactly like whisper-server is for ASR:

    transcribe-api ──POST /summarize──▶ llama-service ──▶ vLLM ──▶ gpt-oss-120b

llama-service is run UNMODIFIED. The only thing this stack changes about it is one
environment variable — VLLM_API_BASE pointed at the local vLLM instead of Groq — because
that service picks cloud-vs-local purely from that URL. Its /health echoes the resolved
mode back, so the offline pin is verifiable rather than assumed.

WHAT THIS MODULE DOES OWN: mapping llama-service's rich internal structure onto the
response contract this API promises. That mapping is the part specific to this product,
and it is small enough to read in one screen — see to_mom_response().
"""
import logging
import re
from typing import Any, Dict, List

import requests

from config import LLAMA_URL, MOM_TEMPERATURE, MOM_TIMEOUT, REFINE_TIMEOUT

logger = logging.getLogger(__name__)


class MomGenerator:
    """Thin client for llama-service. Stateless — the model lives in vLLM."""

    def __init__(self, base_url: str = LLAMA_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def is_ready(self) -> bool:
        """Probe '/' rather than '/health'.

        This mirrors llama-service's own healthcheck and the reason is worth keeping: its
        /health round-trips to the LLM, so calling it per-request would add seconds to
        every upload and hammer the model with liveness probes. '/' is a static route, so
        it answers immediately and still proves uvicorn is serving.
        """
        try:
            r = self.session.get(f"{self.base_url}/", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def health_detail(self) -> Dict[str, Any]:
        """Full /health — including which LLM backend llama-service actually resolved to.
        Used by this API's /health so 'am I really offline?' is one HTTP call."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=30)
            return r.json() if r.status_code == 200 else {"status": "unreachable"}
        except requests.RequestException as e:
            return {"status": "unreachable", "error": str(e)}

    def identify_speakers(self, transcript: str) -> Dict[str, Any]:
        """Ask llama-service to read real names out of a speaker-labelled transcript.

        Returns {} on any failure. That is the whole error policy: naming speakers IMPROVES
        the minutes but is never required to produce them, so a timeout, a bad JSON parse or
        a cold service must cost the request nothing. The caller keeps the anonymous tags.
        """
        try:
            r = self.session.post(
                f"{self.base_url}/identify-speakers",
                json={"text": transcript, "temperature": 0.01},
                timeout=REFINE_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning(f"[SPEAKER_ID] llama-service returned {r.status_code} — keeping tags")
                return {}
            mapping = r.json()
            return mapping if isinstance(mapping, dict) else {}
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[SPEAKER_ID] unavailable ({e}) — keeping tags")
            return {}

    def correct_transcript(self, transcript: str, known_names: List[str] | None = None) -> str:
        """Repair obvious ASR errors from context, anchored on any confirmed names.

        Returns the ORIGINAL transcript on any failure. Mode is "hinglish" — llama-service's
        conservative TRANSCRIPT_CORRECTION_PROMPT — and that choice is measured, not assumed.
        "translated" looks like the right label (this text did come out of Whisper's translate
        task) but that prompt exists to normalise machine-translated prose, and on a 3-minute
        slice of a real committee meeting it PARAPHRASED:

            mode=translated   83% retained, 73.2% word similarity
                              "inaccuracies that"      -> "of the content"
                              "the more, I think it's" -> "actually"
            mode=hinglish     99% retained, 97.8% word similarity
                              "up-to- date" -> "up-to-date"   "Ali" -> "Allie"
                              "C ulp."      -> "Culp."

        A pass that rewrites a quarter of the words is not correcting the transcript, it is
        replacing it — and every later stage is scored against that text.
        """
        try:
            r = self.session.post(
                f"{self.base_url}/correct-transcript",
                json={
                    "text": transcript,
                    "temperature": 0.1,
                    "mode": "hinglish",
                    "known_names": known_names or [],
                },
                timeout=REFINE_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning(f"[CORRECT] llama-service returned {r.status_code} — using the raw transcript")
                return transcript
            corrected = (r.json() or {}).get("corrected_text") or ""
            corrected = corrected.strip()
            if not corrected:
                return transcript
            # Second line of defence. llm_manager guards each chunk; this guards the whole
            # document, because a per-chunk guard cannot see that half the chunks fell back.
            # 0.90, not 0.75. Real correction removes a few fillers and repair loops — the
            # measured conservative pass keeps 99%. A pass that drops a tenth of the document
            # is paraphrasing or truncating, and the raw transcript is the better input.
            if len(corrected) < len(transcript) * 0.90:
                logger.warning(
                    f"[CORRECT] output shrank {len(transcript)} → {len(corrected)} chars "
                    "— using the raw transcript"
                )
                return transcript
            return corrected
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[CORRECT] unavailable ({e}) — using the raw transcript")
            return transcript

    def generate(self, transcript: str) -> Dict[str, Any]:
        """Send the English transcript, get llama-service's full MoM payload back.

        output_lang is pinned to "English". llama-service can localise into six languages,
        but this API's contract is English-only, and asking for English skips its
        translate-and-re-render path entirely — so the MoM is written in English by the
        model rather than translated into it.
        """
        payload = {
            "text": transcript,
            "temperature": MOM_TEMPERATURE,
            "output_lang": "English",
        }
        logger.info(f"[MOM] POST {self.base_url}/summarize ({len(transcript)} chars)")
        try:
            r = self.session.post(f"{self.base_url}/summarize", json=payload, timeout=MOM_TIMEOUT)
        except requests.RequestException as e:
            raise RuntimeError(f"Cannot reach the local MoM service at {self.base_url}: {e}") from e

        if r.status_code != 200:
            raise RuntimeError(f"llama-service error {r.status_code}: {r.text[:300]}")

        result = r.json()
        logger.info(
            f"[MOM] ✓ generated ({result.get('analysis_length', 0)} chars rendered, "
            f"chunks={result.get('chunks_used')})"
        )
        return result


# ── response mapping ─────────────────────────────────────────────────────────
# llama-service's `content` object is richer than this API's contract:
#
#   header{topic,meeting_date,meeting_time,venue}  agenda[]  attendees[{name,role}]
#   summary  key_points[]  speaker_notes[{speaker,points[]}]  decisions[]  key_figures[]
#   action_items[{task,assigned_to,assigned_by,due}]  purpose
#
# summary / key_points / decisions / action_items map straight across. _key_points() still
# carries fallbacks for a document generated before key_points existed — see it for why.


# Owner/Due packed into the task string by llama-service's FALLBACK action-item pass.
# That pass (used whenever the main JSON pass returns no action items) runs a prompt that
# emits text bullets — "Task — Owner: Name — Due: deadline" — and llm_manager wraps each
# whole bullet as {"task": <the entire bullet>, "assigned_to": "", "due": ""}. The model
# did extract the owner and deadline; they just never reach the structured fields.
#
# Harmless in the MoM product, whose renderer prints the task string verbatim so a human
# still sees "Owner: Rajni". Wrong for a JSON API: a caller reading action_items[].assigned_to
# gets "" while the answer sits in the sibling field. So unpack it here.
_OWNER_DUE_RE = re.compile(
    r"\s*[—–-]\s*Owner:\s*(?P<owner>.*?)\s*(?:[—–-]\s*Due:\s*(?P<due>.*?))?\s*$",
    re.IGNORECASE,
)
# The extraction prompt's literal "no deadline" value; keep `due` empty rather than echoing it.
_DUE_UNSET = {"not specified", "none", "n/a", "tbd", ""}


def _split_owner_due(task: str) -> Dict[str, str]:
    """Pull a trailing '— Owner: X — Due: Y' off a task string into structured fields.

    Returns the cleaned task plus owner/due. A task with no such suffix passes through
    untouched, so this is a no-op on well-formed items from the main JSON pass.
    """
    m = _OWNER_DUE_RE.search(task or "")
    if not m:
        return {"task": (task or "").strip(), "assigned_to": "", "due": ""}
    owner = (m.group("owner") or "").strip()
    due = (m.group("due") or "").strip()
    return {
        "task": task[: m.start()].strip(),
        "assigned_to": "" if owner.lower() in _DUE_UNSET else owner,
        "due": "" if due.lower() in _DUE_UNSET else due,
    }


def _as_list(value: Any) -> List:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _key_points(content: Dict[str, Any]) -> List[str]:
    """Take key_points from the MoM content, with two fallbacks.

    llama-service now emits a real `key_points` field, so this is a read rather than a
    derivation. It used to be derived from speaker_notes[].points, and that was the wrong
    source for a contract field: those bullets are ATTRIBUTED, so their completeness depends
    on diarization and on speaker identity staying stable across chunk boundaries — the least
    reliable part of the pipeline. Measured 2026-09-07 on a 29-minute meeting, the synthesis
    pass kept only the speakers it recognised in the final chunk, and key_points came back
    holding just the last third of the meeting while reading as a complete list.

    The fallbacks remain for an older llama-service or a partial document: speaker_notes
    first (attributed but real content), then agenda (always populated, but topic headings
    rather than content, so it is the weakest answer).
    """
    points: List[str] = [
        p for p in _as_list(content.get("key_points")) if isinstance(p, str) and p.strip()
    ]

    if not points:
        for note in _as_list(content.get("speaker_notes")):
            if isinstance(note, dict):
                points.extend(p for p in _as_list(note.get("points")) if isinstance(p, str) and p.strip())
        if points:
            logger.info("[MOM] key_points fell back to speaker notes (no key_points field returned)")

    if not points:
        points = [a for a in _as_list(content.get("agenda")) if isinstance(a, str) and a.strip()]
        if points:
            logger.info("[MOM] key_points fell back to agenda (no speaker notes returned)")

    # Order-preserving dedup — chunk+synthesise can repeat a point across sections.
    seen = set()
    out = []
    for p in points:
        k = p.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(p.strip())
    return out


def to_mom_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Map llama-service's payload onto this API's `mom` object.

    The five contract keys (title, summary, key_points, decisions, action_items) always
    exist and always have the right type, even if the model returned a partial document —
    a caller should never have to defend against a missing key. Everything beyond those is
    passed through because it is already generated and throwing it away would only make the
    API less useful.

    NOTHING TRANSCRIPT-SHAPED IS RETURNED. The transcript is an internal intermediate: it
    goes into llama-service and never comes back out through this function. llama-service's
    payload does carry `original_length` (a character count of the transcript) and this
    deliberately drops it too, so no property of the transcript leaks into the response.
    """
    content = result.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    header = content.get("header") if isinstance(content.get("header"), dict) else {}

    action_items = []
    for item in _as_list(content.get("action_items")):
        if isinstance(item, dict):
            task = item.get("task", "") or ""
            assigned_to = item.get("assigned_to", "") or ""
            due = item.get("due", "") or ""
            # Only unpack when the structured fields are actually empty — a well-formed
            # item from the main JSON pass must never be second-guessed.
            if not assigned_to and not due:
                parsed = _split_owner_due(task)
                task, assigned_to, due = parsed["task"], parsed["assigned_to"], parsed["due"]
            action_items.append({
                "task": task,
                "assigned_to": assigned_to,
                "assigned_by": item.get("assigned_by", "") or "",
                "due": due,
            })
        elif isinstance(item, str) and item.strip():
            # Legacy/fallback pipeline can emit plain strings. Keep the shape stable.
            parsed = _split_owner_due(item.strip())
            action_items.append({**parsed, "assigned_by": ""})

    return {
        # ── the contract ──
        # `title` is llama-service's header.topic — the 3–6 word plain-language meeting
        # title its prompt already produces. Renamed here to match this API's contract
        # rather than adding a second title-generation step.
        "title": header.get("topic", "") or "",
        "summary": content.get("summary", "") or "",
        "key_points": _key_points(content),
        "decisions": [d for d in _as_list(content.get("decisions")) if isinstance(d, str) and d.strip()],
        "action_items": action_items,
        # ── extras, already generated ──
        "agenda": [a for a in _as_list(content.get("agenda")) if isinstance(a, str) and a.strip()],
        "attendees": _as_list(content.get("attendees")),
        # Quantities the model extracted in its own dedicated pass. Measured the most accurate
        # field in the document (12/12 correct on a 29-minute meeting where the narrative
        # sections dropped whole topics), and until now it reached callers only baked into the
        # `formatted` text — unreadable to anything but a human.
        "key_figures": [f for f in _as_list(content.get("key_figures")) if isinstance(f, str) and f.strip()],
        "purpose": content.get("purpose", "") or "",
        # The fully rendered, human-readable minutes document (headings, bullets, footer).
        # This is what the MoM product actually showed users; keeping it means a caller can
        # display finished minutes without re-assembling them from the fields above.
        "formatted": result.get("analysis", "") or "",
    }
