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


def default_profiles():
    from hub_batch.provider import ProfileManifest, ProfileRegistry
    return ProfileRegistry([
        ProfileManifest("dialogue-vocal", "2026-09-20.v1", "duality-two-stems",
                        outputs=("Vocals", "Instrumental"),
                        models=("melband_roformer_instvox_duality_v2.ckpt",), max_concurrency=1),
        ProfileManifest("instrumental-separation", "2026-09-20.v1", "instrumental-only",
                        outputs=("Instrumental",),
                        models=("mel_band_roformer_instrumental_becruily.ckpt",), max_concurrency=1),
        ProfileManifest("dialogue-and-instrumental", "2026-09-20.v1", "dedicated-me",
                        outputs=("dialogue", "instrumental"),
                        models=("melband_roformer_instvox_duality_v2.ckpt",
                                "mel_band_roformer_instrumental_becruily.ckpt"), max_concurrency=1),
        ProfileManifest("dialogue-and-instrumental-dedicated", "2026-09-20.v1", "dedicated-me",
                        outputs=("dialogue", "instrumental"),
                        models=("melband_roformer_instvox_duality_v2.ckpt",
                                "mel_band_roformer_instrumental_becruily.ckpt"), max_concurrency=1),
    ])


def _provider_jobs(runtime):
    from hub_batch.jobs import Jobs
    input_root = os.getenv("PYMSS_BATCH_INPUT_ROOT")
    output_root = os.getenv("PYMSS_BATCH_OUTPUT_ROOT")
    state_dir = os.getenv("PYMSS_BATCH_STATE_DIR")
    if not input_root or not output_root or not state_dir:
        return None
    return Jobs(runtime, state_dir, input_root, output_root, profile_registry=default_profiles())


def create_app(runtime=None, config=None, jobs=None):
    from fastapi.responses import RedirectResponse
    from ttd_model_runtime import Runtime
    from ttd_model_runtime.integrations.fastapi import attach
    from .server_app import build_app
    runtime = runtime or Runtime(load_model, completion=completion, cleanup=cleanup,
                                 release=release, gpu_process=True, execution_timeout=None)
    jobs = jobs if jobs is not None else _provider_jobs(runtime)
    if jobs is not None:
        runtime.pending_work = jobs.store.pending
    app = build_app(config or server_config(), runtime, jobs=jobs,
                    profile_registry=getattr(jobs, "profile_registry", None))

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
