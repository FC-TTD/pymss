from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from hub_batch.engine import BatchEngine
from hub_batch.provider import ModelCoordinator
from ttd_model_runtime.protocol import NativeCancelled
import pytest
from contextlib import contextmanager
import threading
import time
import json
from fastapi.testclient import TestClient
from hub_batch.api import build_app
from hub_batch.jobs import Jobs, Store, Busy
from ttd_model_runtime.protocol import AdmissionRejected
from ttd_model_runtime.protocol import HubError


class Runtime:
    def __init__(self, engine):
        self.engine = engine
        self.outcomes = []
        self.active = 0
        self.reject = False

    def accept_background_work(self):
        pass

    def complete_background_work(self):
        pass

    @contextmanager
    def execution(self):
        if self.reject:
            raise AdmissionRejected("resource_busy")
        self.active += 1
        try:
            yield
        except NativeCancelled:
            self.outcomes.append("cancelled")
            raise
        except BaseException:
            self.outcomes.append("failed")
            raise
        else:
            self.outcomes.append("succeeded")
        finally:
            self.active -= 1

    def get(self):
        assert self.active
        return self.engine


def wait_task(client, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = client.get(f"/v1/batches/{task_id}").json()
        if task["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
            return task
        time.sleep(.01)
    raise AssertionError("task did not settle")


class Separator:
    model_type = "mel_band_roformer"

    def __init__(self):
        self.config = SimpleNamespace(audio={"sample_rate": 44100})
        self.progress_callback = None
        self.closed = False

    def separate(self, audio, **kwargs):
        if self.progress_callback:
            self.progress_callback(1, 1, "test chunk")
        return {"Vocals": audio.T, "Instrumental": audio.T * .5}

    def close(self):
        self.closed = True


def test_model_coordinator_allows_same_model_capacity_and_drains_before_switch():
    created = []
    entered = []
    release = threading.Event()

    class Model:
        def __init__(self, name):
            self.name = name
            self.closed = False
        def close(self):
            self.closed = True

    def factory(**kwargs):
        model = Model(kwargs["model_name"])
        created.append(model)
        return model

    coordinator = ModelCoordinator(factory)
    first_ready = threading.Event()
    second_ready = threading.Event()

    def same_model():
        with coordinator.lease(model_name="hot", max_concurrency=2):
            entered.append("same")
            first_ready.set()
            release.wait(3)

    def switch_model():
        first_ready.wait(3)
        with coordinator.lease(model_name="cold", max_concurrency=1):
            entered.append("switch")
            second_ready.set()

    first = threading.Thread(target=same_model)
    second = threading.Thread(target=switch_model)
    first.start()
    assert first_ready.wait(3)
    # A second same-model lease can join before the switch is allowed to drain.
    with coordinator.lease(model_name="hot", max_concurrency=2):
        entered.append("same-2")
    second.start()
    time.sleep(.05)
    assert not second_ready.is_set()
    release.set()
    first.join(3)
    second.join(3)
    assert second_ready.is_set()
    assert [model.name for model in created] == ["hot", "cold"]
    assert created[0].closed
    coordinator.close()
    assert created[1].closed


def test_batch_keeps_one_model_and_distinct_results_for_same_names(tmp_path):
    inputs = []
    for index in range(2):
        path = tmp_path / str(index) / "episode.wav"
        path.parent.mkdir()
        sf.write(path, np.ones((4410, 2), dtype=np.float32) * .1, 44100)
        inputs.append(str(path))
    loaded = []

    def factory(**kwargs):
        loaded.append(Separator())
        return loaded[-1]

    engine = BatchEngine(str(tmp_path), separator_factory=factory)
    engine.prepare("task-one")
    try:
        files = engine.run("task-one", "duality-two-stems", inputs, str(tmp_path / "out"))
        assert len(files) == 2
        assert len(loaded) == 1
        outputs = [o for item in files for o in item["outputs"]]
        assert len({o["path"] for o in outputs}) == 4
        for item in files:
            assert item["status"] == "succeeded"
            assert item["input"] in inputs
        for output in outputs:
            info = sf.info(output["path"])
            assert (info.samplerate, info.subtype, info.channels) == (48000, "PCM_24", 2)
        assert not loaded[0].closed
    finally:
        engine.close()
    assert loaded[0].closed


def test_cancel_is_checked_in_worker_and_old_id_cannot_cancel_next_task(tmp_path):
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    engine.prepare("first")
    assert engine.cancel("first")
    with pytest.raises(NativeCancelled):
        engine.run("first", "duality-two-stems", ["never-read.wav"], str(tmp_path))
    engine.prepare("second")
    assert not engine.cancel("first")
    assert engine.cancel("second")
    with pytest.raises(NativeCancelled):
        engine.run("second", "duality-two-stems", ["never-read.wav"], str(tmp_path))
    engine.close()


def test_batch_records_bad_input_without_losing_later_file(tmp_path):
    good = tmp_path / "good.wav"
    sf.write(good, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    engine.prepare("mixed")
    try:
        files = engine.run("mixed", "duality-two-stems", [str(tmp_path / "bad.wav"), str(good)], str(tmp_path / "out"))
        assert [f["status"] for f in files] == ["failed", "succeeded"]
        assert files[0]["outputs"] == []
        assert len(files[1]["outputs"]) == 2
    finally:
        engine.close()


def test_public_batch_api_persists_results_and_streams_same_terminal(tmp_path):
    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    runtime = Runtime(engine)
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(jobs)) as client:
            response = client.post("/v1/batches", json={"recipe": "duality-two-stems", "input_paths": [str(source)]})
            assert response.status_code == 202
            task = wait_task(client, response.json()["id"])
            assert task["status"] == "succeeded"
            assert task["processed_files"] == 1
            assert len(task["files"][0]["outputs"]) == 2
            events = client.get(f"/v1/batches/{task['id']}/events")
            assert '"status": "succeeded"' in events.text
            assert client.post(f"/v1/batches/{task['id']}/cancel").json()["status"] == "succeeded"
        store = Store(tmp_path / "state/batches.sqlite3")
        try:
            assert store.get(task["id"])["status"] == "succeeded"
        finally:
            store.close()
        assert runtime.outcomes == ["succeeded"]
    finally:
        engine.close()


def test_running_cancel_holds_slot_until_worker_stops(tmp_path):
    entered = threading.Event()

    class BlockingSeparator(Separator):
        def separate(self, audio, **kwargs):
            entered.set()
            while True:
                self.progress_callback(0, 1, "waiting")
                time.sleep(.01)

    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: BlockingSeparator())
    runtime = Runtime(engine)
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(jobs)) as client:
            body = {"recipe": "duality-two-stems", "input_paths": [str(source)]}
            task_id = client.post("/v1/batches", json=body).json()["id"]
            assert entered.wait(3)
            assert runtime.active == 1
            assert client.post("/v1/batches", json=body).status_code == 429
            response = client.post(f"/v1/batches/{task_id}/cancel")
            assert response.status_code == 200
            task = wait_task(client, task_id)
            assert task["status"] == "cancelled"
            assert runtime.active == 0
            assert runtime.outcomes == ["cancelled"]
    finally:
        engine.close()


