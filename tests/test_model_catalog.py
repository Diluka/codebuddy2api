"""Model catalog contracts and account isolation; all HTTP is synthetic."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfoNotFoundError

import httpx
from fastapi import HTTPException

from core.model_catalog import CACHE_TTL, CONFIG_URL, USER_AGENT, ModelCatalogManager, discounted_credits, evaluate_models, promotion_active


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def model(mid, **fields):
    return {"id": mid, "name": "Example", "credits": "x1.00", **fields}


def promotion(pid="offer", **fields):
    return {"id": pid, "kind": "discount", "modelIds": ["free"], "badge": {"label": "优惠"},
            "discount": {"factor": 0.5}, **fields}


def config():
    return {"models": [model("free"), model("paid"), model("craft", tags=["craft"])],
            "agents": [{"name": "cli", "models": ["free", "paid"]}],
            "productFeaturesConfig": {"ModelRateLimitCap": {"lines": [{"freeId": "free", "paidId": "paid"}]}},
            "modelPromotions": [promotion()]}


class FakeManager:
    def __init__(self, path, aid):
        self.path, self._lock = path, threading.Lock()
        path.write_text("synthetic-credential-" + aid)
        self.get_headers = Mock(return_value={"Authorization": "Bearer secret-" + aid,
                                             "X-User-Id": aid, "X-Domain": "www.codebuddy.cn"})


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.first, self.second = [FakeManager(Path(temporary.name) / (aid + ".info"), aid) for aid in ("a", "b")]
        self.now = timestamp("2030-01-01T12:00:00+08:00")
        self.requests, self.response = [], {"code": 0, "data": config()}
        self.handler = lambda req: httpx.Response(200, json=self.response)

        def respond(request):
            self.requests.append(request)
            return self.handler(request)

        self.manager = ModelCatalogManager(clock=lambda: self.now,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True))

    def test_fixed_request_never_forwards_credentials_to_redirect_or_foreign_domain(self):
        result = self.manager.get(self.first)
        request = self.requests[0]
        self.assertEqual((str(request.url), request.method, request.content), (CONFIG_URL, "GET", b""))
        self.assertEqual({key: request.headers[key] for key in ("User-Agent", "X-Product", "X-Requested-With", "Authorization")},
                         {"User-Agent": USER_AGENT, "X-Product": "SaaS", "X-Requested-With": "XMLHttpRequest", "Authorization": "Bearer secret-a"})
        self.assertEqual((result["total_models"], result["filtered_models"]), (3, 1))
        self.assertNotIn("secret-a", json.dumps(result))
        self.handler = lambda req: httpx.Response(302, headers={"Location": "https://other.invalid/config"})
        self.assertEqual(self.manager.get_model_ids(self.second), [])
        self.assertEqual([str(req.url) for req in self.requests], [CONFIG_URL, CONFIG_URL])
        self.second.get_headers.return_value["X-Domain"] = "www.workbuddy.ai"
        with self.assertRaises(HTTPException) as error:
            self.manager.get(self.second)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(len(self.requests), 2)

    def test_metadata_and_ids_share_hour_cache_force_refresh_and_account_isolation(self):
        self.assertEqual(CACHE_TTL, 3600)
        first = self.manager.get(self.first)
        self.now += CACHE_TTL - 1
        self.assertEqual(self.manager.get_model_ids(self.first), ["free"])
        self.assertEqual(self.manager.get(self.first)["fetched_at"], first["fetched_at"])
        self.assertEqual(len(self.requests), 1)
        self.now += 1
        self.manager.get(self.first)
        self.manager.get(self.first, force=True)
        self.manager.get(self.second)
        self.manager.get(self.first)
        self.assertEqual([req.headers["X-User-Id"] for req in self.requests], ["a", "a", "a", "b"])

    def test_failed_refresh_keeps_display_cache_but_ids_and_new_credentials_reject_it(self):
        first = self.manager.get(self.first)
        self.handler = lambda req: httpx.Response(403, json={"msg": "secret-a raw error"})
        failed = self.manager.get(self.first, force=True)
        self.assertTrue(failed["stale"])
        self.assertEqual((failed["models"], failed["fetched_at"]), (first["models"], first["fetched_at"]))
        self.assertNotIn("secret-a", json.dumps(failed))
        self.assertEqual(self.manager.get_model_ids(self.first), [])
        self.assertEqual(self.manager.get_model_ids(self.second), [])
        self.first.path.write_text("replacement-credential")
        with self.assertRaises(HTTPException):
            self.manager.get(self.first)
        self.handler = lambda req: httpx.Response(200, json=self.response)
        self.response["data"]["models"][0]["credits"] = "x3.00"
        recovered = self.manager.get(self.first)
        self.assertFalse(recovered["stale"])
        self.assertEqual(recovered["models"][0]["credits"], "x3.00")
        self.assertEqual(self.manager.get_model_ids(self.first), ["free"])
        self.response["data"]["models"] = []
        self.manager.get(self.first, force=True)
        self.assertEqual(self.manager.get_model_ids(self.first), [])

    def test_credential_change_during_request_discards_success_and_failed_refresh(self):
        for status in (200, 500):
            with self.subTest(status=status):
                self.handler = lambda req: httpx.Response(200, json=self.response)
                self.manager.get(self.first, force=True)

                def changed(request):
                    self.first.path.write_text("changed-during-request-" + str(status))
                    return httpx.Response(status, json=self.response)

                self.handler = changed
                with self.assertRaises(HTTPException) as error:
                    self.manager.get(self.first, force=True)
                self.assertEqual(error.exception.status_code, 400 if status == 200 else 502)

    def test_token_refresh_retries_headers_once_to_use_stable_credentials(self):
        old_headers = dict(self.first.get_headers.return_value)
        new_headers = {**old_headers, "Authorization": "Bearer replacement-token"}

        def headers():
            if self.first.get_headers.call_count == 1:
                self.first.path.write_text("refreshed-credential")
                return old_headers
            return new_headers

        self.first.get_headers.side_effect = headers
        self.manager.get(self.first)
        self.assertEqual(self.first.get_headers.call_count, 2)
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer replacement-token")

    def test_bound_account_and_explicit_none_override_defaults_and_restore_on_exit(self):
        self.handler = lambda req: httpx.Response(200, json={"data": {"models": [model(req.headers["X-User-Id"])]}})
        with self.manager.use_credential(self.first):
            self.assertEqual(self.manager.get_model_ids(self.second), ["a"])
            with self.assertRaisesRegex(ValueError, "leave context"):
                with self.manager.use_credential(None):
                    self.assertEqual(self.manager.get_model_ids(self.second), [])
                    raise ValueError("leave context")
            self.assertEqual(self.manager.get_model_ids(self.second), ["a"])
        self.assertEqual(self.manager.get_model_ids(self.second), ["b"])
        self.assertEqual(self.manager.get_model_ids(), [])
        with self.assertRaises(HTTPException):
            self.manager.get(None)
        self.assertEqual(len(self.requests), 2)

    def test_concurrent_calls_coalesce_per_account_without_blocking_other_accounts(self):
        entered, release = threading.Event(), threading.Event()

        def respond(request):
            if request.headers["X-User-Id"] == "a":
                entered.set()
                self.assertTrue(release.wait(2))
            return httpx.Response(200, json={"data": {"models": [model(request.headers["X-User-Id"])]}})

        self.handler = respond
        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(self.manager.get_model_ids, self.first)
            self.assertTrue(entered.wait(2))
            repeat = executor.submit(self.manager.get_model_ids, self.first)
            try:
                other = executor.submit(self.manager.get_model_ids, self.second)
                self.assertEqual(other.result(1), ["b"])
            finally:
                release.set()
            self.assertEqual(first.result(2), ["a"])
            self.assertEqual(repeat.result(2), ["a"])
        self.assertEqual(len(self.requests), 2)

    def test_cached_promotions_recalculate_without_remote_request(self):
        self.response["data"]["modelPromotions"] = [promotion(schedule={"validUntil": "2030-01-01T12:01:00+08:00"})]
        self.assertEqual(self.manager.get(self.first)["models"][0]["effective_credits"], "0.50x")
        self.now += 60
        result = self.manager.get(self.first)["models"][0]
        self.assertIsNone(result["promotion"])
        self.assertEqual(result["effective_credits"], "x1.00")
        self.assertEqual(len(self.requests), 1)


class ModelRulesTests(unittest.TestCase):
    def setUp(self):
        self.now = timestamp("2030-01-01T12:00:00+08:00")

    def test_agent_order_availability_custom_models_and_fallback(self):
        data = {"models": [model("a"), model("b", tags=["chat"]), model("craft"), model("custom:x", tags=["custom"])],
                "agents": [{"name": "cli", "models": ["craft"]}, {"name": "main", "tags": ["default"], "models": ["b", "a", "b", "craft"]}],
                "availableModels": ["a", "b", "custom:x"]}
        self.assertEqual([m["id"] for m in evaluate_models(data, self.now)], ["b", "a", "custom:x"])
        data["agents"] = [{"name": "cli", "models": []}]
        self.assertEqual([m["id"] for m in evaluate_models(data, self.now)], ["b", "custom:x"])
        data["agents"] = []
        self.assertEqual([m["id"] for m in evaluate_models(data, self.now)], ["a", "b", "custom:x"])

    def test_free_account_filter_keeps_zero_and_positive_rates_but_excludes_paid_and_unpriced(self):
        data = config()
        data["models"][1]["credits"] = "x0.00"
        data["models"].extend([model("regular-x", credits="x2.00"), model("zero", credits="x0.00"),
                               model("unpriced", credits=None), model("blank", credits=" ")])
        data["agents"][0]["models"].extend(["regular-x", "zero", "unpriced", "blank"])
        models = evaluate_models(data, self.now)
        self.assertEqual([m["id"] for m in models], ["free", "regular-x", "zero"])
        self.assertEqual([m["line_type"] for m in models], ["free", "unmarked", "unmarked"])
        self.assertEqual([m["effective_credits"] for m in models], ["0.50x", "x2.00", "x0.00"])

    def test_auto_expansion_uses_available_metadata_and_marks_dynamic_routes(self):
        data = {"models": [model("auto"), model("fast-model", credits="x0.31"), model("balanced-model", credits="x0.75"),
                           model("deep-model", credits="x1.30"), model("a")],
                "agents": [{"name": "cli", "models": ["fast-model", "auto", "a", "auto"]}],
                "availableModels": ["auto", "a"]}
        models = evaluate_models(data, self.now)
        self.assertEqual([m["id"] for m in models], ["fast-model", "balanced-model", "deep-model", "a"])
        self.assertEqual([m["credits"] for m in models[:3]], ["x0.31", "x0.75", "x1.30"])
        self.assertEqual([m["is_dynamic_route"] for m in models], [True, True, True, False])
        data["availableModels"] = ["fast-model", "a"]
        self.assertEqual([m["id"] for m in evaluate_models(data, self.now)], ["fast-model", "a"])
        data["models"] = [model("auto"), model("a", tags=["chat"])]
        data["agents"][0]["models"] = ["auto"]
        self.assertEqual(evaluate_models(data, self.now), [])

    def test_description_and_capabilities_use_upstream_values_without_app_defaults(self):
        data = {"models": [model("step", descriptionZh="中文描述", description="English", supportsImages=True,
                                maxInputTokens=1000000, maxOutputTokens=64000, supportsReasoning=True,
                                reasoning={"supportedEfforts": ["low", "medium", "high"], "canDisableThinking": False},
                                contextWindow={"defaultLength": 300000}),
                           model("other", description="English", supportsImages="true")]}
        original = copy.deepcopy(data)
        step, other = evaluate_models(data, self.now)
        self.assertEqual((step["description"], other["description"]), ("中文描述", "English"))
        self.assertEqual((step["max_input_tokens"], step["max_output_tokens"]), (1000000, 64000))
        self.assertTrue(step["supports_images"] and step["supports_reasoning"])
        self.assertEqual(step["reasoning_efforts"], ["low", "medium", "high"])
        self.assertEqual((other["max_input_tokens"], other["max_output_tokens"], other["reasoning_efforts"]), (None, None, []))
        self.assertFalse(other["supports_images"])
        self.assertEqual(data, original)

    def test_reasoning_disable_uses_off_and_preserves_supported_levels(self):
        cases = [({"supportedEfforts": ["high", "xhigh"], "canDisableThinking": True}, ["off", "high", "xhigh"]),
                 ({"supportedEfforts": ["none", "off", "high"]}, ["off", "high"]),
                 ({"defaultEffort": "high"}, [])]
        for reasoning, expected in cases:
            with self.subTest(reasoning=reasoning):
                models = evaluate_models({"models": [model("a", reasoning=reasoning)]}, self.now)
                self.assertEqual(models[0]["reasoning_efforts"], expected)

    def test_promotion_priority_prefers_model_offers_and_stable_active_tie(self):
        data = config()
        data["modelPromotions"] = [promotion("product", priority=1000)]
        data["models"][0]["promotions"] = [promotion("first", priority=20), promotion("tie", priority=20),
            promotion("future", priority=200, schedule={"validFrom": "2031-01-01T00:00:00Z"}),
            promotion("disabled", priority=300, enabled=False)]
        chosen = evaluate_models(data, self.now)[0]["promotion"]
        self.assertEqual((chosen["id"], chosen["discounted_credits"]), ("first", "0.50x"))

    def test_inactive_badge_does_not_apply_discount_or_description(self):
        data = config()
        data["modelPromotions"] = [promotion(badge={"label": "稍后优惠", "display": "always"},
            hover={"textZh": "不应显示"}, schedule={"validFrom": "2031-01-01T00:00:00Z"})]
        shown = evaluate_models(data, self.now)[0]
        self.assertFalse(shown["promotion"]["active"])
        self.assertEqual((shown["promotion"]["description"], shown["promotion"]["discounted_credits"], shown["effective_credits"]), ("", None, "x1.00"))
        data["modelPromotions"][0]["badge"]["display"] = "activeOnly"
        self.assertIsNone(evaluate_models(data, self.now)[0]["promotion"])

    def test_promotion_time_boundaries_and_beijing_overnight_schedule(self):
        offer = promotion(schedule={"timezone": "Asia/Shanghai", "validFrom": "2030-01-01T23:00:00+08:00",
            "validUntil": "2030-01-03T08:00:00+08:00", "daily": [{"start": "23:00", "end": "08:00"}]})
        cases = [("2030-01-01T22:59:59+08:00", False), ("2030-01-01T15:00:00+00:00", True),
                 ("2030-01-02T07:59:59+08:00", True), ("2030-01-02T08:00:00+08:00", False),
                 ("2030-01-03T08:00:00+08:00", False)]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                self.assertEqual(promotion_active(offer, timestamp(instant)), expected)
        with patch("core.model_catalog.ZoneInfo", side_effect=ZoneInfoNotFoundError("Asia/Shanghai")):
            self.assertTrue(promotion_active(offer, timestamp("2030-01-01T15:00:00+00:00")))

    def test_discount_explicit_zero_and_factor_follow_app(self):
        self.assertEqual(discounted_credits("x0.79", {"factor": 0.5, "discountedCredits": "0.50x"}), "0.50x")
        self.assertEqual(discounted_credits("x0.79", {"factor": 0.5}), "0.395x")
        self.assertEqual(discounted_credits("x0.79", {"factor": 0, "discountedCredits": "0x"}), "0.00x")
        self.assertIsNone(discounted_credits("x0.79", {"factor": 0}))


if __name__ == "__main__":
    unittest.main()
