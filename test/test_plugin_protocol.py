"""Plugin protocol v1 tests: manifests, lifecycle, dependency resolution."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from pymss.plugins import ManifestError, PluginManager, read_manifest, satisfies
from pymss.plugins.lifecycle import pymss_package_version
from pymss.plugins.graph_ops import build_graph_operation
import pymss.operations as ops


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name: str, body: str, extra: dict | None = None) -> Path:
        plugin = self.root / name
        plugin.mkdir(parents=True, exist_ok=True)
        plugin.joinpath("pyproject.toml").write_text(body, encoding="utf-8")
        plugin.joinpath("__init__.py").write_text("", encoding="utf-8")
        for rel, content in (extra or {}).items():
            target = plugin / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return plugin

    def test_legacy_plugin_without_manifest(self):
        plugin = self.write("plain", "")
        plugin.joinpath("pyproject.toml").unlink()
        self.assertIsNone(read_manifest(plugin))

    def test_full_manifest_parses_and_resolves_paths(self):
        plugin = self.write(
            "chain",
            """
[project]
name = "pymss-plugin-chain"
version = "1.2.0"

[tool.pymss.plugin]
id = "author.chain"
api_version = 1
requires_pymss = ">=2.1"

[tool.pymss.plugin.dependencies]
"author.denoise" = ">=1,<2"

[[tool.pymss.plugin.contributions.graphs]]
id = "chain"
file = "graphs/chain.json"
operation = "author.chain.process"

[[tool.pymss.plugin.contributions.views]]
id = "editor"
title = "Chain Editor"
entry = "ui/index.html"
placements = ["tool", "workspace"]
operation = "author.chain.process"
permissions = ["operations", "files.pickAudio"]

