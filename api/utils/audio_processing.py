"""Audio preprocessing — ffmpeg decode/normalise, plus the MoM service's noise filter.

The denoise filter chain is carried over VERBATIM from the MoM gateway
(backend/utils/audio_processing.py); only the container format changed (mp3 -> wav,
see normalize_audio below for why).
"""
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Applied in order (unchanged from the MoM service):
#   1. highpass=f=100      — cuts sub-100Hz rumble (fans, AC, table vibrations)
#   2. afftdn=nf=-30:nr=15 — FFT noise reduction, stronger floor + reduction strength
#   3. agate               — noise gate: silences audio below threshold between words
#                            threshold=0.015 (~-36dB), ratio=10:1, fast attack, slow release
_DENOISE_CHAIN = (
    "highpass=f=100,"
    "afftdn=nf=-30:nr=15,"
    "agate=threshold=0.015:ratio=10:attack=10:release=400"
)


def normalize_audio(audio_bytes: bytes, denoise: bool = False) -> bytes:
    """Decode ANY ffmpeg-readable audio/video container to 16 kHz mono PCM WAV.

    WHY THIS IS MANDATORY, not a nicety. whisper-server decodes with miniaudio (built
    without WHISPER_FFMPEG, started without --convert). Measured against this exact
    build: WAV, MP3 and FLAC decode fine; m4a/AAC and webm/opus are both rejected with a
    bare "Invalid request" and no detail. Those two are precisely what phones (m4a) and
    browser MediaRecorder (webm/opus) produce, so for a standalone API that takes
    whatever the caller sends, the decode has to happen HERE or those uploads just fail.

    The MoM stack never hit this because its uploads had already been through ffmpeg
    upstream in the gateway.

    16 kHz mono is also exactly what Whisper consumes internally, so resampling here
    costs nothing: whisper.cpp would do the same conversion itself on a 44.1 kHz stereo
    input.

    Raises RuntimeError when ffmpeg cannot decode the input — i.e. it is not audio.
    Unlike the MoM version this does NOT silently fall back to the original bytes: there
    the fallback meant "skip an optional quality filter", here it would mean handing
    whisper-server a file it cannot read, turning a clear error into an empty transcript.
    """
    if not audio_bytes:
        raise RuntimeError("Empty audio upload")

    temp_in = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_out.close()

    try:
        temp_in.write(audio_bytes)
        temp_in.flush()
        temp_in.close()

        command = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", temp_in.name,
        ]
        if denoise:
            logger.info("[AUDIO] Applying FFT denoise + noise gate")
            command += ["-af", _DENOISE_CHAIN]
        command += [
            "-ar", "16000",       # Whisper's native sample rate
            "-ac", "1",           # mono
            "-c:a", "pcm_s16le",  # what whisper-server's WAV reader expects
            temp_out.name,
        ]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg could not decode the uploaded file: {result.stderr.strip()[:300]}"
            )

        with open(temp_out.name, "rb") as f:
            wav_bytes = f.read()

        if not wav_bytes:
            raise RuntimeError("ffmpeg produced no audio — the file may contain no audio stream")

        logger.info(
            f"[AUDIO] ✓ Normalised {len(audio_bytes)/1e6:.1f}MB → "
            f"{len(wav_bytes)/1e6:.1f}MB 16kHz mono WAV (denoise={denoise})"
        )
        return wav_bytes

    finally:
        for path in (temp_in.name, temp_out.name):
            try:
                os.unlink(path)
            except OSError:
                pass
