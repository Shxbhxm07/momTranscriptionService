import os
import shutil
import tempfile
import uuid
import logging
import subprocess
import torch
from contextlib import asynccontextmanager

from typing import List, Optional
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uvicorn

from config import get_base_cfg
from core.diarizer import DiarizerEngine
from core.speaker_db import (
    init_speaker_db, enroll_speaker, get_all_speakers, delete_speaker,
    delete_speaker_by_name, search_similar_speakers, get_speaker_embeddings_by_name,
    get_speaker_samples, reset_all_speakers, get_all_speaker_embeddings, THRESHOLD,
    ENROLL_CONSISTENCY_THRESHOLD,
)
from core.speaker_identifier import SpeakerIdentifier
from core import diagnostics
from utils.audio_utils import convert_to_wav

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Persistent directories (under /app/data which is a named Docker volume)
SAMPLES_DIR    = os.path.join("/app/data", "samples")     # enrolled WAV samples
RECORDINGS_DIR = os.path.join("/app/data", "recordings")  # browser recordings (pre-enrollment)

diarizer_engine = None
speaker_identifier = SpeakerIdentifier()


def _to_wav_ffmpeg(src: str, dst: str) -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", "-f", "wav", dst],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0 and os.path.exists(dst)
    except Exception as e:
        logger.error("ffmpeg error: %s", e)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global diarizer_engine
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    try:
        if torch.cuda.is_available():
            logger.info(f"GPU: {torch.cuda.get_device_name(0)}")
            logger.info(f"Memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} GB")

        base_cfg = get_base_cfg()
        diarizer_engine = DiarizerEngine(base_cfg)
        init_speaker_db()

        logger.info("✓ NeMo configuration loaded")
        logger.info("✓ Diarization service ready")
        logger.info("✓ Speaker enrollment DB ready")
    except Exception as e:
        logger.error(f"Startup error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="NeMo Diarization Service",
    version="3.0",
    description="Speaker diarization + enrollment using NeMo TitaNet + Qdrant",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "service": "NeMo Diarization",
        "version": "3.0",
        "status": "ready",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "nemo_loaded": diarizer_engine is not None and diarizer_engine.is_loaded,
        "gpu_available": torch.cuda.is_available(),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
    }


# =============================================================================
# RECORDING SAVE (pre-enrollment step)
# =============================================================================

