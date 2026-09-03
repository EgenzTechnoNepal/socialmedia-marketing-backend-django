import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:"


def _key_bytes(key: str) -> bytes:
    raw = key.encode("utf-8")
    if len(raw) >= 32:
        return raw[:32]
    return raw + b"\x00" * (32 - len(raw))


def encrypt(plaintext: str, key: str) -> str:
    if not key or not plaintext:
        return plaintext
    if plaintext.startswith(PREFIX):
        return plaintext
    nonce = os.urandom(12)
    cipher = AESGCM(_key_bytes(key)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return PREFIX + base64.b64encode(nonce + cipher).decode("ascii")


def decrypt(ciphertext: str, key: str) -> str:
    if not key or not ciphertext or not ciphertext.startswith(PREFIX):
        return ciphertext
    data = base64.b64decode(ciphertext[len(PREFIX) :])
    nonce, rest = data[:12], data[12:]
    return AESGCM(_key_bytes(key)).decrypt(nonce, rest, None).decode("utf-8")
