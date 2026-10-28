"""Export options cross discovery, admission, real DAG encoding and artifact validation."""
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from hub_batch.api import build_app
from hub_batch.engine import BatchEngine
from hub_batch.jobs import Jobs
from hub_batch.profiles import default_profiles
from hub_batch.provider import ProfileRegistry
from test.test_dayan_profile_batch import CASES, MetadataRuntime
from test.test_hub_batch import wait_task

VERSION = "2026-10-28.v2"

@pytest.mark.parametrize("name,model,stems", CASES)
@pytest.mark.parametrize("channels,rate,encoding,source_channels", [
    (None, 48000, "PCM_24", 1),
    (1, 16000, "PCM_16", 2),
    (2, 44100, "PCM_32", 1),
    ("source", 48000, "FLOAT", 1),
    ("source", 32000, "PCM_24", 2),
])
@pytest.mark.parametrize("combined", [False, True])
def test_options_reach_real_media(tmp_path, name, model, stems, channels, rate, encoding, source_channels, combined):
    class Separator:
        config = SimpleNamespace(audio={"sample_rate": 44100})
        progress_callback = None
        def separate(self, audio, **kwargs):
            # Upstream returns samples-first and preserves a mono source.
            return {stem: audio.T * (i + 1) / 20 for i, stem in enumerate(stems)}
        def close(self):
            pass
    source = tmp_path / "source.wav"
    samples = np.column_stack([np.full(4411, .2 + i * .1) for i in range(source_channels)])
    sf.write(source, samples, 44100)
    actual = default_profiles().resolve(name, VERSION)
    manifest = replace(actual, implementation={**actual.implementation, "assets": []})
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    backend = engine
    if combined:
        from hub_runtime.native import UnifiedBackend
        backend = UnifiedBackend.__new__(UnifiedBackend)
        backend.batch = engine
    jobs = Jobs(MetadataRuntime(backend), tmp_path / "state", tmp_path, tmp_path / "out", ProfileRegistry([manifest]))
    options = {} if channels is None else {"audio_params": {"channels": channels, "sample_rate": rate, "wav_bit_depth": encoding}}
    try:
        with TestClient(build_app(jobs)) as client:
            response = client.post("/v1/separations", json={"profile_id": name,
                "profile_version": VERSION, "inputs": [str(source)], "options": options})
            assert response.status_code == 202, response.json()
            result = wait_task(client, response.json()["id"])
            assert result["status"] == "succeeded", result
            expected_channels = source_channels if channels == "source" else channels or 2
            assert result["options"] == options
            assert len(result["files"][0]["outputs"]) == len(stems)
            for output in result["files"][0]["outputs"]:
                info = sf.info(output["path"])
                assert (info.channels, info.samplerate, info.subtype) == (expected_channels, rate, encoding)
                assert output["channels"] == info.channels
                assert output["sample_rate"] == info.samplerate
                assert output["encoding"] == info.subtype
                assert output["bits_per_sample"] == {"PCM_16":16,"PCM_24":24,"PCM_32":32,"FLOAT":32}[encoding]
                assert abs(info.duration - 4411 / 44100) < 1 / rate
                assert np.isfinite(sf.read(output["path"])[0]).all()
            assert client.get(f"/v1/batches/{result['id']}/result").json()["files"] == result["files"]
    finally:
        engine.close()

@pytest.mark.parametrize("options", [
    {"channels": 1}, {"audio_params": None}, {"audio_params": {"channels": True}},
    {"audio_params": {"channels": 0}}, {"audio_params": {"channels": 6}},
    {"audio_params": {"channels": "mono"}}, {"audio_params": {"sample_rate": "16000"}},
    {"audio_params": {"sample_rate": True}}, {"audio_params": {"sample_rate": 0}},
    {"audio_params": {"sample_rate": 192001}}, {"audio_params": {"wav_bit_depth": "PCM_8"}},
    {"audio_params": {"bits_per_sample": 16}}, {"audio_params": {"typo": 1}},
])
def test_invalid_options_rejected_before_admission(tmp_path, options):
    jobs = Jobs(MetadataRuntime(None), tmp_path / "state", tmp_path, tmp_path / "out", default_profiles())
    with TestClient(build_app(jobs)) as client:
        for route in ["/v1/separations", "/v1/batches"]:
            result = client.post(route, json={"profile_id": "fx", "profile_version": VERSION,
                "inputs": ["never-read.wav"], "options": options})
            assert result.status_code == 400, result.json()
        assert jobs.store.pending() == 0


def test_versioned_discovery_preserves_published_manifests():
    from hub_batch.contract import discovery
    registry = default_profiles()
    payload = discovery(registry)
    assert payload["contract_version"] == "2026-10-23.v1"
    for name, _, _ in CASES:
        old = registry.resolve(name, "2026-10-23.v1")
        new = registry.resolve(name, VERSION)
        assert old.options == {}
        assert old.digest != new.digest
        assert new.implementation["dag_sha256"] == old.implementation["dag_sha256"]
        assert new.options["audio_params"]["channels"]["default"] == 2
        assert new.options["audio_params"]["channels"]["enum"] == [1, 2, "source"]
        assert new.implementation["native_audio"]["channels"] in (1, 2)


def test_coordinator_failed_unload_blocks_new_and_same_model():
    from hub_batch.provider import ModelCoordinator
    made, closed = [], []
    failing = True
    def factory(model):
        made.append(model)
        return model
    def close(model):
        closed.append(model)
        if failing:
            raise RuntimeError("unload failed")
    coordinator = ModelCoordinator(factory, close=close)
    with coordinator.lease(model="a"):
        pass
    for model in ["b", "b", "a"]:
        with pytest.raises(RuntimeError, match="unload failed"):
            with coordinator.lease(model=model):
                pytest.fail("admitted with unknown residency")
        assert made == ["a"]
    failing = False
    with coordinator.lease(model="b"):
        pass
    assert made == ["a", "b"]
    assert closed == ["a"] * 4
    coordinator.close()