def test_admission_rejection_never_loads_or_executes_model(tmp_path):
    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    loaded = []
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: loaded.append(1))
    runtime = Runtime(engine)
    runtime.reject = True
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out")
    with TestClient(build_app(jobs)) as client:
        task_id = client.post("/v1/batches", json={"recipe": "duality-two-stems", "input_paths": [str(source)]}).json()["id"]
        task = wait_task(client, task_id)
        assert task["status"] == "failed"
        assert task["error"] == "resource_busy"
        assert loaded == []
        assert runtime.outcomes == []


def test_restart_does_not_replay_unconfirmed_task(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "batches.sqlite3")
    store.put({"id": "interrupted", "status": "running"})
    store.close()
    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    jobs = Jobs(Runtime(None), state, tmp_path, tmp_path / "out")
    try:
        assert jobs.store.get("interrupted")["status"] == "unknown"
        with pytest.raises(Busy):
            jobs.submit("duality-two-stems", [str(source)])
    finally:
        jobs.close()


def test_api_auth_and_input_root_are_checked_before_acceptance(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    outside = tmp_path / "outside.wav"
    sf.write(outside, np.zeros((4410, 2)), 44100)
    runtime = Runtime(None)
    jobs = Jobs(runtime, tmp_path / "state", inputs, tmp_path / "out")
    with TestClient(build_app(jobs, api_key="test-key")) as client:
        body = {"recipe": "duality-two-stems", "input_paths": [str(outside)]}
        assert client.post("/v1/batches", json=body).status_code == 401
        assert client.post("/v1/batches", json=body, headers={"Authorization": "Bearer test-key"}).status_code == 400
        assert client.post("/v1/batches", json={**body, "params": {"batch_size": 20}}, headers={"Authorization": "Bearer test-key"}).status_code == 422
        assert jobs.store.pending() == 0


def test_cancel_keeps_committed_file_without_progress_delivery(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())

    def lost_observer(event):
        if event["kind"] == "file_done":
            engine.cancel(engine.task_id)

    monkeypatch.setattr("hub_batch.engine.engine_progress", lambda: lost_observer)
    runtime = Runtime(engine)
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(jobs)) as client:
            tid = client.post("/v1/batches", json={"recipe": "duality-two-stems", "input_paths": [str(source)] * 2}).json()["id"]
            task = wait_task(client, tid)
            assert task["status"] == "cancelled"
            assert task["processed_files"] == 1
            assert len(task["files"][0]["outputs"]) == 2
    finally:
        engine.close()


