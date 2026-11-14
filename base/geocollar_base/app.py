# SPDX-License-Identifier: GPL-3.0-or-later
"""Web front end and API for managing GeoCollars. Runs in Docker or as a Home Assistant app."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import firmware as fw
from . import geo
from . import protocol
from . import radio
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
        "home": os.environ.get("GEOCOLLAR_HOME", ""),  # "lat, lon"; else Home Assistant's home
    }
    options = Path(settings["data_dir"]) / "options.json"
    if options.exists():
        settings.update({k: v for k, v in json.loads(options.read_text()).items() if v not in (None, "")})
    return settings


class CollarIn(BaseModel):
    node_id: str
    name: str | None = None


class CollarPatch(BaseModel):
    name: str


class ContactIn(BaseModel):
    name: str = ""
    phone: str = ""


class RadioIn(BaseModel):
    long_name: str | None = None
    short_name: str | None = None
    region: str | None = None
    preset: str | None = None
    hop_limit: int | None = None
    channel_key: str = "keep"  # keep, default (public LongFast key) or new (random private key)
    channel_name: str | None = None
    public_secondary: bool | None = None  # keep a LongFast channel for lost-dog messages
    update_collars: bool = True  # move collars over the mesh first
    skip_unreachable: bool = False  # change anyway, leaving collars that did not answer behind


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
    node_id: str | None = None  # which collar is plugged in; found from its MAC address when left out
    name: str | None = None  # the dog's name, if this turns out to be a new collar
    setup: bool = False  # afterwards, set the collar up for this base (admin key, LoRa, channels)
    lock: bool = True  # and lock it to this base


class UsbSetupIn(BaseModel):
    port: str
    name: str | None = None
    lock: bool = True  # lock the collar to this base (Meshtastic managed mode)


class DeviceFlashedIn(BaseModel):
    node_id: str
    build: str
    via: str = "browser"
    name: str | None = None
    mode: str = "update"


class OtaIn(BaseModel):
    build: str
    ip: str | None = None  # the collar's address in update mode, if known
    send_wifi: bool = True
    resume: bool = False


class FlashedIn(BaseModel):
    build: str
    via: str = "browser"


class CommandIn(BaseModel):
    action: str  # beep, vibrate, shock, corrections_off, corrections_on, force_lost, clear_lost, request_status, silence_lost
    intensity: int = 0
    duration_ms: int = 1000
    confirm: bool = False  # shock_on must be confirmed by the owner


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
        link = MeshLink(lambda n, s: on_status(n, s), settings.get("mesh_serial", ""), settings.get("mesh_host", ""),
                        on_hello=lambda n, h: on_hello(n, h))
        connect = bool(settings.get("mesh_serial") or settings.get("mesh_host"))
    else:
        connect = False

    sim = Simulator(store.record_status, store.set_sim)

    # -- Lost beacon: the dog's name (the collar's name) and the owner's contact go to each collar,
    # which uses them only when the dog has been out for 90 s.
    def owner_for(node_id: str) -> list[str]:
        c = store.collar(node_id) or {}
        contact = store.setting("contact", {}) or {}
        dog = c.get("name") or ""
        if dog == node_id:
            dog = ""  # never named: the collar falls back to "Dog"
        return [protocol.fit_text(dog, protocol.MAX_DOG_NAME),
                protocol.fit_text(contact.get("name", ""), protocol.MAX_OWNER_NAME),
                protocol.fit_text(contact.get("phone", ""), protocol.MAX_PHONE)]

    def push_owner(node_id: str) -> bool:
        o = owner_for(node_id)
        if node_id in sim:
            sim.set_owner(node_id, *o)
        elif link.connected:
            try:
                link.send_owner(node_id, *o)
            except Exception as e:  # noqa: BLE001 - retried on the collar's next report
                log.warning("could not send the owner contact to %s: %s", node_id, e)
                return False
        else:
            return False
        store.set_collar_info(node_id, "owner_sent", o)
        return True

    def on_status(node_id: str, status: dict) -> None:
        status = dict(status)
        pki, key = status.pop("pki", False), status.pop("sender_key", "")
        c = store.collar(node_id)
        if c is None:
            log.info("ignoring a report from %s, which is not a collar on this base", node_id)
            return
        if not pki or not key:
            log.warning("ignoring a report from %s that was not PKI-encrypted (spoofed?)", node_id)
            return
        if c.get("public_key") and c["public_key"] != key:
            log.warning("ignoring a report from %s: its public key changed (spoofed, or the collar was erased; "
                        "remove and re-add the collar to accept the new key)", node_id)
            store.set_collar_info(node_id, "key_mismatch", time.time())
            return
        if not c.get("public_key"):
            store.set_collar_info(node_id, "public_key", key)
        store.record_status(node_id, status)
        # A collar that has not got the current name and contact yet gets them when it next reports.
        if c and node_id not in sim and c.get("owner_sent") != owner_for(node_id):
            push_owner(node_id)

    # -- New collars: a GeoCollar collar no base has adopted says hello on the mesh now and then.
    DISCOVERED_KEEP_S = 24 * 3600

    def on_hello(node_id: str, hello: dict) -> None:
        c = store.collar(node_id)
        if c is not None:
            # A collar only reports to a base that has talked to it, so one saying hello hasn't heard
            # from this base yet (just set up, reinstalled, or the base's first message was lost while
            # it restarted). Answer with the owner details; the collar replies with a status report.
            # Hellos aren't authenticated, so this is rate-limited and only ever sends to the collar.
            if c.get("sim") or time.time() - (c.get("hello") or {}).get("seen", 0) < 30:
                return
            store.set_collar_info(node_id, "hello", {**hello, "seen": time.time()})
            if hello.get("has_admin_key"):
                push_owner(node_id)
            return
        found = {k: v for k, v in (store.setting("discovered", {}) or {}).items()
                 if time.time() - v.get("seen", 0) < DISCOVERED_KEEP_S}
        prev = found.get(node_id)
        if prev and time.time() - prev.get("seen", 0) < 30:
            return  # heard it recently; don't rewrite the data file for every hello
        found[node_id] = {**hello, "seen": time.time(), "via": "mesh"}
        if len(found) > 20:  # anyone can say hello, so keep only the 20 most recent
            found = dict(sorted(found.items(), key=lambda kv: kv[1].get("seen", 0))[-20:])
        store.set_setting("discovered", found)

    def note_device(node_id: str, via: str, name: str | None) -> bool:
        """A collar found while flashing or setting up over USB: add it if it is new."""
        if not re.fullmatch(r"![0-9a-f]{8}", node_id or ""):
            return False
        is_new = store.collar(node_id) is None
        if is_new:
            dog = " ".join((name or "").split())
            store.upsert_collar(node_id, protocol.fit_text(dog, protocol.MAX_DOG_NAME) or None)
            store.set_collar_info(node_id, "added_via", via)
            push_owner(node_id)
        found = store.setting("discovered", {}) or {}
        if found.pop(node_id, None) is not None:
            store.set_setting("discovered", found)
        return is_new

    def note_setup(node_id: str | None, result: dict) -> None:
        if not node_id or store.collar(node_id) is None:
            return
        if result.get("ble_pin"):
            store.set_collar_info(node_id, "ble_pin", result["ble_pin"])
        if result.get("locked") or result.get("already_locked"):
            store.set_collar_info(node_id, "locked", True)
        store.set_collar_info(node_id, "public_key", None)  # re-learn it from the next report

    def adopt_setup(job, result: dict, name: str | None) -> None:
        """After a USB setup: keep the collar's record if its node ID changed, add it if new."""
        new, old = result.get("node_id"), result.get("previous_node_id")
        if not new:
            return
        if old and old != new and store.collar(old) is not None:
            if store.collar(new) is None:
                store.move_collar(old, new)
            else:
                store.remove_collar(old)
            job.log(f"Moved the collar's record from {old} to its new node ID {new}")
        result["new_collar"] = note_device(new, "usb", name)
        note_setup(new, result)

    for node_id, c in store.collars().items():
        if c.get("sim"):
            sim.add(node_id, c["sim"])
            sim.set_owner(node_id, *owner_for(node_id))
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
    app.state.on_status = on_status
    app.state.on_hello = on_hello
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
        c = store.upsert_collar(body.node_id, body.name)
        push_owner(body.node_id)
        found = store.setting("discovered", {}) or {}
        if found.pop(body.node_id, None) is not None:
            store.set_setting("discovered", found)
        return c

    # -- Map: address search and the home location ---------------------------------------------
    geocoder = geo.Geocoder()
    app.state.geocoder = geocoder

    @app.get("/api/geocode")
    def geocode(q: str, west: float | None = None, south: float | None = None, east: float | None = None,
                north: float | None = None):
        vb = (west, south, east, north) if None not in (west, south, east, north) else None
        try:
            return {"results": geocoder.search(q, vb)}
        except geo.GeoError as e:
            raise HTTPException(400 if "characters" in str(e) or "long" in str(e) else 502, str(e)) from e

    @app.get("/api/home")
    def home():
        """Where to start the map: the app option, else Home Assistant's home, else nothing."""
        return geo.parse_home(settings.get("home", "")) or geo.ha_home() or {}

    @app.get("/api/discovered")
    def discovered():
        """Collars heard saying hello on the mesh that are not on this base yet."""
        known = store.collars()
        return {k: v for k, v in (store.setting("discovered", {}) or {}).items()
                if k not in known and time.time() - v.get("seen", 0) < DISCOVERED_KEEP_S}

    @app.delete("/api/discovered/{node_id}")
    def dismiss_discovered(node_id: str):
        found = store.setting("discovered", {}) or {}
        found.pop(node_id, None)
        store.set_setting("discovered", found)
        return {"dismissed": node_id}

    @app.get("/api/base")
    def base_info():
        """The base radio's node ID and public key: the key goes into each collar's admin keys."""
        info = link.base_info() if link.connected and hasattr(link, "base_info") else {}
        return dict(info, radio_pending=bool(radio_pending)) if info else info

    @app.patch("/api/collars/{node_id}")
    def rename_collar(node_id: str, body: CollarPatch):
        """The collar's name is the dog's name: it is what the lost beacon says."""
        require(node_id)
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(400, "give the dog a name")
        if len(name.encode()) > protocol.MAX_DOG_NAME:
            raise HTTPException(400, f"the dog's name is at most {protocol.MAX_DOG_NAME} characters")
        c = store.upsert_collar(node_id, name)
        return {**c, "owner_sent": push_owner(node_id)}

    @app.get("/api/contact")
    def get_contact():
        return store.setting("contact", {"name": "", "phone": ""})

    @app.put("/api/contact")
    def put_contact(body: ContactIn):
        name, phone = " ".join(body.name.split()), " ".join(body.phone.split())
        if len(name.encode()) > protocol.MAX_OWNER_NAME:
            raise HTTPException(400, f"your name is at most {protocol.MAX_OWNER_NAME} characters")
        if len(phone.encode()) > protocol.MAX_PHONE:
            raise HTTPException(400, f"the phone number is at most {protocol.MAX_PHONE} characters")
        if phone and not all(ch.isdigit() or ch in "+-.() " for ch in phone):
            raise HTTPException(400, "the phone number can have digits, spaces and + - . ( ) only")
        store.set_setting("contact", {"name": name, "phone": phone})
        sent = [node_id for node_id in store.collars() if push_owner(node_id)]
        return {"name": name, "phone": phone, "sent_to": sent}

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
            if store.collar(node_id).get("owner_sent") != owner_for(node_id):
                push_owner(node_id)
        return {"fence_version": version, "chunks_sent": sent}

    @app.post("/api/collars/{node_id}/command")
    def command(node_id: str, body: CommandIn):
        require(node_id)
        try:
            action = protocol.CommandAction[body.action.upper()]
        except KeyError as e:
            raise HTTPException(400, f"unknown action {body.action}") from e
        if action == protocol.CommandAction.SHOCK_ON:
            if not body.confirm:
                raise HTTPException(400, "turning shock on needs confirmation")
            if not 1 <= body.intensity <= protocol.MAX_SHOCK_LEVEL:
                raise HTTPException(400, f"shock level must be 1 to {protocol.MAX_SHOCK_LEVEL}")
        if node_id in sim:
            sim.command(node_id, action, body.intensity)
            if node_id in sim and action in (protocol.CommandAction.SHOCK_ON, protocol.CommandAction.SHOCK_OFF):
                store.set_sim(node_id, sim.collars[node_id].settings())
        elif not link.connected:
            raise HTTPException(503, "not connected to a Meshtastic node")
        else:
            link.send_command(node_id, action, body.intensity, body.duration_ms, store.next_command_seq())
        if action == protocol.CommandAction.SHOCK_ON:
            store.set_collar_info(node_id, "shock_level", body.intensity)
            log.warning("shock turned on for %s at level %d", node_id, body.intensity)
        if node_id in sim:
            sim.report_now(node_id)
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
            # The ID read from the chip is the collar's node ID only until it has a key: right after a
            # full install, or for a collar already listed under it. A set-up 2.8 collar's ID is a
            # hash of its key, so an update can't be matched by chip ID; the setup below finds it.
            chip_id = result.get("node_id")
            node_id = body.node_id or (chip_id if chip_id and (body.mode == "full" or store.collar(chip_id)) else None)
            if node_id and not body.node_id and not body.setup:
                result["new_collar"] = note_device(node_id, "usb", body.name)
                if result["new_collar"]:
                    job.log(f"Added {node_id} to the base as a new collar")
            if body.mode == "full" and node_id:  # erased: the collar has a new key pair now
                store.set_collar_info(node_id, "public_key", None)
            if body.setup:
                c = store.collar(node_id) if node_id else None
                name = c["name"] if c and c["name"] != node_id else (body.name or "")
                result.update(fw.setup_collar(job, body.port, getattr(link, "iface", None), name, lock=body.lock))
                if node_id and node_id != result.get("previous_node_id") and store.collar(node_id) is not None:
                    result.setdefault("previous_node_id", node_id)
                adopt_setup(job, result, body.name)
                node_id = result.get("node_id") or node_id
            record_flash(node_id, m, "usb")
            return result

        return start("usb", f"Flash over USB ({body.port})", run)

    @app.post("/api/firmware/setup-usb")
    def firmware_setup_usb(body: UsbSetupIn):
        """Set up a collar that already runs GeoCollar, plugged into the base, without flashing."""
        base_radio = settings.get("mesh_serial", "")
        if base_radio and os.path.realpath(body.port) == os.path.realpath(base_radio):
            raise HTTPException(400, "that port is the base station's own Meshtastic radio")
        if not any(p["device"] == body.port for p in fw.serial_ports()):
            raise HTTPException(400, f"{body.port} is not a serial port on this machine")

        def run(job):
            result = fw.setup_collar(job, body.port, getattr(link, "iface", None), (body.name or "").strip(),
                                     boot_wait_s=45, lock=body.lock)
            adopt_setup(job, result, body.name)
            return result

        return start("usb", f"Set up the collar on {body.port}", run)

    @app.websocket("/api/firmware/setup-web")
    async def firmware_setup_web(ws: WebSocket, name: str = "", lock: bool = True):
        """Set up a collar plugged into the computer the page is open on: the page relays the
        collar's serial port (Web Serial) over this WebSocket, and the base runs the same setup as
        "Set up only" on its own USB.

        WebSockets ignore CORS, so a page on another site must not reach this: it would receive the
        base's channel keys. Through Home Assistant's ingress (which adds X-Ingress-Path and checks
        the login) or from the page on this app's own address only."""
        origin = urlsplit(ws.headers.get("origin", ""))
        if not ws.headers.get("x-ingress-path") and origin.netloc != ws.headers.get("host"):
            await ws.close(code=1008)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()

        def send(data: bytes) -> None:
            asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop).result(timeout=10)

        stream = fw.SocketStream(send)
        dog = " ".join(name.split())[:39]

        def run(job):
            try:
                result = fw.setup_collar(job, "this computer's USB", getattr(link, "iface", None), dog,
                                         open_iface=lambda _port: fw.open_socket_node(stream), boot_wait_s=90,
                                         lock=lock)
            finally:
                asyncio.run_coroutine_threadsafe(ws.send_json({"done": True}), loop)
            adopt_setup(job, result, dog or None)
            return result

        try:
            job = jobs.start("usb", "Set up the collar on this computer's USB", run).view()
        except fw.FirmwareError as e:
            await ws.send_json({"error": str(e)})
            await ws.close()
            return
        await ws.send_json({"job": job["id"]})
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    stream.feed(msg["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            stream.shutdown()

    # -- Base radio settings ----------------------------------------------------------------------
    # Set when collars moved to new settings but the base radio then failed to take them; the plan
    # (with any new channel key) is kept so the change can be finished rather than started again.
    radio_pending: dict = {}

    def apply_base(job, lora, chans, names, moved, left) -> None:
        job.set_step("Changing the base radio", 0.9)
        if moved:  # kept until the base takes them, even if this job is cancelled
            radio_pending.update(lora=lora, chans=chans, names=names, moved=moved, left=left)
        for attempt in range(6):
            try:
                link.apply_local(lora, chans, names.get("long_name"), names.get("short_name"))
                radio_pending.clear()
                break
            except Exception as e:  # noqa: BLE001 - often the radio reconnecting; retry for a minute or so
                job.log(f"The base radio did not take the settings: {e}")
                if attempt == 5 or not moved:
                    raise fw.FirmwareError(
                        f"the base radio did not take the new settings ({e})"
                        + (". The collars that moved can't hear it until it does: use \"Finish the change\" "
                           "under Base radio once it is connected" if moved else "")) from e
                job.check_cancel()
                time.sleep(15)
        if chans is not None:  # the mesh settings changed: watch for each collar on the new ones
            now = time.time()
            for node_id in moved:
                store.set_collar_info(node_id, "radio_changed", {"at": now, "moved": True})
            for node_id in left:
                store.set_collar_info(node_id, "radio_changed", {"at": now, "moved": False})

    @app.get("/api/base/settings")
    def base_settings():
        if not link.connected:
            raise HTTPException(503, "the base radio is not connected")
        try:
            current = link.radio_settings()
        except Exception as e:  # noqa: BLE001 - the radio may still be loading its config
            raise HTTPException(503, f"could not read the base radio's settings: {e}") from e
        real = [k for k, c in store.collars().items() if not c.get("sim")]
        return {"settings": current, "options": radio.options(), "collars": len(real), "pending": bool(radio_pending)}

    @app.post("/api/base/settings/finish")
    def finish_base_settings():
        if not radio_pending:
            raise HTTPException(409, "there is no unfinished change")
        if not link.connected:
            raise HTTPException(503, "the base radio is not connected")
        p = dict(radio_pending)

        def run(job):
            apply_base(job, p["lora"], p["chans"], p["names"], p["moved"], p["left"])
            job.set_step("Done. The base radio restarts with the new settings, and the app reconnects within a minute.", 1.0)
            return {"changed": True, "moved": p["moved"]}

        return start("radio", "Finish changing the base radio's settings", run)

    @app.put("/api/base/settings")
    def change_base_settings(body: RadioIn):
        if not link.connected:
            raise HTTPException(503, "the base radio is not connected")
        if radio_pending:
            raise HTTPException(409, "finish the last change first: the collars already moved to it")
        req = {k: v for k, v in body.model_dump().items()
               if v is not None and k not in ("update_collars", "skip_unreachable")}
        try:
            req = radio.validate(req)
        except (radio.RadioError, ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e

        def run(job):
            current = link.radio_settings()
            try:
                lora, chans, mesh_changed = radio.plan(link.iface.localNode, req)
            except radio.RadioError as e:
                raise fw.FirmwareError(str(e)) from e
            names = {k: req[k] for k in ("long_name", "short_name") if k in req and req[k] != current.get(k)}
            hop_changed = req.get("hop_limit", current["hop_limit"]) != current["hop_limit"]
            real = {k: c for k, c in store.collars().items() if not c.get("sim")}
            moved, left = [], []
            if mesh_changed and real and body.update_collars:
                job.set_step("Checking that every collar can hear the base", 0.0)
                reachable = []
                for i, (node_id, c) in enumerate(real.items()):
                    job.check_cancel()
                    try:
                        link.get_metadata(node_id, timeout=60)  # also fetches its admin session key
                        reachable.append(node_id)
                        job.log(f"{c['name']} answered")
                    except Exception as e:  # noqa: BLE001
                        left.append(node_id)
                        job.log(f"{c['name']} did not answer: {e}")
                    job.progress = (i + 1) / len(real) / 3
                if left and not body.skip_unreachable:
                    one = len(left) == 1
                    raise fw.FirmwareError(
                        "nothing was changed: " + ", ".join(real[k]["name"] for k in left)
                        + f" did not answer. Bring {'it' if one else 'them'} in range and try again, or tick "
                        f"\"Change anyway\" ({'it' if one else 'they'} would then need setting up again over USB, "
                        f"and a full install if {'it is' if one else 'they are'} locked to this base)")
                moves = radio.primary_moves(link.iface.localNode, lora, chans)
                for i, node_id in enumerate(reachable):
                    job.check_cancel()
                    name = real[node_id]["name"]
                    job.set_step(f"Moving {name} to the new settings", 1 / 3 + (i / len(reachable)) / 3)
                    try:
                        confirmed = link.push_radio(node_id, lora, chans, moves)
                    except Exception as e:  # noqa: BLE001
                        left.append(node_id)
                        job.log(f"{name} did not take the new settings: {e}")
                        continue
                    moved.append(node_id)
                    job.log(f"{name} has the new settings and is restarting" if confirmed else
                            f"{name} took the new settings; it switched channel before it could confirm the last "
                            "step, so its first report after the change confirms it")
                if not moved and not body.skip_unreachable:
                    raise fw.FirmwareError("no collar took the new settings, so the base radio was left as it was")
            elif mesh_changed and real:
                left = list(real)
                job.log("Collars were not moved; they keep the old settings")
            if not (mesh_changed or names or hop_changed):
                job.set_step("Nothing to change.", 1.0)
                return {"changed": False}
            apply_base(job, lora if (mesh_changed or hop_changed) else None, chans if mesh_changed else None,
                       names, moved, left)
            job.set_step("Done. The base radio restarts if its radio settings changed, and the app reconnects "
                         "within a minute." + (f" Not moved: {', '.join(real[k]['name'] for k in left)}." if left else ""), 1.0)
            return {"changed": True, "moved": moved, "left_behind": left}

        return start("radio", "Change the base radio's settings", run)

    @app.post("/api/firmware/flashed")
    def firmware_device_flashed(body: DeviceFlashedIn):
        """Flashed from the browser: the page read the collar's MAC address, so add it if new."""
        m = build_or_404(body.build)
        node_id = body.node_id.lower()
        if not re.fullmatch(r"![0-9a-f]{8}", node_id):
            raise HTTPException(400, "bad node ID")
        # The chip's ID is the node ID only until the collar makes its key (see flash-usb), so an
        # update adds nothing new; "Set up only" finds the real ID.
        if body.mode != "full" and store.collar(node_id) is None:
            return {"node_id": node_id, "new_collar": False, "collar": None}
        new = note_device(node_id, body.via, body.name)
        record_flash(node_id, m, body.via)
        if body.mode == "full":  # erased: the collar has a new key pair now
            store.set_collar_info(node_id, "public_key", None)
        return {"node_id": node_id, "new_collar": new, "collar": store.collar(node_id)}

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
        push_owner(node_id)
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
