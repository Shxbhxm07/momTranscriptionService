"""MinIO object storage — fetch the input audio, store the generated minutes.

OBJECT KEYS, NOT URLS. The Kafka message calls them `file_urls`, but the values have no scheme and
no host — "docutalk/doccomparecheck1/9f3adfa7-..." is a bucket followed by a key. Nothing can fetch
that without credentials, so this module resolves the pair rather than treating it as a URL.

WHERE THE BUCKET COMES FROM is deliberately configurable, because the reference message is
ambiguous about it. With MINIO_INPUT_BUCKET set, the whole path is the key inside that bucket; with
it unset, the FIRST SEGMENT is read as the bucket. Getting this backwards produces a confusing
NoSuchKey rather than an obvious error, so it is a setting rather than a guess baked into code.
"""
import io
import os
import logging
from typing import Tuple

from minio import Minio
from minio.error import S3Error

from config import (MINIO_ACCESS_KEY, MINIO_ENDPOINT, MINIO_INPUT_BUCKET,
                    MINIO_SECRET_KEY, MINIO_SECURE, MINIO_SUMMARY_BUCKET)

logger = logging.getLogger(__name__)


def split_object_path(path: str) -> Tuple[str, str]:
    """'docutalk/prefix/uuid' → ('docutalk', 'prefix/uuid'), unless a bucket is configured."""
    path = (path or "").lstrip("/")
    if MINIO_INPUT_BUCKET:
        return MINIO_INPUT_BUCKET, path
    bucket, _, key = path.partition("/")
    return bucket, key


class ObjectStore:
    """Thin MinIO wrapper. Constructed once; the client holds its own connection pool."""

    def __init__(self):
        self.client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
                            secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)

    def is_ready(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except Exception as e:
            logger.warning(f"[MINIO] not reachable at {MINIO_ENDPOINT}: {e}")
            return False

    def download(self, object_path: str) -> bytes:
        """Fetch one object by its 'bucket/key' path. Raises on anything missing."""
        bucket, key = split_object_path(object_path)
        if not bucket or not key:
            raise ValueError(f"cannot resolve bucket/key from {object_path!r}")
        resp = None
        try:
            resp = self.client.get_object(bucket, key)
            data = resp.read()
        except S3Error as e:
            # Name the bucket and key: "NoSuchKey" alone cannot tell you whether the path was
            # split wrongly or the object genuinely is not there.
            raise RuntimeError(f"MinIO get failed for bucket={bucket!r} key={key!r}: {e.code}") from e
        finally:
            if resp is not None:
                resp.close(); resp.release_conn()
        logger.info(f"[MINIO] ↓ {bucket}/{key} ({len(data)} bytes)")
        return data

    def download_to_file(self, object_path: str, dest_path: str) -> int:
        """Stream one object to a local file and return its size — for video, which can be gigabytes.

        download() returns bytes, which is right for a meeting recording and wrong for a video: the
        whole file would sit in the consumer's memory and then be copied again into the request body.
        This writes it to disk in chunks and hands back only the size.
        """
        bucket, key = split_object_path(object_path)
        if not bucket or not key:
            raise ValueError(f"cannot resolve bucket/key from {object_path!r}")
        self.client.fget_object(bucket, key, dest_path)
        return os.path.getsize(dest_path)

    def upload(self, key: str, data: bytes, content_type: str, bucket: str = "") -> Tuple[str, str]:
        """Store bytes and return (bucket, key) for the acknowledgement."""
        bucket = bucket or MINIO_SUMMARY_BUCKET
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)
            logger.info(f"[MINIO] created bucket {bucket!r}")
        self.client.put_object(bucket, key, io.BytesIO(data), length=len(data),
                               content_type=content_type)
        logger.info(f"[MINIO] ↑ {bucket}/{key} ({len(data)} bytes)")
        return bucket, key
