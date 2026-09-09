from typing import List, Optional, Dict
from pydantic import BaseModel

class SummarizeRequest(BaseModel):
    text: str
    language: str = "en"
    max_length: int = 4000
    temperature: float = 0.05
    output_lang: str = "English"   # language for the generated MoM (default English = unchanged)
    metadata: Optional[Dict] = None  # {date, time, venue} — deterministic, overrides the FALLBACK bridge

class LocalizeMomRequest(BaseModel):
    content: Dict                    # the English structured MoM content (from /summarize)
    target_lang: str = "English"     # language to localize into
    metadata: Optional[Dict] = None  # {date, time, venue}
    temperature: float = 0.1

class TranslateRequest(BaseModel):
    text: str
    source_lang: str = "en"
    target_lang: str = "hi"
    temperature: float = 0.1
    context: str = ""  # last few sentences of prior translation for boundary continuity

class TranslateBatchRequest(BaseModel):
    texts: List[str]
    source_lang: str = "en"
    target_lang: str = "hi"

class TranslateMomRequest(BaseModel):
    text: str                      # a FINISHED English MoM
    target_lang: str = "English"   # language to translate the MoM into
    temperature: float = 0.1

class SpeakerMapRequest(BaseModel):
    text: str
    temperature: float = 0.01

class CorrectTranscriptRequest(BaseModel):
    text: str
    temperature: float = 0.1
    mode: str = "hinglish"  # "hinglish" or "translated"
    known_names: list[str] = []  # people confirmed present (voice-ID / named labels) — anchors name correction
