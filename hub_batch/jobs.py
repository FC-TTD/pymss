"""CPU task ownership. The SDK owns resource admission and actual completion."""
from contextvars import copy_context
from dataclasses import dataclass, field
import fcntl
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from ttd_model_runtime.engine import progress_scope
from ttd_model_runtime.protocol import AdmissionRejected, HubError, NativeCancelled

from .engine import RECIPES
from .provider import ProfileRegistry

TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}


class Busy(RuntimeError):
    pass


class FileFailed(RuntimeError):
    pass


def committed_files(task):
    path = Path(task["output_dir"]) / "files.json"
    if not path.is_file():
        return None
    saved = json.loads(path.read_text())
    if saved["task_id"] != task["id"] or not isinstance(saved["files"], list):
        raise ValueError("batch result identity mismatch")
    return saved["files"]


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.owner_file = open(str(path) + ".lock", "a+")
        try:
            fcntl.flock(self.owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_file.close()
            raise RuntimeError("batch state is already owned by another process") from None
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        for task_id, body in self.db.execute("SELECT id,body FROM batches").fetchall():
            task = json.loads(body)
            if task["status"] not in TERMINAL:
                task.update(status="unknown", error="service restarted before confirmed completion")
                try:
                    files = committed_files(task)
                    if files is not None:
                        task.update(files=files, processed_files=len(files))
                except (OSError, ValueError, KeyError):
                    task["error"] = "service restarted; result manifest could not be confirmed"
                self.put(task)
        self.db.commit()

    def put(self, task):
        with self.lock:
            task["updated_at"] = time.time()
            self.db.execute("INSERT OR REPLACE INTO batches VALUES (?,?)", (task["id"], json.dumps(task)))
            self.db.commit()

    def get(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT body FROM batches WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return json.loads(row[0])

    def update(self, task_id, **values):
        with self.lock:
            task = self.get(task_id)
            task.update(values)
            self.put(task)
            return task

    def pending(self):
        with self.lock:
            return sum(json.loads(row[0])["status"] not in {"succeeded", "failed", "cancelled"}
                       for row in self.db.execute("SELECT body FROM batches"))

    def close(self):
        self.db.close()
        self.owner_file.close()


@dataclass
class Owner:
    task_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    engine: object = None
    thread: object = None


class Jobs:
    def __init__(self, runtime, state_dir, input_root, output_root, profile_registry: ProfileRegistry | None = None):
        self.runtime = runtime
        self.profile_registry = profile_registry
        self.input_root = Path(input_root).resolve(strict=True)
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self.store = Store(Path(state_dir) / "batches.sqlite3")
        self.guard = threading.RLock()
        self.owner = None
        self.closed = False

    def submit(self, recipe=None, paths=None, output_dir=None, *, profile=None, profile_version=None):
        if not paths:
            raise ValueError("input_paths are required")
        manifest = None
        if profile is not None:
            if self.profile_registry is None:
                raise ValueError("profile registry is not configured")
            try:
                manifest = self.profile_registry.resolve(str(profile), profile_version)
            except KeyError as exc:
                raise ValueError(f"profile not found: {exc.args[0]}") from None
            recipe = manifest.dag
        if recipe not in RECIPES:
            raise ValueError("a supported recipe or profile is required")
        inputs = []
        for raw in paths:
            path = Path(raw).resolve(strict=True)
            if not path.is_relative_to(self.input_root) or not path.is_file():
                raise ValueError("input must be a file inside the configured input root")
            inputs.append(str(path))
        with self.guard:
            if self.closed or self.owner is not None or self.store.pending():
                raise Busy("another batch is active or its outcome is unknown")
            task_id = uuid4().hex
            output = Path(output_dir).resolve() if output_dir else self.output_root / task_id
            if not output.is_relative_to(self.output_root):
                raise ValueError("output_dir must be inside the configured output root")
            output.mkdir(parents=True, exist_ok=True)
            task = {"id": task_id, "status": "queued", "recipe": recipe,
                    "profile": manifest.to_dict() if manifest is not None else None,
                    "input_paths": inputs, "output_dir": str(output), "files": [],
                    "processed_files": 0, "total_files": len(inputs), "created_at": time.time()}
            self.store.put(task)
            owner = self.owner = Owner(task_id)
            self.runtime.accept_background_work()
            context = copy_context()
            owner.thread = threading.Thread(target=context.run, args=(self._run, owner), daemon=True)
            try:
                owner.thread.start()
            except BaseException:
                self.store.update(task_id, status="failed", error="could not start batch worker")
                self.owner = None
                self.runtime.complete_background_work()
                raise
            return task

    def _run(self, owner):
        task_id = owner.task_id
        task = self.store.get(task_id)
        last_progress = 0.0

        def event(value):
            nonlocal last_progress
            if value["kind"] == "file_done":
                with self.store.lock:
                    current = self.store.get(task_id)
                    files = current["files"] + [value["file"]]
                    self.store.update(task_id, files=files, processed_files=len(files))
            elif value["kind"] != "progress" or time.monotonic() - last_progress > .1:
                last_progress = time.monotonic()
                self.store.update(task_id, progress=value)

        status, error, files = "failed", None, None
        try:
            if owner.cancel.is_set():
                raise NativeCancelled()
            with self.runtime.execution():
                with self.guard:
                    owner.engine = self.runtime.get()
                    if not owner.cancel.is_set():
                        self.store.update(task_id, status="running")
                    owner.engine.prepare(task_id)
                    if owner.cancel.is_set():
                        owner.engine.cancel(task_id)
                with progress_scope(event):
                    if task.get("profile") is not None:
                        from .provider import ProfileManifest
                        manifest = ProfileManifest.from_dict(task["profile"])
                        files = owner.engine.run_profile(task_id, manifest, task["input_paths"], task["output_dir"])
                    else:
                        files = owner.engine.run(task_id, task["recipe"], task["input_paths"], task["output_dir"])
                if any(item["status"] != "succeeded" for item in files):
                    raise FileFailed("one or more input files failed")
            status = "succeeded"
        except NativeCancelled:
            status = "cancelled"
        except AdmissionRejected as exc:
            error = exc.code
        except HubError as exc:
            status, error = "unknown", exc.code
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                values = {"status": status, "error": error}
                try:
                    saved = committed_files(task)
                    if saved is not None:
                        files = saved
                except (OSError, ValueError, KeyError):
                    status = "unknown"
                    values.update(status=status, error="result manifest could not be confirmed")
                if files is not None:
                    values.update(files=files, processed_files=len(files))
                self.store.update(task_id, **values)
                if status != "unknown":
                    self.runtime.complete_background_work()
            finally:
                with self.guard:
                    self.owner = None

    def cancel(self, task_id):
        with self.guard:
            task = self.store.get(task_id)
            if task["status"] in TERMINAL:
                return task
            owner = self.owner
            if owner is None or owner.task_id != task_id:
                return self.store.update(task_id, status="unknown", error="task owner unavailable")
            owner.cancel.set()
            self.store.update(task_id, status="cancelling")
            if owner.engine is not None:
                owner.engine.cancel(task_id)
            return self.store.get(task_id)

    def close(self):
        with self.guard:
            self.closed = True
            owner = self.owner
            if owner is not None:
                self.cancel(owner.task_id)
        if owner is not None:
            owner.thread.join()
        self.store.close()