[[tool.pymss.plugin.contributions.player]]
operation = "author.chain.process"
scopes = ["clip"]
modes = ["rendered_preview", "offline_export"]
""",
            {"graphs/chain.json": "{}", "ui/index.html": "<html></html>"},
        )
        manifest = read_manifest(plugin)
        assert manifest is not None
        self.assertEqual(manifest.plugin_id, "author.chain")
        self.assertEqual(manifest.version, "1.2.0")
        self.assertEqual(manifest.dependencies, {"author.denoise": ">=1,<2"})
        self.assertEqual(manifest.graphs[0].file, plugin / "graphs" / "chain.json")
        self.assertEqual(manifest.views[0].placements, ("tool", "workspace"))
        self.assertIn("files.pickAudio", manifest.views[0].permissions)
        self.assertEqual(manifest.player[0].scopes, ("clip",))

    def test_invalid_fields_are_rejected(self):
        with self.assertRaises(ManifestError):
            read_manifest(self.write("bad-id", '[tool.pymss.plugin]\nid = "Not Valid"\napi_version = 1\n'))
        with self.assertRaises(ManifestError):
            read_manifest(self.write("bad-api", '[tool.pymss.plugin]\nid = "a.b"\napi_version = "1"\n'))
        with self.assertRaises(ManifestError):
            read_manifest(self.write(
                "escape", '[tool.pymss.plugin]\nid = "a.b"\napi_version = 1\n\n'
                '[tool.pymss.plugin.contributions.views]\nid = "v"\nentry = "../../secrets.html"\n'))
        with self.assertRaises(ManifestError):
            read_manifest(self.write(
                "missing-entry", '[tool.pymss.plugin]\nid = "a.b"\napi_version = 1\n\n'
                '[tool.pymss.plugin.contributions.views]\nid = "v"\nentry = "ui/missing.html"\n'))
        with self.assertRaises(ManifestError):
            read_manifest(self.write(
                "bad-placement", '[tool.pymss.plugin]\nid = "a.b"\napi_version = 1\n\n'
                '[tool.pymss.plugin.contributions.views]\nid = "v"\nentry = "ui/index.html"\nplacements = ["sidebar"]\n',
                {"ui/index.html": ""}))
        with self.assertRaises(ManifestError):
            read_manifest(self.write(
                "bad-constraint", '[tool.pymss.plugin]\nid = "a.b"\napi_version = 1\n\n'
                '[tool.pymss.plugin.dependencies]\n"other" = "latest"\n'))

    def test_version_constraints(self):
        self.assertTrue(satisfies("1.2.0", ">=1,<2"))
        self.assertTrue(satisfies("1.2", "==1.2.0"))
        self.assertTrue(satisfies("1.2.3", "~=1.2"))
        self.assertFalse(satisfies("1.3.0", "~=1.2"))
        self.assertFalse(satisfies("2.0.0", "<2"))
        self.assertFalse(satisfies("1.0.0", ">=1.5"))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.environ["PYMSS_PLUGINS_DIR"] = str(self.root)
        self.addCleanup(os.environ.pop, "PYMSS_PLUGINS_DIR", None)

    def plugin(self, name: str, plugin_id: str, version: str, deps: dict | None = None,
               body: str = "", broken: bool = False) -> Path:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        lines = [
            "[project]", f'name = "pymss-plugin-{name}"', f'version = "{version}"', "",
            "[tool.pymss.plugin]", f'id = "{plugin_id}"', "api_version = 1", "",
        ]
        if deps:
            lines.append("[tool.pymss.plugin.dependencies]")
            lines += [f'"{k}" = "{v}"' for k, v in deps.items()]
            lines.append("")
        directory.joinpath("pyproject.toml").write_text("\n".join(lines), encoding="utf-8")
        code = body or (
            "def setup(plugin):\n"
            f'    plugin.register_operation(id="{plugin_id}.run", title="{plugin_id}", inputs={{}},\n'
            '        params={"type":"object","properties":{}}, outputs={}, run=lambda c,i,p: {})\n'
        )
        if broken:
            code += "\nraise RuntimeError('setup exploded')\n"
        directory.joinpath("__init__.py").write_text(code, encoding="utf-8")
        return directory

    def states(self, manager: PluginManager):
        return {s.manifest.plugin_id if s.manifest else s.name: s for s in manager.discover()}

    def test_dependency_order_and_registration(self):
        self.plugin("master", "author.master", "1.0.0", deps={"author.util": ">=1"})
        self.plugin("util", "author.util", "1.4.0")
        manager = PluginManager(self.root)
        manager.discover()
        manager.resolve(pymss_version=pymss_package_version())
        order = [s.manifest.plugin_id for s in manager.activation_order()]
        self.assertEqual(order, ["author.util", "author.master"])

        ops._DEFAULT_RUNTIME = None
        runtime = ops.Runtime()
        rows = manager.activate(lambda source: ops._RuntimePluginHandle(runtime, source=source))
        by_id = {row["plugin_id"]: row for row in rows}
        self.assertEqual(by_id["author.master"]["status"], "activated")
        self.assertIn("author.util.run", runtime.list_operations()[0]["id"].join([]) or
                      [op["id"] for op in runtime.list_operations()])
        self.assertTrue(by_id["author.util"]["loaded"])

    def test_missing_and_version_unsatisfied_dependencies_block(self):
        self.plugin("needs", "author.needs", "1.0.0", deps={"author.gone": ">=1"})
        self.plugin("util", "author.util", "1.0.0")
        self.plugin("picky", "author.picky", "1.0.0", deps={"author.util": ">=2"})
        manager = PluginManager(self.root)
        states = {s.manifest.plugin_id: s for s in manager.discover() if s.manifest}
        manager.resolve(pymss_version="2.1.8")
        self.assertEqual(states["author.needs"].status, "blocked")
        self.assertIn("missing dependency", states["author.needs"].error)
        self.assertEqual(states["author.picky"].status, "blocked")
        self.assertIn("unsatisfied", states["author.picky"].error)
        self.assertEqual(states["author.util"].status, "resolved")

    def test_cycle_is_blocked(self):
        self.plugin("a", "author.a", "1.0.0", deps={"author.b": ">=1"})
        self.plugin("b", "author.b", "1.0.0", deps={"author.a": ">=1"})
        manager = PluginManager(self.root)
        states = {s.manifest.plugin_id: s for s in manager.discover() if s.manifest}
        manager.resolve()
        self.assertEqual(states["author.a"].status, "blocked")
        self.assertEqual(states["author.b"].status, "blocked")
        self.assertIn("cycle", (states["author.a"].error or "") + (states["author.b"].error or ""))

    def test_failed_activation_blocks_dependents_and_reports(self):
        self.plugin("broken", "author.broken", "1.0.0", broken=True)
        self.plugin("user", "author.user", "1.0.0", deps={"author.broken": ">=1"})
        manager = PluginManager(self.root)
        manager.discover()
        manager.resolve()
        runtime = ops.Runtime()
        rows = manager.activate(lambda source: ops._RuntimePluginHandle(runtime, source=source))
        by_id = {row["plugin_id"]: row for row in rows}
        self.assertEqual(by_id["author.broken"]["status"], "failed")
        self.assertIn("setup exploded", by_id["author.broken"]["error"])
        self.assertEqual(by_id["author.user"]["status"], "blocked")
        self.assertIn("failed to activate", by_id["author.user"]["error"])
        self.assertEqual([op["id"] for op in runtime.list_operations()], [])

    def test_future_api_version_is_blocked_not_crashed(self):
        directory = self.root / "future"
        directory.mkdir(parents=True)
        directory.joinpath("pyproject.toml").write_text(
            '[tool.pymss.plugin]\nid = "author.future"\napi_version = 99\n', encoding="utf-8")
        directory.joinpath("__init__.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
        manager = PluginManager(self.root)
        states = {s.manifest.plugin_id: s for s in manager.discover() if s.manifest}
        manager.resolve()
        self.assertEqual(states["author.future"].status, "blocked")
        self.assertIn("api_version 99", states["author.future"].error)

    def test_runtime_load_plugins_reports_manifest_and_legacy_rows(self):
        self.plugin("util", "author.util", "1.0.0")
        legacy = self.root / "legacy-cap"
        legacy.mkdir()
        legacy.joinpath("__init__.py").write_text(
            "from pymss.plugins import register_capability\n"
            "register_capability('legacy_shout', lambda x: x.upper(), source='legacy-cap')\n",
            encoding="utf-8")
        ops._DEFAULT_RUNTIME = None
        import pymss.plugins as plugs
        plugs.reset()
        runtime = ops.Runtime()
        report = runtime.load_plugins()
        by_name = {row["name"]: row for row in report}
        self.assertTrue(by_name["util"]["loaded"])
        self.assertEqual(by_name["util"]["plugin_id"], "author.util")
        self.assertFalse(by_name["util"]["legacy"])
        self.assertTrue(by_name["legacy-cap"]["loaded"])
        self.assertTrue(by_name["legacy-cap"].get("legacy", False))
        self.assertIn("author.util.run", by_name["util"]["operations"])

    def test_broken_manifest_is_blocked_with_reason(self):
        directory = self.root / "badm"
        directory.mkdir(parents=True)
        directory.joinpath("pyproject.toml").write_text(
            '[tool.pymss.plugin]\nid = "Bad Id"\napi_version = 1\n', encoding="utf-8")
        directory.joinpath("__init__.py").write_text("", encoding="utf-8")
        manager = PluginManager(self.root)
        states = {s.name: s for s in manager.discover()}
        self.assertEqual(states["badm"].status, "blocked")
        self.assertIn("plugin id", states["badm"].error)


class GraphOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def graph(self) -> Path:
        graph = {
            "nodes": [
                {"id": 1, "type": "pymss_load_audio", "widgets_values": [None, "input"],
                 "inputs": [], "outputs": [{"name": "audio"}], "pos": [0, 0]},
                {"id": 2, "type": "AudioAdjustVolume", "widgets_values": [6],
                 "inputs": [{"name": "audio", "type": "AUDIO", "link": 1}, {"name": "volume", "type": "INT", "link": 2}],
                 "outputs": [{"name": "audio"}], "pos": [0, 0]},
            ],
            "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 1, 1, 2, 1, "INT"]],
        }
        path = self.root / "graph.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        return path

    def test_graph_operation_executes_end_to_end(self):
        import numpy as np

        run = build_graph_operation(
            graph_file=self.graph(),
            audio_input={"node": "1", "widget": 0, "name": "audio"},
            param_bindings={"gain": {"nodes": ["2"]}},
            outputs={"audio": {"node": "2", "output": 0}},
        )
        waveform = np.ones((2, 4410), dtype=np.float32) * 0.1
        from pymss.graph.core import numpy_to_audio
        result = run(None, {"audio": numpy_to_audio(waveform, 44100)}, {"gain": 1.0})
        out = result["audio"]
        data, sr = out.audio, out.sample_rate
        self.assertEqual(sr, 44100)
        # The volume widget is dB: +6 dB ≈ ×1.995
        self.assertAlmostEqual(float(np.abs(data).max()), 0.1 * 10 ** (6 / 20), places=4)

    def test_binding_errors_raise_at_build_or_run_not_silently(self):
        run = build_graph_operation(
            graph_file=self.graph(),
            audio_input={"node": "999", "widget": 0, "name": "audio"},
            outputs={"audio": {"node": "2"}},
        )
        from pymss.graph.core import numpy_to_audio
        import numpy as np
        with self.assertRaises(Exception):
            run(None, {"audio": numpy_to_audio(np.zeros((1, 10), dtype=np.float32), 44100)}, {})


if __name__ == "__main__":
    unittest.main()
