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

import logging
import os
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

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


def _scan_folder(plugins_dir: Path, report: PluginLoadReport) -> None:
    """Use the same manifest lifecycle for CLI and other runtime hosts."""
    if not plugins_dir.exists():
        return
    from ..operations import default_runtime

    for row in default_runtime().load_plugins():
        name = str(row.get("name") or "")
        directory = plugins_dir / name
        entry = next(
            (directory / filename for filename in ("__init__.py", "plugin.py") if (directory / filename).is_file()),
            directory,
        )
        report.results.append(PluginLoadResult(
            name=name,
            source=f"folder:{directory}",
            loaded=bool(row.get("loaded")),
            error=row.get("error"),
            path=str(entry),
        ))


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
