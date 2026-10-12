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
        # Model-list fallback must never send these synthetic credentials upstream.
        catalog_patch = patch.object(admin_server.model_catalog_manager, "get", side_effect=RuntimeError("synthetic remote unavailable"))
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
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
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})
        await self.login()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.client.post("/admin/api/keys", json={"name": "bad"})).status_code, 403)

    async def test_account_import_switch_disable_delete_and_restart(self):
        await self.login()
        first = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        result = await self.client.post("/admin/api/accounts", json={"name": "Second", "credential": credential("two")})
        self.assertEqual(result.status_code, 200)
        aid = result.json()["id"]
        self.assertTrue(first["active"])
        result = await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(admin_server.converter.CONFIG["cred"].path.name, aid + ".info")
        await self.client.patch("/admin/api/accounts/" + aid, json={"enabled": False})
        self.assertIsNone(admin_server.converter.CONFIG["cred"])
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})).status_code, 400)
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"active": True, "name": "Primary"})
        await self.client.delete("/admin/api/accounts/" + aid)
        self.assertTrue(list((self.root / "management/trash").glob("*/credential.json")))
        await self.client.aclose()
        self.make_app()
        await self.login()
        accounts = (await self.client.get("/admin/api/overview")).json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["active"])
        self.assertEqual(accounts[0]["name"], "Primary")

    async def test_model_catalog_auth_and_selected_account(self):
        store = self.app.state.store
        selected = admin_server.converter.CONFIG["cred"]
        loop_thread = threading.get_ident()

        def lookup(credential, force=False):
            self.assertIs(credential, selected)
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertTrue(store.lock.acquire(timeout=0.2))
            store.lock.release()
            return {"models": []}

        with patch.object(admin_server.model_catalog_manager, "get", side_effect=lookup) as get:
            self.assertEqual((await self.client.get("/admin/api/models")).status_code, 401)
            self.assertEqual((await self.client.get("/admin/api/models", headers={"Authorization": "Bearer " + API})).status_code, 401)
            self.assertEqual((await self.client.post("/admin/api/models/refresh")).status_code, 401)
            get.assert_not_called()
            await self.login()
            aid = store.data["active"]
            for method, path, force in (("GET", "/admin/api/models", False), ("POST", "/admin/api/models/refresh", True)):
                response = await self.client.request(method, path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"models": [], "account": {"id": aid, "name": store.data["accounts"][aid]["name"]}})
                get.assert_called_once_with(selected, force=force)
                get.reset_mock()
            self.client.headers.pop("X-CSRF-Token")
            self.assertEqual((await self.client.post("/admin/api/models/refresh")).status_code, 403)
            get.assert_not_called()

    async def test_model_catalog_requires_unchanged_enabled_account(self):
        await self.login()
        store = self.app.state.store
        aid = store.data["active"]
        with patch.object(admin_server.model_catalog_manager, "get") as get:
            store.data["active"] = None
            self.assertEqual((await self.client.get("/admin/api/models")).status_code, 400)
            store.data["active"] = aid
            store.data["accounts"][aid]["enabled"] = False
            self.assertEqual((await self.client.get("/admin/api/models")).status_code, 400)
            get.assert_not_called()
        store.data["accounts"][aid]["enabled"] = True

        for failed in (False, True):
            with self.subTest(upstream_failed=failed):
                store.data["active"] = aid

                def lookup(credential, force=False):
                    with store.lock:
                        store.data["active"] = None
                    if failed:
                        raise HTTPException(502, "remote unavailable")
                    return {"models": [{"id": "remote-model"}]}

                with patch.object(admin_server.model_catalog_manager, "get", side_effect=lookup):
                    response = await self.client.get("/admin/api/models")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("remote-model", response.text)

    async def test_model_lookup_pins_account_and_rejects_switch(self):
        await self.login()
        selected = admin_server.converter.CONFIG["cred"]

        def switch_before_remote_lookup():
            admin_server.converter.CONFIG["cred"] = None
            return []

        result = {"models": [{"id": "remote-model"}], "stale": False, "error": None}
        with patch.object(admin_server.converter, "_load_models_from_workbuddy", side_effect=switch_before_remote_lookup), \
                patch.object(admin_server.model_catalog_manager, "get", return_value=result) as get:
            for method, path in (("GET", "/admin/api/overview"), ("POST", "/admin/api/test")):
                admin_server.converter.CONFIG["cred"] = selected
                response = await self.client.request(method, path, json={"model": "remote-model", "prompt": "test"})
                self.assertEqual(response.status_code, 409)
                get.assert_called_once_with(selected)
                get.reset_mock()
        self.assertEqual(list(self.app.state.store.events), [])

    async def test_concurrent_test_started_during_lookup_is_rejected_without_queueing(self):
        await self.login()
        loop = asyncio.get_running_loop()
        test_lock = self.app.state.store.test_lock

        def lookup(credential):
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
