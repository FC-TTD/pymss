"""Retirement must reject before filesystem, runtime and durable acceptance."""
import pytest
from fastapi.testclient import TestClient
from hub_batch.api import build_app
from hub_batch.jobs import Jobs
from hub_batch.profiles import default_profiles
from hub_batch.retirement import ProfileRetired
from test.test_hub_batch import Runtime


@pytest.mark.parametrize("version", ["2026-09-20.v1", "2026-10-23.v1"])
@pytest.mark.parametrize("combined", [False, True])
def test_retired_create_has_no_side_effect_and_preserves_history(tmp_path, version, combined):
    runtime = Runtime(None)
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out", default_profiles())
    if combined:
        from hub_runtime.server_app import build_app as native_app
        from pymss.server.config import ServerConfig
        app = native_app(ServerConfig(model_dir=str(tmp_path), webui=False), runtime, jobs=jobs, initialize=False)
    else:
        app = build_app(jobs)
    manifest = default_profiles().resolve("dialogue-vocal", version)
    old = {"id":"old", "status":"succeeded", "profile":manifest.to_dict(), "output_dir":str(tmp_path / "old"), "files":[]}
    jobs.store.put(old)
    with TestClient(app) as client:
        for route in ("/v1/separations", "/v1/batches"):
            response = client.post(route,json={"profile_id":"dialogue-vocal","profile_version":version,"inputs":["/never/read/source.wav"],"output_dir":"/never/create"})
            assert response.status_code == 410, response.text
            assert "profile_retired" in response.text
        assert jobs.store.pending() == 0
        assert client.get("/v1/batches/old").json()["profile_version"] == version
        assert client.get("/v1/batches/old/result").json()["implementation_version"] == manifest.digest
        active = client.get("/v1/separations/profiles").json()
        assert active["contract_version"] == "2026-10-23.v1"
        assert {p["profile_version"] for p in active["profiles"]} == {"2026-10-28.v2"}
        assert len(active["profiles"]) == 9


@pytest.mark.parametrize("version", ["2026-09-20.v1", "2026-10-23.v1"])
def test_direct_and_compat_ignore_no_version_identity(tmp_path, version):
    from hub_batch.msst_compat import CompatTasks
    jobs = Jobs(Runtime(None), tmp_path / "state", tmp_path, tmp_path / "out", default_profiles())
    compat = CompatTasks(Runtime(None), tmp_path / "compat", tmp_path, tmp_path / "out")
    try:
        with pytest.raises(ProfileRetired):
            jobs.submit(profile="dialogue-vocal", profile_version=version, paths=["unread"])
        with pytest.raises(ProfileRetired):
            compat.submit({"model_name":"expert", "profile_version":version},"async")
        with TestClient(build_app(jobs,compat=compat,profile_registry=default_profiles())) as client:
            for identity in ({"profile":"dialogue-vocal"}, {"profile_id":"dialogue-vocal"}, {}):
                response=client.post("/api/v1/tasks/msst-batch",json={**identity,"profile_version":version,"model_name":"expert"})
                assert response.status_code == 410
            response=client.post("/api/v1/tasks/msst-batch",json={"profile":"dialogue-vocal", "profile_version":"2026-10-28.v2"})
            assert response.status_code == 400
            assert "require /v1/separations" in response.text
    finally:
        jobs.close(); compat.close()
