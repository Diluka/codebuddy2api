"""Generic memory-cache contracts, independent of model APIs and credentials."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from core.memory_cache import CacheInvalidated, MemoryCache


class MemoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100, 1000

    def cache(self, **options):
        return MemoryCache(clock=lambda: self.now, wall_clock=lambda: self.wall, **options)

    def test_unlimited_ttl_falsy_values_and_hashable_scopes(self):
        cache = self.cache()
        values = {"none": None, "zero": 0, "false": False, "empty": ""}
        result = cache.get_many(values, lambda keys, context: values, scope="first")
        self.assertTrue(all(entry.found and not entry.stale for entry in result.values()))
        self.assertEqual({key: entry.value for key, entry in result.items()}, values)
        cache.set(("tuple", 1), [], scope=("second", 2))
        unused = Mock(side_effect=AssertionError("fresh entry should not load"))
        self.now += 1000000
        self.assertIsNone(cache.get("none", unused, scope="first").value)
        self.assertEqual(cache.get(("tuple", 1), unused, scope=("second", 2)).value, [])
        cache.clear(scope="first")
        self.assertEqual(cache.get("none", lambda key, context: "reloaded", scope="first").value, "reloaded")
        self.assertEqual(cache.get(("tuple", 1), unused, scope=("second", 2)).value, [])
        cache.clear()
        self.assertEqual(cache.get(("tuple", 1), unused, scope=("second", 2)).value, [])
        cache.clear(scope=("second", 2))
        self.assertEqual(cache.get(("tuple", 1), lambda key, context: "cleared", scope=("second", 2)).value, "cleared")

    def test_per_key_ttl_partial_batches_and_wall_clock_changes(self):
        cache = self.cache(ttl=50, retry_ttl=10, stale_if_error=True, batch_size=2)
        calls = []
        omitted = set()

        def load(keys, context):
            calls.append(keys)
            return {key: "value-" + key for key in keys if key not in omitted}

        first = cache.get_many(["a"], load)["a"]
        self.now += 20
        self.wall -= 500
        cache.get_many(["a", "b"], load)
        self.assertEqual(calls, [["a"], ["b"]])
        self.wall += 1000000
        self.now += 29
        self.assertEqual(cache.get_many(["a", "b"], load)["a"].updated_at, first.updated_at)
        self.assertEqual(len(calls), 2)
        self.now += 1
        cache.get_many(["a", "b"], load)
        self.assertEqual(calls[-1], ["a"])
        self.now += 20
        previous = cache.get("b", lambda key, context: "value-b", force=True)
        self.wall += 20
        omitted.add("b")
        old = cache.get_many(["a", "b", "c", "d"], load, force=True)
        self.assertEqual(calls[-2:], [["a", "b"], ["c", "d"]])
        self.assertTrue(old["b"].found and old["b"].stale)
        self.assertEqual(old["b"].updated_at, previous.updated_at)
        self.assertEqual(old["b"].value, previous.value)
        count = len(calls)
        self.now += 9
        cache.get_many(["a", "b", "c", "d"], load)
        self.assertEqual(len(calls), count)
        self.now += 1
        omitted.clear()
        self.assertFalse(cache.get_many(["a", "b", "c", "d"], load)["b"].stale)
        self.assertEqual(calls[-1], ["b"])

    def test_overlapping_force_refresh_merges_keys_without_blocking_other_scopes(self):
        entered, waiting = threading.Event(), threading.Event()

        class ObservedLoadLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == "cache-waiter":
                    waiting.set()
                self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()

        cache, scope = self.cache(ttl=50, batch_size=1), "account"
        # Observe the wait after the caller has recorded its cache generation.
        cache._scope(scope).load_lock = ObservedLoadLock()
        calls = []

        def load(keys, context):
            calls.append(keys)
            if keys == ["b"]:
                entered.set()
                self.assertTrue(waiting.wait(2))
                cache.get("barrier", lambda key, context: True, scope="other")
            return {key: "fresh-" + key for key in keys}

        def overlap():
            threading.current_thread().name = "cache-waiter"
            return cache.get_many(["a", "b", "c"], load, scope=scope, force=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.get_many, ["a", "b"], load, scope=scope, force=True)
            self.assertTrue(entered.wait(2))
            second = executor.submit(overlap)
            self.assertEqual(first.result(2)["a"].value, "fresh-a")
            result = second.result(2)
        self.assertEqual(calls, [["a"], ["b"], ["c"]])
        self.assertEqual({key: value.value for key, value in result.items()},
                         {"a": "fresh-a", "b": "fresh-b", "c": "fresh-c"})

    def test_mutations_do_not_wait_for_or_restore_an_old_inflight_load(self):
        for operation in ("set", "invalidate", "clear"):
            with self.subTest(operation=operation):
                cache = self.cache(ttl=50, stale_if_error=True)
                entered, release = threading.Event(), threading.Event()
                cache.set("key", "cached-old", scope="account")

                def load(key, context):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return "inflight-old"

                def mutate():
                    if operation == "set":
                        cache.set("key", "manual-new", scope="account")
                    elif operation == "invalidate":
                        cache.invalidate("key", scope="account")
                    else:
                        cache.clear(scope="account")

                with ThreadPoolExecutor(max_workers=2) as executor:
                    pending = executor.submit(cache.get, "key", load, scope="account", force=True)
                    self.assertTrue(entered.wait(2))
                    try:
                        executor.submit(mutate).result(1)
                    finally:
                        release.set()
                    with self.assertRaises(CacheInvalidated):
                        pending.result(2)
                loader = Mock(return_value="loaded-new")
                current = cache.get("key", loader, scope="account")
                self.assertEqual(current.value, "manual-new" if operation == "set" else "loaded-new")
                self.assertEqual(loader.call_count, 0 if operation == "set" else 1)

    def test_strict_errors_missing_results_and_stale_fallback(self):
        cache = self.cache(ttl=50)
        cache.set("old", "cached")
        failure = Mock(side_effect=ValueError("failed"))
        with self.assertRaises(ValueError):
            cache.get("old", failure, force=True)
        with self.assertRaises(KeyError):
            cache.get_many(["good", "missing"], lambda keys, context: {"good": 0})
        good = cache.get("good", Mock(side_effect=AssertionError("successful partial result was lost")))
        self.assertTrue(good.found)
        self.assertEqual(good.value, 0)
        fallback = self.cache(ttl=50, stale_if_error=True, errors=(ValueError,))
        fallback.set("old", "cached")
        result = fallback.get("old", failure, force=True)
        self.assertTrue(result.found and result.stale)
        self.assertEqual((result.value, result.updated_at), ("cached", self.wall))
        missing = fallback.get("absent", failure)
        self.assertFalse(missing.found)
        with self.assertRaises(RuntimeError):
            fallback.get("old", Mock(side_effect=RuntimeError("not recoverable")), force=True)

    def test_preparation_versions_and_uncertain_identity_never_use_stale_fallback(self):
        cache = self.cache(ttl=50, stale_if_error=True)
        version = "old"
        cache.set("fresh", "old-value", scope="account", revision=version)

        def prepare():
            nonlocal version
            version = "new"
            return version, "request-context"

        loader = Mock(return_value={"fresh": "new-value", "added": "new-added"})
        result = cache.get_many(["fresh", "added"], loader, scope="account",
                                revision=lambda: version, prepare=prepare)
        loader.assert_called_once_with(["fresh", "added"], "request-context")
        self.assertEqual(result["fresh"].value, "new-value")

        def change_during_load(key, context):
            nonlocal version
            version = "replaced"
            return "wrong-version"

        with self.assertRaises(CacheInvalidated):
            cache.get("fresh", change_during_load, scope="account", revision=lambda: version, force=True)
        with self.assertRaises(CacheInvalidated):
            cache.get("fresh", lambda key, context: "unverified", scope="account",
                      revision=Mock(side_effect=OSError("identity cannot be read")))
