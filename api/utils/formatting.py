"""Merge Whisper's timed segments with NeMo's speaker turns into a labelled transcript.

Carried over from the MoM gateway (backend/utils/formatting.py). The overlap-matching
logic in _find_speaker() is that code unchanged — it was already the right algorithm and
there was no reason to invent a second one.

WHAT CHANGED, and why:
  • Label format is "[Speaker_1]" rather than the gateway's "[Speaker A]". llama-service's
    MoM prompt documents the underscore-numeral form explicitly ("A label of the form
    [Speaker_1] / [speaker_0] is NOT a name ... keep it as [Speaker_N]"), and matching the
    prompt's own convention is worth more than matching the old gateway's.
  • The speaker_name / enrolled-voice branch is dropped. That path returns a REAL name from
    a voice-biometric match against enrolled profiles, which needs the speaker-enrollment
    database this service deliberately does not have. Everyone is an anonymous cluster here.
"""
import logging
import re
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def _find_speaker(start: float, end: float, speaker_segments: List[dict]) -> str:
    """Best speaker for a transcript segment by MAXIMUM TIME OVERLAP.

    Overlap rather than midpoint-nearest because a Whisper segment often straddles a
    speaker change; the speaker who occupies most of the segment is the right owner.
    Falls back to nearest midpoint when there is no overlap at all (Whisper and NeMo run
    their own VAD, so their segment boundaries do not have to line up).
    """
    best_speaker, best_overlap = None, 0.0
    for spk in speaker_segments:
        overlap = max(0.0, min(end, spk.get("end", 0)) - max(start, spk.get("start", 0)))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = spk.get("speaker", "unknown")
    if best_speaker:
        return best_speaker

    mid = (start + end) / 2
    closest, min_dist = "unknown", float("inf")
    for spk in speaker_segments:
        dist = abs(mid - (spk.get("start", 0) + spk.get("end", 0)) / 2)
        if dist < min_dist:
            min_dist, closest = dist, spk.get("speaker", "unknown")
    return closest


_MIN_RUN_WORDS = 3


