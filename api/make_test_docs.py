"""Generate the test documents for test_translate.sh, in every supported format.

    python3 make_test_docs.py [out_dir]        # default ./samples/documents

Fixtures are generated rather than committed because they are binary, derivable, and
several megabytes of it. Everything is produced from two source documents — one English,
one Hindi — so the same text arrives through all four readers and any difference in the
output is the READER's, not the content's.

    .docx   written directly (includes a table, which is where a naive reader loses text)
    .doc    LibreOffice, docx → legacy binary
    .pdf    LibreOffice, docx → PDF with a real text layer
    .txt    written directly
    *_scanned.pdf   each PDF page rendered to a 300 dpi image and rebuilt as an
                    image-only PDF — a TRUE scan with a zero-character text layer, which
                    is the only way to exercise the OCR path honestly

Needs LibreOffice on PATH for .doc/.pdf; without it the other formats are still written.
"""
import os
import shutil
import subprocess
import sys

ENGLISH_TITLE = "Quarterly Procurement Review"
ENGLISH_PARAGRAPHS = [
    "The Ministry of Finance has issued new guidelines for the procurement of goods and "
    "services by all central government departments, effective from the next financial year.",
    "Under the revised framework, every purchase above five lakh rupees requires a "
    "competitive bidding process with at least three qualified vendors.",
    "Quarterly compliance reports shall be submitted to the Ministry by the fifteenth day "
    "of the month following the end of each quarter.",
]
ENGLISH_TABLE = [
    ("Category", "Threshold", "Approver"),
    ("Goods", "5,00,000", "Joint Secretary"),
    ("Services", "10,00,000", "Additional Secretary"),
]

HINDI_TITLE = "तिमाही खरीद समीक्षा"
HINDI_PARAGRAPHS = [
    "वित्त मंत्रालय ने सभी केंद्रीय सरकारी विभागों द्वारा वस्तुओं और सेवाओं की खरीद के लिए नए "
    "दिशानिर्देश जारी किए हैं, जो अगले वित्तीय वर्ष से प्रभावी होंगे।",
    "संशोधित ढांचे के तहत, पांच लाख रुपये से अधिक की प्रत्येक खरीद के लिए कम से कम तीन योग्य "
    "विक्रेताओं के साथ प्रतिस्पर्धी बोली प्रक्रिया आवश्यक है।",
    "त्रैमासिक अनुपालन रिपोर्ट प्रत्येक तिमाही की समाप्ति के बाद अगले महीने की पंद्रह तारीख तक "
    "प्रस्तुत की जाएगी।",
]


def write_docx(path, title, paragraphs, table=None):
    import docx

    document = docx.Document()
    document.add_heading(title, level=1)
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row, values in zip(grid.rows, table):
            for cell, value in zip(row.cells, values):
                cell.text = value
    document.save(path)
    return path


def write_txt(path, title, paragraphs):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(title + "\n\n" + "\n\n".join(paragraphs) + "\n")
    return path


def convert(soffice, src, fmt, out_dir):
    """LibreOffice conversion. HOME is redirected because LibreOffice refuses to start
    when it cannot write a user profile — the usual reason this works on a laptop and
    hangs in a container."""
    subprocess.run(
        [soffice, f"-env:UserInstallation=file://{out_dir}/.loprofile",
         "--headless", "--norestore", "--convert-to", fmt, "--outdir", out_dir, src],
        capture_output=True, timeout=300, check=False, env={**os.environ, "HOME": out_dir},
    )
    produced = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + "." + fmt)
    return produced if os.path.exists(produced) else None


def rasterize(pdf_path, out_path, dpi=300):
    """PDF → image-only PDF, i.e. a synthetic scan with no text layer at all."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    images = []
    try:
        for index in range(len(pdf)):
            bitmap = pdf[index].render(scale=dpi / 72)
            images.append(bitmap.to_pil().convert("RGB"))
    finally:
        pdf.close()
    images[0].save(out_path, save_all=True, append_images=images[1:], resolution=dpi)
    for image in images:
        image.close()
    return out_path


def main():
    out_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "./samples/documents")
    os.makedirs(out_dir, exist_ok=True)

    written = [
        write_docx(f"{out_dir}/english.docx", ENGLISH_TITLE, ENGLISH_PARAGRAPHS, ENGLISH_TABLE),
        write_docx(f"{out_dir}/hindi.docx", HINDI_TITLE, HINDI_PARAGRAPHS),
        write_txt(f"{out_dir}/english.txt", ENGLISH_TITLE, ENGLISH_PARAGRAPHS),
        write_txt(f"{out_dir}/hindi.txt", HINDI_TITLE, HINDI_PARAGRAPHS),
    ]

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print("LibreOffice not found — skipping .doc, .pdf and the scanned PDFs.")
    else:
        for name in ("english", "hindi"):
            docx_path = f"{out_dir}/{name}.docx"
            for fmt in ("pdf", "doc"):
                produced = convert(soffice, docx_path, fmt, out_dir)
                if produced:
                    written.append(produced)
                else:
                    print(f"  ! LibreOffice could not produce {name}.{fmt}")
            pdf_path = f"{out_dir}/{name}.pdf"
            if os.path.exists(pdf_path):
                try:
                    written.append(rasterize(pdf_path, f"{out_dir}/{name}_scanned.pdf"))
                except Exception as e:
                    print(f"  ! could not rasterize {name}.pdf: {e}")
        shutil.rmtree(f"{out_dir}/.loprofile", ignore_errors=True)

    for path in written:
        print(f"  {os.path.getsize(path):>9,} B  {os.path.basename(path)}")
    print(f"\n{len(written)} document(s) in {out_dir}")


if __name__ == "__main__":
    main()
