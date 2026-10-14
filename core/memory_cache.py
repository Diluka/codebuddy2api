"""Versioned in-memory values with coalesced loading and optional stale fallback."""
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace


class CacheInvalidated(RuntimeError):
    """A version changed, became unreadable, or was actively invalidated."""


@dataclass(frozen=True)
class CacheResult:
    found: bool
    value: object = None
    updated_at: float | None = None
    stale: bool = False


@dataclass
class _Entry:
    result: CacheResult
    expires_at: float | None
    generation: int
    retry_at: float = 0
    error: Exception | None = None


@dataclass
class _Scope:
    lock: object = field(default_factory=threading.Lock)
    load_lock: object = field(default_factory=threading.Lock)
    entries: dict = field(default_factory=dict)
    revision: object = None
    generation: int = 0
    active_generation: int | None = None
    mutation: int = 0


class MemoryCache:
    def __init__(self, ttl=None, *, stale_if_error=False, retry_ttl=0,
                 batch_size=None, errors=(Exception,), clock=time.monotonic,
                 wall_clock=time.time):
        if (ttl is not None and ttl < 0) or retry_ttl < 0:
            raise ValueError("cache lifetimes must be non-negative")
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.ttl, self.stale_if_error, self.retry_ttl = ttl, stale_if_error, retry_ttl
        self.batch_size, self.errors = batch_size, errors
        self.clock, self.wall_clock = clock, wall_clock
        self._scopes = {}
        self._lock = threading.Lock()

    def _scope(self, scope):
        with self._lock:
            return self._scopes.setdefault(scope, _Scope())

    def _success(self, value, generation):
        expires = None if self.ttl is None else self.clock() + self.ttl
        return _Entry(CacheResult(True, value, self.wall_clock()), expires, generation)

    @contextmanager
    def _loading(self, state):
        with state.load_lock:
            with state.lock:
                state.active_generation = state.generation
            try:
                yield
            finally:
                with state.lock:
                    state.active_generation = None

    def get(self, key, load, *, scope=None, revision=None, prepare=None, force=False):
        return self.get_many([key], lambda keys, context: {key: load(key, context)},
                             scope=scope, revision=revision, prepare=prepare, force=force)[key]

    def get_many(self, keys, load, *, scope=None, revision=None, prepare=None, force=False):
        """Load due keys in batches; prepare() returns a paired version/context.

        Loading serializes within a namespace. Its small state lock remains free
        during callbacks so set/invalidate/clear can reject an in-flight result.
        Missing mapping keys count as failures (KeyError without stale fallback).
        """
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return {}
        state = self._scope(scope)
        with state.lock:
            # Waiters also reuse batches completed earlier in the active call.
            observed = state.generation if state.active_generation is None else state.active_generation
        with self._loading(state):
            with state.lock:
                mutation, version = state.mutation, state.revision

            def guard():
                if state.mutation != mutation:
                    raise CacheInvalidated("cache was actively updated or invalidated")

            def reject():
                with state.lock:
                    guard()
                    state.entries.clear()
                    state.revision = None
                    state.mutation += 1
                raise CacheInvalidated("cache version changed or could not be confirmed")

            def current_version():
                with state.lock:
                    guard()
                try:
                    current = revision() if revision is not None else version
                except Exception:
                    reject()
                with state.lock:
                    guard()
                return current

            def validate():
                if current_version() != version:
                    reject()

            def due_keys():
                now = self.clock()
                with state.lock:
                    guard()
                    if state.revision != version:
                        state.entries.clear()
                        state.revision = version
                    pending = []
                    for key in keys:
                        entry = state.entries.get(key)
                        if entry is None:
                            pending.append(key)
                            continue
                        expired = entry.expires_at is not None and now >= entry.expires_at
                        due = now >= entry.retry_at and (entry.result.stale or expired)
                        if entry.generation <= observed and (force or due):
                            pending.append(key)
                    return pending

            version = current_version()
            pending = due_keys()
            context, preparation_error = None, None
            if pending and prepare is not None:
                try:
                    version, context = prepare()
                except Exception as error:
                    validate()
                    if isinstance(error, CacheInvalidated) or not isinstance(error, self.errors):
                        raise
                    preparation_error = error
                validate()
                # A new prepared version invalidates even previously fresh keys.
                pending = due_keys()
            batch_size = self.batch_size or len(keys)
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset:offset + batch_size]
                validate()
                error, values = preparation_error, {}
                if error is None:
                    try:
                        values = load(batch, context)
                        if not isinstance(values, Mapping):
                            raise TypeError("cache loader must return a mapping")
                    except Exception as caught:
                        validate()
                        if isinstance(caught, CacheInvalidated) or not isinstance(caught, self.errors):
                            raise
                        error, values = caught, {}
                validate()
                now = self.clock()
                with state.lock:
                    guard()
                    state.generation += 1
                    for key in batch:
                        if key in values:
                            entry = self._success(values[key], state.generation)
                        else:
                            previous = state.entries.get(key)
                            result = replace(previous.result, stale=True) if previous else CacheResult(False, stale=True)
                            entry = _Entry(result, previous.expires_at if previous else None,
                                           state.generation, now + self.retry_ttl,
                                           error if error is not None else KeyError(key))
                        state.entries[key] = entry
            validate()
            now = self.clock()
            with state.lock:
                guard()
                results = {}
                for key in keys:
                    entry = state.entries[key]
                    if entry.error is not None and not self.stale_if_error:
                        raise entry.error
                    expired = entry.expires_at is not None and now >= entry.expires_at
                    results[key] = replace(entry.result, stale=entry.result.stale or expired)
                return results

    def set(self, key, value, *, scope=None, revision=None):
        state = self._scope(scope)
        with state.lock:
            state.mutation += 1
            state.generation += 1
            if state.revision != revision:
                state.entries.clear()
                state.revision = revision
            state.entries[key] = self._success(value, state.generation)

    def invalidate(self, key, *, scope=None):
        state = self._scope(scope)
        with state.lock:
            state.mutation += 1
            state.entries.pop(key, None)

    def clear(self, *, scope=None):
        state = self._scope(scope)
        with state.lock:
            state.mutation += 1
            state.entries.clear()
            state.revision = None
