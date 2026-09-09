"""Write the finished MoM into Elasticsearch.

TWO INDICES, SPLIT BY HOW THE JOB ARRIVED — attachments (file_urls) go to one, already-ingested
files (document_ids / file_fids) to the other. That split comes from the backend contract, not from
anything this code needs; indexing by delivery route rather than by content is unusual and worth
revisiting with them, so it lives in config rather than being assumed here.

THE MAPPING BELOW IS OUR BEST GUESS and is meant to be replaced. Their index may already exist with
its own mapping, in which case ensure_indices() should not run at all — set ELASTIC_CREATE_INDICES
to false. It is written out explicitly rather than relying on dynamic mapping because dynamic
mapping would make every string a text+keyword pair and silently turn `decisions` into a full-text
field nobody can aggregate on.
"""
import logging
from typing import Any, Dict

from elasticsearch import Elasticsearch

from config import (ELASTIC_CREATE_INDICES, ELASTIC_INDEX_ATTACHED,
                    ELASTIC_INDEX_INGESTED, ELASTIC_PASSWORD, ELASTIC_URL, ELASTIC_USER)

logger = logging.getLogger(__name__)

# keyword  → exact match, filterable, aggregatable (ids, tenant, owners)
# text     → full-text searchable prose (summary, key points)
# date     → range queries
MOM_MAPPING: Dict[str, Any] = {
    "properties": {
        "tenant_id":       {"type": "keyword"},
        "user_id":         {"type": "keyword"},
        "conversation_id": {"type": "keyword"},
        "document_ids":    {"type": "keyword"},
        "file_fids":       {"type": "keyword"},
        "document_names":  {"type": "keyword"},
        "source":          {"type": "keyword"},   # "attached" | "ingested"
        "indexed_at":      {"type": "date"},
        "title":           {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "summary":         {"type": "text"},
        "key_points":      {"type": "text"},
        "decisions":       {"type": "text"},
        "key_figures":     {"type": "text"},
        "purpose":         {"type": "text"},
        "agenda":          {"type": "text"},
        "attendees": {"type": "nested", "properties": {
            "name": {"type": "keyword"}, "role": {"type": "keyword"}}},
        "action_items": {"type": "nested", "properties": {
            "task":        {"type": "text"},
            "assigned_to": {"type": "keyword"},
            "assigned_by": {"type": "keyword"},
            "due":         {"type": "keyword"}}},
        # Where the generated .docx landed, mirroring the acknowledgement fields.
        "summary_bucket":     {"type": "keyword"},
        "summary_object_key": {"type": "keyword"},
    }
}


class MomIndex:
    def __init__(self):
        auth = (ELASTIC_USER, ELASTIC_PASSWORD) if ELASTIC_USER else None
        self.client = Elasticsearch(ELASTIC_URL, basic_auth=auth, request_timeout=30)

    def is_ready(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception as e:
            logger.warning(f"[ELASTIC] not reachable at {ELASTIC_URL}: {e}")
            return False

    def ensure_indices(self):
        """Create both indices if missing. No-op when they own the schema."""
        if not ELASTIC_CREATE_INDICES:
            return
        for name in (ELASTIC_INDEX_ATTACHED, ELASTIC_INDEX_INGESTED):
            if not self.client.indices.exists(index=name):
                self.client.indices.create(index=name, mappings=MOM_MAPPING)
                logger.info(f"[ELASTIC] created index {name!r}")

    def index_mom(self, job, mom: Dict[str, Any], *, source: str,
                  summary_bucket: str = "", summary_object_key: str = "") -> tuple:
        """Write one MoM. Returns (index, _id).

        The document id is the conversation id where there is one, otherwise the joined
        document_ids. Kafka is at-least-once, so a redelivered message MUST overwrite rather than
        create a second copy — a stable id is what makes the write idempotent.
        """
        import datetime
        index = ELASTIC_INDEX_ATTACHED if source == "attached" else ELASTIC_INDEX_INGESTED
        doc_id = job.conversation_id or "-".join(job.document_ids) or None
        body = {
            "tenant_id": job.tenant_id, "user_id": job.user_id,
            "conversation_id": job.conversation_id,
            "document_ids": job.document_ids, "file_fids": job.file_fids,
            "document_names": job.document_names, "source": source,
            "indexed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "title": mom.get("title", ""), "summary": mom.get("summary", ""),
            "key_points": mom.get("key_points", []), "decisions": mom.get("decisions", []),
            "key_figures": mom.get("key_figures", []), "purpose": mom.get("purpose", ""),
            "agenda": mom.get("agenda", []), "attendees": mom.get("attendees", []),
            "action_items": mom.get("action_items", []),
            "summary_bucket": summary_bucket, "summary_object_key": summary_object_key,
        }
        self.client.index(index=index, id=doc_id, document=body, refresh=True)
        logger.info(f"[ELASTIC] indexed {index}/{doc_id} ({source})")
        return index, doc_id
