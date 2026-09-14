"""Document bytes → plain-text blocks, entirely offline.

Four input formats, one output shape: a list of paragraph-level strings that the
translator can chunk and send to the LLM. Everything here is CPU-only and needs no
network — the parsers are pure-Python wheels, and OCR is a local tesseract binary with
its language data baked into the image at build time.

    .txt   → decoded, paragraph-split
    .docx  → python-docx, walking the body IN DOCUMENT ORDER so tables stay in place
    .doc   → LibreOffice headless → .docx → the path above
    .pdf   → pypdfium2 text layer, per page, falling back to OCR on pages that have none

WHY PER-PAGE OCR RATHER THAN PER-DOCUMENT. A scanned appendix bolted onto a born-digital
report is the common real-world case for Hindi government PDFs, and an all-or-nothing
decision gets it wrong in both directions: OCR the whole file and the clean pages come
back worse than their own text layer, skip OCR entirely and the scanned half silently
vanishes. Deciding page by page costs nothing and is right on mixed documents — the
Extraction below reports exactly how many pages took each route.

WHY PDF LINES ARE REFLOWED. A PDF text layer stores VISUAL lines: a sentence spanning
three lines is three strings, broken mid-clause. Translating those separately hands the
model sentence fragments and gets fragment-quality output back. _reflow() rejoins lines
into sentences before anything is translated — see it for the join rule.
"""
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional

from config import (
    ENABLE_OCR,
    MIN_PAGE_TEXT_CHARS,
    OCR_DPI,
    OCR_LANGS,
    OCR_MAX_PAGES,
    SOFFICE_TIMEOUT,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".doc", ".txt")


class DocumentError(Exception):
    """Input this service cannot read. Carries a message meant for the API caller —
    it is surfaced verbatim as an HTTP detail, so it must say what to do next."""


@dataclass
class Extraction:
    """The text of a document, plus how it was obtained.

    `source` and the page counters are not decoration: OCR'd text is materially less
    reliable than a text layer, and a caller comparing a bad translation against its
    input needs to know which one it came from. They are reported in the response.
    """
    blocks: List[str] = field(default_factory=list)
    source: str = ""                 # txt | docx | doc | pdf-text | pdf-ocr | pdf-mixed
    pages: Optional[int] = None
    text_layer_pages: int = 0
    ocr_pages: int = 0

    @property
    def chars(self) -> int:
        return sum(len(b) for b in self.blocks)


# ── shared text shaping ──────────────────────────────────────────────────────

# Sentence terminators for BOTH scripts. The danda (।) and double danda (॥) are Hindi's
# full stops; without them every Devanagari paragraph looks like one unbroken sentence to
# the chunker, which then splits it mid-clause on a character count instead.
_SENTENCE_END = re.compile(r'[.!?।॥:;]["\'”’)\]]*\s*$')

# A line ending in a hyphen is a word split across lines by the PDF's layout engine
# ("proc-\nurement"); rejoin without the hyphen and without a space.
_HYPHEN_BREAK = re.compile(r'(\w)[-‐‑]\s*$')

# Bullets, numbered items and headings start their own block: they are not continuations
# of the previous line even when that line has no terminator.
_BLOCK_START = re.compile(r'^\s*(?:[-•·◦▪*]|\(?\d{1,3}[.)]|[a-zA-Z][.)]|[०-९]{1,3}[.)])\s+')


def _reflow(lines: List[str]) -> List[str]:
    """Visual lines → paragraph blocks.

    Join rule, applied in order: a blank line ends the block; a line that looks like a
    bullet/heading starts a new one; a line ending in a sentence terminator ends the
    current one; anything else is a continuation and is joined with a single space (or
    with none, when the previous line ended mid-word in a hyphen).

    This is a heuristic and it will occasionally merge a short heading into the paragraph
    below it. That costs a little formatting fidelity in the output text and nothing in
    translation quality, which is the trade being made — the alternative, translating
    half-sentences, degrades the actual words.
    """
    blocks: List[str] = []
    current = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = ""
            continue
        if current and _BLOCK_START.match(line):
            blocks.append(current)
            current = line
            continue
        if not current:
            current = line
        elif _HYPHEN_BREAK.search(current):
            current = _HYPHEN_BREAK.sub(r'\1', current) + line
        else:
            current = f"{current} {line}"
        if _SENTENCE_END.search(current):
            blocks.append(current)
            current = ""
    if current:
        blocks.append(current)
    return blocks


def _clean(blocks: List[str]) -> List[str]:
    """Drop empties and collapse runs of intra-block whitespace.

    Kept deliberately shallow: no case changes, no punctuation normalisation, no
    de-duplication. Everything this function touches is whitespace, because anything more
    would be editing the user's document before translating it.
    """
    out = []
    for b in blocks:
        s = re.sub(r"[ \t ]+", " ", (b or "").replace("\r", " ")).strip()
        if s:
            out.append(s)
    return out


