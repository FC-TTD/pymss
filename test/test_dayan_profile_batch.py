"""Real packaged DAG/media contract with CPU inference doubles, not smoke."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from hub_batch.api import build_app
from hub_batch.engine import BatchEngine
from hub_batch.jobs import Jobs
from hub_batch.profiles import default_profiles
from hub_batch.provider import ProfileRegistry
from test.test_hub_batch import Runtime

CASES = [
    ("dialogue-vocal", "melband_roformer_instvox_duality_v2.ckpt", ("Vocals", "Instrumental")),
    ("dialogue-vocal-b", "mel_band_roformer_vocals_becruily.ckpt", ("vocals", "other")),
    ("instrumental-quality", "big_beta5e.ckpt", ("vocals", "other")),
    ("instrumental-separation", "mel_band_roformer_instrumental_becruily.ckpt", ("Instrumental", "Vocals")),
    ("male-female", "bs_roformer_male_female_by_aufr33_sdr_7.2889.ckpt", ("male", "female")),
    ("dereverb", "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt", ("noreverb", "reverb")),
    ("fx", "model_bandit_plus_dnr_sdr_11.47.ckpt", ("Speech", "Music", "Effects")),
    ("music-stems", "HTDemucs4.th", ("drums", "bass", "other", "vocals")),
    ("mp3-restoration", "apollo_model_uni.ckpt", ("restored", "addition")),
]


class MetadataRuntime(Runtime):
    def __init__(self, engine):
        super().__init__(engine)
        self.metadata = []

    @contextmanager
    def execution(self, *, provider_metadata=None):
        self.metadata.append(provider_metadata)
        with super().execution():
            yield


@pytest.mark.parametrize("profile,model,model_stems", CASES)
def test_complete_profile_runs_packaged_dag_and_exports_semantic_wav(tmp_path, profile, model, model_stems):
    calls = []

    class Separator:
        config = SimpleNamespace(audio={"sample_rate": 44100})
        progress_callback = None

        def separate(self, audio, **kwargs):
            # Reverse native ordering to prove role identity comes from stems.
            return {stem: np.ones_like(audio) * (index + 1) / 20
                    for index, stem in reversed(list(enumerate(model_stems)))}

        def close(self):
            pass

    def factory(**kwargs):
        calls.append(kwargs)
        return Separator()

    source = tmp_path / "source.wav"
    sf.write(source, np.zeros((4410, 2), dtype=np.float32), 44100)
    actual = default_profiles().resolve(profile, "2026-10-23.v1")
    # Exercise streamed identity validation using a tiny pinned fixture. The
    # packaged DAG and model/native role declarations are the real manifests.
    asset = tmp_path / "fixture.ckpt"
    asset.write_bytes(b"trusted-fixture")
    manifest = replace(actual, implementation={**actual.implementation, "assets": [{
        "kind": "weights", "model": model, "relpath": asset.name, "size_bytes": asset.stat().st_size,
        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
    }]})
    engine = BatchEngine(str(tmp_path), separator_factory=factory)
    runtime = MetadataRuntime(engine)
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out", ProfileRegistry([manifest]))
    try:
        with TestClient(build_app(jobs)) as client:
            response = client.post("/v1/separations", json={"profile_id": profile,
                "profile_version": manifest.version, "implementation_version": manifest.digest,
                "inputs": [str(source)], "options": {}})
            assert response.status_code == 202, response.json()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = client.get(f"/v1/batches/{response.json()['id']}").json()
                if result["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                    break
                time.sleep(.01)
            assert result["status"] == "succeeded", result
            assert result["implementation_version"] == manifest.digest
            assert result["created_at"] <= result["started_at"] <= result["finished_at"] <= result["updated_at"]
            assert runtime.metadata == [{"implementation": "pymss-profile", "profile_id": profile,
                "profile_version": manifest.version, "implementation_digest": manifest.digest}]
            assert len(calls) == 1
            assert calls[0]["model_name"] == model
            assert calls[0]["download"] is False
            assert not calls[0].get("inference_params")
            outputs = result["files"][0]["outputs"]
            assert [output["stem"] for output in outputs] == list(model_stems)
            assert [output["role"] for output in outputs] == [item["role"] for item in manifest.output_roles]
            assert result["files"][0]["state"] == "succeeded"
            assert client.get(f"/v1/batches/{result['id']}/result").json()["files"] == result["files"]
            for output, stem in zip(outputs, model_stems):
                info = sf.info(output["path"])
                assert (info.samplerate, info.subtype, info.channels) == (48000, "PCM_24", 2)
                assert output["size_bytes"] > 0 and len(output["sha256"]) == 64
                assert output["required"] is True and output["duration_ms"] == 100
                samples, _ = sf.read(output["path"])
                assert np.mean(samples) == pytest.approx((model_stems.index(stem) + 1) / 20, abs=1e-5)
    finally:
        engine.close()


@pytest.mark.parametrize("name", ["dialogue-vocal", "instrumental-separation", "dialogue-and-instrumental", "dialogue-and-instrumental-dedicated"])
def test_legacy_profile_retains_digest_and_adds_explicit_output_identity(tmp_path, name):
    from hub_batch.provider import profile_output_contract
    from test.test_hub_batch import wait_task
    manifest = default_profiles().resolve(name, "2026-09-20.v1")
    before = manifest.digest

    class Separator:
        config = SimpleNamespace(audio={"sample_rate": 44100})
        progress_callback = None

        def separate(self, audio, **kwargs):
            return {"Vocals": audio, "Instrumental": audio * .5}

        def close(self):
            pass

    source = tmp_path / "source.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    jobs = Jobs(Runtime(engine), tmp_path / "state", tmp_path, tmp_path / "out", default_profiles())
    try:
        with TestClient(build_app(jobs)) as client:
            response = client.post("/v1/separations", json={"profile_id": name,
                "profile_version": manifest.version, "inputs": [str(source)]})
            assert response.status_code == 202
            task = wait_task(client, response.json()["id"])
            assert task["status"] == "succeeded", task
            assert task["implementation_version"] == before == default_profiles().resolve(name, manifest.version).digest
            roles, _ = profile_output_contract(manifest)
            outputs = task["files"][0]["outputs"]
            assert [output["role"] for output in outputs] == [item["role"] for item in roles]
            assert all(len(output["sha256"]) == 64 and output["channels"] == 2 for output in outputs)
    finally:
        engine.close()
