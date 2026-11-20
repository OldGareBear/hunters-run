"""Envelope encryption for files the portal stores itself (the local backend).

Each object gets a random 256-bit data key. The data key is wrapped with the
master key (STORAGE_ENCRYPTION_KEY) and both steps use AES-256-GCM with the
storage key as associated data, so a blob can't be swapped onto another document.

Blob layout: b"HOA1" | wrap_nonce(12) | wrapped_dek(48) | data_nonce(12) | ciphertext
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.storage.base import StorageError

MAGIC = b"HOA1"
DEV_KEY = b"dev-only-insecure-storage-key-32"


def decode_key(key_b64: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(key_b64.encode())
    except Exception as exc:  # pragma: no cover - defensive
        raise StorageError("STORAGE_ENCRYPTION_KEY is not valid base64") from exc
    if len(key) != 32:
        raise StorageError("STORAGE_ENCRYPTION_KEY must decode to 32 bytes")
    return key


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class Envelope:
    def __init__(self, master_key: bytes) -> None:
        self._master = AESGCM(master_key)

    def seal(self, key: str, data: bytes) -> bytes:
        dek = AESGCM.generate_key(bit_length=256)
        wrap_nonce, data_nonce = os.urandom(12), os.urandom(12)
        aad = key.encode()
        wrapped = self._master.encrypt(wrap_nonce, dek, aad)
        return (
            MAGIC + wrap_nonce + wrapped + data_nonce + AESGCM(dek).encrypt(data_nonce, data, aad)
        )

    def open(self, key: str, blob: bytes) -> bytes:
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
