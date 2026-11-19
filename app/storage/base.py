"""Storage abstraction for document files.

Every backend must encrypt at rest. Dropbox and local storage encrypt in the
portal before writing (app/storage/crypto.py); S3 uses SSE-KMS. A new backend
only needs to implement these three methods.
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
