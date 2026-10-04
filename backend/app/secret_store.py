"""Authenticated encryption envelope for small user-owned API secrets.

Uses AES-256-GCM (a vetted AEAD construction) rather than a hand-rolled
HMAC-stream cipher: AES-GCM's confidentiality/integrity have been through
extensive public cryptanalysis, avoiding the subtle failure modes a
custom encrypt-then-MAC scheme can hide (nonce-reuse tracking, an
unvetted KDF, etc). The encryption key is derived from `settings.
auth_secret` via HKDF with a purpose-specific label, so a future need to
rotate or scope this key doesn't require touching session-token signing,
which reuses the same top-level secret for a different purpose.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from .config import settings

_NONCE_SIZE = 12  # AES-GCM's standard, recommended nonce size


def _derive_key() -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"gitwalk-secret-store-v2",
    ).derive(settings.auth_secret.encode("utf-8"))


def encrypt_secret(value: str) -> str:
    key = _derive_key()
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(envelope: str) -> str:
    packed = base64.urlsafe_b64decode(envelope.encode("ascii"))
    if len(packed) < _NONCE_SIZE + 16:  # nonce + GCM's 16-byte tag, at minimum
        raise ValueError("Invalid encrypted secret")
    nonce, ciphertext = packed[:_NONCE_SIZE], packed[_NONCE_SIZE:]
    try:
        plaintext = AESGCM(_derive_key()).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("Encrypted secret authentication failed") from exc
    return plaintext.decode("utf-8")
