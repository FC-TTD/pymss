"""Copied native PYMSS HTTP application; only model execution is delegated to Hub.

Origin: pymss/server/app.py in administrator preview baseline ba46145.
"""
import asyncio
import base64
import binascii
import json
import logging
from pathlib import Path

from pymss.model_download import DownloadError, download_model
from pymss.model_registry import model_root
from pymss.server.audio import (
    decode_pcm,
    json_response,
    normalize_stems,
    parse_int,
    validate_common_options,
    zip_response,
)
from pymss.server.config import ServerConfig
from pymss.server.errors import APIError
from pymss.server.models import (
    catalog_model_card,
    catalog_model_detail,
    filter_catalog_models,
    parse_include_files,
    parse_local_filter,
    parse_supported_filter,
)
from pymss.server.state import DEFAULT_ENDPOINT, InferenceParameterError, model_card
from ttd_model_runtime import HubError
from .state import load_state, model_spec, metadata_from_dict, mark_loaded, weights_resident
from pymss.server.webui import register_webui_routes


try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:  # pragma: no cover - exercised only without optional deps.
    raise RuntimeError("Install server dependencies with `pip install pymss[server]` or `uv sync --extra server`.") from exc


def _error_response(exc):
    """Implement the error response helper.

    Args:
        exc (Any): Exc value.

    Returns:
        Any: Computed result."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "param": exc.param,
                "code": exc.code,
            }
        },
    )


def _check_auth(request, state):
    """Check auth.

    Args:
        request (Request): Incoming FastAPI request.
        state (Any): State value.

    Returns:
        None: This callable completes for its side effects."""
    if not state.config.api_key:
        return
    expected = f"Bearer {state.config.api_key}"
    if request.headers.get("authorization") != expected:
        raise APIError(401, "invalid_api_key", "Invalid or missing API key.")


def _content_type(request):
    """Implement the content type helper.

    Args:
        request (Request): Incoming FastAPI request.

    Returns:
        Any: Computed result."""
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def _read_body(request, state):
    """Read body.

    Args:
        request (Request): Incoming FastAPI request.
        state (Any): State value.

    Returns:
        Any: Computed result."""
    max_request_bytes = state.config.max_request_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise APIError(400, "invalid_request", "Invalid Content-Length header.")
        if declared_length < 0:
            raise APIError(400, "invalid_request", "Invalid Content-Length header.")
        if declared_length > max_request_bytes:
            raise APIError(413, "request_too_large", "Request body is too large.")

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_request_bytes:
            raise APIError(413, "request_too_large", "Request body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _require_request_model(model):
    """Implement the require request model helper.

    Args:
        model (str): Model value.

    Returns:
        Any: Computed result."""
    if not model:
        raise APIError(400, "invalid_model", "The 'model' field is required.", param="model")
    return str(model)


def _require_loaded_for_inference(state):
    """Implement the require loaded for inference helper.

    Args:
        state (Any): State value.

    Returns:
        Any: Computed result."""
    if state.model_loading:
        raise APIError(409, "model_operation_in_progress", "A model load or switch operation is in progress.")
    loaded = state.loaded
    if loaded is None:
        raise APIError(503, "model_not_loaded", "No model is currently loaded.", param="model")
    if not loaded.instruments:
        raise APIError(503, "model_not_loaded", "Model configuration is unavailable; load the model with /v1/models/load first.", param="model")
    return loaded


def _require_model_id(loaded, model):
    """Implement the require model id helper.

    Args:
        loaded (LoadedModel): Loaded value.
        model (str): Model value.

    Returns:
        Any: Computed result."""
    model = _require_request_model(model)
    if not loaded.is_model_id(model):
        raise APIError(404, "model_not_found", f"Model {model!r} is not loaded by this process.", param="model")
    return model


def _validate_download_source(source, endpoint, *, source_required=False):
    """Validate download source.

    Args:
        source (str): Download source name.
        endpoint (str | None): Optional custom download endpoint.
        source_required (Any, optional): Source required value. Defaults to False.

    Returns:
        None: This callable completes for its side effects."""
    if source_required and not source:
        raise APIError(400, "invalid_download_source", "The 'source' field is required.", param="source")
    if source is not None and source not in {"modelscope", "huggingface", "hf-mirror"}:
        raise APIError(
            400,
            "invalid_download_source",
            "source must be one of: modelscope, huggingface, hf-mirror.",
            param="source",
        )
    if endpoint is not DEFAULT_ENDPOINT and endpoint is not None and not isinstance(endpoint, str):
        raise APIError(400, "invalid_download_source", "endpoint must be a string or null.", param="endpoint")


def _effective_download_source(state, source, endpoint):
    """Implement the effective download source helper.

    Args:
        state (Any): State value.
        source (str): Download source name.
        endpoint (str | None): Optional custom download endpoint.

    Returns:
        Any: Computed result."""
    effective_source = source or state.config.source
    effective_endpoint = state.config.endpoint if endpoint is DEFAULT_ENDPOINT else endpoint
    return effective_source, effective_endpoint


def _query_endpoint(query_params):
    """Implement the query endpoint helper.

    Args:
        query_params (Any): Query params value.

    Returns:
        Any: Computed result."""
    return query_params["endpoint"] if "endpoint" in query_params else DEFAULT_ENDPOINT


def _parse_bool_field(value, default, param):
    """Parse bool field.

    Args:
        value (Any): Value value.
        default (Any): Default value.
        param (str | None): Param value.

    Returns:
        Any: Parsed value."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise APIError(400, "invalid_request", f"{param} must be a boolean.", param=param)


