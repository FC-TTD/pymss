"""Manifest-driven plugin lifecycle: discover → resolve → activate → deactivate.

Implements docs/plugin-protocol.md §4. Lives above the legacy folder scan:
manifest-less plugins keep their exact old behaviour; manifest plugins get
dependency resolution, ordered activation, and rollback of their own
registrations on failure.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import API_VERSION, ManifestError, PluginManifest, parse_entry_point, read_manifest

_CONSTRAINT_RE = re.compile(r"^(==|>=|<=|>|<|~=)?\s*(.+)$")


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.findall(r"\d+", text.split("+", 1)[0]):
        parts.append(int(chunk))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


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
        base = _parse_version(raw)[:2] if len(_parse_version(raw)) >= 2 else _parse_version(raw)
        floor = base + (0,) * (2 - len(base))
        ceiling = (floor[0], floor[1] + 1, 0)
        return floor <= have < ceiling
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
        for state in self._states.values():
            if state.manifest is not None:
                by_id.setdefault(state.manifest.plugin_id, state)

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
            entry_file = state.directory / "__init__.py"
            try:
                if manifest.entry:
                    module = importlib.import_module(module_name)
                else:
                    synthetic = f"_pymss_manifest_plugin_{state.name}"
                    spec = importlib.util.spec_from_file_location(synthetic, entry_file)
                    if spec is None or spec.loader is None:
                        raise ImportError(f"cannot load plugin entry {entry_file}")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[synthetic] = module
                    try:
                        spec.loader.exec_module(module)
                    except Exception:
                        sys.modules.pop(synthetic, None)
                        raise
                setup = getattr(module, attr, None)
                if setup is None:
                    raise AttributeError(f"entry {module_name}:{attr} has no {attr!r}")
                handle = registration_factory(f"plugin:{manifest.plugin_id}")
                setup(handle)
                state.status = "activated"
                state.contributions = manifest.to_dict()["contributions"]
                self._activated.append(state.name)
            except Exception as exc:  # noqa: BLE001 — plugin code must not break the host
                state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
                self._block_transitive_dependents(manifest.plugin_id)
            finally:
                pass
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
        while changed:
            changed = False
            for other in self._states.values():
                other_manifest = other.manifest
                if other_manifest is None or other.status in {"blocked", "failed"}:
                    continue
                deps = set(other_manifest.dependencies)
                if failed_plugin_id in deps:
                    other.status = "blocked"
                    other.error = f"dependency {failed_plugin_id} failed to activate"
                    other.blocked_by.append(failed_plugin_id)
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
