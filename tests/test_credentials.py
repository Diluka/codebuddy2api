"""Credential cache and atomic token refresh, using synthetic accounts only."""
import base64
import copy
import hashlib
import json
import os

import httpx
import pytest

from core import workbuddy_atrest_crypto
from core.credentials import CredentialManager
from core.memory_cache import CacheInvalidated
from core.workbuddy_atrest_crypto import decrypt_auth_field, encrypt_auth_field, is_encrypted_field


def make_manager(tmp_path, *, expired=False, encrypted=False):
    seal = encrypt_auth_field if encrypted else lambda value: value
    session = {"account": {"uid": "synthetic-account"}, "auth": {
        "accessToken": seal("old-access"), "refreshToken": seal("old-refresh"),
        "domain": "www.workbuddy.cn", "expiresAt": 0 if expired else 9999999999999}}
    path = tmp_path / "account.info"
    path.write_text(json.dumps(session), encoding="utf-8")
    return CredentialManager(path)


def mock_refresh(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs))


def test_file_replacement_with_same_mtime_reloads_and_invalid_file_never_uses_old_credentials(tmp_path):
    manager = make_manager(tmp_path)
    assert manager.get_headers()["Authorization"] == "Bearer old-access"
    previous = manager._session()
    metadata = manager.path.stat()
    replacement = copy.deepcopy(previous)
    replacement["auth"]["accessToken"] = "new-access"
    manager.path.write_text(json.dumps(replacement), encoding="utf-8")
    os.utime(manager.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert manager.get_headers()["Authorization"] == "Bearer new-access"
    assert previous["auth"]["accessToken"] == "old-access"
    manager.path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        manager.get_headers()


@pytest.mark.parametrize("failure", ["network", "write", "replacement"])
def test_failed_refresh_does_not_modify_cached_reference_or_persisted_session(tmp_path, monkeypatch, failure):
    manager = make_manager(tmp_path, expired=True)
    previous = manager._session()
    expected, persisted = copy.deepcopy(previous), manager.path.read_bytes()
    replacement = copy.deepcopy(previous)
    replacement["auth"].update(accessToken="replacement-access", expiresAt=9999999999999)
    replacement_bytes = json.dumps(replacement).encode("utf-8")

    def respond(request):
        if failure == "network":
            raise httpx.ConnectError("synthetic connection failure")
        if failure == "replacement":
            manager.path.write_bytes(replacement_bytes)
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "new-access", "expiresIn": 3600}})

    mock_refresh(monkeypatch, respond)
    if failure == "write":
        def fail_replace(source, destination):
            raise OSError("synthetic atomic write failure")
        monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises({"network": RuntimeError, "write": OSError, "replacement": CacheInvalidated}[failure]):
        manager.get_headers()
    assert previous == expected
    if failure == "replacement":
        assert manager._session() == replacement
        assert manager.path.read_bytes() == replacement_bytes
        assert manager.get_headers()["Authorization"] == "Bearer replacement-access"
    else:
        assert manager._session() is previous
        assert manager.path.read_bytes() == persisted


@pytest.mark.parametrize("encrypted", [False, True])
def test_successful_refresh_updates_file_and_cache_without_mutating_old_reference(tmp_path, monkeypatch, encrypted):
    monkeypatch.setenv("WORKBUDDY_AT_REST_SECRET", base64.b64encode(hashlib.sha256(b"credential-cache-test").digest()).decode())
    monkeypatch.setattr(workbuddy_atrest_crypto, "_cached_key", None)
    manager = make_manager(tmp_path, expired=True, encrypted=encrypted)
    previous, requests = manager._session(), []
    expected = copy.deepcopy(previous)

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer old-access"
        assert request.headers["X-Refresh-Token"] == "old-refresh"
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "new-access", "expiresIn": 3600}})

    mock_refresh(monkeypatch, respond)
    assert manager.get_headers()["Authorization"] == "Bearer new-access"
    assert manager.get_headers()["Authorization"] == "Bearer new-access"
    assert len(requests) == 1
    assert previous == expected
    current = manager._session()
    assert current == json.loads(manager.path.read_text(encoding="utf-8"))
    assert current is not previous
    assert not manager.summary()["token_expired"]
    assert decrypt_auth_field(current["auth"]["refreshToken"]) == "old-refresh"
    assert is_encrypted_field(current["auth"]["accessToken"]) is encrypted
    assert is_encrypted_field(current["auth"]["refreshToken"]) is encrypted
