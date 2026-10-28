"""Runs only inside the SDK-owned GPU process."""
from pathlib import Path
from copy import deepcopy
import json
import hashlib
import os
import re
import threading
import numpy as np

from pymss.audio_io import load_audio, save_audio

from pymss.graph import SeparatorCache, load_comfy_file, run_dag
from ttd_model_runtime.engine import engine_progress
from ttd_model_runtime.protocol import NativeCancelled

from .provider import CoordinatedSeparator, ModelCoordinator, SharedModel, PCM_BITS, profile_audio_params, profile_output_contract

RECIPES = ("duality-two-stems", "instrumental-only", "dedicated-me")
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

    def run(self, task_id, recipe, inputs, output_dir, *, max_concurrency=None, profile=None, options=None):
        if recipe not in RECIPES and not (profile and profile.implementation):
            raise ValueError("unsupported recipe")
        audio_params = profile_audio_params(profile, options) if profile else None
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
            if not re.fullmatch(r"[A-Za-z0-9._-]+", recipe):
                raise ValueError("invalid packaged recipe reference")
            recipe_path = Path(__file__).parent / "recipes" / f"{recipe}.json"
            if profile and profile.implementation:
                notify({"kind": "stage", "stage": "verifying_implementation"})
                self._verify_implementation(profile, recipe_path)
            dag = load_comfy_file(recipe_path)
            for index, source in enumerate(inputs):
                notify({"kind": "file_start", "index": index, "input": source})

                def progress(done, total, message):
                    notify({"kind": "progress", "index": index, "done": done,
                            "total": total, "message": message})

                item = {"index": index, "input": source, "outputs": []}
                try:
                    source_channels = validate_duration(source)
                    effective = dict(audio_params) if audio_params else None
                    if effective and effective["channels"] == "source":
                        if source_channels not in (1, 2):
                            raise ValueError("source channel policy supports mono or stereo input; select channels 1 or 2 to downmix")
                        effective["channels"] = source_channels
                    export_dag = self._configure_export(dag, effective) if effective else dag
                    result = run_dag(
                        export_dag, input_path=source,
                        output_dir=Path(output_dir) / f"{index:06d}",
                        model_dir=self.model_dir, download=False, device="cuda",
                        separator_cache=self.cache, name_prefix=f"{index:06d}",
                        progress_callback=progress,
                        audio_params=effective,
                    )
                    outputs = [r.to_dict() for r in result.records]
                    if profile:
                        outputs = self._profile_outputs(profile, outputs, Path(output_dir) / f"{index:06d}", effective)
                    item.update(status="succeeded", outputs=outputs)
                    if effective:
                        item["audio_params"] = effective
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

    def run_profile(self, task_id, profile, inputs, output_dir, *, options=None):
        """Execute an immutable profile manifest through the DAG runner."""
        return self.run(
            task_id, profile.dag, inputs, output_dir,
            max_concurrency=profile.max_concurrency, profile=profile, options=options,
        )

    @staticmethod
    def _configure_export(dag, audio_params):
        """Apply resolved options to a run-local copy of the pinned DAG.

        Model nodes and intermediate audio stay untouched. Save widgets carry
        rate/encoding in the existing graph format; audio_params carries the
        channel conversion to the saver. The verified on-disk DAG is immutable.
        """
        configured = deepcopy(dag)
        for node in configured.nodes:
            if node.type != "pymss_save_audio":
                continue
            widgets = node.data["widgets_values"]
            rate_index = 1 if str(widgets[1]).isdigit() else 2
            widgets[rate_index] = str(audio_params["sample_rate"])
            widgets[rate_index + 1] = audio_params["wav_bit_depth"]
        return configured

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _verify_implementation(self, profile, recipe_path):
        if self._sha256(recipe_path) != profile.implementation.get("dag_sha256"):
            raise ValueError("profile DAG digest mismatch")
        root = Path(self.model_dir).resolve(strict=True)
        for asset in profile.implementation.get("assets", ()):
            path = (root / asset["relpath"]).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("profile asset must be a file inside the model directory")
            if path.stat().st_size != asset["size_bytes"] or self._sha256(path) != asset["sha256"]:
                raise ValueError("profile asset digest mismatch")

    @classmethod
    def _profile_outputs(cls, profile, outputs, output_dir, audio_params=None):
        import soundfile as sf

        roles, media = profile_output_contract(profile)
        params = audio_params or profile_audio_params(profile)
        encoding = params["wav_bit_depth"]
        expected = {item["native_stem"]: item for item in roles}
        stems = [item["stem"] for item in outputs]
        if len(stems) != len(expected) or set(stems) != set(expected):
            raise ValueError("profile output stems do not match the complete declared roles")
        root = Path(output_dir).resolve(strict=True)
        seen = set()
        validated = {}
        for item in outputs:
            path = Path(item["path"]).resolve(strict=True)
            if not path.is_relative_to(root) or path in seen or not path.is_file():
                raise ValueError("profile output must be a distinct file inside its input output directory")
            seen.add(path)
            info = sf.info(path)
            if (info.format != "WAV" or info.subtype != encoding or info.frames <= 0
                    or info.samplerate != params["sample_rate"] or info.channels != params["channels"]):
                raise ValueError(f"profile output media does not match resolved audio_params: {params}")
            with sf.SoundFile(path) as handle:
                for block in handle.blocks(blocksize=65536):
                    if not np.isfinite(block).all():
                        raise ValueError("profile output contains non-finite audio")
            declaration = expected[item["stem"]]
            validated[item["stem"]] = {
                **item, "role": declaration["role"], "required": declaration["required"],
                "media_type": media["media_type"], "format": "wav", "encoding": encoding,
                "bits_per_sample": PCM_BITS[encoding],
                "sample_rate": info.samplerate, "channels": info.channels,
                "duration_ms": round(info.frames / info.samplerate * 1000),
                "size_bytes": path.stat().st_size, "sha256": cls._sha256(path),
            }
        return [validated[item["native_stem"]] for item in roles]

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
        return stream.codec_context.channels
