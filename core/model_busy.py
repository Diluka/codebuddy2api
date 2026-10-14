"""Read-only model load status with independent per-account, per-model caching."""
import time

import httpx
from fastapi import HTTPException

from .credentials import CredentialManager, REFRESH_ERRORS
from .model_routes import LEGACY_AUTO_ROUTE_IDS
from .memory_cache import CacheInvalidated, CacheResult, MemoryCache

BUSY_URL = "https://copilot.tencent.com/v2/chat/busy"
BUSY_CACHE_TTL = 50


class ModelBusyManager:
    def __init__(self, clock=time.monotonic, client_factory=None, *, wall_clock=time.time):
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=5, follow_redirects=False))
        self.cache = MemoryCache(BUSY_CACHE_TTL, clock=clock, wall_clock=wall_clock,
                                 stale_if_error=True, retry_ttl=50, batch_size=50,
                                 errors=(HTTPException, *REFRESH_ERRORS))

    def _fetch(self, model_ids, headers):
        with self.client_factory() as client:
            response = client.post(BUSY_URL, headers=headers, json={"models": model_ids},
                                   timeout=5, follow_redirects=False)
        if response.status_code != 200:
            raise ValueError("busy query unavailable")
        data = response.json()
        if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), list):
            raise ValueError("invalid busy response")
        # Unknown/missing values take the cache's stale fallback independently.
        result = {}
        for row in data["data"]:
            if isinstance(row, dict) and isinstance(row.get("model"), str) and row["model"] in model_ids:
                level = row.get("busy_level")
                if isinstance(level, str) and level and level != "unknown":
                    result[row["model"]] = level
        return result

    def get(self, models, force=False):
        """Return enriched copies; route/custom entries never enter the query."""
        credential = CredentialManager.current()
        if credential is None:
            raise HTTPException(400, "请先选定模型查询账号")
        ids = [model["id"] for model in models if not model.get("is_dynamic_route") and not model.get("is_custom")
               and model["id"] not in ("auto", *LEGACY_AUTO_ROUTE_IDS)
               and not model["id"].startswith(("custom:", "custom-local:"))]
        if not ids:
            return [dict(model) for model in models]
        try:
            values = self.cache.get_many(ids, self._fetch, scope=credential,
                                         revision=credential.fingerprint, prepare=credential.request_snapshot,
                                         force=force)
        except CacheInvalidated:
            raise HTTPException(400, "登录凭据已变更，请重新加载模型信息") from None
        except (OSError, ValueError, RuntimeError):
            values = {}
        result = []
        for model in models:
            item = dict(model)
            if model["id"] in ids:
                value = values.get(model["id"], CacheResult(False, stale=True))
                item.update(busy_level=value.value if value.found else "unknown", busy_updated_at=value.updated_at,
                            busy_stale=value.stale)
            result.append(item)
        return result


model_busy_manager = ModelBusyManager()
