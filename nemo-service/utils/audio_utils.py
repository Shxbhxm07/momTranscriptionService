import logging
import subprocess

logger = logging.getLogger(__name__)

def convert_to_wav(input_path: str, output_path: str) -> bool:
    """Convert audio to mono 16kHz WAV using ffmpeg"""
    try:
        cmd = [
            'ffmpeg', '-i', input_path,
            '-ar', '16000',
            '-ac', '1',
            '-y', output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")
        return True
    except Exception as e:
        logger.error(f"Audio conversion error: {e}")
        return False
