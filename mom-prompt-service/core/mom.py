"""The user's prompt and documents → structured Minutes of Meeting, via llama-service's /summarize.

No prompt of our own. /summarize already writes the minutes as the structured `content` object the
JSSD renderer reads, with the validate-and-repair retry and the passes that re-ask the model when
decisions or action items come back empty. It was built for meeting transcripts; job.compose_source
labels the user's request and each document so the model reads them as a request and as source
material rather than as something said in a meeting. Whether that is good enough is not yet measured,
see CLAUDE.md.

The mapping below (to_mom_response) is copied unchanged from ../api/core/mom.py, so minutes from a
prompt and minutes from a recording reach the renderer and Elasticsearch in exactly the same shape.
"""
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from config import LLAMA_URL, MOM_TEMPERATURE, MOM_TIMEOUT

logger = logging.getLogger(__name__)


class MomGenerator:
    """llama-service's /summarize, and a cheap liveness check."""

    def __init__(self, base_url: str = LLAMA_URL):
        self.base_url = base_url
        self.session = requests.Session()

    def is_ready(self) -> bool:
        """'/' is static; llama-service's /health calls the model and is too slow to probe per request."""
        try:
            return self.session.get(f"{self.base_url}/", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    def generate(self, text: str, metadata: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """The full /summarize payload: `content` (structured) and `analysis` (rendered text).

        `metadata` is {date, time, venue} when the job states them; llama-service then uses those
        rather than whatever the text suggests. English only, as in the audio service.
        """
        payload: Dict[str, Any] = {"text": text, "temperature": MOM_TEMPERATURE, "output_lang": "English"}
        if metadata:
            payload["metadata"] = metadata
        logger.info(f"[MOM] POST {self.base_url}/summarize ({len(text)} chars)")
        try:
            r = self.session.post(f"{self.base_url}/summarize", json=payload, timeout=MOM_TIMEOUT)
        except requests.RequestException as e:
            raise RuntimeError(f"Cannot reach llama-service at {self.base_url}: {e}") from e
        if r.status_code != 200:
            raise RuntimeError(f"llama-service error {r.status_code}: {r.text[:300]}")
        result = r.json()
        logger.info(f"[MOM] ✓ generated (chunks={result.get('chunks_used')})")
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

    # DEGRADED MODE. llama-service's legacy fallback pipeline (and any future path that loses
    # `content`) returns only rendered text — no structured object. Everything downstream reads
    # the structured fields ONLY: build_mom_docx writes its sections from them, and index_mom
    # indexes them. So an empty `content` used to produce a .docx in MinIO that was a shell of
    # empty headings and an Elasticsearch document with nothing in it — while the Kafka ack still
    # said SUCCESS. A silent empty deliverable is worse than a visible failure.
    #
    # The rendered minutes are right there in `analysis`, so when there is no structure to report,
    # the prose becomes the summary. Nothing is invented: decisions and action items stay empty
    # because none were extracted, which is the truth.
    formatted = result.get("analysis", "") or ""
    summary = content.get("summary", "") or ""
    if formatted.strip() and not summary.strip() and not any(
        content.get(k) for k in ("key_points", "decisions", "action_items",
                                 "agenda", "key_figures", "speaker_notes")):
        logger.warning("[MOM] no structured content — falling back to the rendered document "
                       f"as the summary ({len(formatted)} chars); decisions/action items are empty")
        summary = formatted

    return {
        # ── the contract ──
        # `title` is llama-service's header.topic — the 3–6 word plain-language meeting
        # title its prompt already produces. Renamed here to match this API's contract
        # rather than adding a second title-generation step.
        "title": header.get("topic", "") or "",
        "summary": summary,
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
        # Only when the meeting itself states them; the prompt leaves them empty otherwise. The Word
        # export needs them because a JSSD minutes title must give the date, time and place.
        "meeting_date": header.get("meeting_date", "") or "",
        "meeting_time": header.get("meeting_time", "") or "",
        "venue": header.get("venue", "") or "",
        # The fully rendered, human-readable minutes document (headings, bullets, footer).
        # This is what the MoM product actually showed users; keeping it means a caller can
        # display finished minutes without re-assembling them from the fields above.
        "formatted": formatted,
    }
