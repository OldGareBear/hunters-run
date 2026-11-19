"""Storage abstraction for document files.

Every backend must encrypt at rest. S3 uses SSE-KMS; the local backend uses
envelope encryption with AES-256-GCM. A Dropbox (or other) backend only needs to
implement these three methods.
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
