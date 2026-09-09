"""
Deterministic translation-output validator (Phase 1).

Purpose: stop the model from passing off an untranslated / echoed / half-translated
response as a successful translation. Invalid output is reported so the caller can
substitute None and let the EXISTING None-means-retry pipeline handle it.

Design — one generic pipeline, no ML, no per-language code branches. The ONLY
per-language data is LANG_SCRIPTS (one row per language; Unicode supplies the
character ranges). Adding a language = one row.

Rules, in order, each language-agnostic:
  R0  carve-out      drop proper nouns / numbers / acronyms before measuring, so a line
                     like "MinIO, Temporal," or a correct translation carrying an embedded
                     name ("...我叫Ashutosh。") is never falsely rejected.
  R2  echo           output is a near-copy of the source AND the source was not already in
                     the target script  ->  reject "echo".
  R3  wrong_script   output is not predominantly in the target's script  ->  reject "wrong_script".
  R4  mixed_script   output is mostly target script but still carries source-script residue
                     (a partial translation)  ->  reject "mixed_script".

Known, deliberate Phase-1 limit: when the target shares the source's script (English vs
romanized Hinglish — both Latin), an echo cannot be told apart from a correct passthrough
deterministically. Those are PASSED and logged as `echo_passed_ambiguous` telemetry for a
future, data-driven Phase 2 — we do NOT add heuristic/marker logic here.
"""
import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# --- thresholds (tunable in one place) ---------------------------------------
T_ECHO = 0.90     # similarity at/above which output is treated as a copy of the source
T_CONF = 0.60     # min fraction of content chars that must be in the target script
T_CONTAM = 0.20   # source-script residue at/above which an otherwise-conformant output is "mixed"

# --- scripts: fixed Unicode block ranges (static structure, not a growing list) ----
SCRIPT_RANGES = {
    "Latin":      [(0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F)],
    "Devanagari": [(0x900, 0x97F)],
    "Han":        [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)],
    "Cyrillic":   [(0x400, 0x52F)],
    "Hebrew":     [(0x590, 0x5FF)],
    "Arabic":     [(0x600, 0x6FF), (0x750, 0x77F)],
}

# The ONLY per-language data — a data-driven validation profile per language. Keyword is
# matched as a substring of the target_lang string ("Chinese (Simplified)" matches "chinese").
# Add a language = ONE row, no code change. Profile fields:
#   scripts            : Unicode script(s) that count as the target language (required)
#   conf_threshold     : min fraction of content that must be in `scripts` (default T_CONF)
#   allow_source_latin : True → English (Latin) is expected vocabulary, NOT contamination.
#                        Set for code-mixed targets whose natural output is
#                        "<script> grammar + English tech terms" (e.g. our Hinglish-Hindi;
#                        Marathi / Nepali would be one-row additions below).
LANG_CONFIG = {
    "english": {"scripts": {"Latin"}},
    # Hindi was profiled as a CODE-MIXED target (conf 0.10 + allow_source_latin), which made an
    # untranslated Hinglish line pass as valid Hindi: the model echoed the source (sim=1.00) and
    # `_conforms(source)` said "already Hindi" because a little Devanagari was present, so R3/R4
    # never ran. Users picking "Hindi" expect Hindi, so hold it to the normal bar: a real majority
    # of Devanagari, and untranslated English counts as contamination. (R0 still carves out
    # acronyms/proper nouns, so "AI"/"GPU" don't trip it.)
    "hindi":   {"scripts": {"Devanagari"}},
    "chinese": {"scripts": {"Han"}},
    "russian": {"scripts": {"Cyrillic"}},
    "hebrew":  {"scripts": {"Hebrew"}},
    "arabic":  {"scripts": {"Arabic"}},
    # A genuinely code-mixed target (if ever wanted) would be a separate row, e.g.:
    # "hinglish": {"scripts": {"Devanagari"}, "conf_threshold": 0.10, "allow_source_latin": True},
}

# Hinglish source = Hindi (Devanagari) + English (Latin).
SOURCE_SCRIPTS = {"Latin", "Devanagari"}

_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’]*")
_DIGIT_TOKEN = re.compile(r"\w*\d\w*")


def _config_for(lang):
    """Target language -> its validation profile dict, or {} if unconfigured (script checks
    are then skipped; only the echo check applies)."""
    low = (lang or "").lower()
    for keyword, cfg in LANG_CONFIG.items():
        if keyword in low:
            return cfg
    return {}


def _conf_threshold(cfg):
    """Min fraction of content that must be in the target script(s) — per-language override
    of T_CONF (e.g. Hindi needs only Devanagari PRESENCE, not a majority)."""
    return cfg.get("conf_threshold", T_CONF)


