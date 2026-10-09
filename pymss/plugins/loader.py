"""Plugin discovery and loading.

Two sources:
1. Folder scan: every subdirectory of ``~/.pymss/plugins`` (or
   ``$PYMSS_PLUGINS_DIR``) with an ``__init__.py`` or ``plugin.py``
   is a plugin; we import it.
2. Entry points (optional, for proper packages): packages declare
   ``[project.entry-points."pymss.plugins"]`` and we import the target module.

A single plugin failing to load never blocks others: each import is wrapped in
try/except, failures are recorded, and surfaced via ``pymss plugins list``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENTRYPOINT_GROUP = "pymss.plugins"

DEFAULT_PLUGINS_DIR = Path.home() / ".pymss" / "plugins"


def get_plugins_dir() -> Path:
    """Resolve the plugins directory from env var or default."""
    env = os.environ.get("PYMSS_PLUGINS_DIR")
    if env:
        return Path(env)
    return DEFAULT_PLUGINS_DIR


@dataclass
class PluginLoadResult:
    """Result of loading one plugin."""

    name: str
    source: str  # "folder:<path>" | "entrypoint"
    loaded: bool
    error: str | None = None
    path: str | None = None


@dataclass
class PluginLoadReport:
    """Aggregate load report across all plugins in a bootstrap run."""

    results: list[PluginLoadResult] = field(default_factory=list)
    bootstrapped: bool = False

    @property
    def loaded_names(self) -> list[str]:
        return [r.name for r in self.results if r.loaded]

    @property
    def failed(self) -> list[PluginLoadResult]:
        return [r for r in self.results if not r.loaded]


_LAST_REPORT: PluginLoadReport | None = None


def get_last_report() -> PluginLoadReport | None:
    """Return the most recent bootstrap report (for `pymss plugins list`)."""
    return _LAST_REPORT


def _import_plugin_module(modname: str, path: Path | None) -> bool:
    """Import a plugin module by name (entrypoint) or file path (folder scan).

    Returns True on success. Exceptions are logged by the caller.
    """
    if path is not None:
        # Folder scan: load the file as a top-level module under a synthetic
        # name so plugins don't need to be installed packages.
        spec = importlib.util.spec_from_file_location(modname, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin module from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception:
            sys.modules.pop(modname, None)
            raise
        # Optional new-style protocol: a setup(plugin) function receives a
        # registration handle and contributes operations/capabilities through
        # it. Imported module side effects (old style) still work; setup()
        # failures are raised so the loader can record the plugin as failed
        # instead of silently half-registering.
        _call_plugin_setup(module)
        return True
    imported = importlib.import_module(modname)
    _call_plugin_setup(imported)
    return True


def _call_plugin_setup(module: Any) -> None:
    """Invoke an optional ``setup(plugin)`` hook on a plugin module.

    The hook is optional (legacy import-side-effect plugins keep working).
    Errors propagate to the caller so the plugin is reported as failed.
    """
    setup = getattr(module, "setup", None)
    if setup is None:
        return
    from . import PluginRegistration

    setup(PluginRegistration(source=module.__name__))


def _scan_folder(plugins_dir: Path, report: PluginLoadReport) -> None:
    """Import every subdirectory of plugins_dir that has an entry file."""
    if not plugins_dir.exists():
        return
    for child in sorted(plugins_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        # Prefer __init__.py, fall back to plugin.py.
        entry = None
        for candidate in ("__init__.py", "plugin.py"):
            p = child / candidate
            if p.exists():
                entry = p
                break
        if entry is None:
            report.results.append(
                PluginLoadResult(
                    name=child.name,
                    source=f"folder:{child}",
                    loaded=False,
                    error="no __init__.py or plugin.py found",
                    path=str(child),
                )
            )
            continue
        modname = f"_pymss_plugin_{child.name}"
        try:
            _import_plugin_module(modname, entry)
            report.results.append(
                PluginLoadResult(
                    name=child.name,
                    source=f"folder:{child}",
                    loaded=True,
                    path=str(entry),
                )
            )
            logger.debug("loaded plugin '%s' from %s", child.name, entry)
        except Exception as exc:  # noqa: BLE001
            report.results.append(
                PluginLoadResult(
                    name=child.name,
                    source=f"folder:{child}",
                    loaded=False,
                    error=f"{type(exc).__name__}: {exc}",
                    path=str(entry),
                )
            )
            logger.warning("plugin '%s' failed to load: %s", child.name, exc)


def _scan_entrypoints(report: PluginLoadReport) -> None:
    """Import every declared pymss.plugins entry point."""
    try:
        eps = entry_points()
    except Exception as exc:  # noqa: BLE001
        logger.debug("entry_points() failed: %s", exc)
        return
    # Compatible across Python 3.10+ (EntryPoints) and older (dict).
    group_eps = []
    try:
        group_eps = list(eps.select(group=ENTRYPOINT_GROUP))  # py3.10+
    except AttributeError:
        group_eps = list(eps.get(ENTRYPOINT_GROUP, []))  # type: ignore[union-attr]
    for ep in group_eps:
        try:
            ep.load()
            report.results.append(
                PluginLoadResult(name=ep.name, source="entrypoint", loaded=True)
            )
            logger.debug("loaded entrypoint plugin '%s'", ep.name)
        except Exception as exc:  # noqa: BLE001
            report.results.append(
                PluginLoadResult(
                    name=ep.name,
                    source="entrypoint",
                    loaded=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            logger.warning("entrypoint plugin '%s' failed to load: %s", ep.name, exc)


def bootstrap(force: bool = False) -> PluginLoadReport:
    """Discover and load all plugins.

    Idempotent: subsequent calls return the cached report unless ``force=True``.
    """
    global _LAST_REPORT
    if _LAST_REPORT is not None and _LAST_REPORT.bootstrapped and not force:
        return _LAST_REPORT

    report = PluginLoadReport(bootstrapped=True)
    plugins_dir = get_plugins_dir()
    _scan_folder(plugins_dir, report)
    _scan_entrypoints(report)
    _LAST_REPORT = report
    return report


def ensure_plugins_loaded() -> PluginLoadReport:
    """Load user plugins once per process before an entry point uses them.

    Business entry points (infer / workflow / comfy run / serve / ops) call
    this instead of ``bootstrap()`` directly: same idempotent semantics, but
    named for "prepare the plugin state this process depends on" rather than
    "produce a report for the plugins command". Failures are already isolated
    per plugin; the report is retrievable via ``get_last_report()``.
    """
    return bootstrap()


def reset() -> None:
    """Clear the cached bootstrap report (mainly for tests)."""
    global _LAST_REPORT
    _LAST_REPORT = None


__all__ = [
    "DEFAULT_PLUGINS_DIR",
    "ENTRYPOINT_GROUP",
    "PluginLoadResult",
    "PluginLoadReport",
    "bootstrap",
    "ensure_plugins_loaded",
    "get_plugins_dir",
    "get_last_report",
    "reset",
]
