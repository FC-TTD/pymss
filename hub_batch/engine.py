"""Runs only inside the SDK-owned GPU process."""
from pathlib import Path
import json
import os
import threading
import numpy as np

from pymss.audio_io import load_audio, save_audio

from pymss.graph import SeparatorCache, load_comfy_file, run_dag
from ttd_model_runtime.engine import engine_progress
from ttd_model_runtime.protocol import NativeCancelled

from .provider import CoordinatedSeparator, ModelCoordinator, SharedModel

RECIPES = ("duality-two-stems", "dedicated-me")
BUDGET_BYTES = 8 * 1024 ** 3


class BatchEngine:
    def __init__(self, model_dir, separator_factory=None, *, coordinator=None, profile_concurrency=1):
        self.model_dir = model_dir
        self._owns_coordinator = coordinator is None
        self._profile_concurrency = max(1, int(profile_concurrency))
        factory = separator_factory or SeparatorCache._default_factory
        # Profile DAGs run in one Hub GPU process. With a shared coordinator,
        # cached entries are lazy facades and never own a second separator.
        if coordinator is None:
            self.coordinator = ModelCoordinator(factory=factory)
            self.cache = SeparatorCache(factory=separator_factory, max_entries=1)
        else:
            self.coordinator = coordinator

            def coordinated_factory(**kwargs):
                def load_shared(**load_kwargs):
                    value = factory(**load_kwargs)
                    return value if isinstance(value, SharedModel) else SharedModel(value)

                return CoordinatedSeparator(
                    self.coordinator,
                    load_shared,
                    kwargs,
                    max_concurrency=self._profile_concurrency,
                )

            self.cache = SeparatorCache(factory=coordinated_factory, max_entries=1)
        self.guard = threading.Lock()
        self.execution = threading.Lock()
        self.task_id = None
        self.cancelled = threading.Event()

    def prepare(self, task_id):
        with self.guard:
            if self.task_id is not None:
                raise RuntimeError("batch engine is busy")
            self.task_id = task_id
            self.cancelled.clear()

    def cancel(self, task_id):
        with self.guard:
            if self.task_id != task_id:
                return False
            self.cancelled.set()
            return True

    def run(self, task_id, recipe, inputs, output_dir, *, max_concurrency=None):
        if recipe not in RECIPES:
            raise ValueError("unsupported recipe")
        if not self.execution.acquire(blocking=False):
            raise RuntimeError("batch engine is busy")
        emit = engine_progress()

        def checkpoint():
            if self.cancelled.is_set():
                raise NativeCancelled()

        def notify(event):
            checkpoint()
            if emit:
                emit(event)

        files = []
        previous_concurrency = self._profile_concurrency
        if max_concurrency is not None:
            self._profile_concurrency = max(1, int(max_concurrency))
        try:
            with self.guard:
                if self.task_id != task_id:
                    raise RuntimeError("batch task was not prepared")
            dag = load_comfy_file(Path(__file__).parent / "recipes" / f"{recipe}.json")
            for index, source in enumerate(inputs):
                notify({"kind": "file_start", "index": index, "input": source})

                def progress(done, total, message):
                    notify({"kind": "progress", "index": index, "done": done,
                            "total": total, "message": message})

                item = {"index": index, "input": source, "outputs": []}
                try:
                    validate_duration(source)
                    result = run_dag(
                        dag, input_path=source,
                        output_dir=Path(output_dir) / f"{index:06d}",
                        model_dir=self.model_dir, download=False, device="cuda",
                        separator_cache=self.cache, name_prefix=f"{index:06d}",
                        progress_callback=progress,
                    )
                    item.update(status="succeeded", outputs=[r.to_dict() for r in result.records])
                except NativeCancelled:
                    raise
                except Exception as exc:
                    import torch
                    if isinstance(exc, torch.cuda.OutOfMemoryError):
                        raise
                    item.update(status="failed", error=str(exc))
                files.append(item)
                # A completed file has an authoritative result even if the CPU
                # progress observer disappears or cancellation follows now.
                self._commit_manifest(output_dir, task_id, files)
                if emit:
                    emit({"kind": "file_done", "file": item})
            return files
        finally:
            with self.guard:
                if self.task_id == task_id:
                    self.task_id = None
            self._profile_concurrency = previous_concurrency
            self.execution.release()

    def run_profile(self, task_id, profile, inputs, output_dir):
        """Execute an immutable profile manifest through the DAG runner."""
        return self.run(
            task_id, profile.dag, inputs, output_dir,
            max_concurrency=profile.max_concurrency,
        )

    def close(self):
        self.cache.close()
        if self._owns_coordinator:
            self.coordinator.close()

    @staticmethod
    def _commit_manifest(output_dir, task_id, files):
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "files.json.tmp"
        with temporary.open("w") as handle:
            json.dump({"task_id": task_id, "files": files}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(directory / "files.json")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def run_msst(self, task_id, input_path, output_dir, model_name, stems, params, range_start=None, range_end=None, progress=None):
        """Run the legacy MSST batch shape on a fixed PYMSS model."""
        if not model_name or not stems:
            raise ValueError("model_name and extract_instrumental are required")
        if isinstance(input_path, (list, tuple)):
            paths = [Path(item).resolve(strict=True) for item in input_path]
        else:
            source = Path(input_path).resolve(strict=True)
            paths = [source] if source.is_file() else sorted(
                path for path in source.rglob("*")
                if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
            )
        if range_start is not None or range_end is not None:
            start = max(1, int(range_start or 1)) - 1
            end = int(range_end or len(paths))
            paths = paths[start:end]
        if not paths:
            raise ValueError("no audio files matched input_path/range")
        results = []
        spec = dict(model_name=model_name, model_dir=self.model_dir, device="cuda",
                    output_format="wav", store_dirs={stem: "" for stem in stems},
                    inference_params=dict(params or {}))
        residency_key = ModelCoordinator.key_for({
            "model": model_name, "model_dir": self.model_dir,
            "inference_params": dict(params or {}),
        })
        with self.coordinator.lease(residency_key=residency_key, **spec) as resident:
            separator = getattr(resident, "separator", resident)
            for index, path in enumerate(paths):
                if self.cancelled.is_set():
                    raise NativeCancelled()
                audio, sample_rate = load_audio(path, sr=None, mono=False)
                def callback(done, total, message):
                    if self.cancelled.is_set():
                        raise NativeCancelled()
                    if progress:
                        progress(index + done / max(1, total), len(paths), str(message or ""))
                separator.progress_callback = callback
                separated = separator.separate(np.asarray(audio, dtype=np.float32), pbar=False, stems=stems)
                files = []
                for stem, value in separated.items():
                    name = f"{path.stem}_{stem}.wav"
                    destination = Path(output_dir) / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    save_audio(str(destination), np.asarray(value, dtype=np.float32), sample_rate, "wav", {"wav_bit_depth": "PCM_24"})
                    files.append(str(destination))
                results.append({"input_file": path.name, "output_files": files, "status": "success"})
                if progress:
                    progress(index + 1, len(paths), path.name)
        return results

    def run_model(self, task_id, model_name, inputs, output_dir, *, stems, params=None,
                  max_concurrency=1, progress=None):
        """Run an ordinary catalog/user model through the shared coordinator.

        ``task_id`` is part of the provider execution contract.  Keeping it in
        this lower-level method makes cancellation and completion evidence work
        the same way for profile, legacy, and ordinary model requests.
        """
        if not model_name or not stems:
            raise ValueError("model_name and stems are required")
        if not isinstance(inputs, (list, tuple)):
            inputs = [inputs]
        paths = []
        audio_suffixes = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
        for raw in inputs:
            path = Path(raw).resolve(strict=True)
            if path.is_file():
                paths.append(path)
            elif path.is_dir():
                paths.extend(sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in audio_suffixes))
        if not paths:
            raise ValueError("at least one input is required")
        if not self.execution.acquire(blocking=False):
            raise RuntimeError("batch engine is busy")
        results = []
        try:
            with self.guard:
                if self.task_id != task_id:
                    raise RuntimeError("batch task was not prepared")
            spec = dict(model_name=model_name, model_dir=self.model_dir, device="cuda",
                        output_format="wav", store_dirs={stem: "" for stem in stems},
                        inference_params=dict(params or {}))
            residency_key = ModelCoordinator.key_for({
                "model": model_name, "model_dir": self.model_dir,
                "inference_params": dict(params or {}),
            })
            with self.coordinator.lease(max_concurrency=max_concurrency, residency_key=residency_key, **spec) as resident:
                separator = getattr(resident, "separator", resident)
                for index, path in enumerate(paths):
                    if self.cancelled.is_set():
                        raise NativeCancelled()
                    def callback(done, total, message):
                        if self.cancelled.is_set():
                            raise NativeCancelled()
                        if progress:
                            progress(index + done / max(1, total), len(paths), str(message or ""))

                    try:
                        audio, sample_rate = load_audio(path, sr=None, mono=False)
                        separator.progress_callback = callback
                        separated = separator.separate(np.asarray(audio, dtype=np.float32), pbar=False, stems=stems)
                        files = []
                        for stem, value in separated.items():
                            destination = Path(output_dir) / f"{path.stem}_{stem}.wav"
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            save_audio(str(destination), np.asarray(value, dtype=np.float32), sample_rate, "wav",
                                       {"wav_bit_depth": "PCM_24"})
                            files.append(str(destination))
                        item = {"input_file": path.name, "output_files": files, "status": "success"}
                    except NativeCancelled:
                        raise
                    except Exception as exc:
                        item = {"input_file": path.name, "output_files": [], "status": "failed", "error": str(exc)}
                    results.append(item)
                    self._commit_manifest(output_dir, task_id, results)
                    if progress:
                        progress(index + 1, len(paths), path.name)
            return results
        finally:
            with self.guard:
                if self.task_id == task_id:
                    self.task_id = None
            self.execution.release()


def load_engine():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("Batch inference requires its assigned CUDA GPU")
    return BatchEngine(os.environ["PYMSS_MODEL_DIR"])


def completion(engine):
    import torch
    if torch.cuda.is_initialized():
        torch.cuda.synchronize()


def release(engine):
    engine.close()


def validate_duration(path):
    import av
    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError("input has no audio stream")
        stream = container.streams.audio[0]
        if stream.duration is None or stream.time_base is None:
            raise ValueError("input audio duration is unavailable")
        seconds = float(stream.duration * stream.time_base)
        if not 0 < seconds <= 900:
            raise ValueError("input audio must be between 0 and 900 seconds")
