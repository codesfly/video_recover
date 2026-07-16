from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CookieVault:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        try:
            return self.key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                descriptor = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                return self.key_path.read_bytes().strip()
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(key)
                key_file.flush()
                os.fsync(key_file.fileno())
            return key

    def encrypt(self, cookie: str) -> bytes:
        value = cookie.strip()
        if not value:
            raise ValueError("cookie cannot be empty")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, token: bytes | str) -> str:
        encoded = token.encode("ascii") if isinstance(token, str) else token
        try:
            return self._fernet.decrypt(encoded).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("stored cookie cannot be decrypted") from exc

