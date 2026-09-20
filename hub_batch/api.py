import asyncio
from contextlib import asynccontextmanager
import json
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from ttd_model_runtime.protocol import HubError

from .jobs import Busy, TERMINAL


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: str
    input_paths: list[str] = Field(min_length=1)


def build_app(jobs, api_key=None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await asyncio.to_thread(jobs.close)

    def authorize(authorization: str | None = Header(default=None)):
        if api_key and not secrets.compare_digest(authorization or "", f"Bearer {api_key}"):
            raise HTTPException(401, "invalid API key")

    app = FastAPI(title="PYMSS batch tasks", lifespan=lifespan)

    def get(task_id):
        try:
            return jobs.store.get(task_id)
        except KeyError:
            raise HTTPException(404, "batch not found") from None

    @app.post("/v1/batches", status_code=202, dependencies=[Depends(authorize)])
    def submit(body: BatchRequest):
        try:
            return jobs.submit(body.recipe, body.input_paths)
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
