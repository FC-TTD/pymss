"""Contract tests use CPU fixtures; they are not model inference acceptance."""
from hub_batch.profiles import default_profiles
import pytest


def test_profile_contract_discovery_and_unknown_options_before_acceptance(tmp_path):
    from fastapi.testclient import TestClient
    from hub_batch.api import build_app
    from hub_batch.jobs import Jobs
    from test.test_hub_batch import Runtime

    jobs = Jobs(Runtime(None), tmp_path / "state", tmp_path, tmp_path / "out", default_profiles())
    with TestClient(build_app(jobs, profile_registry=jobs.profile_registry)) as client:
        response = client.get("/v1/separations/profiles")
        assert response.status_code == 200
        payload = response.json()
        assert payload["contract_version"] == "2026-10-23.v1"
        assert len(payload["profiles"]) == 22
        profile = next(item for item in payload["profiles"] if item["profile_id"] == "dialogue-vocal-b" and item["profile_version"] == "2026-10-28.v2")
        assert profile["implementation_version"] == default_profiles().resolve("dialogue-vocal-b").digest
        assert [item["role"] for item in profile["output_roles"]] == ["vocals", "instrumental"]
        source = tmp_path / "input.wav"
        source.write_bytes(b"not read")
        body = {"profile_id": "dialogue-vocal-b", "profile_version": "2026-10-23.v1", "inputs": [str(source)]}
        assert client.post("/v1/separations", json={**body, "options": {"batch_size": 2}}).status_code == 400
        assert client.post("/v1/separations", json={**body, "implementation_version": "wrong"}).status_code == 400
        assert client.post("/v1/separations", json={**body, "params": {"batch_size": 2}}).status_code == 400
        assert client.post("/v1/separations", json={**body, "options": None}).status_code == 400
        assert jobs.store.pending() == 0


def test_mutating_resolved_profile_cannot_change_registered_identity():
    registry = default_profiles()
    manifest = registry.resolve("dialogue-vocal-b")
    digest = manifest.digest
    manifest.implementation["assets"][0]["sha256"] = "changed"
    assert registry.resolve("dialogue-vocal-b").digest == digest


@pytest.mark.parametrize("outcome", ["cancelled", "unknown"])
def test_profile_receipt_preserves_committed_results_and_uncertain_capacity(tmp_path, monkeypatch, outcome):
    from contextlib import contextmanager
    from dataclasses import replace
    import numpy as np
    import soundfile as sf
    from fastapi.testclient import TestClient
    from hub_batch.api import build_app
    from hub_batch.engine import BatchEngine
    from hub_batch.jobs import Jobs
    from hub_batch.provider import ProfileRegistry
    from test.test_dayan_profile_batch import MetadataRuntime
    from test.test_hub_batch import Separator, wait_task
    from ttd_model_runtime.protocol import HubError

    class CompletionRuntime(MetadataRuntime):
        @contextmanager
        def execution(self, **kwargs):
            with super().execution(**kwargs):
                yield
            if outcome == "unknown":
                raise HubError("execution_unknown")

    source = tmp_path / "source.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    actual = default_profiles().resolve("dialogue-vocal")
    manifest = replace(actual, implementation={**actual.implementation, "assets": []})
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    if outcome == "cancelled":
        # A disconnected observer never receives the file_done event; the
        # authoritative per-input manifest is already durable before cancellation.
        def disconnected_observer(event):
            if event["kind"] == "file_done":
                engine.cancel(engine.task_id)
        monkeypatch.setattr("hub_batch.engine.engine_progress", lambda: disconnected_observer)
    jobs = Jobs(CompletionRuntime(engine), tmp_path / "state", tmp_path, tmp_path / "out", ProfileRegistry([manifest]))
    try:
        with TestClient(build_app(jobs)) as client:
            body = {"profile_id": manifest.name, "profile_version": manifest.version,
                    "inputs": [str(source)] * (2 if outcome == "cancelled" else 1)}
            response = client.post("/v1/separations", json=body)
            task = wait_task(client, response.json()["id"])
            assert task["state"] == outcome, task
            result = client.get(f"/v1/batches/{task['id']}/result").json()
            assert len(result["files"]) == 1
            assert len(result["files"][0]["outputs"]) == 2, result
            assert jobs.store.pending() == (1 if outcome == "unknown" else 0)
            if outcome == "unknown":
                assert client.post("/v1/separations", json=body).status_code == 429
    finally:
        engine.close()