# ── .txt ─────────────────────────────────────────────────────────────────────

# UTF-8 first and UTF-8 last is not a typo: the strict pass rejects a file that merely
# looks like UTF-8, letting the legacy encodings have their turn, and the final pass runs
# with errors="replace" so a genuinely broken file still yields text instead of a 500.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-16", "utf-8", "cp1252", "latin-1")


def _extract_txt(raw: bytes) -> Extraction:
    text = None
    for enc in _TEXT_ENCODINGS:
        try:
            text = raw.decode(enc)
            logger.info(f"[DOC] txt decoded as {enc}")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
        logger.warning("[DOC] txt had undecodable bytes — decoded with replacement chars")

    # A file with blank lines is already paragraph-structured, so trust it. One with none
    # is hard-wrapped prose (the classic 72-column .txt), which needs the same rejoining
    # a PDF does — otherwise every wrapped line is translated as its own fragment.
    if re.search(r"\n\s*\n", text):
        blocks = [p for p in re.split(r"\n\s*\n", text)]
        blocks = [re.sub(r"\s*\n\s*", " ", b) for b in blocks]
    else:
        blocks = _reflow(text.split("\n"))
    return Extraction(blocks=_clean(blocks), source="txt")


# ── .docx ────────────────────────────────────────────────────────────────────

def _extract_docx(raw: bytes, source: str = "docx") -> Extraction:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:  # pragma: no cover - dependency is in requirements.txt
        raise DocumentError(f"DOCX support is not installed in this image: {e}")

    try:
        document = docx.Document(io.BytesIO(raw))
    except Exception as e:
        raise DocumentError(
            f"Could not read this .docx — it may be corrupt or password-protected ({e})."
        )

    blocks: List[str] = []
    # Walk body children rather than document.paragraphs, which SKIPS TABLES ENTIRELY.
    # Half the text in a form-shaped government document lives in tables, so reading only
    # the paragraph list silently drops it — and a silent drop is the one failure mode a
    # translator must not have.
    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            blocks.append(Paragraph(child, document).text)
        elif tag == "tbl":
            for row in Table(child, document).rows:
                # Cells joined with " | " so the row stays one translatable unit and the
                # column structure survives into the plain-text output. Translating each
                # cell alone strips the context that makes a one-word cell translatable.
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                line = " | ".join(c for c in cells if c)
                if line:
                    blocks.append(line)
    return Extraction(blocks=_clean(blocks), source=source)


# ── .doc (legacy binary) ─────────────────────────────────────────────────────