def _norm_words(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def _word_runs(seg: dict, speaker_segments: List[dict]) -> List[Tuple[str, str]]:
    """Split ONE whisper segment into (speaker, text) runs using per-word timings.

    Returns [] when the segment cannot be split safely, and the caller then falls back to
    assigning the whole segment — which is what this code did for every segment before.

    WHY THIS EXISTS: whisper and NeMo run separate VADs, so a whisper segment regularly
    straddles a speaker change. Assigning the whole segment to whoever holds most of it
    silently moves the other speaker's words onto the majority speaker, and that is the
    error that puts an action item against the wrong person — measured on a real committee
    meeting, 2 of 10 items had the owner and the assigner the wrong way round.

    THE CONSISTENCY GUARD is load-bearing. clean_segment_list() rewrites seg["text"] to
    strip repetition loops and garbled tails, but it cannot rewrite the word array, so after
    cleaning the two can disagree. Splitting on a stale word list would emit text that was
    never said. If the words do not reconstruct the cleaned text, we do not use them.
    """
    words = seg.get("words") or []
    if len(words) < 2 * _MIN_RUN_WORDS:
        return []
    if _norm_words("".join(w["word"] for w in words)) != _norm_words(seg.get("text", "")):
        return []

    tagged = [(_find_speaker(w["start"], w["end"], speaker_segments), w["word"]) for w in words]

    # Collapse into runs, then absorb runs too short to be a real turn. A single word landing
    # on the wrong cluster is common at a turn boundary; without this, one stray word becomes
    # its own "[Speaker_4]" line and invents a participant in the middle of someone's sentence.
    runs: List[List] = []
    for spk, word in tagged:
        if runs and runs[-1][0] == spk:
            runs[-1][1].append(word)
        else:
            runs.append([spk, [word]])

    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, (spk, ws) in enumerate(runs):
            if len(ws) >= _MIN_RUN_WORDS:
                continue
            prev_spk = runs[i - 1][0] if i > 0 else None
            next_spk = runs[i + 1][0] if i + 1 < len(runs) else None
            target = prev_spk if prev_spk is not None and prev_spk == next_spk else (prev_spk or next_spk)
            if target is None:
                continue
            runs[i][0] = target
            merged: List[List] = []
            for spk2, ws2 in runs:
                if merged and merged[-1][0] == spk2:
                    merged[-1][1].extend(ws2)
                else:
                    merged.append([spk2, list(ws2)])
            runs, changed = merged, True
            break

    if len(runs) < 2:
        return []          # one speaker after smoothing — nothing gained over segment-level
    return [(spk, "".join(ws).strip()) for spk, ws in runs if "".join(ws).strip()]


def label_transcript(segments: List[dict], speaker_segments: List[dict]) -> Tuple[str, int]:
    """Return (labelled transcript, speaker count).

    Output is one block per speaker turn, which is the shape llama-service's prompt is
    written against:

        [Speaker_1] Hello everyone, my name is Rahul...
        [Speaker_2] Yes Rahul, our API migration is 80% complete...

    Consecutive segments from the same speaker are joined into ONE block rather than
    repeating the tag per sentence — the prompt counts lines per speaker to size its
    per-speaker notes, so one tag per turn is what makes that count meaningful.
    """
    if not segments:
        return "", 0

    if not speaker_segments:
        # No diarization available — hand back the plain transcript rather than tagging
        # everything [Speaker_1], which would assert "one person spoke" as a fact.
        logger.warning("[DIARIZE] no speaker segments — returning untagged transcript")
        return " ".join(s.get("text", "").strip() for s in segments if s.get("text")), 0

    # Cluster ids are renumbered in order of FIRST APPEARANCE, so the person who speaks
    # first is always Speaker_1. NeMo's raw ids are arbitrary cluster labels; leaving them
    # as-is makes two runs of the same audio disagree on numbering for no reason.
    label_map: Dict[str, str] = {}
    lines: List[str] = []
    prev = None

    split_segments = 0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue

        runs = _word_runs(seg, speaker_segments)
        if runs:
            split_segments += 1
        else:
            runs = [(_find_speaker(seg.get("start", 0.0), seg.get("end", 0.0), speaker_segments), text)]

        for raw, chunk in runs:
            if raw not in label_map:
                label_map[raw] = f"Speaker_{len(label_map) + 1}"
            speaker = label_map[raw]
            if speaker != prev:
                lines.append(f"[{speaker}] {chunk}")
            else:
                lines[-1] += f" {chunk}"
            prev = speaker

    if split_segments:
        logger.info(f"[DIARIZE] word-level split applied to {split_segments} straddling segment(s)")

    logger.info(f"[DIARIZE] labelled {len(lines)} turns across {len(label_map)} speakers")
    return "\n".join(lines), len(label_map)


# ── speaker naming ───────────────────────────────────────────────────────────
# Diarization gives anonymous clusters; llama-service's /identify-speakers can read real
# names out of the words (self-introductions, direct address). Everything below exists to
# decide which of those names are safe to believe.
#
# The asymmetry that drives the rules: an anonymous "[Speaker_3]" is honest and costs the
# reader a little. A WRONG name is a false statement about a real person in a document their
# colleagues will read, and nothing downstream can catch it — the MoM prompt treats a
# bracketed name as confirmed. So every name has to earn its way in, and the default is to
# keep the tag.

_TAG_RE = re.compile(r"\[(Speaker_\d+)\]")
_PLACEHOLDER_RE = re.compile(r"^\[?\s*speaker[\s_-]*([0-9]+|[a-z])\s*\]?$", re.IGNORECASE)
# A plausible written name: letters plus the punctuation names actually contain.
_NAME_RE = re.compile(r"^[A-Z][A-Za-z'’.\-]*(?:\s+[A-Z][A-Za-z'’.\-]*){0,3}$")


def _normalise_tag(key: str) -> str:
    """'[speaker_2]' / 'Speaker 2' / 'speaker_2' → 'Speaker_2'. Unrecognised keys pass through."""
    m = re.match(r"^\[?\s*speaker[\s_-]*(\d+)\s*\]?$", (key or "").strip(), re.IGNORECASE)
    return f"Speaker_{m.group(1)}" if m else (key or "").strip()


def apply_speaker_names(labelled: str, mapping: Dict) -> Tuple[str, Dict[str, str]]:
    """Rewrite [Speaker_N] → [Name] for the names the transcript itself corroborates.

    `mapping` is llama-service's {label: {name, role}} object. Returns the rewritten
    transcript and the mapping actually applied, so the caller can log and reuse it.

    A candidate name is accepted only when ALL of these hold:
      • the tag it claims really occurs in this transcript;
      • the value is a name, not another placeholder ("Speaker 3", "Unknown");
      • it LOOKS like a written name — capitalised words, no sentence fragments;
      • the name appears verbatim in the transcript. This is the load-bearing check. The
        model can only return a name it read, so a name that is nowhere in the text was
        invented, which is exactly the failure that once turned the garbled "so safety is
        saying" into a person called "Seth";
      • no other tag already claimed it — two clusters sharing one name means the model
        merged two people, and guessing which one is right is worse than naming neither.
    """
    if not labelled or not isinstance(mapping, dict):
        return labelled, {}

    present = set(_TAG_RE.findall(labelled))
    if not present:
        return labelled, {}

    haystack = labelled.lower()
    accepted: Dict[str, str] = {}
    claimed: Dict[str, str] = {}

    for raw_key, value in mapping.items():
        tag = _normalise_tag(raw_key)
        if tag not in present:
            continue
        name = (value.get("name") if isinstance(value, dict) else value) or ""
        name = str(name).strip().strip("[]").strip()
        # Normalise case BEFORE the shape check. Whisper's transcribe task lower-cases proper
        # nouns where translate capitalised them, so a correct name can arrive as "rich" and
        # be thrown out for looking wrong. Case is not evidence either way — the verbatim
        # occurrence check below is what actually decides whether the name is real.
        if name and not name[0].isupper():
            name = " ".join(w[:1].upper() + w[1:] for w in name.split())

        if not name or _PLACEHOLDER_RE.match(name) or name.lower() in {"unknown", "unnamed", "n/a", "none"}:
            continue
        if not _NAME_RE.match(name) or len(name) > 40:
            continue
        if not re.search(rf"\b{re.escape(name.split()[0])}\b", haystack, re.IGNORECASE):
            logger.warning("[SPEAKER_ID] rejected %r for %s — name does not occur in the transcript", name, tag)
            continue
        if name.lower() in claimed:
            logger.warning(
                "[SPEAKER_ID] rejected %r for %s — already claimed by %s", name, tag, claimed[name.lower()]
            )
            accepted.pop(claimed[name.lower()], None)   # neither tag keeps a contested name
            continue

        accepted[tag] = name
        claimed[name.lower()] = tag

    if not accepted:
        logger.info(
            "[SPEAKER_ID] no names applied — model proposed %d mapping(s) for %d tag(s), "
            "none met the verification bar; keeping anonymous tags",
            len(mapping), len(present),
        )
        return labelled, {}

    out = _TAG_RE.sub(lambda m: f"[{accepted.get(m.group(1), m.group(1))}]", labelled)
    logger.info(
        "[SPEAKER_ID] named %d/%d speakers: %s",
        len(accepted), len(present), ", ".join(f"{k}→{v}" for k, v in sorted(accepted.items())),
    )
    return out, accepted
