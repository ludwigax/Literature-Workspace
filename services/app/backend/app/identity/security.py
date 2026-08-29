from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet


def random_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class SecretCipher:
    def __init__(self, secret: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
