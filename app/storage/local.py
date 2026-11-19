"""Local filesystem storage with envelope encryption (AES-256-GCM).

Each file gets a random data key; the data key is wrapped with the master key
(LOCAL_STORAGE_KEY). The file name is the storage key's hash, so nothing about
the document is visible on disk.

File layout: b"HOA1" | wrap_nonce(12) | wrapped_dek(48) | data_nonce(12) | ciphertext
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.storage.base import StorageBackend, StorageError

MAGIC = b"HOA1"


def decode_key(key_b64: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(key_b64.encode())
    except Exception as exc:  # pragma: no cover - defensive
        raise StorageError("LOCAL_STORAGE_KEY is not valid base64") from exc
    if len(key) != 32:
        raise StorageError("LOCAL_STORAGE_KEY must decode to 32 bytes")
    return key


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class LocalEncryptedStorage(StorageBackend):
    name = "local"

    def __init__(self, root: str | Path, master_key: bytes) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._master = AESGCM(master_key)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / digest[:2] / f"{digest}.bin"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        dek = AESGCM.generate_key(bit_length=256)
        wrap_nonce, data_nonce = os.urandom(12), os.urandom(12)
        aad = key.encode()
        wrapped = self._master.encrypt(wrap_nonce, dek, aad)
        ciphertext = AESGCM(dek).encrypt(data_nonce, data, aad)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(MAGIC + wrap_nonce + wrapped + data_nonce + ciphertext)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise StorageError("object not found")
        blob = path.read_bytes()
        if blob[:4] != MAGIC:
            raise StorageError("unrecognised file format")
        wrap_nonce, wrapped = blob[4:16], blob[16:64]
        data_nonce, ciphertext = blob[64:76], blob[76:]
        aad = key.encode()
        try:
            dek = self._master.decrypt(wrap_nonce, wrapped, aad)
            return AESGCM(dek).decrypt(data_nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise StorageError("decryption failed (wrong key or tampered file)") from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