@app.post("/speakers/save-recording")
async def save_recording(file: UploadFile = File(...)):
    """
    Convert a browser recording to 16 kHz mono WAV and persist it.
    Returns {rec_id, url} — pass rec_id to /speakers/enroll to avoid re-upload.
    """
    tmp = tempfile.mkdtemp(prefix="spkrec_")
    try:
        ext = os.path.splitext(file.filename or "audio.webm")[1] or ".webm"
        src = os.path.join(tmp, f"upload{ext}")
        wav_tmp = os.path.join(tmp, "recording.wav")

        with open(src, "wb") as f:
            f.write(await file.read())

        if not _to_wav_ffmpeg(src, wav_tmp):
            raise HTTPException(400, "Audio conversion failed. Make sure ffmpeg is installed.")

        rec_id = str(uuid.uuid4())
        saved  = os.path.join(RECORDINGS_DIR, f"{rec_id}.wav")
        shutil.copy2(wav_tmp, saved)
        logger.info("Recording saved → %s", saved)
        return {"rec_id": rec_id, "url": f"/speakers/recordings/{rec_id}.wav"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/speakers/recordings/{filename}")
def serve_recording(filename: str):
    """Serve a saved recording WAV for browser playback."""
    if ".." in filename:
        raise HTTPException(400, "Invalid path.")
    path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Recording not found.")
    return FileResponse(path, media_type="audio/wav")


@app.get("/speakers/samples/{speaker_dir}/{filename}")
def serve_sample(speaker_dir: str, filename: str):
    """Serve a saved enrollment WAV sample for browser playback."""
    if ".." in speaker_dir or ".." in filename:
        raise HTTPException(400, "Invalid path.")
    path = os.path.join(SAMPLES_DIR, speaker_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Sample not found.")
    return FileResponse(path, media_type="audio/wav")


# =============================================================================
# SPEAKER ENROLLMENT
# =============================================================================

@app.post("/speakers/enroll")
async def enroll_speaker_endpoint(
    name: str = Form(..., description="Speaker name"),
    file: Optional[UploadFile] = File(None, description="Audio sample (any format, ≥ 3 s)"),
    rec_id: Optional[str] = Form(None, description="rec_id from /save-recording"),
    start: Optional[float] = Form(None, description="[A] clip start (s) — enrol from a meeting segment"),
    end: Optional[float] = Form(None, description="[A] clip end (s) — enrol from a meeting segment"),
):
    """
    Enroll a speaker. Accepts either:
      - rec_id: ID returned by /speakers/save-recording (pre-saved WAV)
      - file:   raw audio blob (converted here)

    Calculates a quality_score (cosine similarity vs existing samples).
    Persists the WAV to disk for playback.
    """
    name = name.strip()
    if not name:
        raise HTTPException(400, "Speaker name cannot be empty.")
    if not rec_id and not file:
        raise HTTPException(400, "Provide either rec_id or file.")

    tmp = tempfile.mkdtemp(prefix="enroll_")
    try:
        wav = os.path.join(tmp, "sample.wav")

        if rec_id:
            rec_path = os.path.join(RECORDINGS_DIR, f"{rec_id}.wav")
            if not os.path.isfile(rec_path):
                raise HTTPException(400, f"Recording not found for rec_id={rec_id}. Re-record.")
            shutil.copy2(rec_path, wav)
        else:
            ext = os.path.splitext(file.filename or "audio.webm")[1] or ".webm"
            src = os.path.join(tmp, f"upload{ext}")
            with open(src, "wb") as f:
                f.write(await file.read())
            if not convert_to_wav(src, wav):
                raise HTTPException(400, "Audio conversion failed.")

        # [A] Enrol from a SEGMENT of a longer recording (e.g. one speaker's stretch of a real
        # meeting) so the voiceprint is captured in the SAME acoustic conditions as meetings — the
        # fix for enrol↔meeting channel mismatch (measured: enrolment self-consistency ~0.9 but
        # enrol→meeting only 0.3–0.58 when the enrol mic differs from the meeting mic).
        if start is not None and end is not None and end > start:
            import soundfile as sf
            _d, _sr = sf.read(wav)
            _a, _b = max(0, int(start * _sr)), int(end * _sr)
            if _b > _a:
                sf.write(wav, _d[_a:_b], _sr)
                logger.info("Enrol clip [%.2f-%.2f]s (%d samples)", start, end, _b - _a)

        # Extract embedding
        embedding = speaker_identifier.extract_enrollment_embedding(wav)

        # Quality score: compare new sample against all existing samples for this speaker
        existing_embs = get_speaker_embeddings_by_name(name)
        if existing_embs:
            import numpy as np
            sims = [float(np.dot(embedding / (np.linalg.norm(embedding) + 1e-9),
                                  e / (np.linalg.norm(e) + 1e-9)))
                    for e in existing_embs]
            quality_score = round(float(sum(sims) / len(sims)), 4)
        else:
            quality_score = 0.0  # first sample — nothing to compare against

        # Persist WAV to samples/{speaker_id}/sample_N.wav
        # We need speaker_id first — enroll_speaker returns it
        # But we need it before to compute the path, so do a quick lookup
        from core.speaker_db import get_speaker_by_name
        existing = get_speaker_by_name(name)
        speaker_id = existing["id"] if existing else str(uuid.uuid4())
        sample_index = (existing["sample_count"] + 1) if existing else 1

        spk_dir = os.path.join(SAMPLES_DIR, speaker_id)
        os.makedirs(spk_dir, exist_ok=True)
        saved_wav = os.path.join(spk_dir, f"sample_{sample_index}.wav")
        shutil.copy2(wav, saved_wav)
        audio_file = f"{speaker_id}/sample_{sample_index}.wav"

        # Pass speaker_id so a NEW speaker's saved-sample path and stored payload share ONE id
        # (previously the path used a throwaway uuid while enroll_speaker minted another).
        speaker_id = enroll_speaker(name, embedding, quality_score, audio_file, speaker_id=speaker_id)

        logger.info("✓ Enrolled sample %d for '%s' (quality=%.4f)", sample_index, name, quality_score)
        return {
            "success":       True,
            "id":            speaker_id,
            "name":          name,
            "sample_count":  sample_index,
            "quality_score": quality_score,
            "message":       f"Sample {sample_index} stored for '{name}'.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enrollment error: {e}")
        import traceback; logger.error(traceback.format_exc())
        raise HTTPException(500, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/speakers")
async def list_speakers():
    """Return all enrolled speakers."""
    speakers = get_all_speakers()
    return {"speakers": speakers, "count": len(speakers)}


@app.get("/speakers/{name}/samples")
async def list_speaker_samples(name: str):
    """Return all saved audio samples for a speaker with quality scores and playback URLs."""
    samples = get_speaker_samples(name)
    if not samples:
        raise HTTPException(404, f"Speaker '{name}' not found.")
    return [
        {
            **s,
            "url": f"/speakers/samples/{s['audio_file']}" if s.get("audio_file") else None,
        }
        for s in samples
    ]


@app.delete("/speakers/by-name/{name}")
async def remove_speaker_by_name(name: str):
    """Delete an enrolled speaker by display name (also removes saved audio files)."""
    deleted, speaker_id = delete_speaker_by_name(name)
    if not deleted:
        raise HTTPException(404, f"Speaker '{name}' not found.")
    # Remove persisted audio samples from disk
    if speaker_id:
        spk_dir = os.path.join(SAMPLES_DIR, speaker_id)
        if os.path.isdir(spk_dir):
            shutil.rmtree(spk_dir, ignore_errors=True)
            logger.info("Removed audio samples directory: %s", spk_dir)
    return {"success": True, "message": f"Speaker '{name}' deleted."}


@app.delete("/speakers/{speaker_id}")
async def remove_speaker(speaker_id: str):
    """Delete an enrolled speaker by UUID."""
    deleted = delete_speaker(speaker_id)
    if not deleted:
        raise HTTPException(404, "Speaker not found.")
    spk_dir = os.path.join(SAMPLES_DIR, speaker_id)
    if os.path.isdir(spk_dir):
        shutil.rmtree(spk_dir, ignore_errors=True)
    return {"success": True, "message": f"Speaker {speaker_id} deleted."}


@app.post("/speakers/reset")
async def reset_speakers():
    """Delete ALL enrolled speakers and their audio files."""
    count = reset_all_speakers()
    if os.path.isdir(SAMPLES_DIR):
        shutil.rmtree(SAMPLES_DIR, ignore_errors=True)
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    logger.info("Reset: cleared all speakers and audio samples")
    return {"success": True, "message": f"All speakers cleared ({count} embeddings removed)."}


# =============================================================================
# VOICE TESTING
# =============================================================================

@app.post("/speakers/test-voice")
async def test_voice(file: UploadFile = File(...)):
    """
    Compare a test recording against ALL enrolled speakers.
    Returns results sorted by score — top match first with speaker name.
    """
    speakers = get_all_speakers()
    if not speakers:
        raise HTTPException(400, "No speakers enrolled. Enroll at least one speaker first.")

    tmp = tempfile.mkdtemp(prefix="spktest_")
    try:
        ext = os.path.splitext(file.filename or "audio.webm")[1] or ".webm"
        src = os.path.join(tmp, f"upload{ext}")
        wav = os.path.join(tmp, "test.wav")
        with open(src, "wb") as f:
            f.write(await file.read())
        if not convert_to_wav(src, wav):
            raise HTTPException(400, "Audio conversion failed.")

        embedding = speaker_identifier.extract_enrollment_embedding(wav)
        results = search_similar_speakers(embedding, top_k=max(50, len(speakers) * 5))

        if not results:
            raise HTTPException(500, "Search returned no results.")

        top = results[0]
        return {
            "top_name":      top["name"],
            "top_score":     top["score"],
            "top_score_pct": top["score_pct"],
            "passed":        top["passed"],
            "all_results":   results,
            "threshold":     THRESHOLD,
            "threshold_pct": round(THRESHOLD * 100, 1),
            "message":       f"RECOGNIZED as {top['name']}" if top["passed"] else "NOT RECOGNIZED",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test-voice error: {e}")
        import traceback; logger.error(traceback.format_exc())
        raise HTTPException(500, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/speakers/compare")
async def compare_samples(files: List[UploadFile] = File(...)):
    """
    Compare voice samples for enrollment quality gating.
    2 files → similarity of file2 vs file1.
    3 files → similarity of file3 vs avg(file1, file2).
    """
    if len(files) < 2:
        raise HTTPException(400, "Need at least 2 audio files.")

    tmp = tempfile.mkdtemp(prefix="compare_")
    try:
        wav_paths = []
        for i, f in enumerate(files):
            ext = os.path.splitext(f.filename or "audio.webm")[1] or ".webm"
            src = os.path.join(tmp, f"upload_{i}{ext}")
            with open(src, "wb") as fp:
                fp.write(await f.read())
            wav_path = os.path.join(tmp, f"sample_{i}.wav")
            if not convert_to_wav(src, wav_path):
                raise HTTPException(400, f"Could not convert file {i+1}.")
            wav_paths.append(wav_path)

        score = speaker_identifier.compare_audio_files(wav_paths)
        # `threshold` is returned so the enrolment screen can draw its pass/fail bar from the
        # server's value instead of keeping its own copy. See ENROLL_CONSISTENCY_THRESHOLD in
        # speaker_db.py for why it is 0.70.
        return {
            "score": round(score, 4),
            "passed": score >= ENROLL_CONSISTENCY_THRESHOLD,
            "threshold": ENROLL_CONSISTENCY_THRESHOLD,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Compare error: {e}")
        import traceback; logger.error(traceback.format_exc())
        raise HTTPException(500, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================
# DIARIZATION
# =============================================================================

@app.post("/diarize")
async def diarize(
    file: UploadFile = File(...),
    num_speakers: Optional[int] = None,
    identify: bool = True,
    debug: bool = False,
):
    """
    Speaker diarization. With identify=true, matches clusters against enrolled speakers.

    debug=true (or env DIARIZE_DEBUG=1) enables a diagnostic-only artifact dump for
    this run (RTTM, VAD segments, per-segment levels/clipping/SNR, cluster cosine
    matrices, merge decisions, cfg). It does NOT change the diarization result or the
    response, and is OFF by default.
    """
    if not diarizer_engine or not diarizer_engine.is_loaded:
        raise HTTPException(503, "NeMo not loaded")

    diag_on = diagnostics.enabled(debug)
    diag = {} if diag_on else None

    tmp = None
    try:
        logger.info(f"Diarizing: {file.filename}")
        tmp = tempfile.mkdtemp(prefix="nemo_diarize_")
        os.makedirs(os.path.join(tmp, "speaker_outputs", "embeddings"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "pred_rttms"), exist_ok=True)

        upload_path = os.path.join(tmp, "upload" + os.path.splitext(file.filename)[1])
        with open(upload_path, "wb") as f:
            f.write(await file.read())

        audio_path = os.path.join(tmp, "audio.wav")
        if not convert_to_wav(upload_path, audio_path):
            raise HTTPException(400, "Failed to convert audio.")

        segments = diarizer_engine.diarize(audio_path, tmp, num_speakers, diag=diag)
        found_speakers = len(set(s["speaker"] for s in segments))
        logger.info(f"✓ Found {found_speakers} speakers, {len(segments)} segments")

        # Cluster clean-up runs on EVERY meeting. NeMo's NME-SC already estimates the speaker count;
        # we do NOT second-guess it by merging "similar" clusters — a fixed same-speaker cosine bar
        # wrongly merged two different-but-similar colleagues (0.836), and NO fixed bar is safe as the
        # headcount grows (more people => higher chance two real voices sit above it). So the only
        # clean-up is removing NOISE/phantom clusters, decided by a COUNT-INDEPENDENT acoustic fact:
        # a real speaker's voiceprint matches SOME other human (~0.3-0.6); a noise cluster matches
        # everyone at ~0. That test is identical whether the meeting has 2 or 8 people — no tuning.
        cluster_embs = speaker_identifier.extract_cluster_embeddings(audio_path, segments)
        # Self-calibrating clean-up: MERGE over-splits + DROP noise from THIS meeting's own cosine
        # distribution (no fixed bar). Set SELF_CALIBRATE_CLUSTERS=false to roll back to the old
        # fixed-threshold drop for an A/B on identical audio.
        if os.getenv("SELF_CALIBRATE_CLUSTERS", "true").strip().lower() == "true":
            segments, cluster_embs, _calib = speaker_identifier.calibrate_clusters(segments, cluster_embs)
        else:
            segments, cluster_embs = speaker_identifier.drop_noise_clusters(segments, cluster_embs)
        cleaned_speakers = len(set(s["speaker"] for s in segments))
        if cleaned_speakers != found_speakers:
            logger.info(f"✓ After cluster clean-up: {cleaned_speakers} speakers (was {found_speakers})")
            found_speakers = cleaned_speakers

        speaker_map = {}
        if identify:
            enrolled_count = len(get_all_speakers())
            if enrolled_count > 0:
                logger.info(f"Identifying against {enrolled_count} enrolled profile(s)…")
                speaker_map = speaker_identifier.identify_speakers_with_qdrant(cluster_embs)
                logger.info(f"Speaker map: {speaker_map}" if speaker_map else "No enrolled speakers matched")

        for seg in segments:
            seg["speaker_name"] = speaker_map.get(seg["speaker"], seg["speaker"])

        # Diagnostic-only artifact dump. Read-only; runs after the real result is built
        # and before temp cleanup. Wrapped so instrumentation can never fail diarization.
        if diag_on:
            try:
                if cluster_embs is None:
                    cluster_embs = speaker_identifier.extract_cluster_embeddings(audio_path, segments)
                run_dir = diagnostics.start_run(file.filename)
                if run_dir:
                    diagnostics.write_all(
                        run_dir,
                        audio_path=audio_path,
                        temp_dir=tmp,
                        segments_final=segments,
                        diag=diag,
                        num_speakers=num_speakers,
                        identify=identify,
                        cluster_embeddings=cluster_embs,
                        enrolled=get_all_speaker_embeddings(),
                        speaker_map=speaker_map,
                    )
            except Exception as e:
                logger.error(f"[DIAR-DEBUG] instrumentation failed (diarization unaffected): {e}")
                import traceback; logger.error(traceback.format_exc())

        return {
            "filename":     file.filename,
            "segments":     segments,
            "num_speakers": found_speakers,
            "speaker_map":  speaker_map,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Diarization error: {e}")
        import traceback; logger.error(traceback.format_exc())
        raise HTTPException(500, str(e))
    finally:
        if tmp and os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
