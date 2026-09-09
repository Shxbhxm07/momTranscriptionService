"""
Diagnostic-only instrumentation for the diarization pipeline.

WHEN DISABLED (the default) NOTHING in this module runs: diarization behavior,
output, and performance are byte-for-byte unchanged. The only cost on the hot path
is a single boolean check (`enabled()`).

Enable per-run with `POST /diarize?debug=true`, or globally with env `DIARIZE_DEBUG=1`.

Everything here is best-effort and READ-ONLY with respect to the pipeline:
  * it never mutates `segments`, the clustering result, or the API response;
  * every artifact write is wrapped so an instrumentation failure is logged and
    swallowed — it can never fail a diarization.

Artifacts are written to a timestamped directory under DIARIZE_DEBUG_DIR
(default /app/data/diar_debug, the existing persistent speaker-data volume) so they
survive the request and pod restarts:

  params.json               full resolved NeMo cfg (clustering/VAD/embedding) + run meta
  segments_raw.json         per-segment speaker/start/end/duration BEFORE merge/cap
  segments_final.json       ... AFTER merge/cap (with speaker_name)
  rttm/audio.rttm           raw NeMo RTTM
  nemo_outputs/             snapshot of NeMo's working tree:
                              vad_outputs/      VAD speech segments BEFORE clustering
                              speaker_outputs/  per-scale subsegments + clustering embeddings
                              pred_rttms/
  segment_levels.json       per segment: RMS, dBFS, peak, clip_pct, snr_db_estimate
  cluster_pairwise_cosine.json   N×N cosine between cluster mean embeddings
  cluster_embeddings.npz         the cluster mean d-vectors themselves
  cluster_vs_enrolled.json       every cluster × every enrolled sample cosine + best-per-name
  merge_decisions.json      every phantom/cap decision with the measured values + reason
  summary.txt               human-readable rollup
  input.wav                 copy of the 16 kHz mono audio (reproducible re-analysis)
"""
import os
import json
import shutil
import logging
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)

DEBUG_ENV = "DIARIZE_DEBUG"
DEBUG_DIR = os.getenv("DIARIZE_DEBUG_DIR") or "/app/data/diar_debug"

# |sample| at/above this fraction of full scale is counted as clipped.
CLIP_LEVEL = 0.99


# ---------------------------------------------------------------------------
# enable / run-dir
# ---------------------------------------------------------------------------

def enabled(request_flag: bool = False) -> bool:
    """True if diagnostics should run for this call (env OR per-request flag)."""
    env_on = (os.getenv(DEBUG_ENV) or "").strip().lower() in ("1", "true", "yes", "on")
    return bool(env_on or request_flag)


def start_run(filename: str):
    """Create and return a timestamped run directory, or None on failure."""
    try:
        safe = "".join(c if (c.isalnum() or c in "-._") else "_" for c in (filename or "audio"))[:60]
        run_dir = os.path.join(DEBUG_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}")
        os.makedirs(run_dir, exist_ok=True)
        logger.info("[DIAR-DEBUG] writing artifacts -> %s", run_dir)
        return run_dir
    except Exception as e:
        logger.error("[DIAR-DEBUG] could not create run dir: %s", e)
        return None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float64).flatten()
    b = np.asarray(b, dtype=np.float64).flatten()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _json_default(o):
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _dump(run_dir, name, obj):
    """Write one JSON artifact; failure is logged, not raised."""
    try:
        with open(os.path.join(run_dir, name), "w") as f:
            json.dump(obj, f, indent=2, default=_json_default)
    except Exception as e:
        logger.error("[DIAR-DEBUG] failed writing %s: %s", name, e)


def _finite(x):
    """JSON-safe float (inf/nan -> None)."""
    return round(float(x), 4) if (x is not None and np.isfinite(x)) else None


# ---------------------------------------------------------------------------
# measurements
# ---------------------------------------------------------------------------

