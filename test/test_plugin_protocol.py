"""Plugin protocol v1 tests: manifests, lifecycle, dependency resolution."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch

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
        self.assertTrue(manifest.graphs[0].file.samefile(plugin / "graphs" / "chain.json"))
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
        self.assertTrue(satisfies("1.3.0", "~=1.2"))
        self.assertFalse(satisfies("2.0.0", "~=1.2"))
        self.assertFalse(satisfies("2.0.0", "<2"))
        self.assertFalse(satisfies("1.0.0", ">=1.5"))

    def test_compatible_release_preserves_the_full_lower_bound_and_prefix(self):
        for version, constraint, expected in [
            ("1.4.0", "~=1.4.5", False), ("1.4.5", "~=1.4.5", True),
            ("1.4.9", "~=1.4.5", True), ("1.5.0", "~=1.4.5", False),
            ("1.9.0", "~=1.4", True), ("2.0.0", "~=1.4", False),
            ("1.4.5.0", "~=1.4.5.1", False), ("1.4.5.1", "~=1.4.5.1", True),
            ("1.4.6", "~=1.4.5.1", False), ("1.4.5.0", "==1.4.5", True),
        ]:
            with self.subTest(version=version, constraint=constraint):
                self.assertEqual(satisfies(version, constraint), expected)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"PYMSS_PLUGINS_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        from pymss.plugins import get_registry

        registry = get_registry()
        capabilities, nodes, commands = dict(registry.capabilities), dict(registry.nodes), list(registry.cli_commands)
        self.addCleanup(lambda: registry.capabilities.update(capabilities))
        self.addCleanup(registry.capabilities.clear)
        self.addCleanup(lambda: registry.nodes.update(nodes))
        self.addCleanup(registry.nodes.clear)
        self.addCleanup(lambda: registry.cli_commands.__setitem__(slice(None), commands))
        self.addCleanup(setattr, ops, "_DEFAULT_RUNTIME", ops._DEFAULT_RUNTIME)

    def plugin(self, name: str, plugin_id: str, version: str, deps: dict | None = None,
               body: str = "", broken: bool = False, entry: str | None = None) -> Path:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        lines = [
            "[project]", f'name = "pymss-plugin-{name}"', f'version = "{version}"', "",
            "[tool.pymss.plugin]", f'id = "{plugin_id}"', "api_version = 1", "",
        ]
        if entry is not None:
            lines.append(f'entry = "{entry}"')
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
        self.plugin("grandchild", "author.grandchild", "1.0.0", deps={"author.user": ">=1"})
        manager = PluginManager(self.root)
        manager.discover()
        manager.resolve()
        runtime = ops.Runtime()
        rows = manager.activate(lambda source: ops._RuntimePluginHandle(runtime, source=source))
        by_id = {row["plugin_id"]: row for row in rows}
        self.assertEqual(by_id["author.broken"]["status"], "failed")
        self.assertIn("setup exploded", by_id["author.broken"]["error"])
        self.assertEqual(by_id["author.user"]["status"], "blocked")
        self.assertEqual(by_id["author.grandchild"]["status"], "blocked")
        self.assertIn("failed to activate", by_id["author.user"]["error"])
        self.assertEqual([op["id"] for op in runtime.list_operations()], [])

    def test_duplicate_plugin_ids_block_all_providers_and_dependents(self):
        for first, second in [("1.0.0", "9.0.0"), ("9.0.0", "1.0.0"), ("9.0.0", "9.0.0")]:
            with self.subTest(versions=(first, second)):
                self.plugin("first", "author.provider", first, broken=True)
                self.plugin("second", "author.provider", second, broken=True)
                self.plugin("consumer", "author.consumer", "1.0.0", deps={"author.provider": ">=9"})
                manager = PluginManager(self.root)
                manager.discover()
                manager.resolve()
                with patch("pymss.operations._RuntimePluginHandle") as factory:
                    report = manager.activate(factory)
                factory.assert_not_called()
                self.assertEqual({row["status"] for row in report}, {"blocked"})
                for row in report:
                    self.assertIn("duplicate plugin id", row["error"])
                    self.assertIn("first, second", row["error"])

    def test_duplicate_plugin_id_from_a_second_root_preserves_the_active_provider(self):
        original = self.plugin("original", "author.shared", "1.0.0", body=(
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.original', inputs={}, params={'type':'object'}, "
            "outputs={}, run=lambda c,i,p: {'value': 1})\n"
        ))
        runtime = ops.Runtime()
        self.assertTrue(runtime.load_plugins()[0]["loaded"])
        second_root = self.root / "managed"
        duplicate = second_root / "duplicate"
        duplicate.mkdir(parents=True)
        (duplicate / "pyproject.toml").write_text((original / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
        (duplicate / "__init__.py").write_text(
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.duplicate', inputs={}, params={'type':'object'}, "
            "outputs={}, run=lambda c,i,p: {'value': 2})\n", encoding="utf-8",
        )
        with patch.dict(os.environ, {"PYMSS_PLUGINS_DIR": str(second_root)}):
            report = runtime.load_plugins()
        self.assertEqual(report[0]["status"], "blocked")
        self.assertIn("already activated from", report[0]["error"])
        self.assertEqual([entry["id"] for entry in runtime.list_operations()], ["author.original"])
        self.assertEqual(runtime.run("author.original", inputs={}, params={}), {"value": 1})

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

    def test_entry_modules_and_packages_support_relative_imports(self):
        cases = [
            ("flat", "entry:setup", "entry.py", "helper.py", ".helper"),
            ("package", "pkg:setup", "pkg/__init__.py", "pkg/helper.py", ".helper"),
            ("nested", "pkg.handlers:setup", "pkg/handlers.py", "pkg/helper.py", ".helper"),
            ("default", None, "__init__.py", "helper.py", ".helper"),
        ]
        original_path = list(sys.path)
        for name, entry, filename, helper, relative in cases:
            with self.subTest(entry=entry):
                plugin_id = f"author.{name}"
                directory = self.plugin(name, plugin_id, "1.0.0", entry=entry)
                for path, contents in {
                    helper: "VALUE = 42\n",
                    filename: (
                        f"from {relative} import VALUE\n"
                        "def setup(plugin):\n"
                        f"    plugin.register_operation(id='{plugin_id}.run', inputs={{}}, "
                        "params={'type':'object','properties':{}}, outputs={'value':{'type':'number'}}, "
                        "run=lambda c,i,p: {'value': VALUE})\n"
                    ),
                }.items():
                    target = directory / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(contents, encoding="utf-8")
                if filename.startswith("pkg/"):
                    (directory / "pkg/__init__.py").touch(exist_ok=True)
                runtime = ops.Runtime()
                rows = runtime.load_plugins()
                row = next(row for row in rows if row["name"] == name)
                self.assertTrue(row["loaded"], row)
                self.assertEqual(runtime.run(f"{plugin_id}.run", inputs={}, params={}), {"value": 42})
        self.assertEqual(sys.path, original_path)

    def test_failed_setup_restores_all_registration_types_and_modules(self):
        from pymss.plugins import get_registry

        registry = get_registry()
        registry.register_capability("shared_cap", lambda: "original", source="existing")
        registry.register_node("SharedNode", lambda *args: None, source="existing")
        registry.register_cli(("existing", "run"), lambda args: 0, source="existing")
        original_cap = registry.capabilities["shared_cap"]
        original_node = registry.nodes["SharedNode"]
        original_commands = list(registry.cli_commands)
        modules_before = {name for name in sys.modules if name.startswith("_pymss_manifest_plugin_")}
        self.plugin("broken", "author.broken", "1.0.0", body=(
            "from pymss.plugins import register_capability\n"
            "register_capability('import_side_effect', lambda: 1)\n"
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.broken.run', inputs={}, "
            "params={'type':'object','properties':{}}, outputs={}, run=lambda c,i,p: {})\n"
            "    plugin.register_capability('shared_cap', lambda: 'replacement')\n"
            "    plugin.register_node('SharedNode', lambda *args: None)\n"
            "    plugin.register_cli(('broken','run'), lambda args: 0)\n"
            "    raise RuntimeError('failed after registration')\n"
        ))
        runtime = ops.Runtime()
        row = runtime.load_plugins()[0]
        self.assertFalse(row["loaded"])
        self.assertEqual(runtime.list_operations(), [])
        self.assertIs(registry.capabilities["shared_cap"], original_cap)
        self.assertIs(registry.nodes["SharedNode"], original_node)
        self.assertNotIn("import_side_effect", registry.capabilities)
        self.assertEqual(registry.cli_commands, original_commands)
        self.assertEqual(
            {name for name in sys.modules if name.startswith("_pymss_manifest_plugin_")}, modules_before,
        )

    def test_cli_uses_manifest_lifecycle_and_activates_once(self):
        import pymss.cli as cli
        import pymss.plugins as plugins

        directory = self.plugin("custom", "author.custom", "1.0.0", entry="entry:setup")
        (directory / "entry.py").write_text(
            "from pathlib import Path\n"
            "def setup(plugin):\n"
            "    counter = Path(__file__).parent / 'calls.txt'\n"
            "    counter.write_text((counter.read_text() if counter.exists() else '') + '1')\n"
            "    plugin.register_operation(id='author.custom.run', inputs={}, "
            "params={'type':'object','properties':{}}, outputs={}, run=lambda c,i,p: {})\n",
            encoding="utf-8",
        )
        future = self.plugin("future", "author.future", "1.0.0", body="raise RuntimeError('must not execute')\n")
        manifest = future / "pyproject.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("api_version = 1", "api_version = 999"), encoding="utf-8")
        plugins.reset()
        self.addCleanup(plugins.reset)
        ops._DEFAULT_RUNTIME = None
        output = io.StringIO()
        with patch("pymss.plugins.loader._scan_entrypoints"), redirect_stdout(output):
            self.assertEqual(cli.main(["ops", "list"]), 0)
            self.assertEqual(cli.main(["ops", "list"]), 0)
        self.assertIn("author.custom.run", output.getvalue())
        self.assertEqual((directory / "calls.txt").read_text(), "1")
        failed = next(row for row in plugins.get_last_report().failed if row.name == "future")
        self.assertIn("api_version 999", failed.error)

    def test_failed_entrypoint_restores_operations_and_global_registrations(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from pymss.plugins import PluginRegistration, get_registry
        from pymss.plugins.loader import PluginLoadReport, _scan_entrypoints

        registry = get_registry()
        registry.register_capability("ep_shared", lambda: "original", source="existing")
        original = registry.capabilities["ep_shared"]
        nodes_before, cli_before = dict(registry.nodes), list(registry.cli_commands)
        runtime = ops.Runtime()
        ops._DEFAULT_RUNTIME = runtime

        def fail():
            plugin = PluginRegistration(source="broken-entrypoint")
            plugin.register_capability("ep_shared", lambda: "replacement")
            plugin.register_node("EntrypointNode", lambda *args: None)
            plugin.register_cli(("broken", "ep"), lambda args: 0)
            plugin.register_operation(id="author.entrypoint", inputs={}, params={"type": "object"},
                                      outputs={}, run=lambda c, i, p: {})
            raise RuntimeError("entrypoint import failed")

        entries = Mock()
        entries.select.return_value = [SimpleNamespace(name="broken", load=fail)]
        report = PluginLoadReport()
        with patch("pymss.plugins.loader.entry_points", return_value=entries):
            _scan_entrypoints(report)
        self.assertFalse(report.results[0].loaded)
        self.assertIn("entrypoint import failed", report.results[0].error)
        self.assertEqual(runtime.list_operations(), [])
        self.assertIs(registry.capabilities["ep_shared"], original)
        self.assertEqual(registry.nodes, nodes_before)
        self.assertEqual(registry.cli_commands, cli_before)


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
                {"id": 2, "type": "AudioAdjustVolume", "widgets_values": [0],
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
            param_bindings={"gain": {"nodes": ["2"], "widget": 0}},
            outputs={"audio": {"node": "2", "output": 0}},
        )
        waveform = np.ones((2, 4410), dtype=np.float32) * 0.1
        from pymss.graph.core import numpy_to_audio
        result = run(None, {"audio": numpy_to_audio(waveform, 44100)}, {"gain": 6.0})
        out = result["audio"]
        data, sr = out.audio, out.sample_rate
        self.assertEqual(sr, 44100)
        # The volume widget is dB: +6 dB ≈ ×1.995
        self.assertAlmostEqual(float(np.abs(data).max()), 0.1 * 10 ** (6 / 20), places=4)
        second = run(None, {"audio": numpy_to_audio(waveform, 44100)}, {"gain": -6.0})
        self.assertAlmostEqual(float(np.abs(second["audio"].audio).max()), 0.1 * 10 ** (-6 / 20), places=4)

    def test_binding_errors_are_rejected_at_build_time(self):
        from pymss.graph.core import DAGError

        path = self.graph()
        for overrides in [
            {"audio_input": {"node": "999"}},
            {"audio_input": {"node": "1", "widget": -1}},
            {"audio_input": {"node": "1", "widget": 99}},
            {"audio_input": {"node": "2", "widget": 0}},
            {"param_bindings": {"gain": {"nodes": ["999"]}}},
            *({"param_bindings": {"gain": {"nodes": ["2"], "widget": index}}} for index in [-1, 99, True]),
            *({"outputs": {"audio": {"node": "2", "output": slot}}} for slot in [-1, 1, 99, "0", True]),
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(DAGError):
                build_graph_operation(**{
                    "graph_file": path, "audio_input": {"node": "1"},
                    "outputs": {"audio": {"node": "2"}}, **overrides,
                })

    def test_file_and_runtime_name_input_widgets_both_bind_the_supplied_audio(self):
        import numpy as np
        from pymss.graph.core import numpy_to_audio

        for widget in [0, 1]:
            with self.subTest(widget=widget):
                run = build_graph_operation(graph_file=self.graph(),
                                            audio_input={"node": "1", "widget": widget},
                                            outputs={"audio": {"node": "2"}})
                result = run(None, {"audio": numpy_to_audio(np.full((2, 1000), 0.25, dtype=np.float32), 44100)}, {})
                self.assertAlmostEqual(float(result["audio"].audio.mean()), 0.25, places=4)

    def test_distinct_output_slots_are_lifted_without_leaking_scratch_files(self):
        import numpy as np
        from pymss.graph.core import numpy_to_audio

        path = self.graph()
        graph = json.loads(path.read_text(encoding="utf-8"))
        graph["nodes"][1] = {
            "id": 2, "type": "SplitAudioChannels", "widgets_values": [],
            "inputs": [{"name": "audio", "link": 1}],
            "outputs": [{"name": "left"}, {"name": "right"}],
        }
        graph["links"] = graph["links"][:1]
        path.write_text(json.dumps(graph), encoding="utf-8")
        run = build_graph_operation(graph_file=path, audio_input={"node": "1"}, outputs={
            "left": {"node": "2", "output": 0}, "right": {"node": "2", "output": 1},
        })
        scratch = self.root / "scratch"
        result = run(ops.OperationContext(workdir=str(scratch)), {"audio": numpy_to_audio(
            np.stack([np.full(1000, 0.1), np.full(1000, 0.7)]).astype(np.float32), 44100,
        )}, {})
        self.assertAlmostEqual(float(result["left"].audio.mean()), 0.1, places=4)
        self.assertAlmostEqual(float(result["right"].audio.mean()), 0.7, places=4)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_concurrent_graph_operations_keep_inputs_isolated_and_clean_up(self):
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from pymss.graph.core import numpy_to_audio
        from pymss.plugins import graph_ops

        run = build_graph_operation(graph_file=self.graph(), audio_input={"node": "1"},
                                    outputs={"audio": {"node": "2"}})
        materialize = graph_ops._audio_ref
        barrier = Barrier(2)

        def synchronized(*args):
            path = materialize(*args)
            barrier.wait(timeout=5)
            return path

        scratch = self.root / "scratch"
        context = ops.OperationContext(workdir=str(scratch))
        with patch.object(graph_ops, "_audio_ref", side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda level: run(context, {"audio": numpy_to_audio(
                np.full((2, 1000), level, dtype=np.float32), 44100,
            )}, {}), [0.1, 0.7]))
        for result, level in zip(results, [0.1, 0.7]):
            self.assertAlmostEqual(float(result["audio"].audio.mean()), level, places=4)
        self.assertEqual(list(scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
