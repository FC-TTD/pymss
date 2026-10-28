"""Round 4c: legacy NormalizeLUFS workflow node still runs after the loudnorm
plugin rewrite (old comfy workflows keep executing the same implementation).
"""

from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _load_loudnorm(tmp_path, monkeypatch):
    """Import the real loudnorm plugin the way the loader does."""
    import importlib.util
    import shutil
    import sys

    src = __import__("pathlib").Path(
        __file__).resolve().parents[2] / "pymss-plugins" / "plugins" / "loudnorm"
    dest = tmp_path / "loudnorm"
    shutil.copytree(src, dest)
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("_test_loudnorm", dest / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_loudnorm"] = module
    spec.loader.exec_module(module)
    yield


def test_legacy_node_registered_and_runs():
    from pymss.graph.core import (
        AUDIO,
        DAG,
        DAGLink,
        DAGNode,
        NodeContext,
        NodeResult,
        NodeSignature,
        PortSpec,
        SeparatorCache,
        get_node_type,
        register_node,
        run_dag,
        AudioArtifact,
    )
    import tempfile

    info = get_node_type("NormalizeLUFS")
    node = DAGNode(id="n", type="NormalizeLUFS", data={"widgets_values": [-20.0]})
    sig = info.signature(node)
    assert [p.name for p in sig.inputs][:1] == ["audio"]

    def _src_exec(ctx, inputs):
        sr = 44100
        t = np.arange(sr, dtype=np.float32) / sr
        audio = (0.3 * np.stack([np.sin(2 * np.pi * 440 * t)] * 2)).astype(np.float32)
        return NodeResult(outputs={0: AudioArtifact(audio, sr, channel_layout="stereo")})

    register_node("t.src", signature=lambda n: NodeSignature(
        inputs=[], output_names=["audio"], output_types=[AUDIO]
    ), execute=_src_exec)

    def _save_exec(ctx, inputs):
        from pymss.audio_io import save_audio
        art = inputs["audio"]
        out = ctx.output_dir / "legacy.wav"
        save_audio(str(out), art.audio.T, art.sample_rate, "wav", {})
        return NodeResult(outputs={}, saved_paths=[str(out)])

    register_node("t.save", signature=lambda n: NodeSignature(
        inputs=[PortSpec(name="audio", type=AUDIO)], output_names=[], output_types=[]
    ), execute=_save_exec)

    src = DAGNode(id="src", type="t.src")
    legacy = DAGNode(
        id="ln", type="NormalizeLUFS",
        inputs=[DAGLink(1, "src", 0, "ln", 0, AUDIO)],
        data={"widgets_values": [-20.0]},
    )
    save = DAGNode(id="sv", type="t.save",
                   inputs=[DAGLink(2, "ln", 0, "sv", 0, AUDIO)])
    with tempfile.TemporaryDirectory() as od:
        saved = run_dag(DAG(nodes=[src, legacy, save]), output_dir=od)
        assert len(saved) == 1
        import pyloudnorm as pyln
        import soundfile as sf

        data, sr = sf.read(saved[0], dtype="float32")
        meter = pyln.Meter(sr)
        lufs = meter.integrated_loudness(data if data.ndim == 2 else data[:, None])
        assert abs(lufs - (-20.0)) < 0.5
