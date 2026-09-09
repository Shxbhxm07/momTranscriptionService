"""
Speaker Profiles Database
Qdrant vector database storage for enrolled speaker voice embeddings.

Schema / Payload Mapping
─────────────────────────────────────────────────────────────────────
Field          Type      Index     Description
─────────────────────────────────────────────────────────────────────
speaker_id     keyword   ✓         UUID shared across all samples for a speaker
name           keyword   ✓         Display name (case-sensitive)
sample_index   integer   ✓         1-based sample counter per speaker
quality_score  float     ✓         Cosine similarity from enrollment quality check
audio_file     keyword   ✓         Relative path inside SAMPLES_DIR (for playback)
enrolled_at    datetime  ✓         ISO-8601 UTC timestamp of this sample
─────────────────────────────────────────────────────────────────────
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue,
    FilterSelector, PayloadSchemaType,
)

logger = logging.getLogger(__name__)

QDRANT_URL      = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "speaker_profiles"
VECTOR_SIZE     = 192   # TitaNet Large d-vector output dimension
THRESHOLD       = 0.75  # confirmed-match bar (single source of truth). 0.60 was too low: a DIFFERENT
                        # speaker in the SAME room/mic cross-matched an enrolled voice at 0.686 and wrongly
                        # got their name. Genuine same-channel matches score 0.87-0.96, false ones <=0.69,
                        # so 0.75 sits in the gap — an uncertain cluster stays speaker_N instead of borrowing
                        # a name. (Proper long-term fix for the fragile fixed bar is AS-Norm.)

# Enrolment consistency bar — a DIFFERENT question from THRESHOLD above. This one asks "are these
# 3 takes the same voice?" while THRESHOLD asks "is this cluster an enrolled person?".
#
# 0.70 (was 0.90): the whole-file embedding includes silence/noise (no VAD), which drags
# same-speaker consistency down to ~0.7-0.85 on real mics — 0.90 rejected legit enrolments
# (measured: a genuine 3rd Nikhil sample scored 0.72). Different speakers still score < ~0.6, so
# 0.70 keeps the "same voice" guarantee. Proper fix is VAD on the enrolment embedding.
#
# Lives here, and is returned by /speakers/compare, because the enrolment SCREEN also needs it to
# draw the pass/fail bar. It used to be copy-pasted into SpeakerEnrollment.jsx with a comment
# saying "must match nemo-service" — a comment doing a computer's job. Change it here only.
ENROLL_CONSISTENCY_THRESHOLD = 0.70


def _client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def init_speaker_db():
    """
    Create the Qdrant collection and payload indexes if they do not already exist.
    Safe to call on every startup.
    """
    client = _client()
    existing = {c.name for c in client.get_collections().collections}

    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
                on_disk=False,
            ),
        )
        logger.info("✓ Qdrant collection '%s' created (size=%d, Cosine)", COLLECTION_NAME, VECTOR_SIZE)

    _ensure_indexes(client)
    logger.info("✓ Payload indexes ready")


def _ensure_indexes(client: QdrantClient):
    indexes = [
        ("speaker_id",    PayloadSchemaType.KEYWORD),
        ("name",          PayloadSchemaType.KEYWORD),
        ("sample_index",  PayloadSchemaType.INTEGER),
        ("quality_score", PayloadSchemaType.FLOAT),
        ("audio_file",    PayloadSchemaType.KEYWORD),
        ("enrolled_at",   PayloadSchemaType.DATETIME),
    ]
    for field_name, field_type in indexes:
        try:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field_name,
                field_schema=field_type,
            )
        except Exception:
            pass  # already exists


def enroll_speaker(name: str, embedding: np.ndarray,
                   quality_score: float = 0.0,
                   audio_file: str = "",
                   speaker_id: Optional[str] = None) -> str:
    """
    Add a new embedding sample for a speaker. Returns speaker_id (UUID).
    audio_file is a relative path inside SAMPLES_DIR for playback.
    speaker_id: for a NEW speaker, use this id if provided so the caller's saved-sample path and
    the stored payload share ONE id (ignored when the speaker already exists).
    """
    client = _client()

    existing, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )

    if existing:
        speaker_id = existing[0].payload["speaker_id"]
        sample_count = client.count(
            collection_name=COLLECTION_NAME,
            count_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
        ).count + 1
        logger.info("Adding sample %d for existing speaker '%s'", sample_count, name)
    else:
        speaker_id = speaker_id or str(uuid.uuid4())
        sample_count = 1
        logger.info("Enrolling new speaker '%s' (id=%s)", name, speaker_id)

    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding.tolist(),
                payload={
                    "speaker_id":    speaker_id,
                    "name":          name,
                    "sample_index":  sample_count,
                    "quality_score": round(float(quality_score), 4),
                    "audio_file":    audio_file,
                    "enrolled_at":   now_iso,
                },
            )
        ],
    )
    return speaker_id


def get_speaker_embeddings_by_name(name: str) -> List[np.ndarray]:
    """Return all stored embedding vectors for a speaker (for quality comparison)."""
    client = _client()
    points, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
        limit=100,
        with_payload=False,
        with_vectors=True,
    )
    return [np.array(p.vector, dtype=np.float32) for p in points if p.vector]


def get_all_speakers() -> List[dict]:
    """Return all unique enrolled speakers, deduplicated by name."""
    client = _client()
    all_points, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        limit=10000,
        with_payload=True,
        with_vectors=False,
    )

    seen: dict = {}
    for point in all_points:
        p = point.payload
        name = p["name"]
        if name not in seen:
            seen[name] = {
                "id":           p["speaker_id"],
                "name":         name,
                "sample_count": 0,
                "enrolled_at":  p.get("enrolled_at", ""),
            }
        seen[name]["sample_count"] += 1

    return list(seen.values())


def get_all_speaker_embeddings() -> List[dict]:
    """Return all stored embedding points including vectors."""
    client = _client()
    all_points, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        limit=10000,
        with_payload=True,
        with_vectors=True,
    )
    return [
        {
            "id":        p.payload["speaker_id"],
            "name":      p.payload["name"],
            "embedding": np.array(p.vector),
        }
        for p in all_points
    ]


def search_similar_speakers(embedding: np.ndarray, top_k: int = 50) -> List[dict]:
    """
    Query Qdrant for the most similar stored speaker embeddings.
    Returns [{name, speaker_id, score, score_pct, passed, quality_score}] sorted desc.
    Groups by name — keeps best score per speaker.
    """
    client = _client()

    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding.tolist(),
        limit=top_k,
        with_payload=True,
    )
    results = response.points

    best: dict = {}
    for r in results:
        name = r.payload["name"]
        if name not in best or r.score > best[name]["score"]:
            best[name] = {
                "name":          name,
                "speaker_id":    r.payload["speaker_id"],
                "score":         r.score,
                "quality_score": r.payload.get("quality_score", 0.0),
                "sample_index":  r.payload.get("sample_index", 1),
                "enrolled_at":   r.payload.get("enrolled_at", ""),
            }

    return sorted(
        [
            {
                **v,
                "score":     round(v["score"], 4),
                "score_pct": round(max(v["score"], 0.0) * 100, 1),
                "passed":    v["score"] >= THRESHOLD,
            }
            for v in best.values()
        ],
        key=lambda x: x["score"],
        reverse=True,
    )


def get_speaker_samples(name: str) -> List[dict]:
    """Return all stored samples (with audio_file and quality_score) for a speaker."""
    client = _client()
    points, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
        limit=100,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return []
    return sorted(
        [
            {
                "sample_index":  p.payload.get("sample_index", 0),
                "quality_score": p.payload.get("quality_score", None),
                "audio_file":    p.payload.get("audio_file", ""),
                "enrolled_at":   p.payload.get("enrolled_at", ""),
            }
            for p in points
        ],
        key=lambda x: x["sample_index"],
    )


def delete_speaker(speaker_id: str) -> bool:
    """Delete all embedding points for a speaker by UUID."""
    client = _client()
    count = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(must=[FieldCondition(key="speaker_id", match=MatchValue(value=speaker_id))]),
    ).count
    if count == 0:
        return False
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="speaker_id", match=MatchValue(value=speaker_id))])
        ),
    )
    logger.info("Deleted %d embedding(s) for speaker_id=%s", count, speaker_id)
    return True


def delete_speaker_by_name(name: str) -> tuple:
    """
    Delete all embedding points for a speaker by display name.
    Returns (success: bool, speaker_id: str).
    """
    client = _client()
    existing, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not existing:
        return False, ""

    speaker_id = existing[0].payload.get("speaker_id", "")
    count = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
    ).count

    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))])
        ),
    )
    logger.info("Deleted %d embedding(s) for speaker '%s'", count, name)
    return True, speaker_id


def reset_all_speakers() -> int:
    """Delete ALL speaker profiles. Returns count of deleted points."""
    client = _client()
    count = client.count(collection_name=COLLECTION_NAME).count
    client.delete_collection(COLLECTION_NAME)
    init_speaker_db()
    logger.info("Reset: deleted %d speaker embedding(s)", count)
    return count


def get_speaker_by_name(name: str) -> Optional[dict]:
    """Return speaker metadata (no embedding) by display name."""
    client = _client()
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[FieldCondition(key="name", match=MatchValue(value=name))]),
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        return None
    return {
        "id":           results[0].payload["speaker_id"],
        "name":         name,
        "sample_count": len(results),
    }