def _contaminating_scripts(cfg):
    """Source scripts whose presence in the output signals untranslated content: any source
    script that isn't the target's. Code-mixed targets (allow_source_latin) treat English
    (Latin) as expected vocabulary, so it is NOT contamination for them."""
    contaminating = SOURCE_SCRIPTS - (cfg.get("scripts") or set())
    if cfg.get("allow_source_latin"):
        contaminating = contaminating - {"Latin"}
    return contaminating


def _script_of(ch):
    cp = ord(ch)
    for name, ranges in SCRIPT_RANGES.items():
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return name
    return None


def _content_chars(text):
    """R0 carve-out: alphabetic chars after dropping entities — works at the Latin-run
    level (not whitespace tokens) so embedded names survive in space-less scripts like CJK,
    e.g. the "Ashutosh" in "大家好，我叫Ashutosh。" is removed, leaving pure Han to measure.
    Dropped: digit-bearing tokens (60, H2O) and proper-noun/acronym/camelCase Latin runs
    (Kafka, MinIO, MongoDB, END). Non-Latin script chars always pass through untouched."""
    if not text:
        return []
    cleaned = _DIGIT_TOKEN.sub(" ", text)                                # 60, H2O, B2B
    cleaned = _LATIN_RUN.sub(lambda m: " " if (m.group(0).isupper() or m.group(0)[0].isupper()) else m.group(0), cleaned)
    return [c for c in cleaned if c.isalpha()]


def _script_ratios(chars):
    if not chars:
        return {}
    counts = {}
    for c in chars:
        s = _script_of(c)
        if s:
            counts[s] = counts.get(s, 0) + 1
    total = len(chars)
    return {k: v / total for k, v in counts.items()}


def _normalize(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w]", " ", (s or "").lower())).strip()


def _similarity(a, b):
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _conforms(text, target_scripts, conf_threshold, contaminating):
    """Is `text` already in the target language per its profile (enough target-script presence
    and no contaminating source-script residue)? Used on the SOURCE to decide whether an echo
    is actually a valid passthrough."""
    if not target_scripts:
        return False
    ratios = _script_ratios(_content_chars(text))
    if not ratios:
        return True   # nothing measurable -> treat as already-fine, don't demand a retranslation
    conf = sum(ratios.get(s, 0) for s in target_scripts)
    contam = sum(ratios.get(s, 0) for s in contaminating)
    return conf >= conf_threshold and contam < T_CONTAM


def _reject(reason, target_lang, sim, ratios, target_scripts, contaminating, output):
    tgt = sum(ratios.get(s, 0) for s in (target_scripts or ()))
    contam = sum(ratios.get(s, 0) for s in contaminating)
    logger.info(
        "[TRANSLATE-VALIDATE] reject reason=%s target=%r sim=%.2f tgt_ratio=%.2f contam=%.2f text=%r",
        reason, target_lang, sim, tgt, contam, (output or "")[:80],
    )
    return False, reason


def validate_translation(output, source, target_lang):
    """Return (is_valid, reason). reason is None when valid, else one of:
    'empty' | 'echo' | 'wrong_script' | 'mixed_script'. Logs every rejection."""
    cfg = _config_for(target_lang)
    target_scripts = cfg.get("scripts")          # None if unconfigured
    conf_threshold = _conf_threshold(cfg)        # T_CONF, or a per-language override
    contaminating = _contaminating_scripts(cfg)  # source scripts that count as untranslated residue

    if not output or not output.strip():
        return False, "empty"

    content = _content_chars(output)
    if not content:
        return True, None   # pure proper-noun / number line — not validatable, accept (R0)

    ratios = _script_ratios(content)
    sim = _similarity(output, source)

    # R2 — echo: output is a near-copy of the source.
    if sim >= T_ECHO:
        if not _conforms(source, target_scripts, conf_threshold, contaminating):
            return _reject("echo", target_lang, sim, ratios, target_scripts, contaminating, output)
        # Source already conforms to the target — a valid passthrough. Happens for same-script
        # targets (English vs romanized Hinglish) and code-mixed targets (Hinglish-Hindi echo).
        logger.info(
            "[TRANSLATE-VALIDATE] echo_passed_ambiguous target=%r sim=%.2f text=%r",
            target_lang, sim, (output or "")[:60],
        )
        return True, None

    if target_scripts:
        conf = sum(ratios.get(s, 0) for s in target_scripts)
        contam = sum(ratios.get(s, 0) for s in contaminating)
        # R3 — wrong_script: not enough of the target script present.
        if conf < conf_threshold:
            return _reject("wrong_script", target_lang, sim, ratios, target_scripts, contaminating, output)
        # R4 — mixed_script: conformant but still carrying contaminating source-script residue.
        if contam >= T_CONTAM:
            return _reject("mixed_script", target_lang, sim, ratios, target_scripts, contaminating, output)

    return True, None
