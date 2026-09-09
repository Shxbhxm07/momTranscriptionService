import os
import json
import logging
from collections import defaultdict
from omegaconf import OmegaConf

logger = logging.getLogger(__name__)

class DiarizerEngine:
    """Core engine for speaker diarization using NeMo"""

    def __init__(self, base_cfg: dict):
        self.base_cfg = base_cfg
        self.is_loaded = True # We consider it loaded if cfg is passed

    def generate_manifest(self, audio_path: str, manifest_path: str, num_speakers: int = None):
        """Create NeMo manifest file"""
        manifest = {
            "audio_filepath": audio_path,
            "offset": 0,
            "duration": None,
            "label": "infer",
            "text": "-",
            "num_speakers": num_speakers,
            "rttm_filepath": None,
            "uem_filepath": None
        }

        with open(manifest_path, 'w') as f:
            f.write(json.dumps(manifest) + '\n')

    def diarize(self, audio_path: str, temp_dir: str, num_speakers: int = None, diag: dict = None) -> list:
        """Run diarization pipeline.

        `diag` is the diagnostic-only sink (default None = production, no behavior
        change). When a dict is passed, this records the resolved NeMo cfg, the
        pre-merge segments, and a `decisions` list that the merge/cap helpers append
        to. None of this alters the returned segments or the clustering result.
        """
        # Import inside to prevent loading issues if NeMo isn't installed during simple test checks
        from nemo.collections.asr.models.clustering_diarizer import ClusteringDiarizer

        manifest_path = os.path.join(temp_dir, 'manifest.json')
        self.generate_manifest(audio_path, manifest_path, num_speakers)

        cfg = OmegaConf.create(self.base_cfg)
        cfg.diarizer.manifest_filepath = manifest_path
        cfg.diarizer.out_dir = temp_dir

        # When num_speakers is known, use oracle mode — NeMo will enforce exactly that count
        if num_speakers:
            cfg.diarizer.clustering.parameters.oracle_num_speakers = True
            logger.info(f"Oracle mode: using num_speakers={num_speakers}")
        else:
            cfg.diarizer.clustering.parameters.oracle_num_speakers = False

        if diag is not None:
            try:
                diag["cfg"] = OmegaConf.to_container(cfg, resolve=True)
            except Exception:
                diag["cfg"] = dict(self.base_cfg)

        logger.info("Running diarization...")
        diarizer = ClusteringDiarizer(cfg=cfg)
        diarizer.diarize()

        rttm_file = os.path.join(temp_dir, 'pred_rttms', 'audio.rttm')
        segments = self._parse_rttm(rttm_file)
        raw_speakers = set(s['speaker'] for s in segments)
        logger.info(f"RAW NeMo output: {len(raw_speakers)} speakers {raw_speakers}, {len(segments)} segments")

        # Capture the untouched RTTM output before any merge/cap (diagnostic only).
        decisions = None
        if diag is not None:
            diag["segments_raw"] = [dict(s) for s in segments]
            decisions = diag.setdefault("decisions", [])

        # Guard: the only mathematically guaranteed-wrong case is more speakers than segments,
        # since every speaker must have at least 1 segment. NeMo's titanet_large can reliably
        # embed a speaker from a single long segment (3s+), so requiring 2 segments/speaker
        # (the previous num_segments//2 formula) was too aggressive and silently dropped real
        # speakers in short meetings where each person spoke only once.
        if len(segments) > 0 and len(raw_speakers) > len(segments):
            logger.warning(
                f"Impossible result: {len(raw_speakers)} speakers from {len(segments)} segments. "
                f"Capping at {len(segments)} speakers."
            )
            segments = self._cap_speakers(segments, len(segments), decisions=decisions)

        segments = self._merge_phantom_speakers(segments, decisions=decisions)
        return segments

    def _merge_phantom_speakers(self, segments: list, decisions: list = None) -> list:
        """
        Merge phantom speakers into real ones.

        A speaker is considered phantom if ALL three hold simultaneously:
          - total speaking duration < 4.0 seconds (absolute), AND
          - they appear in 2 or fewer segments, AND
          - average segment length < 2.0 seconds

        This targets acoustic boundary noise / transition artifacts only.
        Percentage-based checks (is_tiny_fraction) are intentionally excluded
        to avoid merging real quiet speakers who speak briefly but genuinely.

        Phantom segments are reassigned to the main speaker with the
        maximum time overlap, falling back to nearest midpoint.
        """
        if not segments:
            return segments

        speaker_duration = defaultdict(float)
        speaker_count = defaultdict(int)
        for seg in segments:
            spk = seg['speaker']
            speaker_duration[spk] += seg['duration']
            speaker_count[spk] += 1

        total_audio_duration = sum(speaker_duration.values())
        all_speakers = set(speaker_duration.keys())

        # A speaker is phantom if BOTH of these hold:
        #   1. They account for < 2% of the total audio duration
        #      (lowered from 5% to preserve quiet real speakers —
        #       e.g. in a 10-min meeting, 2% = ~12s which is enough for
        #       "haan", "agree", "theek hai" responses from a quiet participant)
        #   2. Their average segment length is < 2s (short fragmented bursts)
        # This catches acoustic boundary noise / transition artifacts while
        # preserving real minority speakers even if they speak softly or briefly.
        phantom_speakers = set()
        for spk in all_speakers:
            avg_seg = speaker_duration[spk] / speaker_count[spk] if speaker_count[spk] > 0 else 0
            # Phantom only if ALL THREE hold simultaneously:
            #   - total speaking time < 4s (absolute noise, not a real turn)
            #   - appears in ≤ 2 segments (not a sustained speaker)
            #   - avg segment < 2s (short fragmented bursts, not real speech)
            # This targets acoustic boundary noise / transition artifacts only.
            # is_tiny_fraction (pct < 0.05) was removed because it incorrectly
            # merged real quiet speakers who say only a few words (e.g. "haan",
            # "agree") — in a 10-min meeting 5% = 30s, easily under-threshold
            # for a genuine but brief participant.
            is_short_burst = speaker_duration[spk] < 4.0 and speaker_count[spk] <= 2 and avg_seg < 2.0
            if is_short_burst:
                phantom_speakers.add(spk)

        main_speakers = all_speakers - phantom_speakers

        # Diagnostic-only: record the per-speaker measurements + verdict that drove
        # the phantom rule. Purely additive — does not change the decision.
        if decisions is not None:
            for spk in sorted(all_speakers):
                avg_seg = speaker_duration[spk] / speaker_count[spk] if speaker_count[spk] > 0 else 0
                decisions.append({
                    "stage": "merge_phantom_eval",
                    "speaker": spk,
                    "total_duration": round(speaker_duration[spk], 3),
                    "segment_count": speaker_count[spk],
                    "avg_segment": round(avg_seg, 3),
                    "is_phantom": spk in phantom_speakers,
                    "rule": "phantom if total_dur<4.0 AND count<=2 AND avg_seg<2.0",
                })

        if not phantom_speakers:
            return segments

        if not main_speakers:
            # Edge case: all speakers are "phantom" — keep as-is
            if decisions is not None:
                decisions.append({"stage": "merge_phantom", "action": "kept_all",
                                  "reason": "all speakers classified phantom; nothing to merge into"})
            return segments

        logger.info(
            f"Phantom speakers detected: {phantom_speakers} "
            f"(duration: { {s: round(speaker_duration[s], 2) for s in phantom_speakers} }). "
            f"Merging into main speakers: {main_speakers}"
        )

        main_segs = [s for s in segments if s['speaker'] not in phantom_speakers]

        reassigned = defaultdict(lambda: defaultdict(int))  # phantom -> {target: count}
        result = []
        for seg in segments:
            if seg['speaker'] in phantom_speakers:
                src = seg['speaker']
                best_spk = self._nearest_main_speaker(seg, main_segs)
                seg = dict(seg)
                seg['speaker'] = best_spk
                reassigned[src][best_spk] += 1
                logger.debug(f"  Reassigned segment [{seg['start']:.1f}-{seg['end']:.1f}] → {best_spk}")
            result.append(seg)

        if decisions is not None:
            for src, targets in reassigned.items():
                decisions.append({
                    "stage": "merge_phantom",
                    "action": "merged",
                    "speaker": src,
                    "merged_into": dict(targets),
                    "reason": "short-burst phantom reassigned to nearest main speaker by overlap",
                })

        return result

    def _cap_speakers(self, segments: list, max_speakers: int, decisions: list = None) -> list:
        """
        When segment count is too low to trust clustering, keep only the top
        N speakers by total duration and reassign the rest to nearest kept speaker.
        """
        from collections import defaultdict
        speaker_duration = defaultdict(float)
        for seg in segments:
            speaker_duration[seg['speaker']] += seg['duration']

        # Keep top N speakers by duration
        ranked = sorted(speaker_duration.keys(), key=lambda s: speaker_duration[s], reverse=True)
        keep = set(ranked[:max_speakers])
        drop = set(ranked[max_speakers:])

        if not drop:
            return segments

        logger.info(f"Capping speakers: keeping {keep}, dropping {drop}")
        if decisions is not None:
            decisions.append({
                "stage": "cap_speakers",
                "action": "capped",
                "max_speakers": max_speakers,
                "kept": sorted(keep),
                "dropped": sorted(drop),
                "reason": "raw speaker count exceeded segment count (impossible result)",
            })
        keep_segs = [s for s in segments if s['speaker'] in keep]
        result = []
        for seg in segments:
            if seg['speaker'] in drop:
                best = self._nearest_main_speaker(seg, keep_segs)
                seg = dict(seg)
                seg['speaker'] = best
            result.append(seg)
        return result

    def _nearest_main_speaker(self, seg: dict, main_segs: list) -> str:
        """Return the main speaker with max time overlap, or nearest midpoint."""
        seg_start = seg['start']
        seg_end = seg['end']

        best_spk = None
        best_overlap = 0.0
        for ms in main_segs:
            overlap = max(0.0, min(seg_end, ms['end']) - max(seg_start, ms['start']))
            if overlap > best_overlap:
                best_overlap = overlap
                best_spk = ms['speaker']

        if best_spk:
            return best_spk

        # No overlap — use nearest midpoint
        seg_mid = (seg_start + seg_end) / 2
        min_dist = float('inf')
        closest_spk = main_segs[0]['speaker']
        for ms in main_segs:
            ms_mid = (ms['start'] + ms['end']) / 2
            dist = abs(seg_mid - ms_mid)
            if dist < min_dist:
                min_dist = dist
                closest_spk = ms['speaker']

        return closest_spk

    def _parse_rttm(self, rttm_file: str) -> list:
        """Parse RTTM output file"""
        speakers = []
        if os.path.exists(rttm_file):
            with open(rttm_file, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 8:
                        speakers.append({
                            "speaker": parts[7],
                            "start": float(parts[3]),
                            "duration": float(parts[4]),
                            "end": float(parts[3]) + float(parts[4])
                        })
        else:
            logger.error(f"RTTM file not found: {rttm_file}")
            raise RuntimeError("Diarization failed - no output")

        return speakers
