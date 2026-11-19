"""Local filesystem storage, encrypted with the shared envelope scheme.

The file name is a hash of the storage key, so nothing about the document is
visible on disk. Meant for development and single-server installs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.storage.base import StorageBackend, StorageError
from app.storage.crypto import Envelope, decode_key, generate_key

__all__ = ["LocalEncryptedStorage", "decode_key", "generate_key"]


class LocalEncryptedStorage(StorageBackend):
    name = "local"

    def __init__(self, root: str | Path, master_key: bytes) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._env = Envelope(master_key)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / digest[:2] / f"{digest}.bin"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._env.seal(key, data))
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise StorageError("object not found")
        return self._env.open(key, path.read_bytes())

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
