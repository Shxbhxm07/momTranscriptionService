"""Parse the inbound Kafka job and build the acknowledgement.

Kept separate from any Kafka client on purpose: this module is pure data-in/data-out, so the
message shape can be tested without a broker, and swapping the client later touches nothing here.

TWO NAMING CONVENTIONS, DELIBERATELY TOLERATED. The inbound message is mostly snake_case
(tenant_id, file_urls, compare_mode) but not entirely — conversationId and userId are camelCase in
the very same object — while the acknowledgement is camelCase throughout (tenantId, fileIds,
compareMode). Reading both spellings costs one helper and removes a whole class of silent
mis-wiring, where a renamed field simply arrives as None and the job processes the wrong thing.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List


def _get(msg: Dict[str, Any], *names, default=None):
    """First present key among `names`, tolerating snake_case / camelCase drift."""
    for n in names:
        if msg.get(n) not in (None, "", []):
            return msg[n]
    return default


@dataclass
class KafkaJob:
    tenant_id: str = ""
    user_id: str = ""
    conversation_id: str = ""
    mode: str = ""
    compare_mode: str = ""
    # Correlation key echoed back as "fileIds" in the ack. Confirmed against the reference pair:
    # the ack's fileIds carried document_ids ("comparison1"), NOT file_fids ("FileOne0").
    document_ids: List[str] = field(default_factory=list)
    # MinIO OBJECT KEYS, not URLs — "docutalk/doccomparecheck1/9f3a..." has no scheme or host, so
    # these need credentials rather than being fetchable as-is.
    file_urls: List[str] = field(default_factory=list)
    file_fids: List[str] = field(default_factory=list)
    document_names: List[str] = field(default_factory=list)
    # Optional details for the Word minutes that no recording contains: classification, file
    # reference, address, secretary, distribution and so on (see docs/kafka-contract.md).
    mom_meta: Dict[str, Any] = field(default_factory=dict)
    # The backend's own fields, kept EXACTLY as they arrived (same value, same type) so the ack can
    # hand them back untouched. Never parsed, never normalised — `accessVar` in particular is an
    # opaque credential-shaped string and `isUser` is a boolean the backend may also send as text.
    echo: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_attachments(self) -> bool:
        return bool(self.file_urls)

    @property
    def has_ingested(self) -> bool:
        return bool(self.file_fids or self.document_ids)


# Sent by the backend, returned by the ack unchanged. Anything not present stays absent.
ECHO_FIELDS = ("accessVar", "userId", "isUser", "path", "conversationId")


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _paths(msg: Dict[str, Any]) -> List[str]:
    """The audio to fetch, from `file_urls` (reference shape) or `path` (the newer backend)."""
    urls = list(_get(msg, "file_urls", "fileUrls", default=[]) or [])
    if urls:
        return urls
    single = _get(msg, "path", default="")
    return [single] if isinstance(single, str) and single.strip() else []


def parse_job(msg: Dict[str, Any]) -> KafkaJob:
    return KafkaJob(
        tenant_id=_get(msg, "tenant_id", "tenantId", default="") or "",
        user_id=_get(msg, "userId", "user_id", default="") or "",
        conversation_id=_get(msg, "conversationId", "conversation_id", default="") or "",
        mode=_get(msg, "mode", default="") or "",
        compare_mode=_get(msg, "compare_mode", "compareMode", default="") or "",
        document_ids=list(_get(msg, "document_ids", "documentIds", default=[]) or []),
        # `path` is the newer backend's single audio location; file_urls is the reference shape.
        # Either is accepted, so a message from before or after that change processes the same.
        file_urls=_paths(msg),
        file_fids=list(_get(msg, "file_fids", "fileFids", "fIds", default=[]) or []),
        document_names=list(_get(msg, "document_names", "documentNames", default=[]) or []),
        mom_meta=_dict(_get(msg, "mom_meta", "momMeta", default={})),
        echo={k: msg[k] for k in ECHO_FIELDS if k in msg},
        raw=msg,
    )


# The failure description has no length cap but is required to be "short and clear", so a long
# Python traceback is truncated to one readable sentence rather than pasted in whole.
_MAX_DESCRIPTION = 400


def build_ack(job: KafkaJob, *, success: bool, description: str,
              bucket: str = "", object_key: str = "", action: str = "save") -> Dict[str, Any]:
    """The acknowledgement, in the camelCase shape the reference ack uses.

    On failure the bucket/key are omitted rather than sent empty: an empty objectKey reads as
    "there is a file at ''" to anything that tries to fetch it.
    """
    desc = " ".join((description or "").split())
    if len(desc) > _MAX_DESCRIPTION:
        desc = desc[:_MAX_DESCRIPTION - 1].rsplit(" ", 1)[0] + "…"
    # The backend's own fields come back on every ack, success or failure, so it can match the
    # answer to the request. Three are returned untouched; two are ours to fill in, which is what
    # the backend asked for by sending them empty:
    #   path           where the minutes were stored, "bucket/key", the same shape as the audio
    #                  path it sends us. Left as received when there is no file (a failure).
    #   conversationId the job's id, which also names the Elasticsearch document.
    echo = dict(job.echo)
    if "conversationId" in echo or job.conversation_id:
        echo["conversationId"] = job.conversation_id or echo.get("conversationId", "")
    if "path" in echo and success and bucket and object_key:
        echo["path"] = f"{bucket}/{object_key}"

    ack: Dict[str, Any] = {
        **echo,
        "fileIds": job.document_ids,
        "tenantId": job.tenant_id,
        "compareMode": job.compare_mode,
        "action": action,
        "message": "SUCCESS" if success else "FAILURE",
        "description": desc,
    }
    if success and bucket and object_key:
        ack["summaryBucketName"] = bucket
        ack["summaryObjectKey"] = object_key
    return ack


def summary_object_key(job: KafkaJob, digest: str, ext: str = "docx") -> str:
    """Tenant-scoped path, matching the reference: {tenantId}/summaries/{hash}.{ext}

    The newer backend sends no tenant, and "" would produce a key starting with "/" — a folder
    named nothing, at the bucket root. The conversation id takes its place, then the user id, and
    "shared" only if a message carried none of the three.
    """
    scope = (job.tenant_id or job.conversation_id or job.user_id or "shared").strip("/") or "shared"
    return f"{scope}/summaries/{digest}.{ext}"
