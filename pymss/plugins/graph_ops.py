"""DAG-backed operations: expose a comfy-mss graph file as a pymss Operation.

``build_graph_operation`` returns a ``run(ctx, inputs, params)`` handler that
loads the graph, binds the operation's audio input and params onto graph nodes,
executes via ``run_dag``, and lifts declared node outputs to named operation
outputs. Binding errors surface at build time so broken graphs fail during
activation, not mid-task.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..graph.core import AudioArtifact, DAGError, audio_to_numpy, numpy_to_audio

# Per-run materialized input files, keyed by the runtime-input name handed to
# run_dag(inputs=...). Populated inside _run; keyed lookup avoids re-writing
# the same file for repeated invocations of one operation.
_audio_paths: dict[str, str] = {}


def _node_index(dag: Any, node_ref: str) -> int:
    try:
        node_id = int(node_ref)
    except (TypeError, ValueError) as exc:
        raise DAGError(f"graph binding references non-integer node id {node_ref!r}") from exc
    for node in dag.nodes:
        if node.id == node_id:
            return node_id
    raise DAGError(f"graph binding references missing node {node_id}")


def build_graph_operation(
    *,
    graph_file: str | Path,
    audio_input: Mapping[str, Any] | None,
    param_bindings: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any],
    default_sr: int = 44100,
):
    """Build an Operation run handler from a comfy-mss graph file.

    Args:
        graph_file: path to the graph JSON shipped inside the plugin.
        audio_input: ``{"node": <id>, "widget": <index>, "name": <stem>}`` —
            the load node that receives the operation's audio input. When the
            graph has exactly one load node, ``node`` may be omitted.
        param_bindings: operation param name -> ``{"nodes": [<id>...]}`` —
            each target node receives the param in ``node.data["params"]``.
        outputs: output name -> ``{"node": <id>, "output": <slot>}`` —
            the node whose executed output becomes the named operation output.
    """
    path = Path(graph_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    param_bindings = dict(param_bindings or {})
    outputs = dict(outputs)

    def _prepare(dag: Any) -> tuple[int, Any, dict[str, Any], dict[str, Any]]:
        load_nodes = [n for n in dag.nodes if str(n.type).startswith("pymss_load_audio")]
        binding = dict(audio_input or {})
        node_ref = binding.get("node")
        if node_ref is None:
            if len(load_nodes) != 1:
                raise DAGError(
                    "graph has multiple/zero load nodes; audio_input.node is required"
                )
            node_ref = load_nodes[0].id
        input_node = _node_index(dag, str(node_ref))
        widget = binding.get("widget", 0)
        input_name = str(binding.get("name") or "audio")

        bound_params: dict[str, list[int]] = {}
        for pname, target in param_bindings.items():
            nodes = target.get("nodes") if isinstance(target, Mapping) else None
            if not isinstance(nodes, list) or not nodes:
                raise DAGError(f"param binding for {pname!r} needs a non-empty 'nodes' list")
            bound_params[pname] = [_node_index(dag, str(n)) for n in nodes]

        bound_outputs: dict[str, tuple[int, int]] = {}
        for name, target in outputs.items():
            if not isinstance(target, Mapping) or "node" not in target:
                raise DAGError(f"output {name!r} needs a 'node' (and optional 'output' slot)")
            bound_outputs[name] = (
                _node_index(dag, str(target["node"])),
                int(target.get("output", 0)),
            )
        return input_node, widget, input_name, {"params": bound_params, "outputs": bound_outputs}

    def _run(ctx: Any, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
        import numpy as np

        from ..graph.comfy_loader import load_comfy_graph
        from ..graph.core import run_dag

        audio_in = inputs.get("audio")
        waveform, sr = audio_to_numpy(audio_in) if audio_in is not None else (np.zeros((1, 1), dtype=np.float32), default_sr)

        dag = load_comfy_graph(data)
        input_node, widget, input_name, bindings = _prepare(dag)
        nodes_by_id = {n.id: n for n in dag.nodes}
        node = nodes_by_id[input_node]
        # Feed the load node through run_dag's runtime-input mapping: set its
        # input_name widget to our key and pass the materialized file via
        # inputs={key: path}. This is the same channel pymss-studio uses.
        input_key = f"__pymss_op_input_{input_name}"
        widgets = list((node.data.get("widgets_values") or []))
        while len(widgets) < 2:
            widgets.append("")
        widgets[1] = input_key
        node.data = dict(node.data or {})
        node.data["widgets_values"] = widgets
        _audio_ref(ctx, waveform, sr, input_key)

        for pname, node_ids in bindings["params"].items():
            if pname not in params:
                continue
            for node_id in node_ids:
                node = nodes_by_id[node_id]
                merged = dict(node.data.get("params") or {}) if isinstance(node.data, Mapping) else {}
                merged[pname] = params[pname]
                node.data = dict(node.data or {})
                node.data["params"] = merged

        node_results: dict[int, Any] = {}
        result = run_dag(
            dag,
            output_dir=str(_scratch_dir(ctx)),
            inputs={input_key: _audio_paths[input_key]},
            logger=getattr(ctx, "logger", None),
            node_results=node_results,
        )

        lifted: dict[str, Any] = {}
        for name, (node_id, slot) in bindings["outputs"].items():
            record = node_results.get(node_id)
            value = None
            if record is not None:
                outputs_map = getattr(record, "outputs", None)
                if outputs_map is not None:
                    value = outputs_map.get(slot, next(iter(outputs_map.values()), None))
            if isinstance(value, AudioArtifact):
                lifted[name] = numpy_to_audio(value.audio, value.sample_rate)
            else:
                lifted[name] = value
        return lifted

    return _run


def _scratch_dir(ctx: Any) -> Path:
    scratch = getattr(ctx, "scratch_dir", None)
    if scratch is not None:
        directory = Path(scratch)
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    import tempfile

    return Path(tempfile.mkdtemp(prefix="pymss-graph-op-"))


def _audio_ref(ctx: Any, waveform: Any, sr: int, key: str) -> None:
    """Materialize the input waveform into the per-run scratch dir."""
    import soundfile as sf

    path = _scratch_dir(ctx) / f"{key}.wav"
    sf.write(path, waveform.T if waveform.ndim > 1 else waveform, sr)
    _audio_paths[key] = str(path)
