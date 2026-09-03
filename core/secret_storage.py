"""Symmetric encryption for secrets-at-rest (email account passwords, etc.),
matching the "encrypted-at-rest credentials" requirement from the Odysseus
reference research. Key is generated once on first use and never logged or
returned to any API response.
"""
import os

from cryptography.fernet import Fernet

from core.constants import DATA_DIR

_KEY_FILE = os.path.join(DATA_DIR, ".secret_key")

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(_KEY_FILE, "wb") as f:
            f.write(key)
        try:
            os.chmod(_KEY_FILE, 0o600)  # no-op on Windows, real restriction on POSIX
        except OSError:
            pass

    _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