def test_state_write_failure_before_prepare_does_not_poison_engine(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    jobs = Jobs(Runtime(engine), tmp_path / "state", tmp_path, tmp_path / "out")
    update = jobs.store.update

    def fail_running(task_id, **values):
        if values.get("status") == "running":
            raise OSError("disk unavailable")
        return update(task_id, **values)

    monkeypatch.setattr(jobs.store, "update", fail_running)
    with TestClient(build_app(jobs)) as client:
        tid = client.post("/v1/batches", json={"recipe": "duality-two-stems", "input_paths": [str(source)]}).json()["id"]
        assert wait_task(client, tid)["status"] == "failed"
    engine.prepare("later")
    engine.cancel("later")
    with pytest.raises(NativeCancelled):
        engine.run("later", "duality-two-stems", [str(source)], str(tmp_path))
    engine.close()


def test_unknown_completion_preserves_files_and_blocks_new_work(tmp_path):
    class UnknownRuntime(Runtime):
        @contextmanager
        def execution(self):
            with super().execution():
                yield
            raise HubError("execution_unknown")

    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((4410, 2)), 44100)
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    jobs = Jobs(UnknownRuntime(engine), tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(jobs)) as client:
            body = {"recipe": "duality-two-stems", "input_paths": [str(source)]}
            tid = client.post("/v1/batches", json=body).json()["id"]
            task = wait_task(client, tid)
            assert task["status"] == "unknown"
            assert len(task["files"]) == 1
            assert jobs.store.pending() == 1
            assert client.post("/v1/batches", json=body).status_code == 429
    finally:
        engine.close()


def test_api_reports_partial_failure_as_failed_not_success(tmp_path):
    good, bad = tmp_path / "good.wav", tmp_path / "broken.wav"
    sf.write(good, np.zeros((4410, 2)), 44100)
    bad.write_text("not an audio file")
    engine = BatchEngine(str(tmp_path), separator_factory=lambda **kwargs: Separator())
    runtime = Runtime(engine)
    jobs = Jobs(runtime, tmp_path / "state", tmp_path, tmp_path / "out")
    try:
        with TestClient(build_app(jobs)) as client:
            tid = client.post("/v1/batches", json={"recipe": "duality-two-stems", "input_paths": [str(bad), str(good)]}).json()["id"]
            task = wait_task(client, tid)
            assert task["status"] == "failed"
            assert [item["status"] for item in task["files"]] == ["failed", "succeeded"]
            assert task["processed_files"] == 2
        assert runtime.outcomes == ["failed"]
    finally:
        engine.close()


def test_restart_recovers_committed_results_but_not_execution(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    files = [{"index": 0, "input": "first.wav", "status": "succeeded", "outputs": []}]
    (output / "files.json").write_text(json.dumps({"task_id": "interrupted", "files": files}))
    path = tmp_path / "tasks.sqlite3"
    store = Store(path)
    store.put({"id": "interrupted", "status": "running", "output_dir": str(output), "files": []})
    store.close()
    recovered = Store(path)
    try:
        task = recovered.get("interrupted")
        assert task["status"] == "unknown"
        assert task["files"] == files
        assert task["processed_files"] == 1
        assert recovered.pending() == 1
    finally:
        recovered.close()


def test_second_owner_cannot_mark_live_task_as_interrupted(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    first = Store(path)
    try:
        first.put({"id": "live", "status": "running"})
        with pytest.raises(RuntimeError, match="already owned"):
            Store(path)
        assert first.get("live")["status"] == "running"
    finally:
        first.close()
