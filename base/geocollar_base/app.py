# SPDX-License-Identifier: GPL-3.0-or-later
"""Web front end and API for managing GeoCollars. Runs in Docker or as a Home Assistant app."""

from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import protocol
from .mesh import MeshLink
from .sim import Simulator
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


class SimIn(BaseModel):
    name: str | None = None
    lat: float
    lon: float


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

    sim = Simulator(store.record_status, store.set_sim)
    for node_id, c in store.collars().items():
        if c.get("sim"):
            sim.add(node_id, c["sim"])
            if c.get("fences"):
                try:
                    sim.set_fences(node_id, protocol.zones_from_geojson(c["fences"]), c.get("fence_version", 0))
                except (KeyError, ValueError):
                    log.warning("stored fences for %s are invalid", node_id)

    @asynccontextmanager
    async def lifespan(_app):
        if connect:
            link.start()
        sim.start()
        yield
        sim.close()
        link.close()

    app = FastAPI(title="GeoCollar base", lifespan=lifespan)
    app.state.store = store
    app.state.sim = sim
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
        return {
            "mesh_connected": link.connected,
            "mesh_configured": connect or link.connected,
            "mesh_error": getattr(link, "last_error", ""),
        }

    @app.get("/api/collars")
    def list_collars():
        collars = store.collars()
        for node_id, c in collars.items():
            c["sim"] = sim.public(node_id)
        return collars

    @app.post("/api/collars")
    def add_collar(body: CollarIn):
        return store.upsert_collar(body.node_id, body.name)

    @app.delete("/api/collars/{node_id}")
    def remove_collar(node_id: str):
        sim.remove(node_id)
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
        if node_id in sim:
            sim.set_fences(node_id, zones, version)
            sim.report_now(node_id)
            sent = len(protocol.fence_chunks(zones, version))
        elif link.connected:
            sent = link.send_fences(node_id, zones, version)
        return {"fence_version": version, "chunks_sent": sent}

    @app.post("/api/collars/{node_id}/command")
    def command(node_id: str, body: CommandIn):
        require(node_id)
        try:
            action = protocol.CommandAction[body.action.upper()]
        except KeyError as e:
            raise HTTPException(400, f"unknown action {body.action}") from e
        if node_id in sim:
            sim.command(node_id, action, body.intensity)
            sim.report_now(node_id)
            return {"sent": action.name}
        if not link.connected:
            raise HTTPException(503, "not connected to a Meshtastic node")
        link.send_command(node_id, action, body.intensity, body.duration_ms)
        return {"sent": action.name}

    # -- Simulated collars ---------------------------------------------------------------------
    @app.post("/api/sim")
    def add_sim(body: SimIn):
        if not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180):
            raise HTTPException(400, "position out of range")
        node_id = "!5a" + secrets.token_hex(3)  # looks like a node ID; 5a = "sim"
        n = 1 + sum(1 for c in store.collars().values() if c.get("sim"))
        store.upsert_collar(node_id, body.name or f"Simulated dog {n}")
        settings = sim.add(node_id, {"lat": body.lat, "lon": body.lon}).settings()
        store.set_sim(node_id, settings)
        sim.report_now(node_id)
        return {"node_id": node_id, **settings}

    @app.patch("/api/sim/{node_id}")
    def update_sim(node_id: str, changes: dict):
        if node_id not in sim:
            raise HTTPException(404, "not a simulated collar")
        try:
            settings = sim.update(node_id, changes)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        sim.report_now(node_id)
        return settings

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("GEOCOLLAR_PORT", "8099"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)
