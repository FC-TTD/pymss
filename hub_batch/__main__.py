"""Independent CPU entry; Studio retains its existing runtime and model selection."""
import os

from ttd_model_runtime import Runtime
from ttd_model_runtime.integrations.fastapi import attach

from .api import build_app
from .engine import load_engine, completion, release
from .jobs import Jobs
from .msst_compat import CompatTasks
from .provider import ProfileManifest, ProfileRegistry


def default_profiles():
    return ProfileRegistry([
        ProfileManifest("dialogue-vocal", "2026-09-20.v1", "duality-two-stems",
                        outputs=("Vocals", "Instrumental"),
                        models=("melband_roformer_instvox_duality_v2.ckpt",), max_concurrency=1),
        ProfileManifest("instrumental-separation", "2026-09-20.v1", "dedicated-me",
                        outputs=("Instrumental",),
                        models=("mel_band_roformer_instrumental_becruily.ckpt",), max_concurrency=1),
    ])


def create_app():
    runtime = Runtime(load_engine, completion=completion, release=release,
                      gpu_process=True, execution_timeout=None)
    jobs = Jobs(runtime, os.environ["PYMSS_BATCH_STATE_DIR"],
                os.environ["PYMSS_BATCH_INPUT_ROOT"], os.environ["PYMSS_BATCH_OUTPUT_ROOT"])
    compat = CompatTasks(runtime, os.environ["PYMSS_BATCH_STATE_DIR"],
                         os.environ["PYMSS_BATCH_INPUT_ROOT"], os.environ["PYMSS_BATCH_OUTPUT_ROOT"])
    runtime.pending_work = jobs.store.pending
    return attach(build_app(jobs, os.getenv("PYMSS_API_KEY"), compat=compat,
                            profile_registry=default_profiles()), runtime=runtime)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.getenv("PYMSS_BATCH_PORT", "8010")))
