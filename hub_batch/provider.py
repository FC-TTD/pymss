"""Provider contracts shared by profile and ordinary model execution.

This module deliberately contains no HTTP or Gateway concerns.  A provider
resolves immutable profile manifests and uses :class:`ModelCoordinator` to
ensure that a Hub managed process has one resident model at a time.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Callable, Iterator, Mapping


@dataclass
class SharedModel:
    """Resident model wrapper shared by native and provider callers.

    ``metadata`` is optional because a provider-only load does not need the
    native server metadata. The native adapter can derive it from the selected
    model spec when it joins an existing provider residency.
    """

    separator: Any
    metadata: dict[str, Any] | None = None

    def close(self) -> None:
        close = getattr(self.separator, "close", None)
        if callable(close):
            close()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class ProfileManifest:
    """Immutable public profile implementation description.

    ``dag`` is an opaque provider implementation reference.  It is deliberately
    not a checkpoint name in the public Gateway contract.
    """

    name: str
    version: str
    dag: str
    outputs: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    max_concurrency: int = 1
    memory_budget_bytes: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip() or not self.dag.strip():
            raise ValueError("profile name, version and dag are required")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "options", _jsonable(dict(self.options)))

    @property
    def digest(self) -> str:
        payload = {
            "name": self.name,
            "version": self.version,
            "dag": self.dag,
            "outputs": list(self.outputs),
            "models": list(self.models),
            "max_concurrency": self.max_concurrency,
            "memory_budget_bytes": self.memory_budget_bytes,
            "options": self.options,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "dag": self.dag,
            "outputs": list(self.outputs),
            "models": list(self.models),
            "max_concurrency": self.max_concurrency,
            "memory_budget_bytes": self.memory_budget_bytes,
            "options": dict(self.options),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProfileManifest":
        return cls(
            name=str(value["name"]),
            version=str(value["version"]),
            dag=str(value["dag"]),
            outputs=tuple(value.get("outputs", ())),
            models=tuple(value.get("models", ())),
            max_concurrency=int(value.get("max_concurrency", 1)),
            memory_budget_bytes=value.get("memory_budget_bytes"),
            options=value.get("options", {}),
        )


class ProfileRegistry:
    """Versioned profile registry; registration is copy-on-write."""

    def __init__(self, manifests: Mapping[str, ProfileManifest] | list[ProfileManifest] | None = None):
        if manifests is None:
            self._profiles = {}
        elif isinstance(manifests, Mapping):
            self._profiles = dict(manifests)
        else:
            self._profiles = {f"{item.name}@{item.version}": item for item in manifests}

    def register(self, manifest: ProfileManifest) -> None:
        key = f"{manifest.name}@{manifest.version}"
        current = self._profiles.get(key)
        if current is not None and current.digest != manifest.digest:
            raise ValueError(f"profile version already registered with different manifest: {key}")
        self._profiles[key] = manifest

    def resolve(self, name: str, version: str | None = None) -> ProfileManifest:
        if version is None:
            candidates = [item for item in self._profiles.values() if item.name == name]
            if not candidates:
                raise KeyError(name)
            return sorted(candidates, key=lambda item: item.version)[-1]
        try:
            return self._profiles[f"{name}@{version}"]
        except KeyError:
            raise KeyError(f"{name}@{version}") from None

    @classmethod
    def from_json(cls, path: str | Path) -> "ProfileRegistry":
        payload = json.loads(Path(path).read_text())
        entries = payload if isinstance(payload, list) else payload.get("profiles", [payload])
        registry = cls()
        for entry in entries:
            registry.register(ProfileManifest.from_dict(entry))
        return registry


class ModelCoordinator:
    """Single-resident model coordinator for native UI and provider work.

    Calls for the resident model may run concurrently up to the declared
    capacity.  A different model waits for all active calls to finish, closes
    the old separator, and only then loads the new one.
    """

    def __init__(self, factory: Callable[..., Any], *, close: Callable[[Any], None] | None = None):
        self._factory = factory
        self._close = close or (lambda value: getattr(value, "close", lambda: None)())
        self._condition = threading.Condition(threading.RLock())
        self._resident_key: str | None = None
        self._resident: Any = None
        self._active = 0
        self._capacity = 1
        self._switching = False
        self._closed = False

    @property
    def resident_key(self) -> str | None:
        with self._condition:
            return self._resident_key

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    def _key(self, kwargs: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps(_jsonable(kwargs), sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def key_for(kwargs: Mapping[str, Any]) -> str:
        """Return a stable residency key for a model description."""
        return hashlib.sha256(json.dumps(_jsonable(kwargs), sort_keys=True, default=str).encode()).hexdigest()

    @contextmanager
    def lease(
        self,
        *,
        max_concurrency: int = 1,
        residency_key: str | None = None,
        factory: Callable[..., Any] | None = None,
        replace: bool = False,
        **kwargs: Any,
    ) -> Iterator[Any]:
        key = residency_key or self._key(kwargs)
        with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError("model coordinator is closed")
                if self._resident_key == key and not replace and not self._switching and self._active < self._capacity:
                    self._active += 1
                    break
                if self._resident_key is None and not self._switching:
                    self._resident = (factory or self._factory)(**kwargs)
                    self._resident_key = key
                    self._capacity = max(1, int(max_concurrency))
                    self._active = 1
                    break
                if (self._resident_key != key or replace) and self._active == 0 and not self._switching:
                    self._switching = True
                    old = self._resident
                    self._resident = None
                    self._resident_key = None
                    try:
                        if old is not None:
                            self._close(old)
                        self._resident = (factory or self._factory)(**kwargs)
                        self._resident_key = key
                        self._capacity = max(1, int(max_concurrency))
                        self._active = 1
                    finally:
                        self._switching = False
                        self._condition.notify_all()
                    break
                self._condition.wait()
            resident = self._resident
        try:
            yield resident
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def run(self, fn: Callable[[Any], Any], *, max_concurrency: int = 1, **kwargs: Any) -> Any:
        with self.lease(max_concurrency=max_concurrency, **kwargs) as model:
            return fn(model)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            while self._active or self._switching:
                self._condition.wait()
            resident, self._resident = self._resident, None
            self._resident_key = None
            if resident is not None:
                self._close(resident)
            self._condition.notify_all()


class CoordinatedSeparator:
    """Lazy separator facade backed by a shared :class:`ModelCoordinator`.

    DAG execution asks the facade for model metadata and then calls
    ``separate``. Each call acquires a coordinator lease, so a cached facade
    never owns a second hidden model and the lease covers the complete native
    invocation.
    """

    def __init__(
        self,
        coordinator: ModelCoordinator,
        factory: Callable[..., Any],
        kwargs: Mapping[str, Any],
        *,
        max_concurrency: int = 1,
        residency_key: str | None = None,
    ) -> None:
        object.__setattr__(self, "_coordinator", coordinator)
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_kwargs", dict(kwargs))
        object.__setattr__(self, "_max_concurrency", max(1, int(max_concurrency)))
        object.__setattr__(
            self,
            "_residency_key",
            residency_key or ModelCoordinator.key_for(dict(kwargs)),
        )
        object.__setattr__(self, "_progress_callback", None)

    def _target(self, resident: Any) -> Any:
        return getattr(resident, "separator", resident)

    @contextmanager
    def _lease(self) -> Iterator[Any]:
        with self._coordinator.lease(
            max_concurrency=self._max_concurrency,
            residency_key=self._residency_key,
            factory=self._factory,
            **self._kwargs,
        ) as resident:
            yield self._target(resident)

    def separate(self, *args: Any, **kwargs: Any) -> Any:
        with self._lease() as separator:
            callback = self._progress_callback
            if callback is not None:
                try:
                    separator.progress_callback = callback
                except Exception:
                    pass
            return separator.separate(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        with self._lease() as separator:
            return getattr(separator, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "progress_callback":
            object.__setattr__(self, "_progress_callback", value)
            return
        object.__setattr__(self, name, value)

    def __enter__(self) -> "CoordinatedSeparator":
        return self

    def __exit__(self, *_exc: Any) -> None:
        # The coordinator owns residency. A DAG's temporary context must not
        # close a model still used by native UI or another task.
        return None

    def close(self) -> None:
        # SeparatorCache.close() calls close on cached values. Eviction is
        # performed by ModelCoordinator when a different residency is leased.
        return None
