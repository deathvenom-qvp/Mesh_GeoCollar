# SPDX-License-Identifier: GPL-3.0-or-later
"""Web front end and API for managing GeoCollars. Runs in Docker or as a Home Assistant app."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import firmware as fw
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
        "firmware_repo": os.environ.get("GEOCOLLAR_FIRMWARE_REPO", fw.DEFAULT_REPO),
        "ota_subnet": os.environ.get("GEOCOLLAR_OTA_SUBNET", ""),
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


class WifiIn(BaseModel):
    ssid: str
    psk: str | None = None  # None keeps the saved password


class BuildIn(BaseModel):
    ref: str = "main"


class UsbFlashIn(BaseModel):
    build: str
    port: str
    mode: str = "update"
    node_id: str | None = None  # which collar is plugged in, to record what it now runs


class OtaIn(BaseModel):
    build: str
    ip: str | None = None  # the collar's address in update mode, if known
    send_wifi: bool = True
    resume: bool = False


class FlashedIn(BaseModel):
    build: str
    via: str = "browser"


class CommandIn(BaseModel):
    action: str  # beep, vibrate, shock, corrections_off, corrections_on, force_lost, clear_lost, request_status
    intensity: int = 0
    duration_ms: int = 1000


MAX_PARCELS = 500
MAX_PARCEL_POINTS = 100_000


def clean_parcels(fc: dict) -> dict:
    """Keep only (Multi)Polygons in longitude/latitude, with a few short text properties, so a
    county export can't fill the data file. The page parses KML and shapefiles itself."""
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        raise ValueError("expected a GeoJSON FeatureCollection")
    if len(fc["features"]) > MAX_PARCELS:
        raise ValueError(f"at most {MAX_PARCELS} parcels")
    points = 0
    out = []
    for f in fc["features"]:
        g = f.get("geometry") or {}
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        clean_polys = []
        for poly in polys:
            rings = []
            for ring in poly:
                pts = [[round(float(x), 7), round(float(y), 7)] for x, y, *_ in ring]
                if len(pts) < 4:
                    continue
                if not all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in pts):
                    raise ValueError("coordinates are not longitude/latitude")
                points += len(pts)
                rings.append(pts)
            if rings:
                clean_polys.append(rings)
        if not clean_polys:
            continue
        props = {}
        for k, v in list((f.get("properties") or {}).items())[:30]:
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and len(str(k)) <= 40:
                props[str(k)] = v if not isinstance(v, str) else v[:120]
        geometry = {"type": "Polygon", "coordinates": clean_polys[0]} if len(clean_polys) == 1 \
            else {"type": "MultiPolygon", "coordinates": clean_polys}
        out.append({"type": "Feature", "properties": props, "geometry": geometry})
    if points > MAX_PARCEL_POINTS:
        raise ValueError("too many points; export just your own parcel and its neighbours")
    return {"type": "FeatureCollection", "features": out}


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
                    sim.set_fences(node_id, protocol.zones_from_geojson(c["fences"]), c.get("fence_version", 0),
                                   protocol.pulses_from_geojson(c["fences"]))
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
            pulses = protocol.pulses_from_geojson(geojson)
            protocol.fence_chunks(zones, 1, pulses)  # validate size before storing
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"invalid fences: {e}") from e
        version = store.set_fences(node_id, geojson)
        sent = 0
        if node_id in sim:
            sim.set_fences(node_id, zones, version, pulses)
            sim.report_now(node_id)
            sent = len(protocol.fence_chunks(zones, version, pulses))
        elif link.connected:
            sent = link.send_fences(node_id, zones, version, pulses)
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

    # -- Property lines (parcels imported from county GIS files) --------------------------------
    @app.get("/api/parcels")
    def get_parcels():
        return store.setting("parcels") or {"type": "FeatureCollection", "features": []}

    @app.put("/api/parcels")
    def put_parcels(fc: dict):
        try:
            clean = clean_parcels(fc)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"invalid parcels: {e}") from e
        store.set_setting("parcels", clean if clean["features"] else None)
        return {"count": len(clean["features"])}

    # -- Firmware -------------------------------------------------------------------------------
    fw_root = Path(settings["data_dir"]) / "firmware"
    builds = fw.BuildStore(fw_root)
    jobs = fw.JobRunner()
    app.state.builds, app.state.jobs = builds, jobs
    repo = settings.get("firmware_repo") or fw.DEFAULT_REPO

    def subnets() -> list[str]:
        if settings.get("ota_subnet"):
            return [s.strip() for s in str(settings["ota_subnet"]).split(",") if s.strip()]
        return fw.supervisor_subnets() or fw.local_subnets()

    def start(kind: str, title: str, fn) -> dict:
        try:
            return jobs.start(kind, title, fn).view()
        except fw.FirmwareError as e:
            raise HTTPException(409, str(e)) from e

    def build_or_404(build_id: str) -> dict:
        try:
            return builds.manifest(build_id)
        except (fw.FirmwareError, OSError, ValueError) as e:
            raise HTTPException(404, "unknown build") from e

    def record_flash(node_id: str | None, m: dict, via: str) -> None:
        if node_id and store.collar(node_id) is not None:
            store.set_collar_info(node_id, "firmware", {
                "build": m["id"], "commit": m.get("geocollar_commit"), "meshtastic": m.get("meshtastic_version"),
                "flashed_at": time.time(), "via": via,
            })
            store.set_collar_info(node_id, "update_mode", None)

    @app.get("/api/firmware")
    def firmware_overview():
        wifi = store.setting("ota_wifi") or {}
        try:
            ports = fw.serial_ports(settings.get("mesh_serial", ""))
        except Exception as e:  # noqa: BLE001 - pyserial missing or no /dev access
            log.warning("could not list serial ports: %s", e)
            ports = []
        return {
            "repo": repo,
            "builds": builds.list(),
            "job": jobs.current.view() if jobs.current else None,
            "ports": ports,
            "wifi": {"ssid": wifi.get("ssid", ""), "has_psk": bool(wifi.get("psk"))},
            "subnets": subnets(),
            "can_build": bool(shutil.which("git")),
        }

    @app.post("/api/firmware/download")
    def firmware_download():
        return start("download", "Download latest build", lambda job: fw.fetch_release(job, builds, repo))

    @app.post("/api/firmware/build")
    def firmware_build(body: BuildIn):
        return start("build", f"Build {body.ref} from source",
                     lambda job: fw.build_from_source(job, builds, fw_root / "work", repo, body.ref))

    @app.delete("/api/firmware/builds/{build_id}")
    def firmware_delete(build_id: str):
        build_or_404(build_id)
        builds.delete(build_id)
        return {"removed": build_id}

    @app.get("/api/firmware/builds/{build_id}/{kind}")
    def firmware_file(build_id: str, kind: str):
        build_or_404(build_id)
        try:
            return FileResponse(builds.file(build_id, kind), media_type="application/octet-stream")
        except fw.FirmwareError as e:
            raise HTTPException(404, str(e)) from e

    @app.put("/api/firmware/wifi")
    def firmware_wifi(body: WifiIn):
        old = store.setting("ota_wifi") or {}
        psk = old.get("psk", "") if body.psk is None else body.psk
        if not body.ssid.strip():
            store.set_setting("ota_wifi", None)
            return {"ssid": "", "has_psk": False}
        if len(body.ssid.encode()) > 32 or len(psk.encode()) > 63:
            raise HTTPException(400, "Wi-Fi name is at most 32 bytes and password at most 63")
        store.set_setting("ota_wifi", {"ssid": body.ssid, "psk": psk})
        return {"ssid": body.ssid, "has_psk": bool(psk)}

    @app.post("/api/firmware/flash-usb")
    def firmware_flash_usb(body: UsbFlashIn):
        m = build_or_404(body.build)

        def run(job):
            result = fw.flash_usb(job, builds, body.build, body.port, body.mode, settings.get("mesh_serial", ""))
            record_flash(body.node_id, m, "usb")
            return result

        return start("usb", f"Flash over USB ({body.port})", run)

    @app.post("/api/collars/{node_id}/firmware/flashed")
    def firmware_flashed(node_id: str, body: FlashedIn):
        require(node_id)
        record_flash(node_id, build_or_404(body.build), body.via)
        return store.collar(node_id).get("firmware")

    @app.post("/api/collars/{node_id}/firmware/ota")
    def firmware_ota(node_id: str, body: OtaIn):
        c = require(node_id)
        m = build_or_404(body.build)
        if body.ip:
            try:
                ipaddress.ip_address(body.ip)
            except ValueError as e:
                raise HTTPException(400, "not an IP address") from e
        pending = c.get("update_mode")
        if body.resume and (not pending or pending.get("build") != body.build):
            raise HTTPException(409, "this collar is not waiting for that build")
        if node_id in sim:
            return start("ota", f"Wi-Fi update of {c['name']} (simulated)",
                         lambda job: sim_ota(job, node_id, m))
        wifi = store.setting("ota_wifi") if body.send_wifi else None
        if body.send_wifi and not (wifi and wifi.get("ssid")):
            raise HTTPException(400, "save the Wi-Fi network for updates first")

        def run(job):
            result = fw.wifi_ota(job, builds, body.build, link, node_id, wifi, subnets(), body.ip or "", body.resume,
                                 on_update_mode=lambda b: store.set_collar_info(
                                     node_id, "update_mode", {"build": b, "since": time.time()}))
            record_flash(node_id, m, "wifi")
            return result

        return start("ota", f"Wi-Fi update of {c['name']}", run)

    def sim_ota(job, node_id: str, m: dict) -> dict:
        """Walk through the Wi-Fi update steps without a radio, so the page can be tried out."""
        steps = ["Asking the collar for its firmware details", "Sending the collar into update mode",
                 "Looking for the collar on Wi-Fi"]
        for step in steps:
            job.set_step(step)
            for _ in range(10):
                job.check_cancel()
                time.sleep(0.1)
        store.set_collar_info(node_id, "update_mode", {"build": m["id"], "since": time.time()})
        size = m["files"]["app"]["size"]
        job.set_step(f"Sending {size // 1024} KB", 0.0)
        for i in range(20):
            job.check_cancel()
            job.progress = (i + 1) / 20
            time.sleep(0.1)
        job.set_step("Image accepted. The collar is restarting into the new firmware.", 1.0)
        record_flash(node_id, m, "wifi")
        return {"build": m["id"], "commit": m.get("geocollar_commit"), "ip": "simulated"}

    @app.get("/api/firmware/jobs/{job_id}")
    def firmware_job(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        return job.view(since)

    @app.post("/api/firmware/jobs/{job_id}/cancel")
    def firmware_cancel(job_id: str):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        job.cancel.set()
        return job.view()

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
