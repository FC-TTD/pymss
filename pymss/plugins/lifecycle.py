"""Manifest-driven plugin lifecycle: discover → resolve → activate → deactivate.

Implements docs/plugin-protocol.md §4. Lives above the legacy folder scan:
manifest-less plugins keep their exact old behaviour; manifest plugins get
dependency resolution, ordered activation, and rollback of their own
registrations on failure.
"""

from __future__ import annotations

import importlib
import importlib.util
from importlib.machinery import ModuleSpec
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

from .manifest import API_VERSION, ManifestError, PluginManifest, parse_entry_point, read_manifest


def _remove_plugin_modules(namespace: str) -> None:
    for name in tuple(sys.modules):
        if name == namespace or name.startswith(namespace + "."):
            sys.modules.pop(name, None)


def _import_manifest_entry(directory: Path, module_name: str, explicit: bool) -> tuple[ModuleType, str]:
    """Load plugin-local modules beneath a private package without changing sys.path."""
    namespace = f"_pymss_manifest_plugin_{uuid4().hex}"
    entry_file = directory / "__init__.py"
    if explicit and not all(part.isidentifier() for part in module_name.split(".")):
        raise ImportError(f"invalid plugin entry module {module_name!r}")
    try:
        if entry_file.is_file():
            spec = importlib.util.spec_from_file_location(
                namespace, entry_file, submodule_search_locations=[str(directory)],
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load plugin entry {entry_file}")
            root = importlib.util.module_from_spec(spec)
            sys.modules[namespace] = root
            spec.loader.exec_module(root)
        else:
            root = ModuleType(namespace)
            root.__path__ = [str(directory)]
            root.__package__ = namespace
            root.__spec__ = ModuleSpec(namespace, loader=None, is_package=True)
            root.__spec__.submodule_search_locations = root.__path__
            sys.modules[namespace] = root
        if not explicit:
            module = root if entry_file.is_file() else importlib.import_module(f"{namespace}.plugin")
        elif module_name == directory.name and entry_file.is_file() and not (
            (directory / f"{module_name}.py").is_file() or (directory / module_name).is_dir()
        ):
            module = root
        else:
            module = importlib.import_module(f"{namespace}.{module_name}")
        return module, namespace
    except BaseException:
        _remove_plugin_modules(namespace)
        raise

_CONSTRAINT_RE = re.compile(r"^(==|>=|<=|>|<|~=)?\s*(.+)$")


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.findall(r"\d+", text.split("+", 1)[0]):
        parts.append(int(chunk))
    while len(parts) < 3:
        parts.append(0)
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def satisfies(version: str, constraint: str) -> bool:
    """Small PEP 440 subset comparator (no pre-release algebra). Comma = AND."""
    return all(_satisfies_one(version, part) for part in constraint.split(",") if part.strip())


def _satisfies_one(version: str, constraint: str) -> bool:
    match = _CONSTRAINT_RE.match(constraint.strip())
    if not match:
        return False
    operator, raw = match.group(1) or "==", match.group(2).strip()
    have, want = _parse_version(version), _parse_version(raw)
    if operator == "==":
        # Normalize missing trailing components: 1.2 == 1.2.0
        length = max(len(have), len(want))
        have += (0,) * (length - len(have))
        want += (0,) * (length - len(want))
        return have == want
    if operator == ">=":
        return have >= want
    if operator == "<=":
        return have <= want
    if operator == ">":
        return have > want
    if operator == "<":
        return have < want
    if operator == "~=":
        release = re.match(r"^(\d+(?:\.\d+)+)", raw)
        if release is None:
            return False
        prefix = tuple(int(part) for part in release.group(1).split("."))[:-1]
        ceiling = prefix[:-1] + (prefix[-1] + 1,)
        ceiling += (0,) * max(0, 3 - len(ceiling))
        return want <= have < ceiling
    return False


@dataclass
class PluginState:
    directory: Path
    manifest: PluginManifest | None
    name: str
    status: str = "discovered"          # discovered|resolved|activated|blocked|failed
    error: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    contributions: dict[str, Any] = field(default_factory=dict)


class PluginManager:
    """Owns manifest plugins for one host (runtime/process)."""

    def __init__(self, plugins_dir: Path | None = None):
        from .loader import get_plugins_dir

        self.plugins_dir = Path(plugins_dir) if plugins_dir else get_plugins_dir()
        self._states: dict[str, PluginState] = {}
        self._activated: list[str] = []

    # ---- discover ----

    def discover(self) -> list[PluginState]:
        self._states.clear()
        root = self.plugins_dir
        if not root.exists():
            return []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            has_entry = (child / "__init__.py").exists() or (child / "plugin.py").exists()
            has_manifest = (child / "pyproject.toml").exists()
            if not has_entry and not has_manifest:
                continue
            try:
                manifest = read_manifest(child)
            except ManifestError as exc:
                state = PluginState(directory=child, manifest=None, name=child.name, status="blocked", error=str(exc))
                self._states[child.name] = state
                continue
            if manifest is None:
                # Legacy (manifest-less) plugin: handled by the folder scan, not
                # by the lifecycle manager. Keep behaviour identical to before.
                continue
            state = PluginState(directory=child, manifest=manifest, name=child.name)
            self._states[child.name] = state
        return list(self._states.values())

    # ---- resolve ----

    def resolve(self, *, pymss_version: str | None = None) -> list[PluginState]:
        """Resolve dependency graph; mark unsatisfiable plugins blocked."""
        by_id: dict[str, PluginState] = {}
        duplicates: dict[str, list[PluginState]] = {}
        for state in self._states.values():
            if state.manifest is not None:
                plugin_id = state.manifest.plugin_id
                if plugin_id in by_id:
                    duplicates.setdefault(plugin_id, [by_id[plugin_id]]).append(state)
                else:
                    by_id[plugin_id] = state
        for plugin_id, states in duplicates.items():
            error = f"duplicate plugin id {plugin_id!r}: {', '.join(state.name for state in states)}"
            for state in states:
                state.status, state.error = "blocked", error

        def check(state: PluginState, seen: tuple[str, ...]) -> None:
            if state.status in {"resolved", "blocked"}:
                return
            if state.name in seen:
                state.status, state.error = "blocked", f"dependency cycle: {' -> '.join(seen + (state.name,))}"
                return
            manifest = state.manifest
            assert manifest is not None
            if manifest.api_version > API_VERSION:
                state.status = "blocked"
                state.error = (
                    f"plugin requires api_version {manifest.api_version}; this pymss supports {API_VERSION}"
                )
                return
            if manifest.requires_pymss and pymss_version and not satisfies(pymss_version, manifest.requires_pymss):
                state.status = "blocked"
                state.error = f"requires pymss {manifest.requires_pymss}, installed {pymss_version}"
                return
            for dep_id, constraint in manifest.dependencies.items():
                target = by_id.get(dep_id)
                if target is None or target.manifest is None or target.manifest.version is None:
                    state.blocked_by.append(dep_id)
                    state.status = "blocked"
                    state.error = f"missing dependency {dep_id} {constraint}"
                    continue
                check(target, seen + (state.name,))
                if target.status == "blocked":
                    state.blocked_by.append(dep_id)
                    state.status = "blocked"
                    state.error = f"dependency {dep_id} is not loadable ({target.error})"
                    continue
                if not satisfies(target.manifest.version, constraint):
                    state.blocked_by.append(dep_id)
                    state.status = "blocked"
                    state.error = (
                        f"dependency {dep_id} {constraint} unsatisfied by installed {target.manifest.version}"
                    )
            if state.status != "blocked":
                state.status = "resolved"

        for state in self._states.values():
            if state.manifest is not None:
                check(state, ())
        return list(self._states.values())

    # ---- activate ----

    def activation_order(self) -> list[PluginState]:
        """Resolved manifest plugins ordered dependencies-first (stable)."""
        states = [s for s in self._states.values() if s.manifest is not None and s.status == "resolved"]
        resolved: set[str] = set()
        ordered: list[PluginState] = []

        def visit(state: PluginState, trail: tuple[str, ...]) -> None:
            if state.name in resolved or state.name in trail:
                return
            manifest = state.manifest
            assert manifest is not None
            for dep_id in manifest.dependencies:
                target = next(
                    (s for s in states if s.manifest and s.manifest.plugin_id == dep_id), None
                )
                if target is not None:
                    visit(target, trail + (state.name,))
            resolved.add(state.name)
            ordered.append(state)

        for state in states:
            visit(state, ())
        return ordered

    def activate(self, registration_factory) -> list[dict[str, Any]]:
        """Activate resolved plugins in dependency order.

        ``registration_factory(source)`` returns the registration handle passed
        to the plugin's ``setup()``. A plugin whose setup fails is rolled back
        (registrations removed by the caller-provided ``rollback`` on the
        handle) and recorded as failed; dependents become blocked.
        """
        report: list[dict[str, Any]] = []
        ordered = self.activation_order()
        blocked_before_activation = {
            s.name for s in self._states.values() if s.status == "blocked"
        }
        for state in ordered:
            manifest = state.manifest
            assert manifest is not None
            if state.status == "blocked":
                continue
            module_name, attr = parse_entry_point(manifest.entry, state.directory)
            handle = None
            namespace = None
            try:
                handle = registration_factory(f"plugin:{manifest.plugin_id}")
                module, namespace = _import_manifest_entry(state.directory, module_name, bool(manifest.entry))
                setup = getattr(module, attr, None)
                if setup is None:
                    raise AttributeError(f"entry {module_name}:{attr} has no {attr!r}")
                setup(handle)
                state.status = "activated"
                state.contributions = manifest.to_dict()["contributions"]
                self._activated.append(state.name)
            except Exception as exc:  # noqa: BLE001 — plugin code must not break the host
                state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
                # Roll back anything the failing setup() already registered so a
                # half-installed plugin leaves no orphaned operations behind.
                if handle is not None:
                    try:
                        handle.rollback()
                    except Exception:  # noqa: BLE001 — rollback must not mask the failure
                        pass
                if namespace is not None:
                    _remove_plugin_modules(namespace)
                self._block_transitive_dependents(manifest.plugin_id)
        # Rows are built after the whole pass so blocked/failed dependents
        # reflect their final state regardless of activation order.
        report.extend(self._row(state) for state in ordered)
        blocked_before_activation.difference_update(
            row["name"] for row in report
        )
        for state in self._states.values():
            if state.name not in {row["name"] for row in report}:
                report.append(self._row(state))
        return report

    def _block_transitive_dependents(self, failed_plugin_id: str) -> None:
        """Mark every plugin that (transitively) depends on a failed plugin blocked."""
        changed = True
        failed_ids = {failed_plugin_id}
        while changed:
            changed = False
            for other in self._states.values():
                other_manifest = other.manifest
                if other_manifest is None or other.status in {"blocked", "failed"}:
                    continue
                deps = set(other_manifest.dependencies)
                failed_dependencies = deps & failed_ids
                if failed_dependencies:
                    other.status = "blocked"
                    other.error = f"dependency {failed_plugin_id} failed to activate"
                    other.blocked_by.extend(sorted(failed_dependencies))
                    failed_ids.add(other_manifest.plugin_id)
                    changed = True

    # ---- report ----

    def _row(self, state: PluginState) -> dict[str, Any]:
        manifest = state.manifest
        if manifest is None:
            return {"name": state.name, "loaded": False, "error": state.error, "legacy": True, "api_version": 0}
        row = {
            "name": state.name,
            "loaded": state.status == "activated",
            "error": state.error,
            "legacy": False,
            **manifest.to_dict(),
            "status": state.status,
            "blocked_by": state.blocked_by,
        }
        row.pop("contributions", None) if state.status != "activated" else None
        return row

    def report(self) -> list[dict[str, Any]]:
        return [self._row(state) for state in self._states.values()]


def pymss_package_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("pymss")
    except Exception:  # noqa: BLE001 — version probe must never break loading
        return None
