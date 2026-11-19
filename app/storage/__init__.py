from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.storage.base import StorageBackend, StorageError

__all__ = ["StorageBackend", "StorageError", "get_storage"]


def _master_key() -> bytes:
    from app.storage.crypto import DEV_KEY, decode_key

    s = get_settings()
    if s.storage_encryption_key:
        return decode_key(s.storage_encryption_key)
    if s.is_production:  # pragma: no cover - blocked by settings validation too
        raise StorageError("STORAGE_ENCRYPTION_KEY is required")
    return DEV_KEY  # development convenience only


@lru_cache
def get_storage() -> StorageBackend:
    s = get_settings()
    if s.storage_backend == "s3":
        from app.storage.s3 import S3Storage

        return S3Storage(
            s.s3_bucket,
            s.s3_kms_key_id,
            region=s.s3_region,
            prefix=s.s3_prefix,
            endpoint_url=s.s3_endpoint_url,
        )
    from app.storage.local import LocalEncryptedStorage

    return LocalEncryptedStorage(s.local_storage_dir, _master_key())
