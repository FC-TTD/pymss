"""Compatibility API for legacy MSST-WebUI consumers."""
from contextvars import copy_context
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from ttd_model_runtime.engine import progress_scope
from ttd_model_runtime.protocol import NativeCancelled, HubError

from .engine import BatchEngine
from .jobs import Store, TERMINAL


class CompatTasks:
    def __init__(self, runtime, state_dir, input_root, output_root):
        self.runtime = runtime
        self.input_root = Path(input_root).resolve(strict=True)
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self.store = Store(Path(state_dir) / "msst-compat.sqlite3")
        self.guard = threading.RLock()
        self.owner = None
        self.closed = False

    def _validate_root(self, value, root):
        path = Path(value).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("path is outside the configured compatibility root")
        return path

    def submit(self, payload, mode):
        with self.guard:
            if self.closed or self.owner is not None:
                raise RuntimeError("compatibility task is busy")
        raw_inputs = payload.get("input_paths", payload.get("input_path"))
        if raw_inputs is None:
            raise ValueError("input_path or input_paths is required")
        if isinstance(raw_inputs, (str, Path)):
            raw_inputs = [raw_inputs]
        input_path = [self._validate_root(item, self.input_root) for item in raw_inputs]
        params = dict(payload.get("params") or {})
        if params.get("range_start") is not None or params.get("range_end") is not None:
            start = max(1, int(params.get("range_start") or 1)) - 1
            end = int(params.get("range_end") or len(input_path))
            input_path = input_path[start:end]
        if not input_path:
            raise ValueError("no input files matched range")
        output_dir = self._validate_root(payload["output_dir"], self.output_root) if payload.get("output_dir") else self.output_root / uuid4().hex
        output_dir.mkdir(parents=True, exist_ok=True)
        task_id = uuid4().hex
        task = {"id": task_id, "status": "queued", "input_path": str(input_path[0]),
                "input_paths": [str(item) for item in input_path], "output_dir": str(output_dir),
                "model_name": str(payload["model_name"]), "model_type": payload.get("model_type", ""),
                "stems": payload.get("extract_instrumental") or payload.get("stems") or [], "files": [], "progress": 0,
                "processed_files": 0, "total_files": len(input_path), "created_at": time.time()}
        owner = {"id": task_id, "cancel": threading.Event(), "thread": None}
        with self.guard:
            self.owner = owner
        self.store.put(task)
        if mode == "sync":
            self._run(task_id, payload, owner)
            return self.store.get(task_id)
        context = copy_context()
        owner["thread"] = threading.Thread(target=context.run, args=(self._run, task_id, payload, owner), daemon=True)
        owner["thread"].start()
        return self.store.get(task_id)

    def _run(self, task_id, payload, owner=None):
        task = self.store.get(task_id)
        engine = None
        status = "failed"
        error = None
        try:
            with self.runtime.execution():
                engine = self.runtime.get()
                if not isinstance(engine, BatchEngine):
                    raise RuntimeError("compatibility runtime engine is not BatchEngine")
                engine.prepare(task_id)
                if owner is not None and owner["cancel"].is_set():
                    engine.cancel(task_id)
                def progress(done, total, message):
                    self.store.update(task_id, progress=done / max(1, total), processed_files=int(done), total_files=int(total), current=message)
                params = {key: value for key, value in (payload.get("params") or {}).items()
                          if key not in {"range_start", "range_end"}}
                files = engine.run_model(task_id, task["model_name"], task["input_paths"], task["output_dir"],
                                         stems=task["stems"], params=params, progress=progress)
                status = "success" if all(item.get("status") == "success" for item in files) else "failed"
                self.store.update(task_id, files=files, processed_files=len(files), total_files=len(files))
        except NativeCancelled:
            status = "canceled"
        except HubError as exc:
            status = "unknown"
            error = exc.code
        except Exception as exc:
            error = str(exc)
        finally:
            self.store.update(task_id, status=status, error=error)
            with self.guard:
                self.owner = None

    def status(self, task_id): return self.store.get(task_id)

    def result(self, task_id):
        task = self.store.get(task_id)
        files = []
        for item in task.get("files", []):
            files.append({"status": item.get("status", "success"), "output_files": [Path(p).name for p in item.get("output_files", [])]})
        return {"task_id": task_id, "status": task["status"], "files": files}

    def cancel(self, task_id):
        with self.guard:
            owner = self.owner
            if owner is None or owner["id"] != task_id:
                return self.store.update(task_id, status="running", error="task owner unavailable")
            owner["cancel"].set()
            self.store.update(task_id, status="cancelling")
            return self.store.get(task_id)

    def close(self):
        owner = self.owner
        if owner is not None:
            owner["cancel"].set(); owner["thread"].join()
        self.store.close()
