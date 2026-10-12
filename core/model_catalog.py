"""Shared read-only WorkBuddy model configuration, scoped to a credential manager."""
import hashlib
import math
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import HTTPException

CONFIG_URL = "https://www.workbuddy.cn/v3/config"
USER_AGENT = "WorkBuddy/5.7.7 WorkBuddy/5.7.7 CLI/2.156.0"
CACHE_TTL = 3600
DOMAINS = {"workbuddy.cn", "www.workbuddy.cn", "codebuddy.cn", "www.codebuddy.cn"}
FETCH_ERROR = "模型配置查询失败，请检查登录状态或网络后重试"
# /v3/config supplies model metadata but no auto-to-route mapping or route flag.
# This compatibility mapping follows the App's AUTO_TIER_MODEL_IDS; it never
# supplies missing model entries, rates or capabilities.
LEGACY_AUTO_ROUTE_IDS = ("fast-model", "balanced-model", "deep-model")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _array(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    return None


def _token_limit(value):
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        # JavaScript treats date-only ISO values as UTC, naive date-times as local.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value) == 10:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _minute(value):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", _text(value))
    if match:
        hour, minute = map(int, match.groups())
        if hour < 24 and minute < 60:
            return hour * 60 + minute
    return None


def promotion_active(promotion, now):
    schedule = _mapping(promotion.get("schedule"))
    start, end = _date(schedule.get("validFrom")), _date(schedule.get("validUntil"))
    if (start is not None and now < start) or (end is not None and now >= end):
        return False
    daily = _array(schedule.get("daily"))
    if not daily:
        return True
    zone_name = _text(schedule.get("timezone"))
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        # Windows can lack IANA tzdata. Current domestic promotions use Beijing
        # time; preserve that timezone instead of accidentally using server time.
        zone = timezone(timedelta(hours=8)) if zone_name == "Asia/Shanghai" else None
    current = datetime.fromtimestamp(now, zone)
    minute = current.hour * 60 + current.minute
    for interval in daily:
        interval = _mapping(interval)
        start, end = _minute(interval.get("start")), _minute(interval.get("end"))
        if start is None or end is None:
            continue
        if start == end or (start < end and start <= minute < end) or (start > end and (minute >= start or minute < end)):
            return True
    return False


def _credits(value):
    value = _text(value)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(.*)", value)
    if match:
        number, suffix = match.groups()
    else:
        match = re.fullmatch(r"x\s*(\d+(?:\.\d+)?)", value, re.IGNORECASE)
        if not match:
            return None
        number, suffix = match.group(1), "x"
    # Bound precision/size for malformed metadata without losing the source text.
    if len(number) > 20:
        return None
    return Decimal(number), len(number.partition(".")[2]), suffix


def discounted_credits(credits, discount):
    original = _credits(credits)
    explicit = _text(discount.get("discountedCredits"))
    if explicit:
        if not _text(credits):
            return None
        parsed = _credits(explicit)
        if not parsed:
            return explicit
        value, precision, suffix = parsed
        precision = max(precision, original[1] if original else 0, 2)
        return f"{value:.{precision}f}{suffix}"
    factor = _number(discount.get("factor"))
    if not original or factor is None or not 0 < factor < 1:
        return None
    value, precision, suffix = original
    value *= Decimal(str(factor))
    # App rounds the computed multiplier to at most four added decimal places.
    rounded = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    decimals = max(0, -rounded.normalize().as_tuple().exponent)
    precision = max(precision, decimals)
    return f"{value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP):.{precision}f}{suffix}"


def _schedule_text(promotion):
    schedule = _mapping(promotion.get("schedule"))
    parts = []
    if _text(schedule.get("validFrom")):
        parts.append("开始：" + schedule["validFrom"])
    if _text(schedule.get("validUntil")):
        parts.append("结束（不含）：" + schedule["validUntil"])
    intervals = []
    for interval in _array(schedule.get("daily")):
        interval = _mapping(interval)
        start, end = _minute(interval.get("start")), _minute(interval.get("end"))
        if start is not None and end is not None:
            intervals.append("全天" if start == end else f"{interval['start']}—{'次日 ' if start > end else ''}{interval['end']}")
    if intervals:
        parts.append("每日 " + "、".join(intervals))
    if _text(schedule.get("timezone")):
        parts.append(schedule["timezone"])
    return "；".join(parts)


def _promotion(model, product_promotions, now):
    own = _array(model.get("promotions"))
    candidates = own if own else [p for p in product_promotions if model["id"] in _array(p.get("modelIds"))]
    candidates = [p for p in candidates if isinstance(p, dict) and isinstance(p.get("id"), str) and p.get("enabled") is not False]
    active = [p for p in candidates if promotion_active(p, now)]
    if not candidates:
        return None
    chosen = max(active or candidates, key=lambda p: _number(p.get("priority")) or 0)
    is_active = promotion_active(chosen, now)
    badge = _mapping(chosen.get("badge"))
    label = _text(badge.get("label")) if is_active or badge.get("display") == "always" else ""
    description = ""
    discount = None
    if is_active:
        hover = _mapping(chosen.get("hover"))
        description = _text(hover.get("textZh")) or _text(hover.get("textEn"))
        if chosen.get("kind") == "discount":
            discount = discounted_credits(model.get("credits"), _mapping(chosen.get("discount")))
    if not (label or description or discount):
        return None
    return {"id": chosen["id"], "label": label, "description": description,
            "active": is_active, "discounted_credits": discount, "schedule_text": _schedule_text(chosen)}


def _expand_auto_ids(model_ids):
    if "auto" not in model_ids:
        return model_ids
    expanded = []
    for mid in model_ids:
        if mid == "auto":
            expanded.extend(LEGACY_AUTO_ROUTE_IDS)
        elif mid not in LEGACY_AUTO_ROUTE_IDS:
            expanded.append(mid)
    return expanded


def _selected_models(config):
    """Select the App's agent catalog and expand legacy auto into current routes."""
    models = [m for m in _array(config.get("models")) if isinstance(m, dict) and _text(m.get("id"))]
    # App expands auto in models, agent references and availableModels before
    # selection. Keep remote metadata only, without fabricating missing rates.
    by_id = {}
    for model in models:
        by_id.setdefault(model["id"], model)
    models = [by_id[mid] for mid in _expand_auto_ids([m["id"] for m in models]) if mid in by_id]
    available = set(_expand_auto_ids([m for m in _array(config.get("availableModels")) if isinstance(m, str)]))
    if available:
        models = [m for m in models if m["id"] in available or m["id"].removeprefix("custom-local:") in available]
    agents = [a for a in _array(config.get("agents")) if isinstance(a, dict) and _text(a.get("name"))]
    agent = next((a for a in agents if "default" in _array(a.get("tags"))), None)
    if agent is None:
        agent = next((a for a in agents if a["name"] == "cli"), None)
    if agent is None:
        agent = next((a for a in agents if any(isinstance(m, str) for m in _array(a.get("models")))), None)
    selected = []
    if agent is None:
        selected = models
    else:
        for target in _expand_auto_ids(_array(agent.get("models"))):
            if isinstance(target, str):
                found = next((m for m in models if target in (m["id"], m.get("name"))), None)
                if found is not None:
                    selected.append(found)
        allowed = {m["id"] for m in selected}
        # Missing route metadata must not broaden an auto-only agent to all chat models.
        restricted = bool(allowed) or "auto" in _array(agent.get("models"))
        for model in models:
            custom = "custom" in _array(model.get("tags")) or model["id"].startswith(("custom:", "custom-local:"))
            if restricted and model["id"] not in allowed and not custom:
                continue
            if custom or "chat" in _array(model.get("tags")):
                selected.append(model)
    seen, unique = set(), []
    for model in selected:
        if model["id"] not in seen:
            seen.add(model["id"])
            unique.append(model)
    return unique


def evaluate_models(config, now):
    selected = _selected_models(config)
    features = _mapping(config.get("productFeaturesConfig"))
    lines = _array(_mapping(features.get("ModelRateLimitCap")).get("lines"))
    types = {}
    paid_ids = set()
    for line in lines:
        line = _mapping(line)
        paid_id = _text(line.get("paidId"))
        if paid_id:
            paid_ids.add(paid_id)
        for field, kind in (("freeId", "free"), ("paidId", "paid")):
            if _text(line.get(field)):
                mid = _text(line[field])
                types[mid] = kind if mid not in types or types[mid] == kind else "unmarked"
    promotions = [p for p in _array(config.get("modelPromotions")) if isinstance(p, dict) and isinstance(p.get("id"), str)]
    result = []
    for model in selected:
        credits = _text(model.get("credits"))
        # All accounts use the free-account catalog. A positive credit multiplier
        # is not a paid-account requirement; only explicit paid routes are hidden.
        if not credits or model["id"] in paid_ids:
            continue
        promotion = _promotion(model, promotions, now)
        reasoning = _mapping(model.get("reasoning"))
        efforts = []
        for effort in _array(reasoning.get("supportedEfforts")):
            effort = _text(effort)
            effort = "off" if effort == "none" else effort
            if effort and effort not in efforts:
                efforts.append(effort)
        if reasoning.get("canDisableThinking") is True and "off" not in efforts:
            efforts.insert(0, "off")
        result.append({"id": model["id"], "name": _text(model.get("name")) or model["id"],
                       "is_dynamic_route": model["id"] in LEGACY_AUTO_ROUTE_IDS,
                       "description": _text(model.get("descriptionZh")) or _text(model.get("description")),
                       "credits": credits, "effective_credits": (promotion or {}).get("discounted_credits") or credits,
                       "line_type": types.get(model["id"], "unmarked"), "promotion": promotion,
                       "supports_images": model.get("supportsImages") is True,
                       "supports_reasoning": model.get("supportsReasoning") is True,
                       "max_input_tokens": _token_limit(model.get("maxInputTokens")),
                       "max_output_tokens": _token_limit(model.get("maxOutputTokens")),
                       "reasoning_efforts": efforts})
    return result


class ModelCatalogManager:
    """Shared catalog access with isolated account caches and request credentials."""

    def __init__(self, clock=time.time, client_factory=None):
        self.clock, self.client_factory = clock, client_factory
        self._catalogs = WeakKeyDictionary()
        self._lock = threading.Lock()
        self._credential = ContextVar("model_catalog_credential", default=())

    @contextmanager
    def use_credential(self, credential):
        """Pin an account snapshot, including an explicitly missing account."""
        token = self._credential.set((credential,))
        try:
            yield
        finally:
            self._credential.reset(token)

    def get(self, credential, force=False):
        """Return account metadata, preserving refresh errors for the admin UI."""
        if credential is None:
            raise HTTPException(400, "请先选定模型查询账号")
        with self._lock:
            catalog = self._catalogs.get(credential)
            if catalog is None:
                catalog = _AccountModelCatalog(clock=self.clock, client_factory=self.client_factory)
                self._catalogs[credential] = catalog
        return catalog.get(credential, force=force)

    def get_model_ids(self, default_credential=None):
        """Return usable IDs, or an empty list when remote data is unavailable."""
        bound = self._credential.get()
        credential = bound[0] if bound else default_credential
        if credential is None:
            return []
        try:
            result = self.get(credential)
            if result["stale"] or result["error"]:
                return []
            return [model["id"] for model in result["models"]]
        except (HTTPException, httpx.HTTPError, OSError, ValueError, RuntimeError):
            return []


class _AccountModelCatalog:
    def __init__(self, clock=time.time, client_factory=None):
        self.clock = clock
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=15, follow_redirects=False))
        self.lock = threading.Lock()
        self.cache = None

    @staticmethod
    def _fingerprint(manager):
        with manager._lock:
            return hashlib.sha256(manager.path.read_bytes()).hexdigest()

    def _request_headers(self, manager):
        # get_headers owns a non-reentrant lock, so bracket it with snapshots.
        # Its normal token refresh changes the file; repeat once for that case.
        for _ in range(2):
            before = self._fingerprint(manager)
            headers = httpx.Headers(manager.get_headers())
            after = self._fingerprint(manager)
            if before == after:
                return headers, after
        raise HTTPException(400, "登录凭据正在变更，请稍后重新加载模型信息")

    def get(self, manager, force=False):
        with self.lock:
            fingerprint = None
            error = None
            try:
                fingerprint = self._fingerprint(manager)
                previous = self.cache
                if previous and previous["fingerprint"] != fingerprint:
                    self.cache = None
                entry = self.cache
                if force or not entry or self.clock() - entry["fetched_at"] >= CACHE_TTL:
                    # get_headers owns the manager lock and may refresh credentials.
                    # No manager lock spans the configuration HTTP request.
                    headers, fingerprint = self._request_headers(manager)
                    if headers.get("X-Domain", "").lower().strip() not in DOMAINS:
                        raise HTTPException(400, "模型信息查询目前仅支持国内 WorkBuddy / CodeBuddy 账号")
                    entry = self.cache
                    if entry and entry["fingerprint"] != fingerprint:
                        self.cache = None
                    headers.update({"User-Agent": USER_AGENT, "X-Product": "SaaS", "X-Requested-With": "XMLHttpRequest"})
                    with self.client_factory() as client:
                        response = client.get(CONFIG_URL, headers=headers, follow_redirects=False)
                    if response.status_code != 200:
                        raise ValueError("config unavailable")
                    data = response.json()
                    if not isinstance(data, dict) or data.get("code", 0) != 0 or not isinstance(data.get("data"), dict):
                        raise ValueError("invalid config envelope")
                    config = data["data"]
                    if not isinstance(config.get("models"), list):
                        raise ValueError("missing model catalog")
                    if fingerprint != self._fingerprint(manager):
                        raise HTTPException(400, "登录凭据已变更，请重新加载模型信息")
                    # Cache only fields used by this feature, never the full product config.
                    config = {k: config.get(k) for k in ("models", "agents", "availableModels", "modelPromotions", "productFeaturesConfig")}
                    self.cache = {"fingerprint": fingerprint, "config": config, "fetched_at": self.clock()}
            except HTTPException:
                raise
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError, TypeError):
                error = FETCH_ERROR
            # A concurrent credential replacement must not revive the old cache,
            # including when a refresh failed before producing request headers.
            try:
                fingerprint = self._fingerprint(manager)
            except OSError:
                fingerprint = None
            entry = self.cache
            if not entry or fingerprint is None or entry["fingerprint"] != fingerprint:
                raise HTTPException(502, error or FETCH_ERROR)
            now = self.clock()
            config = entry["config"]
            models = evaluate_models(config, now)
            # Keep failed refresh visible until a later successful fetch.
            if error:
                entry["error"] = error
            return {"models": models,
                    "fetched_at": entry["fetched_at"], "evaluated_at": now,
                    "stale": bool(entry.get("error")) or now - entry["fetched_at"] >= CACHE_TTL,
                    "error": entry.get("error"), "total_models": len(_array(config.get("models"))),
                    "filtered_models": len(models)}


model_catalog_manager = ModelCatalogManager()