def _measure_levels(audio_path, segments):
    """Per-segment RMS / dBFS / peak / clip% and an ESTIMATED SNR.

    SNR has no clean reference signal here, so it is an estimate only:
    segment dBFS minus a noise floor taken as the RMS of all non-speech
    (VAD-gap) samples. Reported as `snr_db_estimate`; null when not derivable.
    """
    import soundfile as sf

    audio, sr = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    n = len(audio)

    # Noise floor: RMS of samples NOT covered by any speech segment.
    mask = np.zeros(n, dtype=bool)
    for s in segments:
        a = max(0, int(s["start"] * sr))
        b = min(n, int(s["end"] * sr))
        if b > a:
            mask[a:b] = True
    noise = audio[~mask]
    noise_dbfs = None
    if noise.size > sr * 0.2:  # need ≥0.2s of non-speech to estimate a floor
        noise_rms = float(np.sqrt(np.mean(noise ** 2)))
        if noise_rms > 0:
            noise_dbfs = 20.0 * np.log10(noise_rms)

    rows = []
    for s in segments:
        a = max(0, int(s["start"] * sr))
        b = min(n, int(s["end"] * sr))
        chunk = audio[a:b]
        row = {
            "speaker": s["speaker"],
            "speaker_name": s.get("speaker_name", s["speaker"]),
            "start": round(s["start"], 3),
            "end": round(s["end"], 3),
            "duration": round(s["duration"], 3),
            "samples": int(chunk.size),
        }
        if chunk.size:
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            peak = float(np.max(np.abs(chunk)))
            dbfs = 20.0 * np.log10(rms) if rms > 0 else None
            clip_pct = float(np.mean(np.abs(chunk) >= CLIP_LEVEL) * 100.0)
            snr = (dbfs - noise_dbfs) if (dbfs is not None and noise_dbfs is not None) else None
            row.update({
                "rms": round(rms, 6),
                "dbfs": _finite(dbfs),
                "peak": round(peak, 6),
                "clip_pct": round(clip_pct, 3),
                "snr_db_estimate": _finite(snr),
            })
        rows.append(row)

    return {
        "sample_rate": int(sr),
        "noise_floor_dbfs_estimate": _finite(noise_dbfs),
        "snr_method": ("segment_dbfs - noise_floor_dbfs; noise floor = RMS of "
                       "non-speech (VAD-gap) samples; ESTIMATE ONLY, no clean reference"),
        "clip_level": CLIP_LEVEL,
        "segments": rows,
    }


def _pairwise_cosine(cluster_embeddings):
    ids = sorted(cluster_embeddings.keys())
    matrix = {
        i: {j: round(_cosine(cluster_embeddings[i], cluster_embeddings[j]), 4) for j in ids}
        for i in ids
    }
    return {"clusters": ids, "cosine_matrix": matrix}


def _enrolled_report(cluster_embeddings, enrolled):
    """enrolled: [{name, embedding}, ...] — one entry per stored sample."""
    out = {}
    for cid, emb in cluster_embeddings.items():
        per_sample, best_per_name = [], {}
        for e in enrolled:
            sc = _cosine(emb, e["embedding"])
            per_sample.append({"name": e["name"], "cosine": round(sc, 4)})
            if e["name"] not in best_per_name or sc > best_per_name[e["name"]]:
                best_per_name[e["name"]] = sc
        if best_per_name:
            best_name, best_score = max(best_per_name.items(), key=lambda x: x[1])
        else:
            best_name, best_score = None, None
        out[cid] = {
            "best_match": best_name,
            "best_cosine": round(best_score, 4) if best_score is not None else None,
            "best_per_name": {k: round(v, 4) for k, v in best_per_name.items()},
            "all_samples": sorted(per_sample, key=lambda x: x["cosine"], reverse=True),
        }
    return out


def _snapshot_nemo_tree(temp_dir, run_dir):
    """Copy NeMo's working tree (VAD output, subsegments, embeddings, RTTM)."""
    dst = os.path.join(run_dir, "nemo_outputs")
    os.makedirs(dst, exist_ok=True)
    for sub in ("vad_outputs", "speaker_outputs", "pred_rttms"):
        src = os.path.join(temp_dir, sub)
        if os.path.isdir(src):
            try:
                shutil.copytree(src, os.path.join(dst, sub), dirs_exist_ok=True)
            except Exception as e:
                logger.error("[DIAR-DEBUG] snapshot %s failed: %s", sub, e)
    try:
        for f in os.listdir(temp_dir):
            if f.endswith(".json"):
                shutil.copy2(os.path.join(temp_dir, f), os.path.join(dst, f))
    except Exception as e:
        logger.error("[DIAR-DEBUG] manifest snapshot failed: %s", e)
    # Surface the RTTM at the top level too.
    rttm = os.path.join(temp_dir, "pred_rttms", "audio.rttm")
    if os.path.exists(rttm):
        try:
            os.makedirs(os.path.join(run_dir, "rttm"), exist_ok=True)
            shutil.copy2(rttm, os.path.join(run_dir, "rttm", "audio.rttm"))
        except Exception as e:
            logger.error("[DIAR-DEBUG] rttm copy failed: %s", e)


