"""Render the MoM response object as a Word document.

WHY THIS EXISTS: the Kafka contract does not return minutes over HTTP. The consumer uploads a
.docx to MinIO and sends back only the bucket and object key, so a Word file — not JSON — is the
deliverable for that path. The HTTP API is unchanged and still returns JSON.

Section order mirrors llama-service's text renderer (localization/mom_i18n.render_mom) so the two
outputs cannot drift into describing the same meeting differently. `speaker_notes` is absent here
because the HTTP contract does not expose it — it exists only inside `formatted`.
"""
import io
from typing import Any, Dict

import docx
from docx.shared import Pt


def _heading(doc, text):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(12)
    return p


def _bullets(doc, items):
    """One bullet per item, or a single 'none' line — never an empty section.

    An empty heading reads as data loss; an explicit "None explicitly stated." reads as a
    finding, which is what the text renderer already does for every list section.
    """
    if not items:
        doc.add_paragraph("None explicitly stated.", style="List Bullet")
        return
    for it in items:
        doc.add_paragraph(str(it), style="List Bullet")


def build_mom_docx(mom: Dict[str, Any], meeting_date: str = "") -> bytes:
    """MoM response object → .docx bytes, ready to upload."""
    doc = docx.Document()
    doc.add_heading(mom.get("title") or "Minutes of Meeting", level=0)
    if meeting_date:
        doc.add_paragraph(f"Date: {meeting_date}")

    if mom.get("purpose"):
        _heading(doc, "PURPOSE OF MEETING")
        doc.add_paragraph(mom["purpose"])

    _heading(doc, "AGENDA")
    _bullets(doc, mom.get("agenda") or [])

    _heading(doc, "ATTENDEES")
    att = []
    for a in mom.get("attendees") or []:
        if isinstance(a, dict):
            name, role = a.get("name", ""), a.get("role", "")
            att.append(f"{name} — {role}" if role and role != "Unknown" else name)
        else:
            att.append(str(a))
    _bullets(doc, att)

    _heading(doc, "SUMMARY")
    doc.add_paragraph(mom.get("summary") or "None explicitly stated.")

    _heading(doc, "KEY DISCUSSION POINTS")
    _bullets(doc, mom.get("key_points") or [])

    # Self-conditional, exactly as the text renderer treats it: most meetings have no itemised
    # figures and an empty "KEY FIGURES: None" heading would be noise.
    if mom.get("key_figures"):
        _heading(doc, "KEY FIGURES")
        _bullets(doc, mom["key_figures"])

    _heading(doc, "DECISIONS TAKEN")
    _bullets(doc, mom.get("decisions") or [])

    _heading(doc, "ACTION ITEMS")
    items = mom.get("action_items") or []
    if not items:
        doc.add_paragraph("None explicitly stated.", style="List Bullet")
    for ai in items:
        if not isinstance(ai, dict):
            doc.add_paragraph(str(ai), style="List Bullet")
            continue
        doc.add_paragraph(ai.get("task", ""), style="List Bullet")
        for label, key in (("Assigned to", "assigned_to"), ("Assigned by", "assigned_by"), ("Due on", "due")):
            if (ai.get(key) or "").strip():
                doc.add_paragraph(f"{label}: {ai[key]}")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
