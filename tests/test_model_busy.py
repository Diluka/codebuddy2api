"""Per-model busy cache contracts; all upstream responses are synthetic."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException

from core.credentials import CredentialManager
from core.model_busy import BUSY_CACHE_TTL, BUSY_URL, ModelBusyManager


class BusyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.first, self.second = [CredentialManager(Path(temporary.name) / (aid + ".info"))
                                   for aid in ("a", "b")]
        for aid, credential in zip(("a", "b"), (self.first, self.second)):
            credential.path.write_text("synthetic-credential-" + aid)
            credential.get_headers = Mock(return_value={"Authorization": "Bearer secret-" + aid,
                                                        "X-User-Id": aid, "X-Domain": "www.codebuddy.cn"})
        self.addCleanup(CredentialManager.set_current, CredentialManager.current())
        CredentialManager.set_current(self.first)
        self.now, self.requests = 1000, []
        self.handler = lambda req: self.response(req, "normal")

        def respond(request):
            self.requests.append(request)
            return self.handler(request)

        self.manager = ModelBusyManager(clock=lambda: self.now, wall_clock=lambda: self.now,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True))

    @staticmethod
    def response(request, level):
        return httpx.Response(200, json={"code": 0, "data": [
            {"model": mid, "busy_level": level} for mid in json.loads(request.content)["models"]]})

    def queried(self, start):
        return [mid for req in self.requests[start:] for mid in json.loads(req.content)["models"]]

    def test_per_model_ttl_list_changes_batching_and_route_exclusion(self):
        self.assertEqual(BUSY_CACHE_TTL, 50)
        actual = [{"id": f"model-{i}", "description": "keep me"} for i in range(51)]
        skipped = [{"id": mid} for mid in ("auto", "fast-model", "balanced-model", "deep-model",
                                           "custom:test", "custom-local:test")]
        skipped.append({"id": "future-route", "is_dynamic_route": True})
        skipped.append({"id": "local-provider-model", "is_custom": True})
        models = actual + skipped
        original = copy.deepcopy(models)
        result = self.manager.get(models)
        self.assertEqual(models, original)
        self.assertEqual([len(json.loads(req.content)["models"]) for req in self.requests], [50, 1])
        self.assertCountEqual(self.queried(0), [m["id"] for m in actual])
        self.assertTrue(all(m["busy_level"] == "normal" and not m["busy_stale"] for m in result[:51]))
        self.assertTrue(all(m["description"] == "keep me" for m in result[:51]))
        self.assertEqual(result[51:], skipped)
        for req in self.requests:
            self.assertEqual((str(req.url), req.method), (BUSY_URL, "POST"))
            self.assertEqual(req.extensions["timeout"]["read"], 5)

        self.now += 20
        models = models + [{"id": "later"}]
        count = len(self.requests)
        self.manager.get(models)
        self.assertEqual(self.queried(count), ["later"])
        count = len(self.requests)
        header_count = self.first.get_headers.call_count
        self.now += 10
        self.manager.get(actual[:1])
        self.now += 19
        self.manager.get(models)
        self.assertEqual(len(self.requests), count)  # Removing/readding entries preserves their caches.
        self.assertEqual(self.first.get_headers.call_count, header_count)
        self.now += 1
        self.manager.get(models)
        self.assertCountEqual(self.queried(count), [m["id"] for m in actual])
        count = len(self.requests)
        self.now += 20
        self.manager.get(models)
        self.assertEqual(self.queried(count), ["later"])
        count = len(self.requests)
        self.manager.get(actual[:1] + skipped, force=True)
        self.assertEqual(self.queried(count), [actual[0]["id"]])
        count = len(self.requests)
        self.manager.get(models)
        self.assertEqual(self.manager.get(skipped, force=True), skipped)
        self.assertEqual(len(self.requests), count)

        self.first.get_headers.reset_mock()
        refreshed_headers = {**self.first.get_headers.return_value, "Authorization": "Bearer refreshed-token"}

        def refresh_headers():
            if self.first.get_headers.call_count == 1:
                self.first.path.write_text("refreshed-credential")
            return refreshed_headers

        self.first.get_headers.side_effect = refresh_headers
        selected = [actual[0], {"id": "later"}, {"id": "new-after-refresh"}]
        self.manager.get(selected)
        self.assertCountEqual(self.queried(count), [model["id"] for model in selected])
        self.assertEqual(self.first.get_headers.call_count, 2)
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer refreshed-token")

    def test_unknown_and_failed_queries_keep_old_values_and_retry_after_ttl(self):
        for credential, failure in ((self.first, "unknown"), (self.second, "redirect")):
            with self.subTest(failure=failure):
                CredentialManager.set_current(credential)
                self.handler = lambda req: self.response(req, "warming")
                first = self.manager.get([{"id": "known"}])[0]
                self.assertEqual(first["busy_level"], "warming")  # Accept new upstream states unchanged.
                self.assertFalse(first["busy_stale"])
                self.now += BUSY_CACHE_TTL
                self.handler = (lambda req: self.response(req, "unknown")) if failure == "unknown" else (
                    lambda req: httpx.Response(302, headers={"Location": "https://foreign.invalid/busy"}))
                models = [{"id": "known"}, {"id": "new"}]
                old, unknown = self.manager.get(models)
                self.assertEqual(old["busy_level"], "warming")
                self.assertEqual(old["busy_updated_at"], first["busy_updated_at"])
                self.assertTrue(old["busy_stale"])
                self.assertEqual(unknown["busy_level"], "unknown")
                self.assertIsNone(unknown["busy_updated_at"])
                count = len(self.requests)
                self.manager.get(models)
                self.now += BUSY_CACHE_TTL - 1
                self.manager.get(models)
                self.assertEqual(len(self.requests), count)
                self.now += 1
                self.handler = lambda req: self.response(req, "idle")
                result = self.manager.get(models)
                self.assertCountEqual(self.queried(count), ["known", "new"])
                self.assertTrue(all(m["busy_level"] == "idle" and not m["busy_stale"] for m in result))
                self.assertTrue(all(m["busy_updated_at"] == self.now for m in result))
        self.assertTrue(all(str(req.url) == BUSY_URL for req in self.requests))

    def test_accounts_are_isolated_and_replaced_credentials_discard_inflight_values(self):
        models = [{"id": "shared-model"}]
        CredentialManager.set_current(None)
        with self.assertRaises(HTTPException) as error:
            self.manager.get(models)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.requests, [])

        def switch_account(req):
            CredentialManager.set_current(self.second)
            return self.response(req, "busy" if req.headers["X-User-Id"] == "a" else "idle")

        CredentialManager.set_current(self.first)
        self.handler = switch_account
        with patch.object(CredentialManager, "current", wraps=CredentialManager.current) as current:
            self.assertEqual(self.manager.get(models)[0]["busy_level"], "busy")
            current.assert_called_once_with()
        self.assertEqual(self.manager.get(models)[0]["busy_level"], "idle")
        CredentialManager.set_current(self.first)
        self.assertEqual(self.manager.get(models)[0]["busy_level"], "busy")
        self.assertEqual(len(self.requests), 2)
        self.first.path.write_text("replacement-credential")
        self.handler = lambda req: self.response(req, "unknown")
        self.assertEqual(self.manager.get(models)[0]["busy_level"], "unknown")

        self.assertEqual(len(self.requests), 3)

        def respond(req):
            self.first.path.write_text("changed-during-busy-query")
            return self.response(req, "congested")

        self.handler = respond
        with self.assertRaises(HTTPException) as error:
            self.manager.get(models, force=True)
        self.assertEqual(error.exception.status_code, 400)
        self.handler = lambda req: self.response(req, "unknown")
        self.assertEqual(self.manager.get(models)[0]["busy_level"], "unknown")