def _write_summary(run_dir, levels, pairwise, enrolled_report, decisions, meta):
    """Human-readable rollup, grouped per cluster."""
    try:
        by_spk = {}
        for r in levels.get("segments", []):
            by_spk.setdefault(r["speaker"], []).append(r)

        lines = []
        lines.append("=" * 78)
        lines.append("DIARIZATION DIAGNOSTIC SUMMARY")
        lines.append("=" * 78)
        lines.append(f"file            : {meta.get('filename')}")
        lines.append(f"num_speakers arg: {meta.get('num_speakers')}  (oracle={meta.get('oracle')})")
        lines.append(f"identify        : {meta.get('identify')}")
        lines.append(f"raw clusters    : {meta.get('raw_speakers')}   final: {meta.get('final_speakers')}")
        lines.append(f"total segments  : {meta.get('segment_count')}")
        lines.append(f"noise floor dBFS: {levels.get('noise_floor_dbfs_estimate')} (estimate)")
        lines.append("")
        lines.append("PER-CLUSTER (segments / total dur / avg seg / dBFS range / max clip% / best enrolled):")
        for spk in sorted(by_spk.keys()):
            rows = by_spk[spk]
            durs = [r["duration"] for r in rows]
            dbfs = [r["dbfs"] for r in rows if r.get("dbfs") is not None]
            clips = [r.get("clip_pct", 0) for r in rows]
            er = enrolled_report.get(spk, {}) if enrolled_report else {}
            total = sum(durs)
            avg = total / len(durs) if durs else 0
            dbfs_rng = f"{min(dbfs):.1f}..{max(dbfs):.1f}" if dbfs else "n/a"
            lines.append(
                f"  {spk:<12} n={len(rows):>3}  dur={total:>6.1f}s  avg={avg:>4.1f}s  "
                f"dBFS={dbfs_rng:<14}  clip%max={max(clips) if clips else 0:>5.2f}  "
                f"enrolled={er.get('best_match')}@{er.get('best_cosine')}"
            )
        lines.append("")
        lines.append("PAIRWISE CLUSTER COSINE:")
        cm = pairwise.get("cosine_matrix", {})
        ids = pairwise.get("clusters", [])
        lines.append("            " + "".join(f"{i:>10}" for i in ids))
        for i in ids:
            lines.append(f"  {i:<10}" + "".join(f"{cm[i][j]:>10.3f}" for j in ids))
        lines.append("")
        lines.append("MERGE / CAP DECISIONS:")
        if decisions:
            for d in decisions:
                lines.append(f"  {d}")
        else:
            lines.append("  (none — no phantom/cap merges fired)")
        lines.append("")

        with open(os.path.join(run_dir, "summary.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.error("[DIAR-DEBUG] summary failed: %s", e)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def write_all(run_dir, *, audio_path, temp_dir, segments_final, diag,
              num_speakers, identify, cluster_embeddings, enrolled, speaker_map):
    """Write every artifact. Each step is independently guarded so a single
    failure (e.g. a missing NeMo subdir) does not abort the rest."""
    raw = (diag or {}).get("segments_raw", [])
    decisions = (diag or {}).get("decisions", [])
    cfg = (diag or {}).get("cfg", {})
    raw_speakers = sorted({s["speaker"] for s in raw}) if raw else None
    final_speakers = sorted({s["speaker"] for s in segments_final})

    meta = {
        "filename": os.path.basename(audio_path),
        "num_speakers": num_speakers,
        "oracle": bool(num_speakers),
        "identify": identify,
        "raw_speakers": raw_speakers,
        "final_speakers": final_speakers,
        "segment_count": len(segments_final),
        "speaker_map": speaker_map,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    _dump(run_dir, "params.json", {"meta": meta, "nemo_cfg": cfg})
    _dump(run_dir, "segments_raw.json", raw)
    _dump(run_dir, "segments_final.json", segments_final)
    _dump(run_dir, "merge_decisions.json", decisions)

    levels, pairwise, enrolled_report = {}, {}, {}
    try:
        levels = _measure_levels(audio_path, segments_final)
        _dump(run_dir, "segment_levels.json", levels)
    except Exception as e:
        logger.error("[DIAR-DEBUG] level measurement failed: %s", e)

    if cluster_embeddings:
        try:
            pairwise = _pairwise_cosine(cluster_embeddings)
            _dump(run_dir, "cluster_pairwise_cosine.json", pairwise)
        except Exception as e:
            logger.error("[DIAR-DEBUG] pairwise cosine failed: %s", e)
        try:
            np.savez(os.path.join(run_dir, "cluster_embeddings.npz"),
                     **{k: np.asarray(v) for k, v in cluster_embeddings.items()})
        except Exception as e:
            logger.error("[DIAR-DEBUG] embedding save failed: %s", e)
        if enrolled:
            try:
                enrolled_report = _enrolled_report(cluster_embeddings, enrolled)
                _dump(run_dir, "cluster_vs_enrolled.json", enrolled_report)
            except Exception as e:
                logger.error("[DIAR-DEBUG] enrolled cosine failed: %s", e)

    try:
        _snapshot_nemo_tree(temp_dir, run_dir)
    except Exception as e:
        logger.error("[DIAR-DEBUG] nemo snapshot failed: %s", e)

    try:
        shutil.copy2(audio_path, os.path.join(run_dir, "input.wav"))
    except Exception as e:
        logger.error("[DIAR-DEBUG] input.wav copy failed: %s", e)

    _write_summary(run_dir, levels, pairwise, enrolled_report, decisions, meta)
    logger.info("[DIAR-DEBUG] artifacts complete: %s", run_dir)
