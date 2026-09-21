"""CPU contract checks; no weights, network, CUDA context or model download."""
import asyncio
import base64
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
import io
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import soundfile as sf

import numpy as np

from pymss.model_registry import get_model_entry
from pymss.server.config import ServerConfig
from pymss.server.state import LoadedModel
from ttd_model_runtime.runtime import Runtime

from hub_runtime.__main__ import create_app, describe
from hub_runtime.native import NativeBackend, describe_loaded
from hub_batch.provider import ModelCoordinator, SharedModel
from hub_runtime.server_app import _load_or_switch_model, _run_separation
from hub_runtime.state import metadata_from_dict

MODEL = "BS-Roformer-Resurrection.ckpt"
_owned = ContextVar("fake_owned", default=False)


def metadata(name=MODEL):
    entry = replace(get_model_entry(MODEL), name=name)
    return {
        "entry": asdict(entry), "resolved": {"model_type": entry.model_type},
        "requested_model": name, "model_id": name, "sample_rate": 44100,
        "instruments": ["vocals", "other"], "device": "cuda",
        "inference_params": {}, "supported_parameters": {}, "audio_params": {},
    }


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = None

    def load(self, spec, force=False):
        assert _owned.get(), "model load must own an SDK activity"
        self.calls.append(("load", dict(spec), force))
        if spec["model"] == "missing":
            raise KeyError("missing")
        return metadata(spec["model"])

    def separate(self, spec, mix, stems):
        assert _owned.get(), "GPU inference must own an SDK activity"
        self.calls.append(("separate", dict(spec)))
        self.started.set()
        if self.release is not None:
            assert self.release.wait(5), "test did not release fake inference"
        audio = np.asarray(mix, dtype=np.float32)
        return metadata(spec["model"]), {stem: audio for stem in (stems or ["vocals", "other"])}


class FakeRuntime:
    # Exercise the SDK's actual cancellation-settling task decorator.
    task = Runtime.task

    def __init__(self):
        self.engine = FakeEngine()
        self.active = 0
        self.finished = 0
        self.residency = "unloaded"
        self.engine_id = "engine-1"

    def _nested(self):
        return False

    def pending_work(self):
        return 0

    def get(self):
        assert _owned.get()
        return self.engine

    @asynccontextmanager
    async def aexecution(self):
        self.active += 1
        self.residency = "ready"
        token = _owned.set(True)
        try:
            yield self
        finally:
            _owned.reset(token)
            self.active -= 1
            self.finished += 1

    def status(self):
        return {"accepting": True, "healthy": True, "residency": self.residency,
                "engine": {"id": self.engine_id}}


async def request(app, method, path, *, payload=None, body=b"", headers=None):
    headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["content-type"] = "application/json"
    parsed = urlsplit(path)
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": parsed.path, "raw_path": parsed.path.encode(),
        "query_string": parsed.query.encode(),
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 1), "server": ("pymss-studio", 80),
    }
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(msg for msg in messages if msg["type"] == "http.response.start")
    content = b"".join(msg.get("body", b"") for msg in messages if msg["type"] == "http.response.body")
    return start["status"], content


class ProcessBackend:
    """Plain CPU fixture verifies the actual SDK transport, not CUDA inference."""
    def separate(self, spec, mix, stems):
        return metadata(spec["model"]), {stem: mix for stem in stems}

    def process_id(self):
        return os.getpid()

    def close(self):
        pass


def process_backend():
    return ProcessBackend()


def process_release(backend):
    backend.close()


def process_cleanup():
    pass


class ManagedHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.config = ServerConfig(model=MODEL, model_dir="/nonexistent-model-cache",
                                   device="cuda", max_queue_size=1, webui=False)
        self.app = create_app(runtime=self.runtime, config=self.config)
        self.state = self.app.state.pymss_state
        self.state.loaded = metadata_from_dict(metadata())

    async def separate(self, *, response_format="json", output="pcm_f32le"):
        raw = np.zeros((4410, 2), dtype="<f4").tobytes()
        return await request(self.app, "POST", "/v1/audio/separations", payload={
            "model": self.state.loaded.model_id,
            "input": {"format": "pcm_f32le", "sample_rate": 44100, "channels": 2,
                      "data": base64.b64encode(raw).decode()},
            "stems": ["vocals"], "response_format": response_format,
            "output_audio_format": output,
        })

    async def test_cpu_probes_and_cold_inference_keep_native_response(self):
        status, body = await request(self.app, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["model_loaded"])
        self.assertTrue(json.loads(body)["model_selected"])
        await request(self.app, "GET", "/v1/models")
        await request(self.app, "GET", "/v1/server/info")
        self.assertEqual(self.runtime.engine.calls, [])
        status, body = await self.separate()
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["object"], "audio.separation")
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["usage"], {"type": "duration", "seconds": 0.1})
        self.assertEqual([o["stem"] for o in result["outputs"]], ["vocals"])
        self.assertEqual(self.runtime.finished, 1)
        _, health = await request(self.app, "GET", "/health")
        self.assertTrue(json.loads(health)["model_loaded"])

    async def test_original_wav_encoding_is_48khz_pcm24(self):
        status, body = await self.separate(output="wav")
        self.assertEqual(status, 200)
        encoded = json.loads(body)["outputs"][0]["audio"]
        audio = sf.info(io.BytesIO(base64.b64decode(encoded["data"])))
        self.assertEqual((audio.samplerate, audio.subtype, audio.channels),
                         (48000, "PCM_24", 2))

    async def test_missing_config_requests_explicit_load_without_gpu_work(self):
        self.state.loaded.instruments = ()
        status, body = await self.separate()
        self.assertEqual(status, 503)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "model_not_loaded")
        self.assertIn("/v1/models/load", error["message"])
        self.assertEqual(self.runtime.engine.calls, [])

    async def test_native_limit_rejects_second_request_without_new_activity(self):
        self.runtime.engine.release = threading.Event()
        first = asyncio.create_task(self.separate())
        await asyncio.wait_for(asyncio.to_thread(self.runtime.engine.started.wait), 2)
        try:
            status, body = await self.separate()
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)["error"]["code"], "server_overloaded")
            self.assertEqual(self.runtime.active, 1)
            self.assertEqual(len(self.runtime.engine.calls), 1)
        finally:
            self.runtime.engine.release.set()
            await first
        self.assertEqual(self.state.limiter.active, 0)

    async def test_cancel_waits_for_worker_before_unlock_and_terminal(self):
        self.runtime.engine.release = threading.Event()
        loaded = self.state.loaded
        work = asyncio.create_task(_run_separation(self.state, loaded, MODEL, np.zeros(32), None))
        await asyncio.wait_for(asyncio.to_thread(self.runtime.engine.started.wait), 2)
        work.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(work.done())
        self.assertTrue(self.state.inference_lock.locked())
        self.assertEqual(self.state.limiter.active, 1)
        self.assertEqual(self.runtime.active, 1)
        self.runtime.engine.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertFalse(self.state.inference_lock.locked())
        self.assertEqual(self.runtime.active, 0)
        self.assertEqual(self.state.limiter.active, 0)

    async def test_selected_model_survives_engine_eviction(self):
        target = "another-model.ckpt"
        status, _ = await request(self.app, "POST", "/v1/models/load", payload={
            "model": target, "inference_params": {"batch_size": 2},
            "source": "huggingface", "endpoint": "https://example.invalid",
        })
        self.assertEqual(status, 200)
        self.runtime.residency = "unloaded"
        self.runtime.engine_id = "engine-2"
        self.runtime.engine = FakeEngine()
        _, body = await request(self.app, "GET", "/health")
        self.assertFalse(json.loads(body)["model_loaded"])
        self.assertEqual(json.loads(body)["model"], target)
        status, _ = await self.separate()
        self.assertEqual(status, 200)
        call = self.runtime.engine.calls[0]
        self.assertEqual(call[1]["model"], target)
        self.assertEqual(call[1]["inference_params"], {"batch_size": 2})
        self.assertEqual(call[1]["endpoint"], "https://example.invalid")

    async def test_failed_switch_leaves_unselected_as_native(self):
        status, body = await request(self.app, "POST", "/v1/models/load", payload={"model": "missing"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_found")
        self.assertIsNone(self.state.loaded)
        self.assertIsNone(self.state.selected_spec)
        self.assertFalse(self.state.model_loading)
        self.assertFalse(self.state.model_lock.locked())

    async def test_auth_and_parse_errors_do_not_acquire_gpu(self):
        self.config.api_key = "test-key"
        status, _ = await self.separate()
        self.assertEqual(status, 401)
        status, _ = await request(self.app, "POST", "/v1/audio/separations",
                                  body=b"invalid", headers={"authorization": "Bearer test-key",
                                                          "content-type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(self.runtime.finished, 0)
        self.assertEqual(self.runtime.engine.calls, [])


class NativeBoundaryTests(unittest.TestCase):
    def test_backend_reuses_model_and_serializes_no_gpu_object(self):
        class Separator:
            model_type = "bs_roformer"
            def __init__(self): self.closed = False
            def separate(self, mix, pbar=False, stems=None):
                return {stem: mix for stem in (stems or ["vocals"])}
            def close(self): self.closed = True

        built = []
        def loader(config, model, inference_params=None):
            loaded = metadata_from_dict(metadata(model))
            loaded.separator = Separator()
            built.append(loaded)
            self.assertEqual(config.device, "cuda")
            self.assertEqual(config.device_ids, [0])
            return loaded

        backend = NativeBackend()
        spec = {"model": MODEL, "model_dir": "/models", "source": "hf-mirror",
                "endpoint": None, "inference_params": {}}
        with patch("pymss.server.state.load_model", side_effect=loader):
            desc, results = backend.separate(spec, np.zeros(16), ["vocals"])
            backend.separate(spec, np.zeros(16), ["vocals"])
            self.assertEqual(len(built), 1)
            self.assertNotIn("separator", desc)
            json.dumps(desc)
            self.assertIsInstance(results["vocals"], np.ndarray)
            backend.load(dict(spec, model="other.ckpt"), force=True)
            self.assertTrue(built[0].separator.closed)
            self.assertEqual(len(built), 2)
            backend.close()
            self.assertTrue(built[1].separator.closed)

    def test_coordinated_native_backend_removes_runtime_only_concurrency_field(self):
        class Separator:
            model_type = "bs_roformer"

            def separate(self, mix, pbar=False, stems=None):
                return {stem: mix for stem in (stems or ["vocals"])}

        seen = []

        def factory(**kwargs):
            seen.append(dict(kwargs))
            return SharedModel(Separator(), metadata(kwargs["model"]))

        coordinator = ModelCoordinator(factory)
        backend = NativeBackend(coordinator=coordinator)
        spec = {
            "model": MODEL, "model_dir": "/models", "source": "hf-mirror",
            "endpoint": None, "inference_params": {}, "max_concurrency": 3,
        }
        try:
            desc, results = backend.separate(spec, np.zeros(16), ["vocals"])
            self.assertEqual(desc["model_id"], MODEL)
            self.assertIn("vocals", results)
            self.assertEqual(seen, [{
                "model": MODEL, "model_dir": "/models", "source": "hf-mirror",
                "endpoint": None, "inference_params": {},
            }])
        finally:
            coordinator.close()

    def test_actual_sdk_child_roundtrip_and_acknowledged_release(self):
        from ttd_model_runtime.engine import ProcessModel
        engine = ProcessModel(process_backend, None, process_cleanup, process_release,
                              {"gpu": "GPU-00000000-0000-0000-0000-000000000000", "generation": 1},
                              timeout=30)
        try:
            proxy = engine.start()
            self.assertNotEqual(proxy.process_id(), os.getpid())
            samples = np.arange(64, dtype=np.float32)
            info, result = proxy.separate({"model": MODEL}, samples, ["vocals"])
            self.assertEqual(info["model_id"], MODEL)
            np.testing.assert_array_equal(result["vocals"], samples)
        finally:
            engine.close()
        self.assertTrue(engine.engine_status()["group_empty"])
        self.assertFalse(engine.engine_status()["alive"])

    def test_describe_has_native_routes_without_loading_or_downloading(self):
        with patch("pymss.server.state.load_model", side_effect=AssertionError("GPU load")), \
             patch("pymss.server.state.download_model", side_effect=AssertionError("download")):
            schema = describe()
        self.assertIn("/v1/audio/separations", schema["paths"])
        self.assertIn("/v1/models/load", schema["paths"])
        self.assertIn("/v1/models/download", schema["paths"])
        self.assertIn("/v1/server/info", schema["paths"])


if __name__ == "__main__":
    unittest.main()