def _extract_doc(raw: bytes) -> Extraction:
    """Legacy Word binary → docx via LibreOffice headless, then the docx path.

    There is no pure-Python reader for the pre-2007 binary format that handles Devanagari
    reliably; antiword and catdoc both predate meaningful Unicode support and mangle it.
    LibreOffice is the only fully-offline converter that gets Hindi .doc right, which is
    why the image carries it.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise DocumentError(
            "Legacy .doc conversion is unavailable: LibreOffice is not installed in this "
            "image. Re-save the file as .docx and upload that instead."
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "input.doc")
        with open(src, "wb") as fh:
            fh.write(raw)

        # A private profile dir AND a private HOME. LibreOffice refuses to start when it
        # cannot write a user profile, and in a container HOME is frequently unwritable —
        # this is the usual cause of a converter that works locally and hangs in Docker.
        profile = os.path.join(tmpdir, "loprofile")
        env = {**os.environ, "HOME": tmpdir}
        cmd = [
            soffice,
            f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--nolockcheck", "--nodefault",
            "--convert-to", "docx", "--outdir", tmpdir, src,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=SOFFICE_TIMEOUT, env=env, check=False
            )
        except subprocess.TimeoutExpired:
            raise DocumentError(
                f"Converting this .doc timed out after {SOFFICE_TIMEOUT}s. "
                "Re-save it as .docx and upload that instead."
            )

        out = os.path.join(tmpdir, "input.docx")
        if not os.path.exists(out):
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            raise DocumentError(
                "LibreOffice could not convert this .doc"
                + (f": {detail[:200]}" if detail else ".")
                + " Re-save it as .docx and upload that instead."
            )
        with open(out, "rb") as fh:
            converted = fh.read()

    logger.info("[DOC] .doc converted to .docx via LibreOffice")
    return _extract_docx(converted, source="doc")


# ── .pdf ─────────────────────────────────────────────────────────────────────

def _ocr_page(page, ocr_lang: str) -> str:
    import pytesseract

    # scale is a multiplier on PDF user space, which is 72 dpi — so this renders at
    # OCR_DPI. Devanagari needs the resolution: its matras are one or two pixels tall at
    # 150 dpi and tesseract drops them, turning ो into ा and changing the word.
    bitmap = page.render(scale=OCR_DPI / 72)
    try:
        image = bitmap.to_pil()
        try:
            return pytesseract.image_to_string(image, lang=ocr_lang) or ""
        finally:
            image.close()
    finally:
        bitmap.close()


def _extract_pdf(raw: bytes, ocr_lang: str) -> Extraction:
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # pragma: no cover - dependency is in requirements.txt
        raise DocumentError(f"PDF support is not installed in this image: {e}")

    try:
        pdf = pdfium.PdfDocument(io.BytesIO(raw))
        n_pages = len(pdf)
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise DocumentError("This PDF is password-protected. Upload an unlocked copy.")
        raise DocumentError(f"Could not read this PDF — it may be corrupt ({e}).")

    blocks: List[str] = []
    text_pages = ocr_pages = 0
    ocr_budget_hit = False
    try:
        for index in range(n_pages):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    page_text = textpage.get_text_range() or ""
                finally:
                    textpage.close()

                # The scanned-page test. A scan's text layer is empty or near-empty (a
                # stray header stamped by the scanner), so a low character count is the
                # signal — not zero, which would miss those stamps.
                if len(page_text.strip()) >= MIN_PAGE_TEXT_CHARS:
                    text_pages += 1
                elif not ENABLE_OCR:
                    continue
                elif ocr_pages >= OCR_MAX_PAGES:
                    ocr_budget_hit = True
                    continue
                else:
                    page_text = _ocr_page(page, ocr_lang)
                    ocr_pages += 1
                    logger.info(f"[DOC] page {index + 1}/{n_pages} OCR'd ({len(page_text)} chars)")

                blocks.extend(_reflow(page_text.split("\n")))
            finally:
                page.close()
    finally:
        pdf.close()

    if ocr_budget_hit:
        logger.warning(
            f"[DOC] OCR page budget ({OCR_MAX_PAGES}) reached — later scanned pages skipped"
        )

    blocks = _clean(blocks)
    if not blocks:
        if not ENABLE_OCR:
            raise DocumentError(
                f"No text could be extracted from this PDF ({n_pages} pages). It has no text "
                "layer and OCR is disabled on this service (ENABLE_OCR=false)."
            )
        raise DocumentError(
            f"No text could be extracted from this PDF ({n_pages} pages), with OCR enabled. "
            "The pages are most likely blank, or scans too low-resolution or skewed for OCR "
            "to read."
        )

    source = "pdf-text" if not ocr_pages else ("pdf-ocr" if not text_pages else "pdf-mixed")
    return Extraction(
        blocks=blocks,
        source=source,
        pages=n_pages,
        text_layer_pages=text_pages,
        ocr_pages=ocr_pages,
    )


# ── dispatch ─────────────────────────────────────────────────────────────────

# Magic bytes, checked when the extension is missing or wrong. Uploads routinely arrive
# with a generic name from a mobile client, and a PDF called "scan" should still work.
_MAGIC = (
    (b"%PDF-", ".pdf"),
    (b"PK\x03\x04", ".docx"),          # any OOXML zip; python-docx rejects non-Word ones
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", ".doc"),   # OLE2 compound file
)


def _sniff(raw: bytes) -> Optional[str]:
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    return None


def extract_text_blocks(raw: bytes, filename: str, ocr_lang: str = OCR_LANGS) -> Extraction:
    """Document bytes → Extraction. Raises DocumentError on anything unreadable.

    `ocr_lang` is a tesseract language string ("hin", "eng", "hin+eng"). Naming the one
    language actually expected is materially more accurate than the combined model, so
    the caller passes it whenever the request declared a source language.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    sniffed = _sniff(raw)

    # Trust the magic bytes over the extension when they disagree — a .docx renamed to
    # .doc is common (and the reverse), and dispatching on the name alone fails both.
    if sniffed and sniffed != ext:
        if ext in SUPPORTED_EXTENSIONS:
            logger.warning(f"[DOC] {filename} is named {ext} but its content is {sniffed} — using {sniffed}")
        ext = sniffed
    elif ext not in SUPPORTED_EXTENSIONS:
        # No usable extension and no magic match. Plain text has no magic number, so this
        # is where a .txt with an odd name legitimately lands.
        ext = ".txt"

    if ext == ".pdf":
        return _extract_pdf(raw, ocr_lang)
    if ext == ".docx":
        return _extract_docx(raw)
    if ext == ".doc":
        return _extract_doc(raw)
    return _extract_txt(raw)
