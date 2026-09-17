"""Serve the original PYMSS API/UI with a permanently CPU HTTP process."""
import json
import os
import sys

from .adapter import load_model, completion, cleanup, release


def server_config():
    from pymss.server.config import ServerConfig
    params = json.loads(os.getenv("PYMSS_HUB_INFERENCE_PARAMS", "{}"))
    if not isinstance(params, dict):
        raise ValueError("PYMSS_HUB_INFERENCE_PARAMS must be a JSON object")
    return ServerConfig(
        model=os.getenv("PYMSS_HUB_INITIAL_MODEL", "BS-Roformer-Resurrection.ckpt"),
        model_dir=os.getenv("PYMSS_MODEL_DIR", "/models"),
        source=os.getenv("PYMSS_DOWNLOAD_SOURCE", "hf-mirror"),
        endpoint=os.getenv("PYMSS_DOWNLOAD_ENDPOINT") or None,
        device="cuda", device_ids=[0],
        api_key=os.getenv("PYMSS_API_KEY") or None,
        host="0.0.0.0", port=8000, webui=True,
        inference_params=params,
        max_queue_size=int(os.getenv("PYMSS_MAX_QUEUE_SIZE", "1")),
        max_audio_seconds=float(os.getenv("PYMSS_MAX_AUDIO_SECONDS", "600")),
        max_request_bytes=int(os.getenv("PYMSS_MAX_REQUEST_BYTES", "536870912")),
        request_timeout_seconds=float(os.getenv("PYMSS_REQUEST_TIMEOUT_SECONDS", "0")),
    )


def create_app(runtime=None, config=None):
    from fastapi.responses import RedirectResponse
    from ttd_model_runtime import Runtime
    from ttd_model_runtime.integrations.fastapi import attach
    from .server_app import build_app
    runtime = runtime or Runtime(load_model, completion=completion, cleanup=cleanup,
                                 release=release, gpu_process=True, execution_timeout=None)
    app = build_app(config or server_config(), runtime)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root():
        return RedirectResponse("/ui/", status_code=302)

    return attach(app, runtime=runtime)


def describe():
    from .server_app import build_app
    return build_app(server_config(), None, initialize=False).openapi()


def main():
    if sys.argv[1:] == ["describe"]:
        print(json.dumps(describe(), ensure_ascii=False))
        return
    if sys.argv[1:]:
        raise SystemExit("Use python -m hub_runtime [describe]")
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
