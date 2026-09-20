import asyncio
from contextlib import asynccontextmanager
import json
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from ttd_model_runtime.protocol import HubError

from .jobs import Busy, TERMINAL
from .provider import ProfileRegistry


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: str
    input_paths: list[str] = Field(min_length=1)
    output_dir: str | None = None


def build_app(jobs, api_key=None, compat=None, profile_registry: ProfileRegistry | None = None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if jobs is not None:
                await asyncio.to_thread(jobs.close)
            if compat is not None:
                await asyncio.to_thread(compat.close)

    def authorize(authorization: str | None = Header(default=None)):
        if api_key and not secrets.compare_digest(authorization or "", f"Bearer {api_key}"):
            raise HTTPException(401, "invalid API key")

    app = FastAPI(title="PYMSS batch tasks", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "pymss-provider"}

    if compat is not None:
        def compat_payload(task):
            return {"task_id": task["id"], "status": task["status"], "progress": task.get("progress", 0),
                    "processed_files": task.get("processed_files", 0), "total_files": task.get("total_files", 0),
                    "error": task.get("error")}

        def resolve_provider_payload(payload):
            """Normalize the stable separation shape to the provider task shape."""
            if payload.get("profile"):
                if profile_registry is None:
                    raise ValueError("profile registry is not configured")
                profile = profile_registry.resolve(str(payload["profile"]), payload.get("profile_version"))
                if not profile.models:
                    raise ValueError("profile has no model implementation")
                payload = dict(payload)
                payload.setdefault("model_name", profile.models[0])
                payload.setdefault("stems", list(profile.outputs))
                payload.setdefault("params", dict(profile.options))
            if "inputs" in payload and "input_paths" not in payload:
                payload = dict(payload)
                payload["input_paths"] = payload.pop("inputs")
            return payload

        async def submit_compat(request: Request):
            payload = await request.json()
            payload = resolve_provider_payload(payload)
            mode = request.query_params.get("mode", "async")
            try:
                result = await asyncio.to_thread(compat.submit, payload, mode)
                result = compat_payload(result)
                if mode == "sse":
                    async def one_event():
                        task_id = result["task_id"]
                        yield "event: start\ndata: " + json.dumps(result) + "\n\n"
                        previous = None
                        while True:
                            current = await asyncio.to_thread(compat.status, task_id)
                            payload_text = json.dumps(current)
                            if payload_text != previous:
                                event = "completed" if current["status"] == "success" else "error" if current["status"] in {"failed", "canceled"} else "progress"
                                yield f"event: {event}\ndata: {json.dumps(compat_payload(current))}\n\n"
                                previous = payload_text
                            if current["status"] in {"success", "failed", "canceled", "unknown"}:
                                return
                            await asyncio.sleep(.25)
                    return StreamingResponse(one_event(), media_type="text/event-stream")
                return result
            except (ValueError, KeyError) as exc:
                raise HTTPException(400, str(exc)) from None
            except RuntimeError as exc:
                raise HTTPException(429, str(exc)) from None

        @app.post("/api/v1/tasks/msst-batch", dependencies=[Depends(authorize)])
        async def compat_submit(request: Request):
            return await submit_compat(request)

        @app.post("/v1/separations", dependencies=[Depends(authorize)])
        async def separation_submit(request: Request):
            """Stable Profile + Batch provider smoke endpoint.

            Gateway owns the public business task; this endpoint only exposes
            provider execution and accepts one or many input paths.
            """
            return await submit_compat(request)

        @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(authorize)])
        async def compat_status(task_id: str):
            try:
                return compat_payload(await asyncio.to_thread(compat.status, task_id))
            except KeyError:
                raise HTTPException(404, "task not found") from None

        @app.get("/api/v1/tasks/{task_id}/result", dependencies=[Depends(authorize)])
        async def compat_result(task_id: str):
            try:
                return await asyncio.to_thread(compat.result, task_id)
            except KeyError:
                raise HTTPException(404, "task not found") from None

        @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(authorize)])
        async def compat_cancel(task_id: str):
            try:
                return await asyncio.to_thread(compat.cancel, task_id)
            except KeyError:
                raise HTTPException(404, "task not found") from None

    def get(task_id):
        try:
            return jobs.store.get(task_id)
        except KeyError:
            raise HTTPException(404, "batch not found") from None

    @app.post("/v1/batches", status_code=202, dependencies=[Depends(authorize)])
    def submit(body: BatchRequest):
        try:
            return jobs.submit(body.recipe, body.input_paths, body.output_dir)
        except Busy as exc:
            raise HTTPException(429, str(exc)) from None
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/v1/batches/{task_id}", dependencies=[Depends(authorize)])
    def status(task_id: str):
        return get(task_id)

    @app.post("/v1/batches/{task_id}/cancel", dependencies=[Depends(authorize)])
    def cancel(task_id: str):
        get(task_id)
        try:
            return jobs.cancel(task_id)
        except HubError as exc:
            raise HTTPException(503, exc.code) from None

    @app.get("/v1/batches/{task_id}/events", dependencies=[Depends(authorize)])
    async def events(task_id: str):
        get(task_id)

        async def stream():
            previous = None
            while True:
                task = get(task_id)
                payload = json.dumps(task)
                if payload != previous:
                    yield f"event: task\ndata: {payload}\n\n"
                    previous = payload
                if task["status"] in TERMINAL:
                    return
                await asyncio.sleep(.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
