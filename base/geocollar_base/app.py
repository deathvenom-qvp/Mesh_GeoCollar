# SPDX-License-Identifier: GPL-3.0-or-later
"""Web front end and API for managing GeoCollars. Runs in Docker or as a Home Assistant app."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import protocol
from .mesh import MeshLink
from .store import Store

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def load_settings() -> dict:
    """Environment variables, overridden by Home Assistant's /data/options.json when present."""
    settings = {
        "data_dir": os.environ.get("GEOCOLLAR_DATA_DIR", "/data"),
        "mesh_serial": os.environ.get("GEOCOLLAR_MESH_SERIAL", ""),
        "mesh_host": os.environ.get("GEOCOLLAR_MESH_HOST", ""),
    }
    options = Path(settings["data_dir"]) / "options.json"
    if options.exists():
        settings.update({k: v for k, v in json.loads(options.read_text()).items() if v not in (None, "")})
    return settings


class CollarIn(BaseModel):
    node_id: str
    name: str | None = None


class CommandIn(BaseModel):
    action: str  # beep, vibrate, shock, corrections_off, corrections_on, force_lost, clear_lost, request_status
    intensity: int = 0
    duration_ms: int = 1000


def create_app(settings: dict | None = None, link: MeshLink | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings["data_dir"])

    if link is None:
        link = MeshLink(store.record_status, settings.get("mesh_serial", ""), settings.get("mesh_host", ""))
        connect = bool(settings.get("mesh_serial") or settings.get("mesh_host"))
    else:
        connect = False

    @asynccontextmanager
    async def lifespan(_app):
        if connect:
            try:
                link.connect()
            except Exception:  # keep the UI up even if the radio is missing
                log.exception("could not connect to the Meshtastic node")
        yield
        link.close()

    app = FastAPI(title="GeoCollar base", lifespan=lifespan)
    app.state.store = store
    # Leaflet and Leaflet-Geoman are vendored so the page works without a CDN (e.g. on a LAN).
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def require(node_id: str) -> dict:
        c = store.collar(node_id)
        if c is None:
            raise HTTPException(404, "unknown collar")
        return c

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"mesh_connected": link.connected}

    @app.get("/api/collars")
    def list_collars():
        return store.collars()

    @app.post("/api/collars")
    def add_collar(body: CollarIn):
        return store.upsert_collar(body.node_id, body.name)

    @app.delete("/api/collars/{node_id}")
    def remove_collar(node_id: str):
        if not store.remove_collar(node_id):
            raise HTTPException(404, "unknown collar")
        return {"removed": node_id}

    @app.put("/api/collars/{node_id}/fences")
    def set_fences(node_id: str, geojson: dict):
        require(node_id)
        try:
            zones = protocol.zones_from_geojson(geojson)
            protocol.fence_chunks(zones, 1)  # validate size before storing
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"invalid fences: {e}") from e
        version = store.set_fences(node_id, geojson)
        sent = 0
        if link.connected:
            sent = link.send_fences(node_id, zones, version)
        return {"fence_version": version, "chunks_sent": sent}

    @app.post("/api/collars/{node_id}/command")
    def command(node_id: str, body: CommandIn):
        require(node_id)
        try:
            action = protocol.CommandAction[body.action.upper()]
        except KeyError as e:
            raise HTTPException(400, f"unknown action {body.action}") from e
        if not link.connected:
            raise HTTPException(503, "not connected to a Meshtastic node")
        link.send_command(node_id, action, body.intensity, body.duration_ms)
        return {"sent": action.name}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("GEOCOLLAR_PORT", "8099"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)
