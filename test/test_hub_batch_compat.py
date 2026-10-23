import json
from contextlib import contextmanager

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from hub_batch.api import build_app
from hub_batch.__main__ import default_profiles
from hub_batch.engine import BatchEngine
from hub_batch.msst_compat import CompatTasks
from test.test_hub_batch import Separator


class Runtime:
    def __init__(self, engine): self.engine = engine
    def accept_background_work(self): pass
    def complete_background_work(self): pass
    @contextmanager
    def execution(self):
        yield
    def get(self): return self.engine


def test_legacy_sync_contract_accepts_catalog_model_and_preserves_output_names(tmp_path):
    source = tmp_path / "episode" / "EP01.wav"
    source.parent.mkdir()
    sf.write(source, np.zeros((4410, 2), dtype=np.float32), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    compat = CompatTasks(Runtime(engine), tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(None, compat=compat)) as client:
            response = client.post("/api/v1/tasks/msst-batch?mode=sync", json={
                "input_path": str(source), "output_dir": str(tmp_path / "out"),
                "model_name": "mel_band_roformer_vocals_becruily.ckpt",
                "model_type": "vocal_models", "extract_instrumental": ["Vocals"], "params": {},
            })
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["task_id"] and body["status"] == "success"
            result = client.get(f"/api/v1/tasks/{body['task_id']}/result").json()
            assert result["status"] == "success"
            assert result["files"][0]["output_files"] == ["EP01_Vocals.wav", "EP01_Instrumental.wav"]
    finally:
        compat.close()


def test_legacy_status_uses_client_compatible_task_fields(tmp_path):
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    compat = CompatTasks(Runtime(engine), tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        task = {"id": "task", "status": "running", "progress": .5, "processed_files": 1, "total_files": 2,
                "error": None, "input_path": str(tmp_path), "output_dir": str(tmp_path / "out"),
                "model_name": "x", "stems": ["Vocals"], "files": []}
        compat.store.put(task)
        with TestClient(build_app(None, compat=compat)) as client:
            response = client.get("/api/v1/tasks/task")
            assert response.status_code == 200
            assert response.json()["task_id"] == "task"
            assert response.json()["processed_files"] == 1
    finally:
        compat.close()


def test_stable_profile_provider_shape_resolves_default_manifest(tmp_path):
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    compat = CompatTasks(Runtime(engine), tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(None, compat=compat, profile_registry=default_profiles())) as client:
            response = client.post("/v1/separations", json={"profile":"dialogue-vocal", "profile_version":"2026-09-20.v1", "inputs":[str(tmp_path / "missing.wav")], "output_dir":str(tmp_path / "out")})
            assert response.status_code == 400
    finally:
        compat.close()

def test_default_profile_manifests_match_public_output_roles_and_dags():
    registry = default_profiles()
    assert registry.resolve("dialogue-vocal", "2026-09-20.v1").dag == "duality-two-stems"
    assert registry.resolve("dialogue-vocal", "2026-09-20.v1").outputs == ("Vocals", "Instrumental")
    assert registry.resolve("instrumental-separation", "2026-09-20.v1").dag == "instrumental-only"
    assert registry.resolve("instrumental-separation", "2026-09-20.v1").outputs == ("Instrumental",)
    for name in ("dialogue-and-instrumental", "dialogue-and-instrumental-dedicated"):
        manifest = registry.resolve(name, "2026-09-20.v1")
        assert manifest.dag == "dedicated-me"
        assert manifest.outputs == ("dialogue", "instrumental")
        assert len(manifest.models) == 2

