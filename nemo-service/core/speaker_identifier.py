"""
Speaker Identifier
Uses NeMo TitaNet to extract d-vectors and match them against enrolled speakers.
"""

import logging
import os
import tempfile
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Single source of truth for the voice-match threshold lives in speaker_db.THRESHOLD, so the
# enrollment "passed" flag and live cluster-matching use the SAME cutoff (previously split 0.60
# here vs 0.75 in speaker_db). 0.60 is practical for varying mic/acoustics; Phase-3 calibration /
# AS-Norm will later replace this fixed value. Tune higher if you get false positives.
from core.speaker_db import THRESHOLD as DEFAULT_THRESHOLD


class SpeakerIdentifier:
    """
    Wraps NeMo EncDecSpeakerLabelModel (TitaNet-Large) for:
      1. Enrollment  — extract a d-vector from an audio sample
      2. Cluster ID  — match per-cluster d-vectors against the enrolled DB
    """

    def __init__(self, model_path: str = "titanet_large"):
        self.model_path = model_path
        self._model = None  # lazy-loaded on first use

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_loaded(self):
        if self._model is not None:
            return
        logger.info("Loading TitaNet-Large for speaker identification…")
        try:
            import torch
            from nemo.collections.asr.models import EncDecSpeakerLabelModel

            self._model = EncDecSpeakerLabelModel.from_pretrained(self.model_path)
            # GB10 (sm_121, CUDA 13, NeMo 26.04 container): use GPU — CPU diarization on this chip
            # is ~2 hrs/24min (unusable), GPU ~60s. The old .cpu() pin was for Pascal sm_61 which
            # that NeMo build didn't support; no longer applies. Falls back to CPU if CUDA is down.
            # (If sm_121 throws an NVRTC/arch error, apply the Blackwell patch — see research notes.)
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = self._model.to(device)
            self._model.eval()
            logger.info(f"✓ TitaNet-Large loaded ({device} mode)")

        except Exception as exc:
            logger.error("Failed to load TitaNet: %s", exc)
            raise

    def _extract_from_file(self, wav_path: str) -> np.ndarray:
        """Extract a normalised d-vector from a 16 kHz mono WAV file."""
        import torch

        self._ensure_loaded()
        emb = self._model.get_embedding(wav_path)
        if isinstance(emb, torch.Tensor):
            emb = emb.detach().cpu().numpy()
        emb = np.array(emb).flatten()
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm
        return emb

    @staticmethod
    def _write_temp_wav(audio_data: np.ndarray, sr: int) -> str:
        """Write a numpy audio array to a temp WAV file; caller must delete it."""
        import soundfile as sf

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        sf.write(tmp.name, audio_data, sr)
        return tmp.name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_enrollment_embedding(self, audio_path: str) -> np.ndarray:
        """
        Extract a d-vector from an enrollment audio file.
        The file may be any format supported by ffmpeg; the model expects
        16 kHz mono, but NeMo's get_embedding handles resampling internally.
        """
        return self._extract_from_file(audio_path)

    def extract_cluster_embeddings(
        self,
        audio_path: str,
        segments: List[dict],
    ) -> Dict[str, np.ndarray]:
        """
        For each unique speaker label in `segments`, collect the corresponding
        audio chunks, extract a d-vector per chunk (≥ 0.5 s), and return the
        L2-normalised average as the cluster embedding.

        Args:
            audio_path: path to the 16 kHz mono WAV used for diarization
            segments:   list of {"speaker", "start", "end", "duration"} dicts

        Returns:
            {speaker_label: embedding_array}
        """
        import soundfile as sf

        self._ensure_loaded()

        audio_data, sr = sf.read(audio_path, dtype="float32")
        if audio_data.ndim > 1:
            audio_data = audio_data[:, 0]

        # Group by speaker
        by_speaker: Dict[str, List[dict]] = defaultdict(list)
        for seg in segments:
            by_speaker[seg["speaker"]].append(seg)

        cluster_embeddings: Dict[str, np.ndarray] = {}

        for speaker, segs in by_speaker.items():
            embeddings = []
            for seg in segs:
                start = int(seg["start"] * sr)
                end = int(seg["end"] * sr)
                chunk = audio_data[start:end]

                # Need at least 0.5 s for a reliable embedding
                if len(chunk) < sr * 0.5:
                    continue

                tmp_path = self._write_temp_wav(chunk, sr)
                try:
                    emb = self._extract_from_file(tmp_path)
                    embeddings.append(emb)
                except Exception as exc:
                    logger.warning("Skipping chunk for %s: %s", speaker, exc)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            if embeddings:
                avg = np.mean(embeddings, axis=0)
                norm = np.linalg.norm(avg)
                if norm > 0:
                    avg /= norm
                cluster_embeddings[speaker] = avg
                logger.debug(
                    "Cluster %s: averaged %d chunk embeddings", speaker, len(embeddings)
                )
            else:
                logger.warning("No usable chunks for speaker %s", speaker)

        return cluster_embeddings

    def merge_oversplit_clusters(
        self,
        segments: List[dict],
        cluster_embeddings: Dict[str, np.ndarray],
        threshold: float = 0.92,
    ):
        """Merge diarization clusters whose voiceprints are near-identical (the SAME person split in two).

        NeMo's clustering uses max_rp_threshold=0.45 (raised so large meetings don't collapse),
        which can over-split — one speaker coming out as two clusters. Two clusters with cosine
        >= threshold are treated as the same person and merged.

        The bar is HIGH (0.92) on purpose: merging two DIFFERENT people is the worst possible error
        (a real speaker vanishes), and colleagues with similar voices score higher than you'd think —
        measured 0.836 between two genuinely different speakers on a real meeting, which 0.80 wrongly
        merged. Same-person splits sit at ~0.92+, so 0.92 keeps true splits merging while never
        collapsing similar-but-different voices. Noise/phantom clusters are handled separately by
        drop_noise_clusters(). Embeddings are L2-normalised, so dot product == cosine.

        Returns (relabelled_segments, merged_cluster_embeddings).
        """
        labels = list(cluster_embeddings.keys())
        if len(labels) < 2:
            return segments, cluster_embeddings

        parent = {l: l for l in labels}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        merges = []
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                a, b = labels[i], labels[j]
                cos = float(np.dot(cluster_embeddings[a], cluster_embeddings[b]))
                if cos >= threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
                    merges.append((a, b, round(cos, 3)))

        remap = {l: find(l) for l in labels}
        if all(remap[l] == l for l in labels):
            return segments, cluster_embeddings  # nothing to merge

        logger.info("Merged over-split clusters (cos>=%.2f): %s | remap=%s", threshold, merges, remap)

        new_segments = []
        for seg in segments:
            seg = dict(seg)
            seg["speaker"] = remap[seg["speaker"]]
            new_segments.append(seg)

        # Re-average the merged clusters' embeddings and re-normalise, so identification uses the
        # combined voiceprint (the whole person, not one fragment).
        groups: Dict[str, list] = defaultdict(list)
        for l in labels:
            groups[remap[l]].append(cluster_embeddings[l])
        merged_embs: Dict[str, np.ndarray] = {}
        for root, embs in groups.items():
            avg = np.mean(embs, axis=0)
            n = np.linalg.norm(avg)
            merged_embs[root] = avg / n if n > 0 else avg

        return new_segments, merged_embs

    def drop_noise_clusters(
        self,
        segments: List[dict],
        cluster_embeddings: Dict[str, np.ndarray],
        noise_cos: float = 0.15,
        noise_support: float = 0.30,
        weak_cos: float = 0.35,
        weak_support: float = 0.10,
    ):
        """Remove phantom/noise clusters NeMo over-split off (the fake extra speaker).

        Real speakers' voiceprints are SIMILAR to each other (TitaNet cosine ~0.2-0.6 — they are all
        human voices). A noise/artifact cluster is near-ORTHOGONAL (cosine ~0) to every other cluster.
        So a cluster whose MAX cosine to any other cluster is < min_neighbor_cos is not a person; its
        (usually short) segments are noise/crosstalk and get reassigned to the nearest real speaker by
        time. This signal is COUNT-INDEPENDENT — it works whether the meeting has 2 or 8 people, so it
        never needs per-meeting tuning.

        Measured on a real 2-person meeting NeMo split into 3: the two real speakers scored 0.49 to
        each other; the phantom scored 0.03 / 0.04 to both. A 0.15 bar cleanly separates them and never
        touches a genuine voice. The support cap (skip clusters owning > max_support_fraction of the
        audio) is an extra guard so a dominant cluster is never dropped on a fluke embedding.

        Returns (relabelled_segments, surviving_cluster_embeddings).
        """
        labels = list(cluster_embeddings.keys())
        if len(labels) < 2:
            return segments, cluster_embeddings

        dur: Dict[str, float] = defaultdict(float)
        for s in segments:
            dur[s["speaker"]] += float(s.get("duration", s.get("end", 0) - s.get("start", 0)))
        total = sum(dur.values()) or 1.0

        def max_neighbor_cos(l):
            return max(
                (float(np.dot(cluster_embeddings[l], cluster_embeddings[o])) for o in labels if o != l),
                default=1.0,
            )

        # Two tiers, both count-independent (no per-meeting tuning):
        #   (1) pure noise    — voiceprint orthogonal to EVERY other cluster (cos < noise_cos ~0.15)
        #   (2) weak minority — a SMALL cluster (< weak_support of talk-time) whose BEST match to any
        #       cluster is still below weak_cos (~0.35): it never coheres with the real voices the way
        #       real speakers cohere with each other. Measured: real speakers' best match >= 0.49;
        #       the phantoms we saw peaked at 0.04 (pure noise) and 0.28 (a fragment of another voice).
        def is_phantom(l):
            mc, frac = max_neighbor_cos(l), dur[l] / total
            return (mc < noise_cos and frac < noise_support) or (mc < weak_cos and frac < weak_support)

        noise = {l for l in labels if is_phantom(l)}
        real = [l for l in labels if l not in noise]
        if not noise or not real:
            return segments, cluster_embeddings

        logger.info(
            "Dropping noise/phantom cluster(s): %s",
            {l: {"max_cos": round(max_neighbor_cos(l), 3), "support": round(dur[l] / total, 3)} for l in noise},
        )

        real_segs = [s for s in segments if s["speaker"] in real]
        new_segments = []
        for seg in segments:
            if seg["speaker"] in noise:
                seg = dict(seg)
                seg["speaker"] = self._nearest_by_time(seg, real_segs)
            new_segments.append(seg)

        surviving = {l: cluster_embeddings[l] for l in real}
        return new_segments, surviving

    def calibrate_clusters(self, segments: List[dict], cluster_embeddings: Dict[str, np.ndarray]):
        """Self-calibrating replacement for the fixed-threshold drop_noise_clusters + the (unwired)
        0.92 merge_oversplit_clusters. Decides which clusters to MERGE (over-split repair) and DROP
        (noise) from THIS meeting's OWN cosine distribution — see core.relative_thresholds — so there
        is no per-meeting constant to tune. Biased toward over-splitting (never lose a real speaker).

        Returns (segments, cluster_embeddings, diagnostics).
        """
        from core.relative_thresholds import pairwise_cosine, decide, apply_decision

        labels = list(cluster_embeddings.keys())
        if len(labels) < 2:
            return segments, cluster_embeddings, {"n": len(labels), "note": "single cluster — no-op"}

        # Talk-time fraction per cluster, so drop's support guard never removes a dominant speaker.
        dur: Dict[str, float] = defaultdict(float)
        for s in segments:
            dur[s["speaker"]] += float(s.get("duration", s.get("end", 0) - s.get("start", 0)))
        total = sum(dur.values()) or 1.0
        support = {l: dur[l] / total for l in labels}

        lbls, M = pairwise_cosine(cluster_embeddings)
        decision = decide(lbls, M, support)
        segments, cluster_embeddings = apply_decision(segments, cluster_embeddings, decision)

        if decision.merge_groups or decision.drop:
            logger.info(
                "Self-calibrating clusters: merges=%s drops=%s",
                [sorted(g) for g in decision.merge_groups], sorted(decision.drop),
            )
        logger.debug("Self-calibration diagnostics: %s", decision.diagnostics)
        return segments, cluster_embeddings, decision.diagnostics

    @staticmethod
    def _nearest_by_time(seg: dict, candidates: List[dict]) -> str:
        """Reassign a noise segment to the nearest real-speaker segment: max time overlap, else
        nearest midpoint."""
        if not candidates:
            return seg["speaker"]
        best, best_ov = None, 0.0
        for c in candidates:
            ov = max(0.0, min(seg["end"], c["end"]) - max(seg["start"], c["start"]))
            if ov > best_ov:
                best_ov, best = ov, c["speaker"]
        if best is not None:
            return best
        mid = (seg["start"] + seg["end"]) / 2
        return min(candidates, key=lambda c: abs((c["start"] + c["end"]) / 2 - mid))["speaker"]

    def identify_speakers_with_qdrant(
        self,
        cluster_embeddings: Dict[str, np.ndarray],
        threshold: float = DEFAULT_THRESHOLD,
    ) -> Dict[str, str]:
        """
        Match cluster embeddings against enrolled speakers using Qdrant vector search.

        For each cluster, queries Qdrant for the top-N most similar stored embeddings, groups by
        speaker name, and takes the MAX score across that name's samples (embedding bank: the best
        matching sample wins, not the average).

        ONE-TO-MANY assignment: each cluster takes its single best-matching enrolled name (if the
        score clears `threshold`), and a name MAY be assigned to SEVERAL clusters. This is
        deliberate — NeMo routinely over-splits one real person into multiple clusters, and every
        one of those clusters should carry that person's name instead of leaking out as speaker_N.
        A cluster still gets at most ONE name (its best). Because each cluster is matched against the
        ENROLLED voiceprint (not against other clusters), a cluster only joins a name when it truly
        resembles that person's enrolled voice — safer than the cluster-to-cluster merge we removed.

        Args:
            cluster_embeddings: output of extract_cluster_embeddings()
            threshold:          minimum cosine similarity to accept a match

        Returns:
            {cluster_label: display_name} — only matched clusters included
        """
        from core.speaker_db import search_similar_speakers

        if not cluster_embeddings:
            return {}

        speaker_map: Dict[str, str] = {}
        for cluster_id, c_emb in cluster_embeddings.items():
            results = search_similar_speakers(c_emb, top_k=20)

            # Best score per enrolled name (across their stored samples), then the single best name.
            best_per_speaker: dict = {}
            for r in results:
                name = r["name"]
                if name not in best_per_speaker or r["score"] > best_per_speaker[name]:
                    best_per_speaker[name] = r["score"]
            if not best_per_speaker:
                continue

            name, score = max(best_per_speaker.items(), key=lambda kv: kv[1])
            if score >= threshold:
                speaker_map[cluster_id] = name
                logger.info("Identified: %s → '%s'  (cosine=%.3f)", cluster_id, name, score)
            else:
                logger.info(
                    "No match: %s best=%s score=%.3f < threshold=%.2f",
                    cluster_id, name, score, threshold,
                )

        return speaker_map

    def compare_audio_files(self, file_paths: list) -> float:
        """
        Extract embeddings for each file.
        Compare the LAST file against the L2-normalised average of all previous files.
        Returns cosine similarity in [0, 1].

        Use cases:
          - 2 files: compare sample2 vs sample1
          - 3 files: compare sample3 vs avg(sample1, sample2)
        """
        import numpy as np

        if len(file_paths) < 2:
            raise ValueError("Need at least 2 audio files to compare")

        self._ensure_loaded()

        embeddings = [self._extract_from_file(p) for p in file_paths]

        # Average of all files EXCEPT the last one
        ref = np.mean(embeddings[:-1], axis=0)
        norm = np.linalg.norm(ref)
        if norm > 0:
            ref = ref / norm

        target = embeddings[-1]  # already normalised by _extract_from_file

        score = float(np.dot(ref, target))
        return max(0.0, min(1.0, score))

    def identify_speakers(
        self,
        cluster_embeddings: Dict[str, np.ndarray],
        enrolled_speakers: List[dict],
        threshold: float = DEFAULT_THRESHOLD,
    ) -> Dict[str, str]:
        """
        Greedy best-match assignment: each enrolled speaker may be assigned to
        at most one cluster; each cluster gets at most one name.

        Args:
            cluster_embeddings: output of extract_cluster_embeddings()
            enrolled_speakers:  list of {"id", "name", "embedding"} from DB
            threshold:          minimum cosine similarity to accept a match

        Returns:
            {cluster_label: display_name}  — only matched clusters are included
        """
        if not enrolled_speakers or not cluster_embeddings:
            return {}

        # Build score matrix
        candidates = []
        for cluster_id, c_emb in cluster_embeddings.items():
            for sp in enrolled_speakers:
                score = float(np.dot(c_emb, sp["embedding"]))
                candidates.append((score, cluster_id, sp["name"]))

        # Sort best-first, then greedily assign
        candidates.sort(key=lambda x: x[0], reverse=True)

        used_clusters: set = set()
        used_names: set = set()
        speaker_map: Dict[str, str] = {}

        for score, cluster_id, name in candidates:
            if cluster_id in used_clusters or name in used_names:
                continue
            if score >= threshold:
                speaker_map[cluster_id] = name
                used_clusters.add(cluster_id)
                used_names.add(name)
                logger.info(
                    "Identified: %s → '%s'  (cosine=%.3f)", cluster_id, name, score
                )
            else:
                logger.info(
                    "No match: %s best=%s score=%.3f < threshold=%.2f",
                    cluster_id, name, score, threshold,
                )

        return speaker_map
