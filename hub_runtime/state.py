"""CPU selection metadata survives SDK engine eviction and reload."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from pymss.logger import get_separation_logger


class InferenceGate:
    """Shared inference gate with an exclusive model-switch mode.

    Native requests for the resident model may overlap. A model load/switch
    takes the exclusive mode and waits for every active request to finish.
    ``locked()`` remains a useful activity probe for diagnostics and tests.
    """

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active = 0
        self._exclusive = False

    def locked(self) -> bool:
        return self._active > 0 or self._exclusive

    async def __aenter__(self):
        async with self._condition:
            while self._exclusive:
                await self._condition.wait()
            self._active += 1
        return self

    async def __aexit__(self, *_exc):
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    @asynccontextmanager
    async def exclusive(self):
        async with self._condition:
            while self._exclusive or self._active:
                await self._condition.wait()
            self._exclusive = True
        try:
            yield self
        finally:
            async with self._condition:
                self._exclusive = False
                self._condition.notify_all()

from pymss.model_registry import ModelEntry, resolve_model
from pymss.server.state import (
    LoadedModel, RequestLimiter, ServerState, _preload_config,
    supported_parameters, validate_inference_params,
)


def model_spec(config, model, source=None, endpoint=None, inference_params=None):
    params = dict(config.inference_params or {})
    if inference_params is not None:
        params.update(inference_params)
    return {"model": model, "model_dir": config.model_dir,
            "source": source or config.source, "endpoint": endpoint,
            "inference_params": params, "debug": config.debug,
            "max_concurrency": max(1, int(getattr(config, "max_queue_size", 1)))}


def metadata_from_dict(metadata):
    values = dict(metadata)
    entry = ModelEntry.from_dict(values.pop("entry"))
    resolved = dict(values.pop("resolved"), entry=entry)
    values["instruments"] = tuple(values["instruments"])
    return LoadedModel(separator=None, entry=entry, resolved=resolved, **values)


def selected_metadata(spec):
    """Read installed config/catalog without constructing a model or downloading."""
    resolved = resolve_model(spec["model"], model_dir=spec["model_dir"],
                             require_supported=True, require_exists=False)
    entry = resolved["entry"]
    config_path = resolved.get("config_path")
    config = _preload_config(resolved) if config_path and Path(config_path).is_file() else None
    if resolved["model_type"] == "vr":
        from pymss.modules.vocal_remover.vr_models import get_vr_model_metadata

        native = get_vr_model_metadata(resolved["model_path"])
        instruments = (native["primary_stem"], native["secondary_stem"])
        sample_rate = 44100  # MSSeparator's native VR input rate.
        validate_inference_params(spec["inference_params"], None, "vr")
    elif config is not None:
        validate_inference_params(spec["inference_params"], config, resolved["model_type"])
        instruments = tuple(str(item) for item in config.training.instruments)
        sample_rate = int(config.audio.get("sample_rate", 44100))
    else:
        # Catalog is CPU-only. Actual native metadata replaces this upon load.
        # 2.1.x catalogs no longer contain config_instruments. A category's
        # target_stem is not the model's complete output schema; do not guess it.
        instruments = ()
        sample_rate = 44100
    return LoadedModel(
        separator=None, entry=entry, resolved=resolved,
        requested_model=spec["model"], model_id=entry.name,
        sample_rate=sample_rate, instruments=instruments, device="cuda",
        inference_params=spec["inference_params"],
        supported_parameters=supported_parameters(config, resolved["model_type"]),
    )


def load_state(config, runtime, *, initialize=True):
    state = ServerState(
        config=config, logger=get_separation_logger(),
        operation_lock=asyncio.Lock(), limiter=RequestLimiter(config.max_queue_size),
        model_lock=asyncio.Lock(), inference_lock=InferenceGate(), download_lock=asyncio.Lock(),
    )
    state.runtime = runtime
    state.selected_spec = None
    state.loaded_engine_id = None
    if initialize and config.model:
        state.selected_spec = model_spec(config, config.model, endpoint=config.endpoint)
        state.loaded = selected_metadata(state.selected_spec)
    return state


def mark_loaded(state, loaded, metadata):
    """Update in-place so native queued requests retain their identity fence."""
    actual = metadata_from_dict(metadata)
    loaded.__dict__.update(actual.__dict__)
    state.loaded_engine_id = state.runtime.status().get("engine", {}).get("id")


def weights_resident(state):
    status = state.runtime.status() if state.runtime is not None else {}
    return (state.loaded is not None and state.loaded_engine_id is not None
            and status.get("residency") == "ready"
            and status.get("engine", {}).get("id") == state.loaded_engine_id)
