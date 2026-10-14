import asyncio
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx
from fastapi import HTTPException

import admin.server as admin_server

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"}, "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token", "expiresAt": int(time.time() * 1000) + 3600000}}


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        previous = admin_server.CredentialManager.current()
        self.addCleanup(admin_server.CredentialManager.set_current, previous)
        # Model-list fallback must never send these synthetic credentials upstream.
        catalog_patch = patch.object(admin_server.model_catalog_manager, "get", side_effect=RuntimeError("synthetic remote unavailable"))
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        busy_patch = patch.object(admin_server.model_busy_manager, "get",
                                  side_effect=lambda models, force=False: models)
        self.busy_get = busy_patch.start()
        self.addCleanup(busy_patch.stop)
        local_patch = patch.object(admin_server.converter, "_load_models_from_workbuddy", return_value=[])
        local_patch.start()
        self.addCleanup(local_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.make_app()

    def make_app(self):
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(result.status_code, 200)
        cookie = result.headers["set-cookie"].lower()
        for flag in ["secure", "httponly", "samesite=strict", "path=/admin"]:
            self.assertIn(flag, cookie)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def test_access_boundaries(self):
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)
        self.assertEqual((await self.client.get("/admin/api/overview", headers={"Authorization": "Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models")).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).status_code, 200)
        self.busy_get.assert_not_called()
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})
        await self.login()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.client.post("/admin/api/keys", json={"name": "bad"})).status_code, 403)

    async def test_account_import_switch_disable_delete_and_restart(self):
        await self.login()
        store = self.app.state.store
        first = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        primary = store.manager_for(first["id"], store.data["accounts"][first["id"]])
        self.assertIs(admin_server.CredentialManager.current(), primary)
        result = await self.client.post("/admin/api/accounts", json={"name": "Second", "credential": credential("two")})
        self.assertEqual(result.status_code, 200)
        aid = result.json()["id"]
        self.assertTrue(first["active"])
        self.assertIs(admin_server.CredentialManager.current(), primary)
        secondary = store.manager_for(aid, store.data["accounts"][aid])
        secondary_cache = secondary.cache
        result = await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})
        self.assertEqual(result.status_code, 200)
        self.assertIs(admin_server.CredentialManager.current(), secondary)
        await self.client.patch("/admin/api/accounts/" + aid, json={"enabled": False})
        self.assertIsNone(admin_server.CredentialManager.current())
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})).status_code, 400)
        await self.client.patch("/admin/api/accounts/" + aid, json={"enabled": True, "active": True})
        self.assertIs(admin_server.CredentialManager.current(), secondary)
        self.assertIs(secondary.cache, secondary_cache)
        await self.client.delete("/admin/api/accounts/" + aid)
        self.assertIsNone(admin_server.CredentialManager.current())
        self.assertTrue(list((self.root / "management/trash").glob("*/credential.json")))
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"active": True, "name": "Primary"})
        self.assertIs(admin_server.CredentialManager.current(), primary)
        await self.client.aclose()
        self.make_app()
        await self.login()
        accounts = (await self.client.get("/admin/api/overview")).json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["active"])
        self.assertEqual(accounts[0]["name"], "Primary")
        restarted = self.app.state.store
        self.assertIs(admin_server.CredentialManager.current(), restarted.manager_for(first["id"], restarted.data["accounts"][first["id"]]))
        self.assertEqual(admin_server.CredentialManager.current().path, primary.path)

    async def test_model_catalog_auth_and_selected_account(self):
        store = self.app.state.store
        selected = admin_server.CredentialManager.current()
        loop_thread = threading.get_ident()
        models = [{"id": "remote-model"}]
        busy_models = [{"id": "remote-model", "busy_level": "normal"}]

        def lookup(force=False):
            self.assertIs(admin_server.CredentialManager.current(), selected)
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertTrue(store.lock.acquire(timeout=0.2))
            store.lock.release()
            return {"models": models, "fetched_at": 123}

        def busy_lookup(entries, force=False):
            self.assertIs(admin_server.CredentialManager.current(), selected)
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertTrue(store.lock.acquire(timeout=0.2))
            store.lock.release()
            return busy_models

        self.busy_get.side_effect = busy_lookup

        with patch.object(admin_server.model_catalog_manager, "get", side_effect=lookup) as get:
            self.assertEqual((await self.client.get("/admin/api/models")).status_code, 401)
            self.assertEqual((await self.client.get("/admin/api/models", headers={"Authorization": "Bearer " + API})).status_code, 401)
            self.assertEqual((await self.client.post("/admin/api/models/refresh")).status_code, 401)
            get.assert_not_called()
            self.busy_get.assert_not_called()
            await self.login()
            aid = store.data["active"]
            for method, path, force in (("GET", "/admin/api/models", False), ("POST", "/admin/api/models/refresh", True)):
                response = await self.client.request(method, path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"models": busy_models, "fetched_at": 123,
                                                  "account": {"id": aid, "name": store.data["accounts"][aid]["name"]}})
                get.assert_called_once_with(force=force)
                self.busy_get.assert_called_once_with(models, force=force)
                get.reset_mock()
                self.busy_get.reset_mock()
            self.client.headers.pop("X-CSRF-Token")
            self.assertEqual((await self.client.post("/admin/api/models/refresh")).status_code, 403)
            get.assert_not_called()
            self.busy_get.assert_not_called()

    async def test_model_catalog_requires_unchanged_enabled_account(self):
        await self.login()
        store = self.app.state.store
        aid = store.data["active"]
        with patch.object(admin_server.model_catalog_manager, "get") as get:
            store.data["active"] = None
            store.sync_active()
            self.assertEqual((await self.client.get("/admin/api/models")).status_code, 400)
            store.data["active"] = aid
            store.data["accounts"][aid]["enabled"] = False
            store.sync_active()
            self.assertEqual((await self.client.get("/admin/api/models")).status_code, 400)
            get.assert_not_called()
        store.data["accounts"][aid]["enabled"] = True
        store.sync_active()
        other = (await self.client.post("/admin/api/accounts", json={"credential": credential("two")})).json()["id"]
        loop = asyncio.get_running_loop()

        async def select(account_id):
            response = await self.client.patch("/admin/api/accounts/" + account_id, json={"active": True})
            self.assertEqual(response.status_code, 200)

        # Real selections replace the existing account record, including A -> B -> A.
        cases = (("catalog", False, False), ("catalog", True, False),
                 ("busy", True, False), ("busy", True, True))
        for stage, round_trip, failed in cases:
            with self.subTest(stage=stage, round_trip=round_trip, upstream_failed=failed):
                await select(aid)
                self.busy_get.reset_mock()

                def switch_account():
                    asyncio.run_coroutine_threadsafe(select(other), loop).result(timeout=2)
                    if round_trip:
                        asyncio.run_coroutine_threadsafe(select(aid), loop).result(timeout=2)
                    if failed:
                        raise HTTPException(502, "remote unavailable")

                def lookup(force=False):
                    if stage == "catalog":
                        switch_account()
                    return {"models": [{"id": "remote-model"}]}

                def busy_lookup(models, force=False):
                    if stage == "busy":
                        switch_account()
                    return models

                self.busy_get.side_effect = busy_lookup
                with patch.object(admin_server.model_catalog_manager, "get", side_effect=lookup):
                    response = await self.client.get("/admin/api/models")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("remote-model", response.text)
                if stage == "catalog":
                    self.busy_get.assert_not_called()

    async def test_model_catalog_does_not_combine_different_credential_contents(self):
        await self.login()
        selected = admin_server.CredentialManager.current()
        for stage in ("catalog", "busy"):
            with self.subTest(changed_during=stage):
                calls = 0
                self.busy_get.reset_mock()

                def change_contents():
                    with selected._lock:
                        selected.path.write_bytes(selected.path.read_bytes() + b"\n")

                def lookup(force=False):
                    nonlocal calls
                    calls += 1
                    if calls == 1 and stage == "catalog":
                        change_contents()
                    return {"models": [{"id": "old-credential-model" if calls == 1 else "current-model"}]}

                def busy_lookup(models, force=False):
                    if stage == "busy":
                        change_contents()
                    return [{**model, "busy_level": "normal"} for model in models]

                self.busy_get.side_effect = busy_lookup
                with patch.object(admin_server.model_catalog_manager, "get", side_effect=lookup):
                    response = await self.client.get("/admin/api/models")
                self.assertNotIn("old-credential-model", response.text)
                if stage == "catalog":
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(calls, 2)
                    self.assertEqual(response.json()["models"], [{"id": "current-model", "busy_level": "normal"}])
                    self.busy_get.assert_called_once_with([{"id": "current-model"}], force=False)
                else:
                    self.assertEqual(response.status_code, 400)

    async def test_model_lookup_rejects_account_switch(self):
        await self.login()
        store = self.app.state.store
        selected = admin_server.CredentialManager.current()
        aid = store.data["active"]
        result = {"models": [{"id": "remote-model"}], "stale": False, "error": None}

        for record_replaced in (False, True):
            def switch_before_remote_lookup():
                with store.lock:
                    if record_replaced:
                        # Selecting A again after B replaces A's record but reuses its manager.
                        store.data["accounts"][aid] = dict(store.data["accounts"][aid])
                    else:
                        admin_server.CredentialManager.set_current(None)
                return []

            with patch.object(admin_server.converter, "_load_models_from_workbuddy", side_effect=switch_before_remote_lookup), \
                    patch.object(admin_server.model_catalog_manager, "get", return_value=result) as get:
                for method, path in (("GET", "/admin/api/overview"), ("POST", "/admin/api/test")):
                    with self.subTest(path=path, record_replaced=record_replaced):
                        admin_server.CredentialManager.set_current(selected)
                        response = await self.client.request(method, path, json={"model": "remote-model", "prompt": "test"})
                        self.assertEqual(response.status_code, 409)
                        get.assert_called_once_with()
                        get.reset_mock()
        self.assertEqual(list(store.events), [])

    async def test_concurrent_test_started_during_lookup_is_rejected_without_queueing(self):
        await self.login()
        loop = asyncio.get_running_loop()
        test_lock = self.app.state.store.test_lock

        def lookup():
            asyncio.run_coroutine_threadsafe(test_lock.acquire(), loop).result(timeout=1)
            return {"models": [{"id": "remote-model"}], "stale": False, "error": None}

        try:
            with patch.object(admin_server.model_catalog_manager, "get", side_effect=lookup):
                response = await asyncio.wait_for(
                    self.client.post("/admin/api/test", json={"model": "remote-model", "prompt": "test"}), timeout=2)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(list(self.app.state.store.events), [])
        finally:
            if test_lock.locked():
                test_lock.release()

    async def test_key_revocation_persists_and_no_secret_leak(self):
        await self.login()
        overview = await self.client.get("/admin/api/overview")
        for forbidden in [API, "private-access-token", "private-refresh-token", '"hash"']:
            self.assertNotIn(forbidden, overview.text)
        old = overview.json()["keys"][0]["id"]
        self.assertEqual((await self.client.delete("/admin/api/keys/" + old)).status_code, 400)
        result = await self.client.post("/admin/api/keys", json={"name": "New"})
        newkey = result.json()["key"]
        await self.client.delete("/admin/api/keys/" + old)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)
        self.assertNotIn(newkey, self.app.state.store.path.read_text(encoding="utf-8"))
        await self.client.aclose()
        self.make_app()
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)

    async def test_invalid_credentials_and_limits(self):
        await self.login()
        for doc in [{}, {"auth": {}, "account": {}}, {**credential(), "account": {"uid": "bad\r\nheader"}}, {**credential(), "auth": {**credential()["auth"], "expiresAt": "not-time"}}]:
            r = await self.client.post("/admin/api/accounts", json={"credential": doc})
            self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.post("/admin/api/accounts", content=b"x" * (1024 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        r = await self.client.get("/admin/assets/unknown.js")
        self.assertEqual(r.status_code, 404)

    async def test_logout_and_rate_limit(self):
        await self.login()
        self.assertEqual((await self.client.post("/admin/api/logout")).status_code, 200)
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)
        for _ in range(8):
            self.assertEqual((await self.client.post("/admin/api/login", json={"key":"wrong"})).status_code, 401)
        self.assertEqual((await self.client.post("/admin/api/login", json={"key":ADMIN})).status_code, 429)


if __name__ == "__main__":
    unittest.main()
