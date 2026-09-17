"""One native separator, accessed only inside the SDK engine process.

The HTTP layer keeps the existing PYMSS inference/model-operation locks. This
object adds neither threads nor a competing inference queue.
"""
from dataclasses import asdict


def describe_loaded(loaded):
    """Return metadata only; never serialize the separator or CUDA tensors."""
    return {
        "entry": asdict(loaded.entry),
        "resolved": {key: value for key, value in loaded.resolved.items() if key != "entry"},
        "requested_model": loaded.requested_model,
        "model_id": loaded.model_id,
        "sample_rate": loaded.sample_rate,
        "instruments": list(loaded.instruments),
        "device": loaded.device,
        "inference_params": dict(loaded.inference_params),
        "supported_parameters": loaded.supported_parameters,
        "audio_params": loaded.audio_params,
    }


class NativeBackend:
    def __init__(self):
        self.loaded = None
        self.spec = None

    def load(self, spec, force=False):
        from pymss.server.config import ServerConfig
        from pymss.server.state import load_model
        if not force and self.loaded is not None and spec == self.spec:
            return describe_loaded(self.loaded)
        self.close()
        config = ServerConfig(
            model_dir=spec["model_dir"], source=spec["source"],
            endpoint=spec["endpoint"], device="cuda", device_ids=[0],
            debug=spec.get("debug", False),
        )
        self.loaded = load_model(config, spec["model"], inference_params=spec["inference_params"])
        self.spec = dict(spec)
        return describe_loaded(self.loaded)

    def separate(self, spec, mix, stems):
        metadata = self.load(spec)
        separator = self.loaded.separator
        if separator.model_type == "vr":
            results = separator.separate(mix, pbar=False)
        else:
            results = separator.separate(mix, pbar=False, stems=stems)
        return metadata, results

    def close(self):
        from pymss.server.state import close_loaded_model
        loaded, self.loaded = self.loaded, None
        self.spec = None
        # Preserve native close semantics, including its own transient CPU move.
        close_loaded_model(loaded)
