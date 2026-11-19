from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.storage.base import StorageBackend, StorageError

__all__ = ["StorageBackend", "StorageError", "get_storage"]


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
    from app.storage.local import LocalEncryptedStorage, decode_key

    if not s.local_storage_key:
        if s.is_production:  # pragma: no cover - blocked by settings validation too
            raise StorageError("LOCAL_STORAGE_KEY is required")
        # Development convenience: a fixed, clearly-insecure key.
        key = b"dev-only-insecure-storage-key-32"
    else:
        key = decode_key(s.local_storage_key)
    return LocalEncryptedStorage(s.local_storage_dir, key)
