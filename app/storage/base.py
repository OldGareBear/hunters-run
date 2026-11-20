"""Storage abstraction for document files.

Used for documents uploaded through the portal (DOCUMENT_SOURCE=upload).
Documents synced from Dropbox are read from Dropbox directly (app/dropbox.py).

Every backend must encrypt at rest. Local storage encrypts in the portal before
writing (app/storage/crypto.py); S3 uses SSE-KMS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class StorageError(Exception):
    pass


class StorageBackend(ABC):
    name: str = "base"

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...
