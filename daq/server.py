"""FastAPI app: REST for control, a websocket for the live stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import RigConfig
from .hub import Hub
from .sources import list_serial_ports

log = logging.getLogger("daq.server")

WEB_DIR = Path(__file__).parent / "web"
DOWNLOADABLE_SUFFIXES = {".csv", ".json"}


def create_app(config: RigConfig) -> FastAPI:
    hub = Hub(config)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        await hub.start(config.source)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                hub.recorder.stop()
            await hub.stop()

    app = FastAPI(title=config.name, lifespan=lifespan)
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------------------------------------------------------------- pages

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    # ------------------------------------------------------------------ api

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return config.to_json()

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return hub.state()

    @app.get("/api/history")
    async def get_history(window: float | None = None, maxPoints: int = 4000) -> dict[str, Any]:
        return hub.history(window_s=window, max_points=max(100, min(maxPoints, 50000)))

    @app.get("/api/ports")
    async def get_ports() -> dict[str, Any]:
        return {"ports": list_serial_ports()}

    @app.post("/api/source")
    async def set_source(payload: dict = Body(...)) -> dict[str, Any]:
        kind = str(payload.get("kind", "simulator")).lower()
        if kind not in {"simulator", "serial"}:
            raise HTTPException(400, "kind must be 'simulator' or 'serial'")
        port = payload.get("port") or None
        try:
            await hub.start(kind, port=port)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return hub.state()

    @app.post("/api/tare")
    async def set_tare(payload: dict = Body(...)) -> dict[str, Any]:
        channel_id = str(payload.get("channel", ""))
        enabled = bool(payload.get("enabled", True))
        try:
            tare = await hub.set_tare(channel_id, enabled)
        except KeyError as exc:
            raise HTTPException(404, f"no such channel: {channel_id}") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"channel": channel_id, "tare": tare}

    @app.post("/api/recording/start")
    async def start_recording(payload: dict = Body(default={})) -> dict[str, Any]:
        if hub.recorder.active:
            raise HTTPException(409, "a run is already recording")
        try:
            return await hub.start_recording(
                name=str(payload.get("name", "")),
                operator=str(payload.get("operator", "")),
                notes=str(payload.get("notes", "")),
            )
        except OSError as exc:
            raise HTTPException(500, f"could not open run file: {exc}") from exc

    @app.post("/api/recording/stop")
    async def stop_recording() -> JSONResponse:
        run = await hub.stop_recording()
        if run is None:
            raise HTTPException(409, "no run is recording")
        return JSONResponse(run)

    @app.get("/api/runs")
    async def list_runs() -> dict[str, Any]:
        return {"runs": hub.recorder.list_runs(), "directory": str(config.data_dir)}

    @app.get("/api/runs/{filename}")
    async def download_run(filename: str) -> FileResponse:
        # Only ever serve a plain filename from inside the configured data dir.
        safe_name = Path(filename).name
        if Path(safe_name).suffix.lower() not in DOWNLOADABLE_SUFFIXES:
            raise HTTPException(400, "only .csv and .json run files can be downloaded")
        path = (config.data_dir / safe_name).resolve()
        if path.parent != config.data_dir or not path.is_file():
            raise HTTPException(404, "run file not found")
        return FileResponse(path, filename=safe_name, media_type="text/csv")

    # ------------------------------------------------------------ websocket

    @app.websocket("/ws")
    async def stream(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = hub.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "config": config.to_json(),
                    "state": hub.state(),
                    "history": hub.history(window_s=900, max_points=3000),
                }
            )
            while True:
                message = await queue.get()
                await websocket.send_json(message)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except RuntimeError:
            pass  # socket closed mid-send
        finally:
            hub.unsubscribe(queue)

    return app
