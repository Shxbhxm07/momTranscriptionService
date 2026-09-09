"""Speaker diarization via the local NeMo service — who spoke when.

Reuses ../nemo-service unmodified (NeMo TitaNet). It is called with identify=false, which
matters: identify=true matches each voice cluster against ENROLLED speaker profiles in a
Qdrant vector database, and speaker enrollment is exactly the meeting-management feature
this service was asked to drop. With it off, no Qdrant is needed and no voice biometrics
are stored — clusters stay anonymous (Speaker_1, Speaker_2, …).

WHY THIS EXISTS AT ALL. An earlier revision shipped without diarization on the grounds
that the LLM could resolve speakers from self-introductions. That held for a scripted
standup where everyone introduces themselves and turns are clean. It did NOT hold on a
real 25-minute two-person podcast: the model invented a third participant, left the
attendee list empty, and swapped two speakers' daily routines. Names spoken once at
minute one do not survive twenty-four minutes of rapid back-and-forth. Diarization gives
the model a per-turn signal it cannot get from the words alone.
"""
import logging
import os
from typing import Any, Dict, List

import requests

from config import DIARIZE_TIMEOUT, NEMO_URL

logger = logging.getLogger(__name__)


class Diarizer:
    """Thin client for nemo-service. Stateless — TitaNet lives in that container."""

    def __init__(self, base_url: str = NEMO_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def is_ready(self) -> bool:
        """nemo_loaded flips true only once TitaNet is actually resident."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and bool(r.json().get("nemo_loaded"))
        except (requests.RequestException, ValueError):
            return False

    def diarize(self, audio_path: str) -> List[Dict[str, Any]]:
        """Return [{speaker, start, end}, ...], or [] if diarization is unavailable.

        Returning [] rather than raising is deliberate: diarization IMPROVES the MoM but
        is not required to produce one. A diarizer that is down should cost speaker
        attribution, not the whole request — the caller falls back to a plain transcript.
        """
        with open(audio_path, "rb") as f:
            audio = f.read()

        try:
            r = self.session.post(
                f"{self.base_url}/diarize",
                files={"file": (os.path.basename(audio_path), audio, "audio/wav")},
                # identify=false → no enrolled-profile lookup, so no Qdrant dependency.
                # num_speakers is NOT sent: NeMo's NME-SC estimates the count itself, and
                # forcing a wrong number is worse than letting it decide.
                params={"identify": "false"},
                timeout=DIARIZE_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning(f"[DIARIZE] nemo-service unreachable ({e}) — continuing without speakers")
            return []

        if r.status_code != 200:
            logger.warning(f"[DIARIZE] nemo-service error {r.status_code}: {r.text[:200]}")
            return []

        data = r.json()
        segments = data.get("segments", []) or []
        logger.info(f"[DIARIZE] ✓ {data.get('num_speakers', '?')} speakers, {len(segments)} turns")
        return segments
