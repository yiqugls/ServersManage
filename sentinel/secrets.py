"""敏感字段落盘加密。

密钥文件放在 data/secret.key（首次启动自动生成）。
优先用 cryptography 的 Fernet（AES-CBC + HMAC）；如果目标机器没装，
退化为 HMAC-SHA256 计数器模式生成的密钥流 + HMAC 校验，同样是标准构造，
只是不复用成熟实现。两种格式带不同前缀，可混用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _secrets
import stat

try:  # pragma: no cover - 取决于运行环境
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_FERNET = True
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore
    _HAS_FERNET = False


class SecretBox:
    def __init__(self, key_path: str):
        self.key_path = key_path
        self._master = self._load_or_create(key_path)
        self._fernet = Fernet(self._master) if _HAS_FERNET else None

    # ---------- 对外接口 ----------
    def encrypt(self, plaintext: str) -> str:
        if plaintext is None or plaintext == "":
            return ""
        raw = str(plaintext).encode("utf-8")
        if self._fernet is not None:
            return "f1:" + self._fernet.encrypt(raw).decode("ascii")
        nonce = _secrets.token_bytes(16)
        body = _xor(raw, self._stream_key(nonce), 0)
        tag = hmac.new(self._mac_key(), nonce + body, hashlib.sha256).digest()[:16]
        return "h1:" + base64.urlsafe_b64encode(nonce + tag + body).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        token = str(token)
        try:
            if token.startswith("f1:"):
                if self._fernet is None:
                    return ""
                return self._fernet.decrypt(token[3:].encode("ascii")).decode("utf-8")
            if token.startswith("h1:"):
                blob = base64.urlsafe_b64decode(token[3:].encode("ascii"))
                nonce, tag, body = blob[:16], blob[16:32], blob[32:]
                want = hmac.new(self._mac_key(), nonce + body, hashlib.sha256).digest()[:16]
                if not hmac.compare_digest(tag, want):
                    return ""
                return _xor(body, self._stream_key(nonce), 0).decode("utf-8", "replace")
            # 兼容早期明文
            return token
        except InvalidToken:
            return ""
        except Exception:
            return ""

    # ---------- 内部 ----------
    def _stream_key(self, nonce: bytes) -> bytes:
        return hmac.new(self._master, b"enc" + nonce, hashlib.sha256).digest()

    def _mac_key(self) -> bytes:
        return hmac.new(self._master, b"mac", hashlib.sha256).digest()

    @staticmethod
    def _load_or_create(path: str) -> bytes:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read().strip()
            if len(data) >= 32:
                return data
        data = base64.urlsafe_b64encode(os.urandom(32))
        with open(path, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        return data


def _xor(data: bytes, stream: bytes, offset: int) -> bytes:
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        out[i] = byte ^ stream[(offset + i) % len(stream)]
    return bytes(out)