def _parse_json_request_body(body, loaded, state):
    """Parse json request body.

    Args:
        body (Any): Body value.
        loaded (LoadedModel): Loaded value.
        state (Any): State value.

    Returns:
        Any: Parsed value."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise APIError(400, "invalid_request", "Request body must be valid JSON.")
    if not isinstance(payload, dict):
        raise APIError(400, "invalid_request", "JSON request body must be an object.")

    model = _require_model_id(loaded, payload.get("model"))
    input_data = payload.get("input")
    if not isinstance(input_data, dict):
        raise APIError(400, "invalid_request", "The 'input' object is required.", param="input")

    audio_format = str(input_data.get("format", "")).lower()
    sample_rate = parse_int(input_data.get("sample_rate"), "input.sample_rate")
    channels = parse_int(input_data.get("channels"), "input.channels")
    encoded = input_data.get("data")
    if not isinstance(encoded, str):
        raise APIError(400, "invalid_request", "input.data must be a base64 string.", param="input.data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise APIError(400, "invalid_base64", "input.data must be valid base64.", param="input.data")

    stems = normalize_stems(payload.get("stems"), loaded.instruments)
    response_format = str(payload.get("response_format", "json")).lower()
    output_audio_format = str(payload.get("output_audio_format", "wav")).lower()
    validate_common_options(response_format, output_audio_format)
    mix, seconds = decode_pcm(
        raw,
        audio_format,
        sample_rate,
        channels,
        loaded.sample_rate,
        state.config.max_audio_seconds,
    )
    return model, mix, stems, response_format, output_audio_format, seconds


def _parse_binary_request_body(request, body, loaded, state):
    """Parse binary request body.

    Args:
        request (Request): Incoming FastAPI request.
        body (Any): Body value.
        loaded (LoadedModel): Loaded value.
        state (Any): State value.

    Returns:
        Any: Parsed value."""
    params = request.query_params
    model = _require_model_id(loaded, params.get("model"))
    audio_format = str(params.get("format", "")).lower()
    if not audio_format or params.get("sample_rate") is None or params.get("channels") is None:
        raise APIError(400, "missing_audio_metadata", "format, sample_rate, and channels are required.")
    sample_rate = parse_int(params.get("sample_rate"), "sample_rate", code="invalid_query_parameter")
    channels = parse_int(params.get("channels"), "channels", code="invalid_query_parameter")
    stems = normalize_stems(params.get("stems"), loaded.instruments)
    response_format = str(params.get("response_format", "json")).lower()
    output_audio_format = str(params.get("output_audio_format", "wav")).lower()
    validate_common_options(response_format, output_audio_format)
    mix, seconds = decode_pcm(
        body,
        audio_format,
        sample_rate,
        channels,
        loaded.sample_rate,
        state.config.max_audio_seconds,
    )
    return model, mix, stems, response_format, output_audio_format, seconds


async def _parse_request(request, state, loaded, body=None):
    """Parse request.

    Args:
        request (Request): Incoming FastAPI request.
        state (Any): State value.
        loaded (LoadedModel): Loaded value.
        body (Any, optional): Body value. Defaults to None.

    Returns:
        Any: Parsed value."""
    if body is None:
        body = await _read_body(request, state)
    content_type = _content_type(request)
    if content_type == "application/json":
        return _parse_json_request_body(body, loaded, state)
    if content_type == "application/octet-stream":
        return _parse_binary_request_body(request, body, loaded, state)
    raise APIError(415, "unsupported_content_type", "Content-Type must be application/json or application/octet-stream.")


async def _run_separation(state, loaded, model, mix, stems):
    """Run native inference under the shared child-side model coordinator."""
    if state.model_loading:
        raise APIError(409, "model_operation_in_progress", "A model load or switch operation is in progress.")
    acquired = await state.limiter.acquire()
    if not acquired:
        raise APIError(429, "server_overloaded", "Inference queue is full.")
    try:
        # Snapshot the selected model under the short CPU state lock. The lock
        # is released before GPU work, so same-model calls can run concurrently;
        # model switching cannot clear selected_spec between the check and use.
        async with state.inference_lock:
            if state.model_loading:
                raise APIError(409, "model_operation_in_progress", "A model load or switch operation is in progress.")
            if state.loaded is not loaded:
                raise APIError(404, "model_not_found", f"Model {model!r} is not loaded by this process.", param="model")
            spec = dict(state.selected_spec or {})

        @state.runtime.task
        async def owned():
            engine = state.runtime.get()
            metadata, results = await asyncio.to_thread(engine.separate, spec, mix, stems)
            mark_loaded(state, loaded, metadata)
            return results

        # Runtime.task settles actual child work before propagating cancellation.
        # Native UI and provider work synchronize in the shared ModelCoordinator.
        if state.config.request_timeout_seconds:
            try:
                return await asyncio.wait_for(owned(), timeout=state.config.request_timeout_seconds)
            except asyncio.TimeoutError:
                raise APIError(504, "separation_timeout", "Separation request timed out.")
        return await owned()
    finally:
        await state.limiter.release()


def _parse_load_payload(payload):
    """Parse load payload.

    Args:
        payload (Any): Payload value.

    Returns:
        Any: Parsed value."""
    if not isinstance(payload, dict):
        raise APIError(400, "invalid_request", "JSON request body must be an object.")
    model = payload.get("model")
    if not model:
        raise APIError(400, "invalid_model", "The 'model' field is required.", param="model")
    inference_params = payload.get("inference_params")
    if inference_params is None:
        inference_params = None
    elif not isinstance(inference_params, dict):
        raise APIError(400, "invalid_inference_parameter", "inference_params must be an object.", param="inference_params")
    source = payload.get("source")
    endpoint = payload["endpoint"] if "endpoint" in payload else DEFAULT_ENDPOINT
    _validate_download_source(source, endpoint)
    return str(model), source, endpoint, inference_params


async def _load_or_switch_model(state, model, source, endpoint, inference_params):
    """Native model-switch semantics, with loading fenced by an SDK activity."""
    async with state.operation_lock:
        if state.model_lock.locked():
            raise APIError(409, "model_operation_in_progress", "A model load or switch operation is in progress.")
        if state.download_lock.locked():
            raise APIError(409, "model_download_in_progress", "A model download operation is in progress.")
        await state.model_lock.acquire()
    try:
        previous_loaded = state.loaded is not None
        state.loaded = None
        state.selected_spec = None
        state.loaded_engine_id = None
        state.model_loading = True
        state.model_loading_target = model
        effective_source, effective_endpoint = _effective_download_source(state, source, endpoint)
        spec = model_spec(state.config, model, effective_source, effective_endpoint, inference_params)
        async with state.inference_lock:
            @state.runtime.task
            async def owned():
                metadata = await asyncio.to_thread(state.runtime.get().load, spec, True)
                loaded = metadata_from_dict(metadata)
                state.selected_spec = spec
                state.loaded = loaded
                mark_loaded(state, loaded, metadata)
                return loaded

            try:
                loaded = await owned()
            except HubError:
                raise
            except InferenceParameterError as exc:
                raise APIError(400, "invalid_inference_parameter", str(exc), param="inference_params")
            except ValueError as exc:
                raise APIError(400, "invalid_model", str(exc), param="model")
            except KeyError as exc:
                raise APIError(404, "model_not_found", str(exc), param="model")
            except Exception as exc:
                state.logger.exception("Model load failed")
                raise APIError(500, "model_load_failed", str(exc), error_type="server_error")
        return previous_loaded, loaded
    finally:
        state.model_loading = False
        state.model_loading_target = None
        state.model_lock.release()


def _parse_download_payload(payload):
    """Parse download payload.

    Args:
        payload (Any): Payload value.

    Returns:
        Any: Parsed value."""
    if not isinstance(payload, dict):
        raise APIError(400, "invalid_request", "JSON request body must be an object.")
    model = payload.get("model")
    if not model:
        raise APIError(400, "invalid_model", "The 'model' field is required.", param="model")
    source = payload.get("source")
    endpoint = payload["endpoint"] if "endpoint" in payload else DEFAULT_ENDPOINT
    _validate_download_source(source, endpoint)
    force = _parse_bool_field(payload.get("force"), False, "force")
    verify = _parse_bool_field(payload.get("verify"), True, "verify")
    timeout = payload.get("timeout_seconds", 30)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise APIError(400, "invalid_request", "timeout_seconds must be a number.", param="timeout_seconds")
    if timeout <= 0:
        raise APIError(400, "invalid_request", "timeout_seconds must be greater than 0.", param="timeout_seconds")
    if timeout.is_integer():
        timeout = int(timeout)
    return str(model), source, endpoint, force, verify, timeout


def _download_result_relpaths(paths, model_dir):
    """Download result relpaths.

    Args:
        paths (Any): Paths value.
        model_dir (str | os.PathLike | None): Local model cache directory. Uses the package default when None.

    Returns:
        Any: Computed result."""
    root = model_root(model_dir).resolve()
    relpaths = []
    for path in paths:
        try:
            relpaths.append(Path(path).resolve().relative_to(root).as_posix())
        except (OSError, ValueError):
            relpaths.append("<path_resolve_error>")
    return relpaths


async def _download_model_to_local(state, model, source, endpoint, force, verify, timeout):
    """Download model to local.

    Args:
        state (Any): State value.
        model (str): Model value.
        source (str): Download source name.
        endpoint (str | None): Optional custom download endpoint.
        force (bool): Whether to overwrite or redownload existing files.
        verify (bool): Whether to validate downloads with available metadata.
        timeout (int): Network timeout in seconds.

    Returns:
        Any: Computed result."""
    async with state.operation_lock:
        if state.model_lock.locked():
            raise APIError(409, "model_operation_in_progress", "A model load or switch operation is in progress.")
        if state.download_lock.locked():
            raise APIError(409, "model_download_in_progress", "A model download operation is in progress.")
        await state.download_lock.acquire()
    try:
        state.model_downloading = True
        state.model_downloading_target = model
        effective_source, effective_endpoint = _effective_download_source(state, source, endpoint)
        try:
            result = await asyncio.to_thread(
                download_model,
                model,
                model_dir=state.config.model_dir,
                source=effective_source,
                endpoint=effective_endpoint,
                verify=verify,
                force=force,
                timeout=timeout,
            )
        except KeyError as exc:
            raise APIError(404, "model_not_found", str(exc), param="model")
        except (DownloadError, OSError) as exc:
            state.logger.exception("Model download failed")
            raise APIError(500, "model_download_failed", str(exc), error_type="server_error")
        except Exception as exc:
            state.logger.exception("Model download failed")
            raise APIError(500, "model_download_failed", str(exc), error_type="server_error")
        entry = result["entry"]
        return {
            "object": "model.download",
            "model": catalog_model_card(
                entry,
                model_dir=state.config.model_dir,
                source=effective_source,
                endpoint=effective_endpoint,
                include_files=False,
            ),
            "source": effective_source,
            "endpoint": effective_endpoint,
            "downloaded": _download_result_relpaths(result.get("downloaded", []), state.config.model_dir),
            "skipped": _download_result_relpaths(result.get("skipped", []), state.config.model_dir),
        }
    finally:
        state.model_downloading = False
        state.model_downloading_target = None
        state.download_lock.release()


def _download_source_response(state):
    """Download source response.

    Args:
        state (Any): State value.

    Returns:
        Any: Computed result."""
    return {
        "object": "download.source",
        "source": state.config.source,
        "endpoint": state.config.endpoint,
    }


def _server_info_response(state):
    """Implement the server info response helper.

    Args:
        state (Any): State value.

    Returns:
        Any: Computed result."""
    return {
        "object": "server.info",
        "webui": {
            "enabled": bool(state.config.webui),
            "path": "/ui/" if state.config.webui else None,
        },
        "auth": {
            "api_key_required": bool(state.config.api_key),
        },
        "limits": {
            "max_audio_seconds": state.config.max_audio_seconds,
            "max_request_bytes": state.config.max_request_bytes,
            "max_queue_size": state.config.max_queue_size,
            "request_timeout_seconds": state.config.request_timeout_seconds,
        },
        "download_source": {
            "source": state.config.source,
            "endpoint": state.config.endpoint,
        },
    }


async def _update_download_source(state, source, endpoint):
    """Update download source.

    Args:
        state (Any): State value.
        source (str): Download source name.
        endpoint (str | None): Optional custom download endpoint.

    Returns:
        Any: Computed result."""
    _validate_download_source(source, endpoint, source_required=True)
    async with state.operation_lock:
        if state.model_lock.locked():
            raise APIError(409, "model_operation_in_progress", "A model load or switch operation is in progress.")
        if state.download_lock.locked():
            raise APIError(409, "model_download_in_progress", "A model download operation is in progress.")
        await state.download_lock.acquire()
    try:
        state.config.source = source
        if endpoint is not DEFAULT_ENDPOINT:
            state.config.endpoint = endpoint
        return _download_source_response(state)
    finally:
        state.download_lock.release()


def build_app(config, runtime, *, initialize=True, jobs=None, profile_registry=None):
    """Create the FastAPI application.

    Args:
        config (AttrDict | dict): Loaded pymss configuration.

    Returns:
        FastAPI: Configured application instance.

    Example:
        >>> app = create_app()"""
    state = load_state(config, runtime, initialize=initialize)
    app = FastAPI(title="pymss server", version="1")
    app.state.pymss_state = state
    app.state.pymss_jobs = jobs
    app.state.pymss_profile_registry = profile_registry
    if jobs is not None:
        @app.on_event("shutdown")
        async def close_provider_jobs():
            await asyncio.to_thread(jobs.close)
    if config.webui:
        register_webui_routes(app)

    @app.exception_handler(APIError)
    async def handle_api_error(_request, exc):
        """Implement the handle api error helper.

        Args:
            _request (Any):  request value.
            exc (Any): Exc value.

        Returns:
            Any: Computed result."""
        return _error_response(exc)

    @app.get("/health")
    async def health():
        """Implement the health helper.

        Args:
            None: This callable does not accept user-provided arguments.

        Returns:
            Any: Computed result."""
        loaded = state.loaded
        return {
            "status": "ok",
            "model_loaded": weights_resident(state),
            "model_selected": loaded is not None,
            "model_loading": state.model_loading,
            "model": loaded.model_id if loaded is not None else None,
            "device": loaded.device if weights_resident(state) else None,
        }

    @app.get("/v1/models")
    async def list_models(request: Request):
        """List model catalog entries.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            list[ModelEntry]: Matching catalog entries.

        Example:
            >>> models = list_models(supported=True)
            >>> models[0].name"""
        _check_auth(request, state)
        loaded = state.loaded
        return {
            "object": "list",
            "data": [] if loaded is None else [model_card(loaded)],
        }

    @app.get("/v1/models/{model}")
    async def get_model(model: str, request: Request):
        """Return model.

        Args:
            model (str): Model value.
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        loaded = state.loaded
        if loaded is None or not loaded.is_model_id(model):
            raise APIError(404, "model_not_found", f"Model {model!r} is not loaded by this process.", param="model")
        return model_card(loaded)

    @app.get("/v1/catalog/models")
    async def list_catalog_models(request: Request):
        """Implement the list catalog models helper.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        try:
            supported = parse_supported_filter(request.query_params.get("supported"))
            local = parse_local_filter(request.query_params.get("local"))
            include_files = parse_include_files(request.query_params.get("include_files"))
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc))
        source, endpoint = _effective_download_source(
            state,
            request.query_params.get("source"),
            _query_endpoint(request.query_params),
        )
        _validate_download_source(source, endpoint)
        entries = filter_catalog_models(
            category=request.query_params.get("category"),
            supported=supported,
            local=local,
            q=request.query_params.get("q"),
            model_dir=state.config.model_dir,
        )
        return {
            "object": "list",
            "data": [
                catalog_model_card(
                    entry,
                    model_dir=state.config.model_dir,
                    source=source,
                    endpoint=endpoint,
                    include_files=include_files,
                )
                for entry in entries
            ],
            "pymss": {
                "source": source,
                "endpoint": endpoint,
                "total": len(entries),
            },
        }

    @app.get("/v1/catalog/models/{model}")
    async def get_catalog_model(model: str, request: Request):
        """Return catalog model.

        Args:
            model (str): Model value.
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        source, endpoint = _effective_download_source(
            state,
            request.query_params.get("source"),
            _query_endpoint(request.query_params),
        )
        _validate_download_source(source, endpoint)
        try:
            return catalog_model_detail(model, model_dir=state.config.model_dir, source=source, endpoint=endpoint)
        except KeyError as exc:
            raise APIError(404, "model_not_found", str(exc), param="model")

    @app.post("/v1/models/load")
    async def load_model_endpoint(request: Request):
        """Load model endpoint.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        body = await _read_body(request, state)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "invalid_request", "Request body must be valid JSON.")
        model, source, endpoint, inference_params = _parse_load_payload(payload)
        previous_loaded, loaded = await _load_or_switch_model(state, model, source, endpoint, inference_params)
        return {
            "object": "model.load",
            "previous_model_loaded": previous_loaded,
            "model_loaded": True,
            "model": model_card(loaded),
        }

    @app.post("/v1/models/download")
    async def download_model_endpoint(request: Request):
        """Download model endpoint.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        body = await _read_body(request, state)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "invalid_request", "Request body must be valid JSON.")
        model, source, endpoint, force, verify, timeout = _parse_download_payload(payload)
        return await _download_model_to_local(state, model, source, endpoint, force, verify, timeout)

    @app.get("/v1/download-source")
    async def get_download_source(request: Request):
        """Return download source.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        return _download_source_response(state)

    @app.post("/v1/download-source")
    async def update_download_source(request: Request):
        """Update download source.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        body = await _read_body(request, state)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "invalid_request", "Request body must be valid JSON.")
        if not isinstance(payload, dict):
            raise APIError(400, "invalid_request", "JSON request body must be an object.")
        endpoint = payload["endpoint"] if "endpoint" in payload else DEFAULT_ENDPOINT
        return await _update_download_source(state, payload.get("source"), endpoint)

    @app.get("/v1/server/info")
    async def get_server_info(request: Request):
        """Return server info.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        return _server_info_response(state)

    @app.post("/v1/audio/separations")
    async def separate_audio(request: Request):
        """Separate audio.

        Args:
            request (Request): Incoming FastAPI request.

        Returns:
            Any: Computed result."""
        _check_auth(request, state)
        body = await _read_body(request, state)
        loaded = _require_loaded_for_inference(state)
        model, mix, stems, response_format, output_audio_format, input_seconds = await _parse_request(
            request,
            state,
            loaded,
            body,
        )
        try:
            results = await _run_separation(state, loaded, model, mix, stems)
        except (APIError, HubError):
            raise
        except Exception as exc:
            state.logger.exception("Separation failed")
            raise APIError(500, "separation_failed", str(exc), error_type="server_error")

        try:
            if response_format == "json":
                return json_response(loaded, model, results, stems, input_seconds, output_audio_format)

            content = zip_response(loaded, model, results, stems, input_seconds, output_audio_format)
            return Response(content=content, media_type="application/zip")
        except APIError:
            raise
        except Exception as exc:
            state.logger.exception("Encoding separation response failed")
            raise APIError(500, "separation_failed", str(exc), error_type="server_error")

    def _provider_jobs():
        if jobs is None:
            raise APIError(503, "separation_unavailable", "Provider batch service is unavailable.")
        return jobs

    async def _batch_payload(request):
        body = await _read_body(request, state)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "invalid_request", "Request body must be valid JSON.")
        if not isinstance(payload, dict):
            raise APIError(400, "invalid_request", "JSON request body must be an object.")
        inputs = payload.get("input_paths", payload.get("inputs"))
        if not isinstance(inputs, list) or not inputs or any(not isinstance(item, str) for item in inputs):
            raise APIError(400, "invalid_request", "inputs must be a non-empty list of paths.", param="inputs")
        profile = payload.get("profile_id", payload.get("profile"))
        profile_version = payload.get("profile_version")
        recipe = payload.get("recipe")
        if profile is None and recipe is None:
            raise APIError(400, "invalid_request", "profile_id/profile or recipe is required.")
        return payload, inputs, profile, profile_version, recipe

    @app.post("/v1/separations", status_code=202)
    async def submit_separation(request: Request):
        """Submit a provider Profile task through the formal Runtime entry."""
        _check_auth(request, state)
        provider_jobs = _provider_jobs()
        payload, inputs, profile, profile_version, recipe = await _batch_payload(request)
        try:
            return provider_jobs.submit(
                recipe=recipe,
                paths=inputs,
                output_dir=payload.get("output_dir"),
                profile=profile,
                profile_version=profile_version,
            )
        except (ValueError, OSError) as exc:
            raise APIError(400, "invalid_request", str(exc)) from None
        except Exception as exc:
            from hub_batch.jobs import Busy
            if isinstance(exc, Busy):
                raise APIError(429, "server_overloaded", str(exc)) from None
            raise

    @app.post("/v1/batches", status_code=202)
    async def submit_batch(request: Request):
        """Submit a fixed DAG or immutable Profile over one or more files."""
        _check_auth(request, state)
        provider_jobs = _provider_jobs()
        payload, inputs, profile, profile_version, recipe = await _batch_payload(request)
        try:
            return provider_jobs.submit(
                recipe=recipe,
                paths=inputs,
                output_dir=payload.get("output_dir"),
                profile=profile,
                profile_version=profile_version,
            )
        except (ValueError, OSError) as exc:
            raise APIError(400, "invalid_request", str(exc)) from None
        except Exception as exc:
            from hub_batch.jobs import Busy
            if isinstance(exc, Busy):
                raise APIError(429, "server_overloaded", str(exc)) from None
            raise

    @app.get("/v1/batches/{task_id}")
    async def provider_batch_status(task_id: str, request: Request):
        _check_auth(request, state)
        task = _provider_jobs().store
        try:
            return task.get(task_id)
        except KeyError:
            raise APIError(404, "task_not_found", "Batch task was not found.") from None

    @app.post("/v1/batches/{task_id}/cancel")
    async def provider_batch_cancel(task_id: str, request: Request):
        _check_auth(request, state)
        try:
            return _provider_jobs().cancel(task_id)
        except KeyError:
            raise APIError(404, "task_not_found", "Batch task was not found.") from None
        except HubError as exc:
            raise APIError(503, exc.code, str(exc)) from None

    @app.get("/v1/batches/{task_id}/events")
    async def provider_batch_events(task_id: str, request: Request):
        _check_auth(request, state)
        provider_jobs = _provider_jobs()
        try:
            provider_jobs.store.get(task_id)
        except KeyError:
            raise APIError(404, "task_not_found", "Batch task was not found.") from None

        async def stream():
            previous = None
            while True:
                try:
                    task = provider_jobs.store.get(task_id)
                except KeyError:
                    return
                payload = json.dumps(task, ensure_ascii=False)
                if payload != previous:
                    yield f"event: task\ndata: {payload}\n\n"
                    previous = payload
                if task["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                    return
                await asyncio.sleep(.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
