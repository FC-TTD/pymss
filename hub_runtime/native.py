"""One native separator, accessed only inside the SDK engine process."""
from dataclasses import asdict
from typing import Any

from pymss.graph import SeparatorCache

from hub_batch.engine import BatchEngine
from hub_batch.provider import ModelCoordinator, SharedModel


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
    def __init__(self, coordinator: ModelCoordinator | None = None):
        self.loaded = None
        self.spec = None
        self.coordinator = coordinator

    @staticmethod
    def _residency_key(spec: dict[str, Any]) -> str:
        # Native and provider paths use the same model description identity;
        # runtime-only fields such as stems and output paths stay out of it.
        return ModelCoordinator.key_for({
            "model": spec.get("model", spec.get("model_name")),
            "model_dir": spec.get("model_dir"),
            "inference_params": spec.get("inference_params", {}),
        })

    @staticmethod
    def _native_factory(**spec):
        from pymss.server.config import ServerConfig
        from pymss.server.state import load_model
        config = ServerConfig(
            model_dir=spec["model_dir"], source=spec["source"],
            endpoint=spec["endpoint"], device="cuda", device_ids=[0],
            debug=spec.get("debug", False),
        )
        loaded = load_model(config, spec["model"], inference_params=spec.get("inference_params"))
        return SharedModel(loaded.separator, describe_loaded(loaded))

    def _metadata_from_spec(self, spec):
        from .state import selected_metadata
        return describe_loaded(selected_metadata(spec))

    def load(self, spec, force=False):
        if self.coordinator is None:
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

        key = self._residency_key(spec)
        lease_spec = dict(spec)
        max_concurrency = lease_spec.pop("max_concurrency", 1)
        with self.coordinator.lease(
            max_concurrency=max_concurrency,
            residency_key=key,
            factory=self._native_factory,
            replace=force,
            **lease_spec,
        ) as shared:
            metadata = getattr(shared, "metadata", None) or self._metadata_from_spec(spec)
        self.spec = dict(spec)
        self.loaded = metadata
        return metadata

    def separate(self, spec, mix, stems):
        if self.coordinator is None:
            metadata = self.load(spec)
            separator = self.loaded.separator
            if separator.model_type == "vr":
                results = separator.separate(mix, pbar=False)
            else:
                results = separator.separate(mix, pbar=False, stems=stems)
            return metadata, results

        key = self._residency_key(spec)
        lease_spec = dict(spec)
        max_concurrency = lease_spec.pop("max_concurrency", 1)
        with self.coordinator.lease(
            max_concurrency=max_concurrency,
            residency_key=key,
            factory=self._native_factory,
            **lease_spec,
        ) as shared:
            metadata = getattr(shared, "metadata", None) or self._metadata_from_spec(spec)
            separator = getattr(shared, "separator", shared)
            if separator.model_type == "vr":
                results = separator.separate(mix, pbar=False)
            else:
                results = separator.separate(mix, pbar=False, stems=stems)
        self.spec = dict(spec)
        self.loaded = metadata
        return metadata, results

    def close(self):
        if self.coordinator is not None:
            # The unified backend owns the coordinator because batch and native
            # callers share it. Its release path closes the resident exactly once.
            self.spec = None
            self.loaded = None
            return
        from pymss.server.state import close_loaded_model
        loaded, self.loaded = self.loaded, None
        self.spec = None
        close_loaded_model(loaded)


class UnifiedBackend:
    """Single GPU backend for native PYMSS and provider Profile/Batch work."""

    def __init__(self, model_dir: str):
        def fallback_factory(**kwargs):
            return SharedModel(SeparatorCache._default_factory(**kwargs))

        self.coordinator = ModelCoordinator(fallback_factory)
        self.native = NativeBackend(self.coordinator)
        self.batch = BatchEngine(model_dir, coordinator=self.coordinator)

    # Native UI/API hooks.
    def load(self, spec, force=False):
        return self.native.load(spec, force)

    def separate(self, spec, mix, stems):
        return self.native.separate(spec, mix, stems)

    # Provider task hooks used by hub_batch.jobs.
    def prepare(self, task_id):
        return self.batch.prepare(task_id)

    def cancel(self, task_id):
        return self.batch.cancel(task_id)

    def run(self, task_id, recipe, inputs, output_dir, **kwargs):
        return self.batch.run(task_id, recipe, inputs, output_dir, **kwargs)

    def run_profile(self, task_id, profile, inputs, output_dir):
        return self.batch.run_profile(task_id, profile, inputs, output_dir)

    def run_model(self, *args, **kwargs):
        return self.batch.run_model(*args, **kwargs)

    def run_msst(self, *args, **kwargs):
        return self.batch.run_msst(*args, **kwargs)

    def close(self):
        self.native.close()
        self.batch.close()
        self.coordinator.close()