@pytest.mark.parametrize("failure", ["missing_role", "bad_media", "duplicate_path", "outside_path"])
def test_bad_profile_output_is_failed_and_previous_committed_input_remains(tmp_path, monkeypatch, failure):
    from dataclasses import replace
    from pathlib import Path
    from types import SimpleNamespace
    import numpy as np
    import soundfile as sf
    from fastapi.testclient import TestClient
    from hub_batch.api import build_app
    from hub_batch.engine import BatchEngine
    from hub_batch.jobs import Jobs
    from hub_batch.provider import ProfileRegistry
    from test.test_dayan_profile_batch import MetadataRuntime
    from test.test_hub_batch import wait_task

    source = tmp_path / "source.wav"
    sf.write(source, np.zeros((4800, 2)), 48000)
    actual = default_profiles().resolve("dialogue-vocal")
    manifest = replace(actual, implementation={**actual.implementation, "assets": []})
    calls = []

    def run_dag(dag, **kwargs):
        index = len(calls)
        calls.append(index)
        root = Path(kwargs["output_dir"])
        root.mkdir(parents=True)
        outputs = []
        for stem in manifest.outputs:
            path = root / (stem + ".wav")
            subtype = "PCM_16" if index == 1 and failure == "bad_media" else "PCM_24"
            sf.write(path, np.zeros((4800, 2)), 48000, subtype=subtype)
            outputs.append({"stem": stem, "path": str(path)})
        if index == 1:
            if failure == "missing_role":
                outputs.pop()
            elif failure == "duplicate_path":
                outputs[1]["path"] = outputs[0]["path"]
            elif failure == "outside_path":
                outputs[1]["path"] = str(source)
        return SimpleNamespace(records=[SimpleNamespace(to_dict=lambda value=value: value) for value in outputs])

    monkeypatch.setattr("hub_batch.engine.run_dag", run_dag)
    engine = BatchEngine(str(tmp_path))
    jobs = Jobs(MetadataRuntime(engine), tmp_path / "state", tmp_path, tmp_path / "out", ProfileRegistry([manifest]))
    try:
        with TestClient(build_app(jobs)) as client:
            response = client.post("/v1/separations", json={"profile_id": manifest.name,
                "profile_version": manifest.version, "inputs": [str(source), str(source)]})
            assert response.status_code == 202
            result = wait_task(client, response.json()["id"])
            assert result["status"] == "failed"
            assert [item["state"] for item in result["files"]] == ["succeeded", "failed"]
            assert len(result["files"][0]["outputs"]) == 2
            assert result["files"][1]["outputs"] == []
            committed = client.get(f"/v1/batches/{result['id']}/result").json()
            assert committed["files"] == result["files"]
            assert all(Path(item["path"]).exists() for item in committed["files"][0]["outputs"])
    finally:
        engine.close()


def test_combined_runtime_uses_same_profile_discovery_and_validation(tmp_path):
    from fastapi.testclient import TestClient
    from hub_batch.jobs import Jobs
    from hub_runtime.server_app import build_app
    from pymss.server.config import ServerConfig
    from test.test_hub_batch import Runtime
    from hub_batch.contract import discovery

    jobs = Jobs(Runtime(None), tmp_path / "state", tmp_path, tmp_path / "out", default_profiles())
    config = ServerConfig(model_dir=str(tmp_path), webui=False)
    with TestClient(build_app(config, Runtime(None), jobs=jobs, initialize=False)) as client:
        assert client.get("/v1/separations/profiles").json() == discovery(default_profiles())
        payload = {"profile_id": "dialogue-vocal-b", "profile_version": "2026-10-23.v1", "inputs": ["never-read.wav"]}
        assert client.post("/v1/separations", json={**payload, "options": {"batch_size": 2}}).status_code == 400
        assert client.post("/v1/batches", json={**payload, "params": {"batch_size": 2}}).status_code == 400
        assert jobs.store.pending() == 0


def test_profile_rejects_tampered_model_before_loading(tmp_path):
    import hashlib
    from pathlib import Path
    import pytest
    from hub_batch.engine import BatchEngine
    from hub_batch.provider import ProfileManifest

    path = tmp_path / "weights.ckpt"
    path.write_bytes(b"changed")
    dag = Path(__file__).parents[1] / "hub_batch/recipes/duality-two-stems.json"
    manifest = ProfileManifest("test", "1", "duality-two-stems", models=("test",), implementation={
        "dag_sha256": hashlib.sha256(dag.read_bytes()).hexdigest(),
        "assets": [{"kind": "weights", "model": "test", "relpath": "weights.ckpt", "size_bytes": 7, "sha256": hashlib.sha256(b"trusted").hexdigest()}],
    })
    loads = []
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kw: loads.append(kw))
    engine.prepare("tampered")
    try:
        with pytest.raises(ValueError, match="asset digest"):
            engine.run_profile("tampered", manifest, [], str(tmp_path / "out"))
        assert loads == []
    finally:
        engine.close()


def test_registry_publishes_new_complete_version_without_changing_legacy():
    registry = default_profiles()
    expected = {
        "dialogue-vocal": ["vocals", "instrumental"],
        "dialogue-vocal-b": ["vocals", "instrumental"],
        "instrumental-quality": ["vocals", "instrumental"],
        "instrumental-separation": ["instrumental", "vocals"],
        "male-female": ["male", "female"],
        "dereverb": ["dry-vocal", "reverb"],
        "fx": ["dialogue", "music", "fx"],
        "music-stems": ["drums", "bass", "other", "vocals"],
        "mp3-restoration": ["restored", "addition"],
    }
    for name, roles in expected.items():
        manifest = registry.resolve(name, "2026-10-23.v1")
        assert [item["role"] for item in manifest.output_roles] == roles
        assert all(item["required"] for item in manifest.output_roles)
        assert manifest.implementation["dag_sha256"]
        assert all(asset["sha256"] for asset in manifest.implementation["assets"])
        assert manifest.output_media == {"media_type": "audio/wav", "container": "wav", "sample_rate": 48000, "bits_per_sample": 24, "channels": 2}
    assert registry.resolve("dialogue-vocal", "2026-09-20.v1").digest == "0b2aa266b4ede85128921b38fd08b60b3f43de027f74e27e02ed0b80e430201a"
    assert registry.resolve("instrumental-separation", "2026-09-20.v1").outputs == ("Instrumental",)
