"""WorkBuddy account credentials, token refresh and request snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path

import httpx
from fastapi import HTTPException

from .memory_cache import CacheInvalidated, MemoryCache
from .workbuddy_atrest_crypto import (
    decrypt_auth_field,
    encrypt_auth_field,
    is_encrypted_field,
)

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"
WORKBUDDY_USER_AGENT = "WorkBuddy/5.8.2 WorkBuddy/5.8.2 CLI/2.161.0"
DOMAINS = {"workbuddy.cn", "www.workbuddy.cn", "codebuddy.cn", "www.codebuddy.cn"}
REFRESH_ERRORS = (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError, TypeError)


class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    _current: CredentialManager | None = None

    @classmethod
    def set_current(cls, credential: CredentialManager | None):
        """由账号管理层更新当前实例引用，保留实例及其缓存。"""
        cls._current = credential

    @classmethod
    def current(cls) -> CredentialManager | None:
        """返回当前选定账号已有的管理器；未选定时返回 None。"""
        return cls._current

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.cache = MemoryCache(ttl=None, stale_if_error=False)

    def _read_raw(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _session(self) -> dict:
        return self.cache.get("session", lambda key, context: self._read_raw(),
                              revision=self._fingerprint).value

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        version = self._fingerprint()

        def validate():
            try:
                current = self._fingerprint() == version
            except OSError:
                current = False
            if not current:
                self.cache.invalidate("session")
                raise CacheInvalidated("credentials changed during token refresh")

        s = deepcopy(self._session())
        validate()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = decrypt_auth_field(auth.get("refreshToken", ""))
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        if not isinstance(new_auth, dict) or not new_auth.get("accessToken"):
            raise RuntimeError("刷新响应缺少访问令牌")
        # 继承部分字段（refreshToken 兜底取解密后的旧值，避免加密信封被二次加密）
        new_auth["refreshToken"] = new_auth.get("refreshToken") or decrypt_auth_field(
            auth.get("refreshToken", "")
        )
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = (
                int(time.time() * 1000) + new_auth["expiresIn"] * 1000
            )
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = (
                int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
            )
        s["auth"] = new_auth
        # issue #23：若原文件为 $wbEncrypted 加密格式，回写前必须重新加密，
        # 否则 WorkBuddy 客户端读到明文会报 integrity 错误、可能重置登录态。
        if is_encrypted_field(auth.get("accessToken")):
            new_auth["accessToken"] = encrypt_auth_field(decrypt_auth_field(new_auth["accessToken"]))
            new_auth["refreshToken"] = encrypt_auth_field(decrypt_auth_field(new_auth["refreshToken"]))
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(s, ensure_ascii=False, indent=2).encode("utf-8")
        with open(tmp, "wb") as f:
            f.write(payload)
        validate()
        os.replace(tmp, self.path)
        self.cache.set("session", s, revision=hashlib.sha256(payload).hexdigest())

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {decrypt_auth_field(auth.get('accessToken', ''))}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def _fingerprint(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def fingerprint(self) -> str:
        """读取凭据文件版本，用于隔离不同登录态。"""
        with self._lock:
            return self._fingerprint()

    def request_snapshot(self) -> tuple[str, httpx.Headers]:
        """需要请求时才刷新凭据，返回稳定配对的版本和模型 API 请求头。"""
        # get_headers owns a non-reentrant lock and may update the auth file.
        for _ in range(2):
            before = self.fingerprint()
            headers = httpx.Headers(self.get_headers())
            after = self.fingerprint()
            if before == after:
                if headers.get("X-Domain", "").lower().strip() not in DOMAINS:
                    raise HTTPException(400, "模型信息查询目前仅支持国内 WorkBuddy / CodeBuddy 账号")
                headers.update({"User-Agent": WORKBUDDY_USER_AGENT,
                                "X-Product": "SaaS", "X-Requested-With": "XMLHttpRequest"})
                return after, headers
        raise HTTPException(400, "登录凭据正在变更，请稍后重新加载模型信息")

    def summary(self) -> dict:
        with self._lock:
            s = self._session()
            auth = s.get("auth") or {}
            acct = s.get("account") or {}
            exp = auth.get("expiresAt", 0)
            return {
                "uid": acct.get("uid"),
                "nickname": acct.get("nickname"),
                "enterpriseName": acct.get("enterpriseName"),
                "token_expires_at": exp,
                "token_expired": self._is_expired(),
            }
