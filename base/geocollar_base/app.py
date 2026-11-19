# SPDX-License-Identifier: GPL-3.0-or-later
"""Web front end and API for managing GeoCollars. Runs in Docker or as a Home Assistant app."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import struct
import threading
import time
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
import hmac
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import firmware as fw
from .collarlog import CollarLog
from . import geo
from . import phoneapi as papi
from . import protocol
from . import radio
from .mesh import MeshLink
from . import sim as simmod
from .sim import Simulator
from .store import Store, fresh_position

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def load_settings() -> dict:
    """Environment variables, overridden by Home Assistant's /data/options.json when present."""
    settings = {
        "data_dir": os.environ.get("GEOCOLLAR_DATA_DIR", "/data"),
        "mesh_serial": os.environ.get("GEOCOLLAR_MESH_SERIAL", ""),
        "mesh_host": os.environ.get("GEOCOLLAR_MESH_HOST", ""),
        "firmware_repo": os.environ.get("GEOCOLLAR_FIRMWARE_REPO", fw.DEFAULT_REPO),
        "home": os.environ.get("GEOCOLLAR_HOME", ""),  # "lat, lon"; else Home Assistant's home
        # Where phones reach the base, if not the address worked out (see phone_urls). The page's
        # Settings tab overrides it.
        "phone_url": os.environ.get("GEOCOLLAR_PHONE_URL", ""),
        "phone_port": os.environ.get("GEOCOLLAR_PHONE_PORT", str(papi.DEFAULT_PORT)),
        # Names besides local ones that the page may be opened by, comma-separated (a reverse
        # proxy's, for example). See PageGuard.
        "allowed_hosts": os.environ.get("GEOCOLLAR_ALLOWED_HOSTS", ""),
    }
    options = Path(settings["data_dir"]) / "options.json"
    if options.exists():
        settings.update({k: v for k, v in json.loads(options.read_text()).items() if v not in (None, "")})
    return settings


class CollarIn(BaseModel):
    node_id: str
    name: str | None = None


class CollarPatch(BaseModel):
    name: str | None = None
    report_s: int | None = None  # how often the collar reports, seconds
    gps_inside_s: int | None = None  # GPS fix interval inside the fences, seconds (0: always on)
    gps_everywhere: bool | None = None  # that interval wherever the dog is inside, not only near the bands


class ContactIn(BaseModel):
    name: str = ""
    phone: str = ""


ANCHOR_KINDS = {"off": protocol.AnchorKind.NONE, "beacon": protocol.AnchorKind.BEACON,
                "base": protocol.AnchorKind.BASE_RADIO}
GPS_RESTART_CHOICES = (10, 30, 60)  # seconds the collar's GPS may take to fix after home mode


class HomeAnchorIn(BaseModel):
    kind: str = "off"  # off, beacon (a Bluetooth beacon by its address) or base (the base radio)
    address: str = ""  # the beacon's Bluetooth address
    rssi: int = -75  # how strong the anchor must be heard, dBm: how close counts as home
    lat: float | None = None  # where the anchor stands; left out: the base's home location
    lon: float | None = None
    gps_restart_s: int = 30  # for the room check only


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


class BuildIn(BaseModel):
    ref: str = "main"


class CheckEveryIn(BaseModel):
    every: str  # never, daily, weekly or monthly


class UsbFlashIn(BaseModel):
    build: str | None = None  # left out: the latest, downloaded first if it is not here yet
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


class FlashedIn(BaseModel):
    build: str
    via: str = "browser"


class AppIn(BaseModel):
    name: str = ""  # whose phone it is, for the list


class HaLinkIn(BaseModel):
    external: str | None = None  # Home Assistant's address from outside, with the integration's path
    internal: str | None = None  # and on the home network


class PhoneAccessIn(BaseModel):
    url: str = ""  # the Base URL phones use from outside the home; empty: work it out


class CommandIn(BaseModel):
    action: str  # beep, vibrate, shock, corrections_off, corrections_on, force_lost, clear_lost, request_status, silence_lost
    intensity: int = 0
    duration_ms: int = 1000
    confirm: bool = False  # shock_on must be confirmed by the owner


NODE_ID = re.compile(r"![0-9a-f]{8}")  # a Meshtastic node ID as the page and the radio write it
MAX_APP_NAME = 40  # characters of a linked phone's name
APP_RESEND_MAX_S = 3600  # an unanswered app key list is sent again after 1, 2, 4... minutes, up to this
MAX_CORRECTION_MS = 3000  # the collar firmware's cap on one beep, vibration or shock


def clean_name(name: str | None) -> str:
    """A dog's name as typed: single spaces, nothing unprintable. Raises ValueError if too long."""
    name = " ".join("".join(ch for ch in (name or "") if ch.isprintable()).split())
    if len(name.encode()) > protocol.MAX_DOG_NAME:
        raise ValueError(f"the dog's name is at most {protocol.MAX_DOG_NAME} characters")
    return name


def same_site(headers) -> bool:
    """Whether a request that changes something comes from this app's own page.

    The page has no login, so a page on another website open in the owner's browser could
    otherwise post commands to the base behind their back. Browsers name the page a request comes
    from in the Origin header and do not let a page forge it, so: no Origin (not a browser: curl,
    an automation) passes; through Home Assistant's ingress (X-Ingress-Path, which a page cannot
    add) Home Assistant has checked the login; otherwise the Origin must be the address this app
    was reached on."""
    origin = headers.get("origin")
    if origin is None or headers.get("x-ingress-path"):
        return True
    host = urlsplit(origin).netloc
    return bool(host) and host in (headers.get("host"), headers.get("x-forwarded-host"))


def add_phone_middleware(app: FastAPI) -> None:
    """For the phone API: refuse an oversized body before reading it, and answer a browser's
    cross-origin questions (the app in a browser, for trying it out). Only for the phone API's
    paths: the page's own API must stay closed to other websites."""

    app.add_middleware(PhoneBodyLimit)

    @app.middleware("http")
    async def phone_api_edges(request: Request, call_next):
        path = request.scope["path"]
        at = path.find(papi.PREFIX + "/")
        if at < 0:
            return await call_next(request)
        if at > 0:
            # Behind a reverse proxy that passes on a path of its own in front (a Base URL like
            # https://example.com/geocollar, forwarded without stripping /geocollar): drop it.
            request.scope["path"] = path[at:]
            request.scope["raw_path"] = path[at:].encode()
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "3600"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = papi.MAX_BODY + 1
        if size > papi.MAX_BODY:
            return JSONResponse({"detail": "The request is too large.", "code": "too_large"}, status_code=413, headers=cors)
        response = await call_next(request)
        response.headers.update(cors)
        return response


LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")
# Home Assistant's internal network: the Supervisor's ingress proxy is .2, Home Assistant itself
# (and a port the owner opened on the Network tab) comes from .1, every other app has an address of its own.
HA_NETWORK = ipaddress.ip_network("172.30.32.0/23")
HA_TRUSTED = {ipaddress.ip_address("172.30.32.1"), ipaddress.ip_address("172.30.32.2")}


def host_name(header: str | None) -> str:
    """The name in a Host header, lower case, without the port or IPv6 brackets."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def local_host(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """Whether the page may answer to this name: an IP address, localhost, a name with no dot (the
    machine's own, or an app's on Home Assistant's network), a home-network name (.local, .lan,
    .home.arpa...), or one the owner listed. A public name is what another website uses to reach
    the page through DNS rebinding: its name resolving, for a moment, to the base's address."""
    if not name:
        return False
    if name in extra:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or name.endswith(".localhost") or "." not in name.rstrip(".") \
        or name.rstrip(".").endswith(LOCAL_SUFFIXES)


def parse_hosts(text: str) -> frozenset[str]:
    return frozenset(h for h in (host_name(x) for x in (text or "").replace(" ", ",").split(",")) if h)


class PageGuard:
    """The page has no login, so only those it is meant for may use it:

    - Browsers only by a local name (local_host). The check against other sites (same_site) trusts
      the Origin header, which matches the Host header when another website reaches the base by
      DNS rebinding. That website's name is not local, so it is refused here.
    - In Home Assistant, not the other apps on its internal network: only the Supervisor's ingress
      (the sidebar, behind the login) and Home Assistant itself (the GeoCollar integration).

    The phone API is left alone: every request to it is sealed with a phone's key."""

    def __init__(self, app, extra_hosts: frozenset[str] = frozenset(), in_ha: bool = False):
        self.app = app
        self.extra = extra_hosts
        self.in_ha = in_ha

    def refused(self, scope) -> str | None:
        if scope.get("path", "").find(papi.PREFIX + "/") >= 0:
            return None
        if self.in_ha:
            try:
                ip = ipaddress.ip_address((scope.get("client") or ("",))[0])
            except ValueError:
                ip = None
            if ip is not None and ip in HA_NETWORK and ip not in HA_TRUSTED:
                return f"another app on Home Assistant's network ({ip})"
        headers = dict(scope.get("headers") or [])
        name = host_name(headers.get(b"host", b"").decode("latin-1"))
        if not local_host(name, self.extra):
            return f"the name {name or '(none)'!r}, which is not a local one"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        why = self.refused(scope)
        if why is None:
            return await self.app(scope, receive, send)
        log.warning("refused %s %s: %s. To open the page by another name, add it to allowed_hosts",
                    scope.get("method", "WEBSOCKET"), scope.get("path"), why)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "This page answers only on your home network, by its own address. "
                                     "To open it by another name, add the name to allowed_hosts."}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class PhoneBodyLimit:
    """Refuse a phone API body over papi.MAX_BODY as it arrives. The Content-Length check in
    phone_api_edges does not see a chunked request, which has none: without this, one such
    request to the port opened to the internet could fill the base's memory."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").find(papi.PREFIX + "/") < 0:
            return await self.app(scope, receive, send)
        total = 0

        async def limited():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > papi.MAX_BODY:
                    raise HTTPException(413, "The request is too large.")
            return message

        await self.app(scope, limited, send)


def phone_openapi(app: FastAPI) -> dict:
    """The OpenAPI document, with the schemas of what travels inside the envelopes."""
    if app.openapi_schema:
        return app.openapi_schema
    doc = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    doc.setdefault("components", {}).setdefault("schemas", {}).update(papi.plaintext_components())
    app.openapi_schema = doc
    return doc


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
    clog = CollarLog(settings["data_dir"])  # each collar's reports over time, for the Log tab
    # Wi-Fi updates were removed in 0.17. The home Wi-Fi password an older version kept here for
    # them is forgotten; the network's name is kept until the collars have been told to forget
    # it too (see forget-wifi).
    old_wifi = store.setting("ota_wifi")
    if old_wifi is not None:
        store.set_setting("ota_wifi", None)
        if isinstance(old_wifi, dict) and old_wifi.get("ssid"):
            store.set_setting("wifi_leftover", {"ssid": old_wifi["ssid"]})
        store.refresh_backup()  # the hourly backup would otherwise keep the password

    def wifi_leftover() -> str | None:
        """The Wi-Fi network real collars may still hold, or None once every one has been asked."""
        left = store.setting("wifi_leftover") or {}
        if not left.get("ssid"):
            return None
        if not any(not c.get("sim") and not c.get("wifi_forgotten") for c in store.collars().values()):
            return None
        return left["ssid"]

    if link is None:
        link = MeshLink(lambda n, s: on_status(n, s), settings.get("mesh_serial", ""), settings.get("mesh_host", ""),
                        on_hello=lambda n, h: on_hello(n, h))
        connect = bool(settings.get("mesh_serial") or settings.get("mesh_host"))
    else:
        connect = False
    # The collar's public key as read over USB at setup, which the radio needs to write to it.
    link.key_for = lambda n: (store.collar(n) or {}).get("public_key")
    link.on_send_error = lambda n, r: on_send_error(n, r)
    link.on_app_ack = lambda n, a: on_app_ack(n, a)

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
        seq = 0
        if node_id in sim:
            sim.set_owner(node_id, *o)
        elif link.connected:
            # Numbered, so a recording of it cannot be played back to the collar later; but only
            # for a collar whose reports say its firmware reads the number (0.16 on). Older
            # firmware refuses the longer message.
            if (store.collar(node_id) or {}).get("numbered"):
                seq = store.next_command_seq()
            try:
                link.send_owner(node_id, *o, seq)
            except Exception as e:  # noqa: BLE001 - retried on the collar's next report
                log.warning("could not send the owner contact to %s: %s", node_id, e)
                return False
        else:
            return False
        store.set_collar_info(node_id, "owner_sent", o)
        store.set_collar_info(node_id, "owner_seq", seq or None)
        return True

    def home_anchor() -> protocol.HomeAnchor | None:
        """The home anchor the owner chose (kind NONE: home mode off), None if never set."""
        a = store.setting("home_anchor")
        if not a:
            return None
        return protocol.HomeAnchor(ANCHOR_KINDS.get(a.get("kind"), protocol.AnchorKind.NONE),
                                   a.get("address", ""), int(a.get("rssi", -75)))

    home_cache: dict = {}  # "at": the last home location Home Assistant gave
    home_lock = threading.Lock()

    def anchor_follows_home() -> bool:
        """Home mode is on and the owner has not put the anchor anywhere: it is at the home location."""
        a = store.setting("home_anchor") or {}
        return a.get("kind", "off") != "off" and (a.get("lat") is None or a.get("lon") is None)

    def home_moved(at: tuple[float, float]) -> None:
        """The home location changed (or Home Assistant gave it for the first time): an anchor at
        the home location moves with it. Collars only listen for the anchor, so only the simulated
        ones, which stand in for its radio range with this position, need telling."""
        if not anchor_follows_home():
            return
        log.info("the home location is now %.5f, %.5f: the home anchor moves with it", *at)
        anchor = home_anchor()
        for node_id in store.collars():
            if node_id in sim:
                sim.set_anchor(node_id, anchor, at)

    def base_home(fresh: bool = False) -> tuple[float, float] | None:
        """The base's home location: the app option, else Home Assistant's home. Home Assistant is
        asked once, and again when fresh: when the owner saves the home mode setting, so a home
        moved there reaches the base then. Until it has answered (it may not be up yet) it is
        asked each time; after that, if it can't be reached, the last location it gave stands."""
        h = geo.parse_home(settings.get("home", ""))
        if h:
            return float(h["lat"]), float(h["lon"])
        with home_lock:
            if not fresh and "at" in home_cache:
                return home_cache["at"]
        h = geo.ha_home()
        with home_lock:
            before = home_cache.get("at")
            if h:
                home_cache["at"] = (float(h["lat"]), float(h["lon"]))
            at = home_cache.get("at")
        if at is not None and at != before:
            home_moved(at)
        return at

    def anchor_position() -> tuple[float, float] | None:
        """Where the anchor stands: where the owner put it, else the base's home location."""
        a = store.setting("home_anchor") or {}
        if a.get("kind", "off") == "off":
            return None
        if a.get("lat") is not None and a.get("lon") is not None:
            return float(a["lat"]), float(a["lon"])
        return base_home()

    def gps_inside_for(c: dict) -> int:
        """How often the collar takes a GPS fix inside the fences: the owner's choice, else 3 s."""
        v = c.get("gps_inside_s")
        return protocol.DEFAULT_GPS_INSIDE_S if v is None else int(v)

    def config_for(node_id: str):
        """What the collar should have been sent: its report interval, the home anchor (kind NONE
        until the owner turns home mode on) and its GPS interval inside the fences."""
        c = store.collar(node_id) or {}
        anchor = home_anchor() or protocol.HomeAnchor()
        return [c.get("report_s") or 0, int(anchor.kind), anchor.address, anchor.rssi, gps_inside_for(c),
                bool(c.get("gps_everywhere"))]

    def push_config(node_id: str) -> bool:
        """The collar's report interval, if the owner set one, the home anchor and its GPS interval
        inside the fences."""
        c = store.collar(node_id) or {}
        want = config_for(node_id)
        report_s = c.get("report_s") or 0
        anchor = home_anchor() or protocol.HomeAnchor()
        seq = 0
        if node_id in sim:
            if report_s:
                sim.update(node_id, {"report_s": min(report_s, 600)})
            sim.set_anchor(node_id, anchor, anchor_position())
        elif link.connected:
            seq = store.next_command_seq()  # always numbered: firmware 0.15 skips the number
            try:
                link.send_config(node_id, report_s, seq, anchor, gps_inside_for(c), bool(c.get("gps_everywhere")))
            except Exception as e:  # noqa: BLE001 - retried on the collar's next report
                log.warning("could not send the collar settings to %s: %s", node_id, e)
                return False
        else:
            return False
        store.set_collar_info(node_id, "config_sent", want)
        store.set_collar_info(node_id, "config_seq", seq or None)
        return True

    # -- The companion app: each phone gets a key of its own in a QR code, and the collars get the
    # list of keys over the mesh. The phone then talks to a collar over the collar's Bluetooth.
    #
    # A phone is linked to the whole base ("phones" in the settings): every collar on the base,
    # and every one added later, gets a key made from that phone's key (protocol.collar_app_key).
    # Phones linked to one collar by an older version of the base are still in that collar's
    # "apps" and keep working there until removed.
    def home_id() -> str:
        """This base's id in the pairing codes, so a phone can tell its bases apart."""
        h = store.setting("home_id")
        if not h:
            h = secrets.token_hex(4)
            store.set_setting("home_id", h)
        return h

    def phones() -> list[dict]:
        return store.setting("phones") or []

    def public_apps(c: dict) -> list[dict]:
        """Linked phones without their keys: a key leaves the base once, in its QR code."""
        return [{"id": f"{a['id']:08x}", "name": a.get("name", ""), "created": a.get("created")}
                for a in c.get("apps") or []]

    def app_keys_for(node_id: str, c: dict) -> list[tuple[int, bytes]]:
        """The keys a collar should hold: phones linked to it alone, then the base's phones."""
        keys = [(a["id"], bytes.fromhex(a["key"])) for a in c.get("apps") or []]
        keys += [(p["id"], protocol.collar_app_key(bytes.fromhex(p["key"]), node_id)) for p in phones()]
        return keys[:protocol.MAX_APP_KEYS]

    def keys_print(keys: list[tuple[int, bytes]]) -> str:
        """A fingerprint of a key list, to tell whether the list on the collar is the one wanted now."""
        return hashlib.sha256(b"".join(struct.pack("<I", i) + k for i, k in keys)).hexdigest()[:16]

    def apps_synced(node_id: str, c: dict) -> bool:
        """Whether the collar has confirmed the list of phones as it is now."""
        keys = app_keys_for(node_id, c)
        if c.get("apps_seq") is None:  # never sent: fine as long as there is nothing to send
            return not keys
        # A record from before the fingerprint was kept counts as the list it was sent with.
        return c.get("apps_seq") == c.get("apps_acked") and c.get("apps_for", keys_print(keys)) == keys_print(keys)

    def public_collar(c: dict | None, node_id: str | None = None) -> dict | None:
        """A collar as the page gets it."""
        if c is None:
            return None
        c = dict(c)
        c["gps_inside_s"] = gps_inside_for(c)
        c["gps_everywhere"] = bool(c.get("gps_everywhere"))
        if node_id:
            c["apps_synced"] = apps_synced(node_id, c)
            c["app_keys"] = len(app_keys_for(node_id, c))
        c["apps"] = public_apps(c)
        c.pop("apps_for", None)
        return c

    def push_apps(node_id: str) -> bool:
        """Send the collar the list of phones that may connect to it. The list is confirmed when
        the collar answers with its number (on_app_ack); until then on_status sends it again."""
        c = store.collar(node_id) or {}
        keys = app_keys_for(node_id, c)
        store.set_collar_info(node_id, "apps_for", keys_print(keys))
        if node_id in sim:  # a simulated collar has no Bluetooth: it just agrees
            seq = store.next_command_seq()
            store.set_collar_info(node_id, "apps_seq", seq)
            store.set_collar_info(node_id, "apps_acked", seq)
            return True
        if not link.connected:
            store.set_collar_info(node_id, "apps_seq", "unsent")  # so it counts as not on the collar yet
            return False
        seq = store.next_command_seq()
        store.set_collar_info(node_id, "apps_seq", seq)
        store.set_collar_info(node_id, "apps_sent", {"at": time.time(), "tries": (c.get("apps_sent") or {}).get("tries", 0) + 1})
        try:
            link.send_app_keys(node_id, keys, seq)
        except Exception as e:  # noqa: BLE001 - sent again on the collar's next report
            log.warning("could not send the app keys to %s: %s", node_id, e)
            return False
        return True

    def apps_changed(node_id: str) -> bool:
        store.set_collar_info(node_id, "apps_sent", None)  # a new list: start counting tries again
        return push_apps(node_id)

    def trusted(node_id: str, c: dict, pki: bool, key: str, what: str) -> bool:
        """Whether a message really comes from the collar: it must be PKI-encrypted, from the key
        this base first saw (or read over USB) for that collar."""
        if not pki or not key:
            log.warning("ignoring %s from %s that was not PKI-encrypted (spoofed?)", what, node_id)
            return False
        if c.get("public_key") and c["public_key"] != key:
            log.warning("ignoring %s from %s: its public key changed (spoofed, or the collar was erased; "
                        "remove and re-add the collar to accept the new key)", what, node_id)
            store.set_collar_info(node_id, "key_mismatch", time.time())
            return False
        return True

    # -- Fences that have not reached the collar. They go in a few packets, and one lost on the
    # mesh (or a save while the radio was not connected) leaves the collar on its old fences, with
    # nothing but the page's "Resend" to fix it. So the base sends them again when a report shows
    # the collar holds another version: 2 minutes after the last try, then 4, 8... up to an hour.
    FENCE_RESEND_FIRST_S, FENCE_RESEND_MAX_S = 120, 3600
    fence_resends: set[str] = set()  # collars whose fences are being sent again right now
    fence_resend_lock = threading.Lock()
    app_state: dict = {}  # filled in once the app exists (for tests: the resend threads)

    def resend_fences(node_id: str, c: dict, held) -> None:
        want = c.get("fence_version") or 0
        if not c.get("fences") or not want or held is None or held == want or not link.connected:
            return
        sent = c.get("fences_sent") or {}
        same = sent.get("version") == want
        tries = sent.get("tries", 0) if same else 0
        wait = min(FENCE_RESEND_MAX_S, FENCE_RESEND_FIRST_S * 2 ** min(tries, 10))
        if same and time.time() - sent.get("at", 0) < wait:
            return
        with fence_resend_lock:
            if node_id in fence_resends:
                return
            fence_resends.add(node_id)

        def run() -> None:
            try:
                version = want
                if held and 0 < (held - want) & 0xFFFF < 0x8000:
                    # The collar holds a newer version than this base knows (a base restored from an
                    # older backup): it would refuse this one, so number the same fences past it.
                    version = store.set_fences(node_id, c["fences"])
                zones = protocol.zones_from_geojson(c["fences"])
                pulses = protocol.pulses_from_geojson(c["fences"])
                store.set_collar_info(node_id, "fences_sent", {"version": version, "at": time.time(), "tries": tries + 1})
                log.info("%s still reports fence version %s: sending version %s again", node_id, held, version)
                link.send_fences(node_id, zones, version, pulses)
            except Exception as e:  # noqa: BLE001 - tried again on a later report
                log.warning("could not send the fences to %s again: %s", node_id, e)
            finally:
                with fence_resend_lock:
                    fence_resends.discard(node_id)

        # Not on the radio's thread, which this report came in on: the packets go 2 s apart.
        t = threading.Thread(target=run, name=f"fences for {node_id}", daemon=True)
        app_state["threads"] = [x for x in app_state.get("threads", []) if x.is_alive()] + [t]
        t.start()

    def on_app_ack(node_id: str, ack: dict) -> None:
        c = store.collar(node_id)
        if c is None or not trusted(node_id, c, ack.get("pki", False), ack.get("sender_key", ""), "an app key answer"):
            return
        if ack.get("seq") == c.get("apps_seq"):
            store.set_collar_info(node_id, "apps_acked", ack["seq"])
            store.set_collar_info(node_id, "apps_sent", None)

    def log_entry(recorded: dict) -> dict:
        """A report for the log: the position only when it was where the dog was at that moment
        (a current fix from enough satellites, not home with the GPS off)."""
        entry = {"t": round(recorded["received"], 3), "state": recorded.get("state"),
                 "bat": recorded.get("battery_pct"), "sats": recorded.get("sats")}
        if recorded.get("home"):
            entry["home"] = True
        if fresh_position(recorded):
            entry["lat"], entry["lon"] = recorded["lat"], recorded["lon"]
        return entry

    def on_status(node_id: str, status: dict) -> None:
        status = dict(status)
        pki, key = status.pop("pki", False), status.pop("sender_key", "")
        c = store.collar(node_id)
        if c is None:
            log.info("ignoring a report from %s, which is not a collar on this base", node_id)
            return
        if not trusted(node_id, c, pki, key, "a report"):
            return
        if not c.get("public_key"):
            store.set_collar_info(node_id, "public_key", key)
        recorded = store.record_status(node_id, status)
        if recorded:
            # The log: every report, with the position only when it was a fix from that moment.
            entry = log_entry(recorded)
            try:
                clog.add(node_id, entry)
            except (OSError, ValueError) as e:  # a full disk must not stop the reports
                log.warning("could not log a report from %s: %s", node_id, e)
        if node_id in sim:
            return
        numbered = bool(status.get("numbered"))
        if bool(c.get("numbered")) != numbered:
            store.set_collar_info(node_id, "numbered", numbered or None)
        # A collar that has not got the current name and contact yet gets them when it next reports.
        # So does one that reads sequence numbers but last got these without one: until a numbered
        # message arrives, it would still accept a recording of an old one.
        if c.get("owner_sent") != owner_for(node_id) or (numbered and not c.get("owner_seq")):
            push_owner(node_id)
        want = config_for(node_id)
        if want is not None and (c.get("config_sent") != want or (numbered and not c.get("config_seq"))):
            push_config(node_id)
        # The list of companion app keys goes again until the collar has confirmed it. So it does
        # when the collar's report disagrees with the base about holding any: a collar that was
        # erased has lost its keys, and one that came from another base may still hold that
        # base's. Collar firmware before 0.20 never answers, so the tries are spaced out.
        apps = app_keys_for(node_id, c)
        if c.get("apps_for") not in (None, keys_print(apps)):
            apps_changed(node_id)  # the list it was sent is not the one wanted now (a new node ID): no waiting
        elif not apps_synced(node_id, c) or bool(apps) != bool(status.get("apps")):
            sent = c.get("apps_sent") or {}
            wait = min(APP_RESEND_MAX_S, 60 * 2 ** min(sent.get("tries", 0), 10) // 2)
            if time.time() - sent.get("at", 0) >= wait:
                push_apps(node_id)
        resend_fences(node_id, c, status.get("fence_version"))

    def ask_node_info(node_id: str) -> None:
        """The radio can't PKI-encrypt to a collar whose public key it doesn't have (one set up by
        hand, or before the base kept the key from USB setup). Swap NodeInfo with it, at most every
        10 minutes; the collar answers once in 12 hours."""
        c = store.collar(node_id) or {}
        if c.get("sim") or not link.connected or time.time() - (c.get("nodeinfo_asked") or 0) < 600:
            return
        try:
            link.request_node_info(node_id)
        except Exception as e:  # noqa: BLE001 - tried again on its next hello
            log.warning("could not ask %s for its NodeInfo: %s", node_id, e)
            return
        store.set_collar_info(node_id, "nodeinfo_asked", time.time())

    def on_send_error(node_id: str, reason: str) -> None:
        if store.collar(node_id) is None:
            return
        store.set_collar_info(node_id, "send_error", {"reason": reason, "at": time.time()})
        # Meshtastic 2.7 says PKI_FAILED, and 2.8 PKI_SEND_FAIL_PUBLIC_KEY, when the radio has no
        # public key for the collar. Give it the one from setup again, and ask the collar for it.
        if reason in ("PKI_FAILED", "PKI_SEND_FAIL_PUBLIC_KEY", "PKI_UNKNOWN_PUBKEY"):
            link.forget_key(node_id)
            ask_node_info(node_id)

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
                if link.connected and not c.get("public_key") and not link.radio_key(node_id):
                    ask_node_info(node_id)  # the answer can't be encrypted to it yet
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

    def note_device(node_id: str, via: str, name: str | None, push: bool = True) -> bool:
        """A collar found while flashing or setting up over USB: add it if it is new."""
        if not re.fullmatch(r"![0-9a-f]{8}", node_id or ""):
            return False
        is_new = store.collar(node_id) is None
        if is_new:
            dog = " ".join((name or "").split())
            store.upsert_collar(node_id, protocol.fit_text(dog, protocol.MAX_DOG_NAME) or None)
            store.set_collar_info(node_id, "added_via", via)
            if push:
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
        # The key read over USB: reports must carry it, and the radio needs it to write to the collar.
        store.set_collar_info(node_id, "public_key", result.get("public_key") or None)
        store.set_collar_info(node_id, "key_mismatch", None)
        store.set_collar_info(node_id, "send_error", None)
        if result.get("public_key") and link.connected:
            try:
                link.ensure_contact(node_id)
            except Exception as e:  # noqa: BLE001 - done again before the next message to it
                log.warning("could not give the base radio the key of %s: %s", node_id, e)
        # A collar that trusts this base reports once it hears from it, so say something now
        # rather than at its next hello.
        if result.get("locked") or result.get("already_locked") or result.get("checked"):
            push_owner(node_id)
            push_config(node_id)

    def adopt_setup(job, result: dict, name: str | None) -> None:
        """After a USB setup: keep the collar's record if its node ID changed, add it if new."""
        new, old = result.get("node_id"), result.get("previous_node_id")
        if not new:
            return
        if old and old != new and store.collar(old) is not None:
            kept = store.collar(new) is None
            if kept:
                store.move_collar(old, new)
            else:
                store.remove_collar(old)
            try:
                if kept:
                    clog.move(old, new)  # its log goes with it
                else:
                    clog.clear(old)
            except (OSError, ValueError) as e:
                log.warning("could not move the log of %s: %s", old, e)
            job.log(f"Moved the collar's record from {old} to its new node ID {new}")
        # Its key goes to the radio (note_setup) before anything is sent to it.
        result["new_collar"] = note_device(new, "usb", name, push=False)
        note_setup(new, result)

    for node_id, c in store.collars().items():
        if c.get("sim"):
            sim.add(node_id, c["sim"])
            sim.set_owner(node_id, *owner_for(node_id))
            sim.set_anchor(node_id, home_anchor(), anchor_position())
            if c.get("fences"):
                try:
                    sim.set_fences(node_id, protocol.zones_from_geojson(c["fences"]), c.get("fence_version", 0),
                                   protocol.pulses_from_geojson(c["fences"]))
                except (KeyError, ValueError):
                    log.warning("stored fences for %s are invalid", node_id)

    stop_checks = threading.Event()

    def update_check_loop() -> None:
        """Looks for newer collar firmware as often as the owner chose (see check_updates)."""
        if stop_checks.wait(90):  # let the app start first
            return
        while True:
            try:
                if check_due():
                    check_updates(auto=True)
            except Exception:  # noqa: BLE001 - a failed check must not end the loop
                log.exception("the firmware update check failed")
            if stop_checks.wait(600):
                return

    @asynccontextmanager
    async def lifespan(_app):
        if connect:
            link.start()
        sim.start()
        threading.Thread(target=update_check_loop, name="firmware update check", daemon=True).start()
        if os.environ.get("SUPERVISOR_TOKEN"):
            threading.Thread(target=lambda: app.state.ha_setup(), name="Home Assistant setup", daemon=True).start()
        yield
        stop_checks.set()
        sim.close()
        link.close()

    app = FastAPI(title="GeoCollar base", lifespan=lifespan)

    @app.middleware("http")
    async def refuse_other_sites(request: Request, call_next):
        # The phone API is not for browsers, and every request to it is sealed with a phone's key.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith(papi.PREFIX + "/") \
                and not same_site(request.headers):
            log.warning("refused a %s to %s from another site (%s)", request.method, request.url.path,
                        request.headers.get("origin"))
            return JSONResponse({"detail": "This request came from another website and was refused."}, status_code=403)
        return await call_next(request)

    add_phone_middleware(app)
    # Outermost: nothing of the page runs for a request it refuses.
    app.add_middleware(PageGuard, extra_hosts=parse_hosts(settings.get("allowed_hosts", "")),
                       in_ha=bool(os.environ.get("SUPERVISOR_TOKEN")))
    app.openapi = lambda: phone_openapi(app)
    app.state.store = store
    app.state.clog = clog
    app.state.sim = sim
    app.state.on_status = on_status
    app.state.on_hello = on_hello
    app.state.on_send_error = on_send_error
    app.state.on_app_ack = on_app_ack
    app.state.background = app_state
    # Leaflet and Leaflet-Geoman are vendored so the page works without a CDN (e.g. on a LAN).
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def require(node_id: str) -> dict:
        c = store.collar(node_id)
        if c is None:
            raise HTTPException(404, "unknown collar")
        return c

    @app.get("/")
    def index():
        # Always ask the base whether the page changed: a browser that kept an older page after
        # an update calls endpoints this version no longer has ("Method Not Allowed").
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

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
        latest = fw.identity_of_latest(store.setting("fw_latest"))
        for node_id in list(collars):
            c = collars[node_id] = public_collar(collars[node_id], node_id)
            c["sim"] = sim.public(node_id)
            # Newer collar firmware is known (see the update check) and this collar runs another one.
            c["update_available"] = not c["sim"] and fw.same_firmware(latest, c.get("firmware")) is False
        return collars

    @app.post("/api/collars")
    def add_collar(body: CollarIn):
        node_id = body.node_id.strip().lower()
        if not NODE_ID.fullmatch(node_id):
            raise HTTPException(400, "a node ID is ! and 8 hex digits, like !a1b2c3d4")
        try:
            name = clean_name(body.name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        c = store.upsert_collar(node_id, name or None)
        push_owner(node_id)
        if phones():  # the phones linked to the base may connect to this collar too
            apps_changed(node_id)
        found = store.setting("discovered", {}) or {}
        if found.pop(node_id, None) is not None:
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
    def update_collar(node_id: str, body: CollarPatch):
        """The collar's name is the dog's name: the lost beacon says it, and the collar takes it as
        its Meshtastic name. report_s is how often the collar reports, gps_inside_s how often it
        takes a GPS fix inside the fences."""
        require(node_id)
        out: dict = {}
        if body.report_s is not None:
            if not protocol.MIN_REPORT_S <= body.report_s <= protocol.MAX_REPORT_S:
                raise HTTPException(400, f"the report interval is {protocol.MIN_REPORT_S} to "
                                         f"{protocol.MAX_REPORT_S} seconds")
        if body.gps_inside_s is not None and not 0 <= body.gps_inside_s <= protocol.MAX_GPS_INSIDE_S:
            raise HTTPException(400, f"the GPS interval inside the fences is 0 to {protocol.MAX_GPS_INSIDE_S} seconds")
        if body.name is not None:
            try:
                name = clean_name(body.name)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            if not name:
                raise HTTPException(400, "give the dog a name")
            store.upsert_collar(node_id, name)
            out["owner_sent"] = push_owner(node_id)
        if body.report_s is not None:
            store.set_collar_info(node_id, "report_s", body.report_s)
        if body.gps_inside_s is not None:
            store.set_collar_info(node_id, "gps_inside_s", body.gps_inside_s)
        if body.gps_everywhere is not None:
            store.set_collar_info(node_id, "gps_everywhere", body.gps_everywhere or None)
        if body.report_s is not None or body.gps_inside_s is not None or body.gps_everywhere is not None:
            if (store.collar(node_id) or {}).get("config_sent") != config_for(node_id):
                out["config_sent"] = push_config(node_id)
        return {**(public_collar(store.collar(node_id), node_id) or {}), **out}

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

    def home_anchor_view() -> dict:
        """The home anchor setting, with what stands in its way: per collar, whether a running dog
        would get its first fix before reaching the bands; for the base radio, its Bluetooth."""
        a = dict(store.setting("home_anchor") or {"kind": "off", "address": "", "rssi": -75})
        a.setdefault("gps_restart_s", 30)
        kind = ANCHOR_KINDS.get(a.get("kind"), protocol.AnchorKind.NONE)
        at = anchor_position()
        out = {**a, "position": list(at) if at else None, "position_set": a.get("lat") is not None,
               "range_m": simmod.anchor_range_m(kind, int(a.get("rssi", -75))), "collars": [], "warnings": []}
        if kind == protocol.AnchorKind.NONE:
            return out
        need = simmod.home_room_needed_m(kind, int(a.get("rssi", -75)), a["gps_restart_s"])
        out["need_m"] = need
        for node_id, c in store.collars().items():
            row = {"node_id": node_id, "name": c.get("name") or node_id,
                   "home": bool((c.get("status") or {}).get("home"))}
            if c.get("fences") and at:
                try:
                    zones = protocol.zones_from_geojson(c["fences"])
                except (KeyError, ValueError):
                    zones = []
                if zones:
                    room = simmod.room_from_anchor_m(zones, *at)
                    row["room_m"] = round(room)
                    row["ok"] = room >= need
            out["collars"].append(row)
        if not at:
            out["warnings"].append("Set the base's home location (or place the anchor on the map) so the distance to "
                                   "the fences can be checked.")
        if kind == protocol.AnchorKind.BASE_RADIO:
            info = link.base_info() if link.connected and hasattr(link, "base_info") else {}
            if info.get("bluetooth") is False:
                out["warnings"].append("Bluetooth is off on the base radio, so collars cannot hear it. Turn it on in "
                                       "the Meshtastic app (Settings > Bluetooth), or use a beacon.")
            if str(info.get("connection", "")).startswith("network"):
                out["warnings"].append("The base radio is connected over the network. Meshtastic turns Bluetooth off "
                                       "while Wi-Fi is on, so collars cannot hear it. Connect it by USB, or use a beacon.")
        return out

    @app.get("/api/home-anchor")
    def get_home_anchor():
        return home_anchor_view()

    @app.put("/api/home-anchor")
    def put_home_anchor(body: HomeAnchorIn):
        """Home mode: while a collar hears this anchor strongly enough, and its last fix was inside,
        clear of every band, it turns its GPS off. Sent to every collar."""
        if body.kind not in ANCHOR_KINDS:
            raise HTTPException(400, f"kind is one of {', '.join(ANCHOR_KINDS)}")
        if not protocol.MIN_ANCHOR_RSSI <= body.rssi <= protocol.MAX_ANCHOR_RSSI:
            raise HTTPException(400, f"the signal threshold is {protocol.MIN_ANCHOR_RSSI} to "
                                     f"{protocol.MAX_ANCHOR_RSSI} dBm")
        address = ""
        if body.kind == "beacon":
            try:
                address = protocol.format_bt_address(protocol.parse_bt_address(body.address))
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if (body.lat is None) != (body.lon is None) or (
                body.lat is not None and not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180)):
            raise HTTPException(400, "give the anchor's position as both lat and lon, or neither")
        if body.gps_restart_s not in GPS_RESTART_CHOICES:
            raise HTTPException(400, f"the GPS restart time is one of {', '.join(map(str, GPS_RESTART_CHOICES))} s")
        store.set_setting("home_anchor", {"kind": body.kind, "address": address, "rssi": body.rssi,
                                          "lat": body.lat, "lon": body.lon, "gps_restart_s": body.gps_restart_s})
        if anchor_follows_home():
            base_home(fresh=True)  # a home moved in Home Assistant since the last save reaches the base now
        sent = [node_id for node_id in store.collars() if push_config(node_id)]
        return {**home_anchor_view(), "sent_to": sent}

    @app.delete("/api/collars/{node_id}")
    def remove_collar(node_id: str):
        # A collar leaving this base should not keep trusting phones this base linked to it.
        if app_keys_for(node_id, store.collar(node_id) or {}) and node_id not in sim and link.connected:
            try:
                link.send_app_keys(node_id, [], store.next_command_seq())
            except Exception as e:  # noqa: BLE001 - the collar is removed either way
                log.warning("could not tell %s to forget its linked phones: %s", node_id, e)
        sim.remove(node_id)
        if not store.remove_collar(node_id):
            raise HTTPException(404, "unknown collar")
        try:
            clog.clear(node_id)
        except (OSError, ValueError) as e:
            log.warning("could not remove the log of %s: %s", node_id, e)
        return {"removed": node_id}

    @app.delete("/api/collars/{node_id}/track")
    def clear_track(node_id: str):
        """Forget the path drawn behind the collar on the map. The log keeps its reports."""
        require(node_id)
        store.clear_track(node_id)
        return {"cleared": node_id}

    @app.get("/api/collars/{node_id}/log")
    def collar_log(node_id: str, since: float = 0, limit: int = 2000, after: float | None = None):
        """The collar's reports from `since` (seconds since 1970) on, oldest first; the newest
        `limit` of them if there are more. With `after`, only those later than that time are
        returned, while total still counts from `since`."""
        require(node_id)
        return clog.read(node_id, since, max(1, min(limit, 5000)), after)

    @app.delete("/api/collars/{node_id}/log")
    def clear_log(node_id: str):
        require(node_id)
        clog.clear(node_id)
        return {"cleared": node_id}

    @app.put("/api/collars/{node_id}/fences")
    def set_fences(node_id: str, geojson: dict):
        require(node_id)
        try:
            zones = protocol.zones_from_geojson(geojson)
            pulses = protocol.pulses_from_geojson(geojson)
            protocol.fence_chunks(zones, 1, pulses)  # validate size before storing
        except (KeyError, ValueError, TypeError, IndexError, AttributeError) as e:
            # Anything that is not the GeoJSON the page sends: a wrong type or a missing level.
            raise HTTPException(400, f"invalid fences: {e}") from e
        version = store.set_fences(node_id, geojson)
        sent = 0
        if node_id in sim:
            sim.set_fences(node_id, zones, version, pulses)
            sim.report_now(node_id)
            sent = len(protocol.fence_chunks(zones, version, pulses))
        elif link.connected:
            store.set_collar_info(node_id, "fences_sent", {"version": version, "at": time.time(), "tries": 0})
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
        if not 0 <= body.intensity <= 99:
            raise HTTPException(400, "the level is 0 to 99")
        if not 1 <= body.duration_ms <= MAX_CORRECTION_MS:
            raise HTTPException(400, f"the duration is 1 to {MAX_CORRECTION_MS} ms")
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

    # -- Companion app ----------------------------------------------------------------------------
    def apps_view(node_id: str) -> dict:
        c = store.collar(node_id) or {}
        return {"apps": public_apps(c), "synced": apps_synced(node_id, c), "max": protocol.MAX_APP_KEYS,
                "pin_known": bool(c.get("ble_pin")), "sim": node_id in sim}

    def code_collars() -> list[dict]:
        """Every collar on the base as a pairing code lists it: how a phone finds it."""
        out = []
        for node_id, c in sorted(store.collars().items(), key=lambda kv: (str(kv[1].get("name") or kv[0]).lower(), kv[0])):
            dog = c.get("name") or ""
            if dog == node_id:
                dog = ""
            out.append({"node_id": node_id, "dog": dog, "pin": c.get("ble_pin"),
                        "ble_name": protocol.mesh_names(dog)[1] if dog else "", "sim": node_id in sim})
        return out

    def phones_room() -> int:
        """How many more phones the base can link: every collar holds at most MAX_APP_KEYS keys."""
        legacy = max((len(c.get("apps") or []) for c in store.collars().values()), default=0)
        return max(0, protocol.MAX_APP_KEYS - legacy - len(phones()))

    # -- Linking codes. "Connect app" shows a QR code that is good once, for ENROLL_TTL_S (see
    # phoneapi). The phone then fetches its keys over the network. Codes live in memory only.
    enrolls: dict[bytes, dict] = {}  # token -> {"phone": id, "secret", "expires", "used"}
    enroll_lock = threading.Lock()
    seen_ivs: dict[str, dict[bytes, float]] = {}  # phone id -> request IVs seen lately -> when
    last_seen: dict[str, float] = {}  # phone id -> when it last used the API (written out now and then)

    def pending_code(phone_id: int) -> dict | None:
        now = time.time()
        with enroll_lock:
            for t in [t for t, e in enrolls.items() if e["expires"] < now - 3600]:
                del enrolls[t]  # an expired code is remembered an hour, to say so
            return next((e for e in enrolls.values() if e["phone"] == phone_id and not e["used"] and e["expires"] > now), None)

    def new_code(p: dict, request: Request | None) -> dict:
        """A one-time code for this phone, replacing any other it had."""
        secret, token = secrets.token_bytes(papi.SECRET_LEN), secrets.token_bytes(papi.TOKEN_LEN)
        urls = phone_urls(request)["urls"]
        code = papi.make_code(secret, token, home_id(), urls)
        expires = time.time() + papi.ENROLL_TTL_S
        with enroll_lock:
            for t in [t for t, e in enrolls.items() if e["phone"] == p["id"] and not e["used"]]:
                del enrolls[t]
            enrolls[token] = {"phone": p["id"], "secret": secret, "expires": expires, "used": False}
        return {"code": code, "expires": expires, "urls": urls}

    def phones_view() -> dict:
        collars = store.collars()
        out = []
        for p in phones():
            pid = f"{p['id']:08x}"
            # Collars added to the base after this phone last scanned a code: it does not know them
            # yet. A phone on the API fetches them by itself.
            missing = [n for n in collars if n not in (p.get("collars") or [])]
            pending = pending_code(p["id"])
            out.append({"id": pid, "name": p.get("name", ""), "created": p.get("created"), "missing": missing,
                        "api": bool(p.get("api_key")), "legacy": p.get("v") != 3,
                        "enrolled": p.get("enrolled"), "last_seen": last_seen.get(pid) or p.get("last_seen"),
                        "code_expires": pending["expires"] if pending else None})
        return {"phones": out, "max": protocol.MAX_APP_KEYS, "room": phones_room(),
                "synced": {n: apps_synced(n, c) for n, c in collars.items()}}

    def find_phone(phone_id: str) -> tuple[list[dict], dict]:
        ps = phones()
        for p in ps:
            if f"{p['id']:08x}" == phone_id.lower():
                return ps, p
        raise HTTPException(404, "unknown phone")

    def save_phone(p: dict) -> None:
        """Write back one phone's entry (the list may have changed since it was read)."""
        ps = phones()
        store.set_setting("phones", [p if x["id"] == p["id"] else x for x in ps])

    def all_apps_changed() -> None:
        for node_id in store.collars():
            apps_changed(node_id)

    @app.get("/api/phones")
    def list_phones():
        """The phones linked to the base, and the collars each one does not know about yet."""
        return phones_view()

    @app.post("/api/phones")
    def link_phone(body: AppIn, request: Request):
        """Link one more phone to every collar on the base: make it a key, send each collar the
        new list, and return a one-time code for the QR. The phone fetches its keys with it."""
        if not store.collars():
            raise HTTPException(409, "add a collar first")
        if phones_room() <= 0:
            raise HTTPException(409, f"each collar takes at most {protocol.MAX_APP_KEYS} phones; remove one first")
        name = " ".join("".join(ch for ch in body.name if ch.isprintable()).split())
        if len(name) > MAX_APP_NAME:
            raise HTTPException(400, f"the phone's name is at most {MAX_APP_NAME} characters")
        ps = phones()
        taken = {p["id"] for p in ps} | {a["id"] for c in store.collars().values() for a in c.get("apps") or []}
        key_id = 0
        while not key_id or key_id in taken:
            key_id = secrets.randbits(32)
        key = secrets.token_bytes(protocol.APP_KEY_LEN)
        listed = code_collars()
        entry = {"id": key_id, "name": name or f"Phone {len(ps) + 1}", "key": key.hex(), "created": time.time(),
                 "collars": [c["node_id"] for c in listed], "v": 3}
        store.set_setting("phones", ps + [entry])
        all_apps_changed()  # the collars start learning the key now; the phone gets it when it scans
        code = new_code(entry, request)
        return {**phones_view(), "phone": {"id": f"{key_id:08x}", "name": entry["name"]}, **code, "collars": listed}

    @app.post("/api/phones/{phone_id}/code")
    def phone_new_code(phone_id: str, request: Request):
        """A new one-time code for a phone already on the list: one whose code ran out, a phone
        that was reset or replaced, or one linked by an older base, to reach the base over the
        network. The phone keeps its key for the collars; whichever phone scans it next gets it."""
        ps, p = find_phone(phone_id)
        code = new_code(p, request)
        return {**phones_view(), "phone": {"id": phone_id.lower(), "name": p.get("name", "")}, **code,
                "collars": code_collars()}

    @app.post("/api/collars/{node_id}/apps")
    def link_app_from_older_page(node_id: str, body: AppIn, request: Request):
        """What "Connect app" on a page from before 0.19.7 calls, still in a browser's cache after
        an update: it links the phone to every collar on the base too, and answers in the shape
        that page reads."""
        require(node_id)
        r = link_phone(body, request)
        return {**apps_view(node_id), "app": r["phone"], "code": r["code"]}

    @app.post("/api/phones/{phone_id}/collars")
    def phone_collar_list(phone_id: str):
        """A code with the base's collars and no key, to bring a phone linked by an older base up
        to date after collars were added. Anyone could read it; without the phone's key it opens
        nothing. Phones on the API fetch the list themselves."""
        ps, p = find_phone(phone_id)
        listed = code_collars()
        p["collars"] = [c["node_id"] for c in listed]
        store.set_setting("phones", ps)
        return {**phones_view(), "phone": {"id": phone_id.lower(), "name": p.get("name", "")},
                "code": protocol.home_pairing_code(home_id(), listed), "collars": listed}

    @app.delete("/api/phones/{phone_id}")
    def unlink_phone(phone_id: str):
        """Take a phone's access away: its API key is deleted at once, and every collar gets the
        list without it."""
        ps, p = find_phone(phone_id)
        store.set_setting("phones", [x for x in ps if x is not p])
        with enroll_lock:
            for t in [t for t, e in enrolls.items() if e["phone"] == p["id"]]:
                del enrolls[t]
        seen_ivs.pop(phone_id.lower(), None)
        last_seen.pop(phone_id.lower(), None)
        all_apps_changed()
        return phones_view()

    # -- Where the phone app reaches the base: the Base URL the owner set, else Home Assistant's
    # own URL with the phone API's port, and the machine's address on the home network.
    ha_cache: dict = {}

    def ha_info() -> dict:
        if ha_cache.get("at", 0) > time.time() - 300:
            return ha_cache["info"]
        info = papi.ha_lookup(settings.get("supervisor_fetch"))
        ha_cache.update(at=time.time(), info=info)
        return info

    def phone_urls(request: Request | None = None) -> dict:
        """The addresses a phone is given, best first, with where each came from and what may
        stand in the way."""
        port = int(settings.get("phone_port") or papi.DEFAULT_PORT)
        items: list[dict] = []
        warnings: list[str] = []
        own = store.setting("phone_url") or settings.get("phone_url") or ""
        if own:
            items.append({"url": own, "source": "set", "label": "the Base URL set here"})
        # Through Home Assistant's own address, when the GeoCollar integration is set up there: it
        # tells the base its addresses (PUT /api/ha-link) on start and every hour.
        hl = store.setting("ha_link") or {}
        through_ha = bool(hl.get("at")) and time.time() - hl["at"] < HA_LINK_FRESH_S
        if through_ha:
            if hl.get("external"):
                items.append({"url": hl["external"], "source": "external_ha",
                              "label": "Home Assistant's own address, through the GeoCollar integration"})
            if hl.get("internal"):
                items.append({"url": hl["internal"], "source": "internal_ha",
                              "label": "Home Assistant on your network, through the GeoCollar integration"})
        ha = ha_info()
        if through_ha:
            pass  # the port is only a way in at home now
        elif ha:
            host_port = ha.get("port")
            if ha.get("port_known") and not host_port:
                warnings.append(f"The phone app's port ({port}) is turned off in this app's Network settings in "
                                "Home Assistant, so phones can't reach the base. Turn it on there and restart the app.")
            hp = int(host_port or port)
            if not own:
                warnings.append("The GeoCollar integration is not set up in Home Assistant yet, so phones can't reach "
                                "the base through Home Assistant's own address. " + ha_integration_hint())
            for key, label in (("external_url", "Home Assistant's external URL"), ("internal_url", "Home Assistant's internal URL")):
                host = urlsplit(ha.get(key) or "").hostname
                if not host or host.endswith(".ui.nabu.casa"):
                    continue  # Home Assistant Cloud passes on Home Assistant itself only, not a port
                items.append({"url": papi.with_port(host, hp), "source": key.split("_")[0] + "_ha", "label": label})
            if ha.get("ip"):
                items.append({"url": papi.with_port(ha["ip"], hp), "source": "ip", "label": "this machine's address on your network"})
        if ha and through_ha and ha.get("ip") and ha.get("port"):
            items.append({"url": papi.with_port(ha["ip"], int(ha["port"])), "source": "ip",
                          "label": "this machine's address on your network, on the phone port"})
        elif not ha:
            # Docker: the address the page was opened on (not through Home Assistant), else this
            # machine's own.
            host = None
            if request is not None and not request.headers.get("x-ingress-path"):
                host = urlsplit("//" + (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")).hostname
            if host and host not in ("localhost", "127.0.0.1", "::1", "testserver"):
                items.append({"url": papi.with_port(host, port), "source": "page", "label": "the address this page is open on"})
            ip = papi.local_ip()
            if ip and not ip.startswith("127."):
                items.append({"url": papi.with_port(ip, port), "source": "ip",
                              "label": "this machine's address (in Docker, the container's: set the Base URL if phones can't reach it)"})
        seen, urls = set(), []
        for it in items:
            if it["url"] not in seen:
                seen.add(it["url"])
                urls.append(it["url"])
        urls = urls[:3]
        if not any(not papi.is_private_host(urlsplit(u).hostname or "") for u in urls):
            warnings.append("Every address here is on your home network: away from home the app can't reach the base. "
                            "Set a Base URL that works from the internet to see the dogs from anywhere.")
        return {"urls": urls, "items": [it for it in items if it["url"] in urls], "warnings": warnings,
                "port": port, "url": own, "ha": bool(ha), "through_ha": through_ha,
                "integration": ha_state.get("install")}

    HA_LINK_FRESH_S = 3 * 3600  # the integration says so every hour
    ha_state: dict = {}  # "install": what install_integration did at start

    def ha_integration_hint() -> str:
        done = ha_state.get("install")
        if done in ("installed", "updated"):
            return "This app has just put it in Home Assistant: restart Home Assistant, then add GeoCollar where it says Discovered (Settings > Devices & services)."
        if done == "current":
            return "It is installed: add GeoCollar where it says Discovered under Settings > Devices & services (restart Home Assistant if it is not there)."
        return ("Copy the folder custom_components/geocollar from this app's repository into Home Assistant's "
                "config folder, restart Home Assistant and add GeoCollar under Settings > Devices & services.")

    def ha_setup() -> None:
        """In Home Assistant: put the GeoCollar integration in place and say where this app is,
        so Home Assistant offers to set it up. Runs once at start, in the background."""
        try:
            ha_state["install"] = papi.install_integration(Path(settings.get("ha_config_dir") or "/homeassistant"))
            log.info("Home Assistant integration: %s", ha_state["install"])
        except OSError as e:
            ha_state["install"] = "no_access"
            log.warning("could not put the GeoCollar integration in Home Assistant: %s", e)
        port = int(settings.get("phone_port") or papi.DEFAULT_PORT)
        if not papi.announce(settings.get("supervisor_fetch"), port, int(os.environ.get("GEOCOLLAR_PORT", "8099"))):
            log.warning("could not announce this app to Home Assistant (no GeoCollar discovery)")

    app.state.ha_setup = ha_setup

    @app.put("/api/ha-link")
    def put_ha_link(body: HaLinkIn):
        """From the GeoCollar integration in Home Assistant: Home Assistant's addresses, with the
        path the phone API is passed on at. Phones are given them."""
        out = {}
        for key in ("external", "internal"):
            v = getattr(body, key)
            try:
                v = papi.clean_url(v or "")
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            if v and not urlsplit(v).path.endswith(papi.HA_PATH):
                raise HTTPException(400, f"the address must end in {papi.HA_PATH}")
            out[key] = v or None
        before = store.setting("ha_link") or {}
        if {k: before.get(k) for k in out} != out or time.time() - before.get("at", 0) > 600:
            store.set_setting("ha_link", {**out, "at": time.time()})
        return phone_urls()

    @app.get("/api/phone-access")
    def get_phone_access(request: Request):
        """The Base URL setting, and the addresses phones are given."""
        return phone_urls(request)

    @app.put("/api/phone-access")
    def put_phone_access(body: PhoneAccessIn, request: Request):
        """Set (or clear, with an empty url) the Base URL. Linked phones get the new addresses the
        next time they reach the base."""
        try:
            url = papi.clean_url(body.url)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        store.set_setting("phone_url", url or None)
        return phone_urls(request)

    # -- The phone API itself (see phoneapi.py and docs/phone-api.md) -----------------------------
    phone_router = APIRouter(prefix=papi.PREFIX, tags=["phone"])
    # Descriptions spelled out: Python's own names for the codes differ between versions.
    ERRORS = {s: {"model": papi.ApiError, "description": d} for s, d in (
        (400, "Damaged request"), (401, "Unknown phone, or clock too far off"), (403, "Does not match the code"),
        (409, "Seen before"), (410, "Code ran out or was used"), (413, "Too large"))}

    def fail(status: int, code: str, detail: str, **extra) -> JSONResponse:
        return JSONResponse({"detail": detail, "code": code, **extra}, status_code=status)

    def server_ms() -> int:
        return int(time.time() * 1000)

    def note_seen(pid: str, p: dict) -> None:
        now = time.time()
        last_seen[pid] = now
        if now - (p.get("last_seen") or 0) > 600:  # on disk now and then, not on every call
            p["last_seen"] = now
            save_phone(p)

    def phone_call(op: str, env: papi.PhoneRequest):
        """Open a phone's request: (phone entry, request body, reply function), or an error response."""
        ps = phones()
        p = next((x for x in ps if f"{x['id']:08x}" == env.phone and x.get("api_key")), None)
        if p is None:
            return fail(401, "unknown_phone", "This phone is not linked to the base (it may have been removed).")
        key = bytes.fromhex(p["api_key"])
        try:
            plain = papi.unseal(key, papi.request_aad(op, env.phone), env.model_dump())
            ts = int(plain["ts"])
            body = plain.get("body") or {}
        except (ValueError, KeyError, TypeError):
            return fail(401, "unknown_phone", "The request does not open with this phone's key. Link the phone again.")
        now = time.time()
        if abs(ts / 1000 - now) > papi.CLOCK_SKEW_S:
            return fail(401, "clock", "The phone's clock is too far from the base's.", server_time=server_ms())
        iv = papi.b64d(env.iv)
        with enroll_lock:
            ivs = seen_ivs.setdefault(env.phone, {})
            for old in [k for k, t in ivs.items() if t < now - 2 * papi.CLOCK_SKEW_S]:
                del ivs[old]
            if iv in ivs:
                return fail(409, "replay", "This request was seen before.")
            ivs[iv] = now
        note_seen(env.phone, p)

        def reply(obj) -> dict:
            return papi.seal(key, papi.reply_aad(op, env.phone, iv), obj)
        return p, body, reply

    def plaintext(op: str) -> dict:
        req, rep = papi.OPERATIONS[op]
        return {"x-plaintext": {"request": {"$ref": f"#/components/schemas/{req.__name__}"},
                                "reply": {"$ref": f"#/components/schemas/{rep.__name__}"}}}

    @phone_router.post("/enroll", response_model=papi.EnrollReply, responses=ERRORS, operation_id="enroll",
                       summary="Link this phone with a Connect app code",
                       openapi_extra={"x-plaintext": {"reply": {"$ref": "#/components/schemas/Welcome"}}})
    def phone_enroll(body: papi.EnrollRequest):
        """Link a phone with the one-time code from the QR code (see the description above)."""
        try:
            token, phone_pub, proof = papi.b64d(body.token), papi.b64d(body.phone_key), papi.b64d(body.proof)
        except ValueError:
            return fail(400, "bad_request", "The request is damaged.")
        with enroll_lock:
            e = enrolls.get(token)
            if e is None or e["expires"] < time.time():
                return fail(410, "expired", "This code has run out. Show a new one under Connect app on the base.")
            if not hmac.compare_digest(proof, papi.enroll_proof(e["secret"], token, phone_pub)):
                return fail(403, "bad_request", "The request does not match the code.")
            if e["used"]:
                return fail(410, "used", "This code was used already. Show a new one under Connect app on the base.")
            mine = papi.new_private_key()
            try:
                shared = papi.ecdh(mine, phone_pub)
            except ValueError:
                return fail(400, "bad_request", "The phone's key is not a P-256 public key.")
            e["used"] = True
        ps = phones()
        p = next((x for x in ps if x["id"] == e["phone"]), None)
        if p is None:
            return fail(410, "expired", "This phone was removed on the base.")
        base_pub = papi.public_bytes(mine)
        key = papi.api_key(shared, e["secret"], token, phone_pub, base_pub)
        listed = code_collars()
        p.update(api_key=key.hex(), enrolled=time.time(), v=3, collars=[c["node_id"] for c in listed])
        save_phone(p)
        pid = f"{p['id']:08x}"
        seen_ivs.pop(pid, None)
        log.info("phone %s (%s) linked over the network", pid, p.get("name"))
        welcome = {"phone_id": pid, "home": home_id(), "ble_key_id": p["id"],
                   "ble_key": papi.b64e(bytes.fromhex(p["key"])), "urls": phone_urls()["urls"],
                   "collars": listed, "server_time": server_ms()}
        return {"base_key": papi.b64e(base_pub), **papi.seal(key, papi.welcome_aad(token), welcome)}

    STATUS_FIELDS = tuple(papi.CollarStatus.model_fields)

    def phone_status(c: dict) -> dict | None:
        s = c.get("status")
        if not s or s.get("state") not in protocol.CollarState.__members__:
            return None
        out = {k: s[k] for k in STATUS_FIELDS if s.get(k) is not None}
        out.update(received=s.get("received") or 0, via="sim" if c.get("sim") else s.get("via") or "mesh",
                   via_phone=s.get("via_phone"), fix_age_s=s.get("fix_age_s"))
        return out

    @phone_router.post("/collars", response_model=papi.Envelope, responses=ERRORS, openapi_extra=plaintext("collars"),
                       operation_id="collars", summary="Every collar with its last report")
    def phone_collars(env: papi.PhoneRequest):
        """Every collar on the base with its last report: what the app shows when it is not near
        the dog. Also the addresses to use from now on."""
        r = phone_call("collars", env)
        if isinstance(r, JSONResponse):
            return r
        p, _, reply = r
        all_c = store.collars()
        listed = code_collars()
        ids = [c["node_id"] for c in listed]
        if p.get("collars") != ids:  # it has every collar now: nothing "missing" on the page
            p["collars"] = ids
            save_phone(p)
        out = [{**c, "status": phone_status(all_c.get(c["node_id"]) or {})} for c in listed]
        return reply({"home": home_id(), "urls": phone_urls()["urls"], "collars": out, "server_time": server_ms()})

    RELAY_EVERY_S = 30
    relayed: dict[str, float] = {}

    def mesh_quiet(c: dict) -> bool:
        """Whether the base has gone without the collar's own reports for longer than it should:
        two of its report intervals and a minute (30 s each while it is out or lost)."""
        s = c.get("status") or {}
        heard = c.get("heard_direct")
        if heard is None and s.get("via") != "phone":
            heard = s.get("received")  # a report from before the base kept this
        if not heard:
            return True
        every = 30 if s.get("state") in ("OUTSIDE", "LOST") or s.get("beacon_active") else (c.get("report_s") or 600)
        return time.time() - heard > 2 * every + 60

    @phone_router.post("/relay", response_model=papi.Envelope, responses=ERRORS, openapi_extra=plaintext("relay"),
                       operation_id="relay", summary="Pass on a collar's report heard over Bluetooth")
    def phone_relay(env: papi.PhoneRequest):
        """A collar's report that the phone got over Bluetooth. The base keeps it when it has not
        heard the collar over the mesh lately; otherwise the collar is getting through itself."""
        r = phone_call("relay", env)
        if isinstance(r, JSONResponse):
            return r
        p, body, reply = r
        try:
            req = papi.RelayRequest.model_validate(body)
        except ValueError:
            return fail(400, "bad_request", "The report is damaged.")
        c = store.collar(req.node_id)
        if c is None:
            return reply({"recorded": False, "reason": "unknown_collar", "next_s": 3600})
        if c.get("sim") or not mesh_quiet(c):
            return reply({"recorded": False, "reason": "heard_over_mesh", "next_s": 120})
        if req.age_ms > 120_000:
            return reply({"recorded": False, "reason": "too_old", "next_s": RELAY_EVERY_S})
        if time.time() - relayed.get(req.node_id, 0) < RELAY_EVERY_S - 5:
            return reply({"recorded": False, "reason": "too_soon", "next_s": RELAY_EVERY_S})
        relayed[req.node_id] = time.time()
        status = req.status.model_dump()
        if status["battery_pct"] is None:
            status["battery_pct"] = 255
        # A fix more than two minutes old is not where the dog is now: shown as the last known.
        if req.fix_age_s is None or req.fix_age_s > 120:
            status["no_fix"] = True
        status.update(msg_type="STATUS", via="phone", via_phone=p.get("name") or f"{p['id']:08x}",
                      fix_age_s=req.fix_age_s)
        if req.phone_position:
            status["phone_pos"] = req.phone_position.model_dump()
        recorded = store.record_status(req.node_id, status, direct=False)
        if recorded:
            entry = {**log_entry(recorded), "via": "phone"}
            try:
                clog.add(req.node_id, entry)
            except (OSError, ValueError) as e:
                log.warning("could not log a report from %s: %s", req.node_id, e)
        log.info("%s passed on a report from %s over Bluetooth", p.get("name"), req.node_id)
        return reply({"recorded": True, "reason": "recorded", "next_s": RELAY_EVERY_S})

    app.include_router(phone_router)
    phone_app = FastAPI(title="GeoCollar phone API", version="1.0.0", description=papi.API_DESCRIPTION,
                        openapi_url=papi.PREFIX + "/openapi.json", docs_url=papi.PREFIX + "/docs", redoc_url=None)
    phone_app.include_router(phone_router)
    add_phone_middleware(phone_app)
    phone_app.openapi = lambda: phone_openapi(phone_app)
    app.state.phone_app = phone_app

    @app.get("/api/collars/{node_id}/apps")
    def list_apps(node_id: str):
        """The phones linked to this collar alone (by an older base), and whether the collar has
        the list of phones yet."""
        require(node_id)
        return apps_view(node_id)

    @app.delete("/api/collars/{node_id}/apps/{app_id}")
    def unlink_app(node_id: str, app_id: str):
        """Take a phone linked to this collar alone off it: the collar gets the list without it."""
        c = require(node_id)
        apps = list(c.get("apps") or [])
        keep = [a for a in apps if f"{a['id']:08x}" != app_id.lower()]
        if len(keep) == len(apps):
            raise HTTPException(404, "unknown app")
        store.set_collar_info(node_id, "apps", keep)  # an empty list is kept: the collar must hear of it
        apps_changed(node_id)
        return apps_view(node_id)

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
                "build": m["id"], **fw.identity(m), "flashed_at": time.time(), "via": via,
            })
            store.set_collar_info(node_id, "update_mode", None)

    # -- Checking for newer collar firmware -------------------------------------------------------
    # The check only tells the owner: it reads the manifest of the newest build and compares it
    # with what each collar was last flashed with. Nothing is downloaded or flashed by it; pressing
    # Flash gets the newest build by itself.
    def check_every() -> str:
        every = store.setting("fw_check_every", "weekly")
        return every if every in fw.CHECK_EVERY else "weekly"

    def note_latest(m: dict) -> None:
        store.set_setting("fw_latest", {**fw.identity(m), "subject": m.get("geocollar_subject") or "",
                                        "built_at": m.get("built_at"), "checked_at": time.time()})
        store.set_setting("fw_check_error", None)
        period = fw.CHECK_EVERY[check_every()]
        store.set_setting("fw_check_next", time.time() + period if period else None)

    def update_status() -> dict:
        latest = store.setting("fw_latest")
        ident = fw.identity_of_latest(latest)
        outdated, unknown = [], []
        for node_id, c in store.collars().items():
            if c.get("sim"):
                continue
            same = fw.same_firmware(ident, c.get("firmware"))
            if same is False:
                outdated.append({"node_id": node_id, "name": c["name"]})
            elif same is None:
                unknown.append({"node_id": node_id, "name": c["name"]})
        return {"every": check_every(), "latest": latest, "error": store.setting("fw_check_error"),
                "outdated": outdated if latest else [], "unknown": unknown if latest else []}

    def check_updates(auto: bool = False) -> dict:
        try:
            m = fw.latest_manifest(repo)
        except fw.FirmwareError as e:
            store.set_setting("fw_check_error", str(e))
            # By itself, the next try is in six hours at the earliest, not at the next look.
            store.set_setting("fw_check_next", time.time() + 6 * 3600 if fw.CHECK_EVERY[check_every()] else None)
            return update_status()
        note_latest(m)
        # A collar flashed with a build from before 0.18 has no source ID, only a commit. Ask
        # GitHub for it once, so that a base-only commit does not count as new collar firmware.
        asked: dict[str, str | None] = {}
        for node_id, c in store.collars().items():
            f = c.get("firmware")
            if c.get("sim") or not f or f.get("source") or not f.get("commit") or len(asked) >= 8:
                continue
            if f["commit"] not in asked:
                asked[f["commit"]] = fw.source_of_commit(repo, f["commit"])
            if asked[f["commit"]]:
                store.set_collar_info(node_id, "firmware", {**f, "source": asked[f["commit"]]})
        status = update_status()
        # Once per new firmware, not once per commit: a base-only commit makes a new build too.
        key = f"{m.get('geocollar_source') or m.get('geocollar_commit')}/{m.get('meshtastic_version')}"
        if auto and status["outdated"] and store.setting("fw_notified") != key:
            names = ", ".join(c["name"] for c in status["outdated"])
            fw.notify_home_assistant(
                "GeoCollar: collar firmware update",
                f"Newer collar firmware is available for {names}. Plug the collar into USB, open GeoCollar's "
                "Settings tab and press Flash: the firmware is downloaded by itself.", "geocollar_firmware")
            store.set_setting("fw_notified", key)
        return status

    def check_due() -> bool:
        return bool(fw.CHECK_EVERY[check_every()]) and time.time() >= (store.setting("fw_check_next") or 0)

    app.state.check_updates, app.state.check_due = check_updates, check_due

    def get_latest(job) -> dict:
        m, is_latest = fw.latest_build(job, builds, repo)
        if is_latest:
            note_latest(m)
        return m

    @app.get("/api/firmware")
    def firmware_overview():
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
            "can_build": bool(shutil.which("git")),
            "update": update_status(),
            # The network that collars may still hold from Wi-Fi updates (see forget-wifi), or None.
            "wifi_leftover": wifi_leftover(),
        }

    @app.post("/api/firmware/check")
    def firmware_check():
        """Look for newer collar firmware now. Only reports; see check_updates."""
        return check_updates()

    @app.put("/api/firmware/check-every")
    def firmware_check_every(body: CheckEveryIn):
        if body.every not in fw.CHECK_EVERY:
            raise HTTPException(400, "every must be never, daily, weekly or monthly")
        store.set_setting("fw_check_every", body.every)
        period = fw.CHECK_EVERY[body.every]
        last = (store.setting("fw_latest") or {}).get("checked_at") or 0
        store.set_setting("fw_check_next", last + period if period else None)
        return update_status()

    @app.post("/api/firmware/download")
    def firmware_download():
        """Get the latest build onto this machine. Flashing from the browser starts with this; the
        job's result is the build's manifest."""
        return start("download", "Get the latest firmware", get_latest)

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

    @app.post("/api/firmware/forget-wifi")
    def firmware_forget_wifi():
        """Wi-Fi updates are gone, but an older version of this app saved the home Wi-Fi name and
        password on every collar it updated that way, and they are still there. This asks each
        collar for its network settings over the mesh and blanks that network where it finds it.
        A collar that had it restarts once. Collars out of reach are left for another run."""
        left = store.setting("wifi_leftover") or {}
        ssid = left.get("ssid")
        if not ssid:
            raise HTTPException(409, "there is no saved Wi-Fi network to remove")
        if not link.connected:
            raise HTTPException(409, "the base station's radio is not connected")

        def run(job):
            todo = {k: c for k, c in store.collars().items() if not c.get("sim") and not c.get("wifi_forgotten")}
            removed, missed = [], []
            for n, (node_id, c) in enumerate(todo.items()):
                job.check_cancel()
                job.set_step(f"Asking {c['name']} for its network settings (over the mesh, can take a minute)",
                             n / max(1, len(todo)))
                try:
                    had = link.forget_wifi(node_id, ssid)
                except Exception as e:  # noqa: BLE001 - out of range, asleep, or the radio dropped out
                    job.log(f"{c['name']}: no answer ({e})")
                    missed.append(c["name"])
                    continue
                store.set_collar_info(node_id, "wifi_forgotten", True)
                job.log(f"{c['name']}: " + ("removed, it is restarting" if had else "did not have it"))
                if had:
                    removed.append(c["name"])
            if not missed:
                store.set_setting("wifi_leftover", None)
            job.set_step((f"Removed from {', '.join(removed)}. " if removed else "No collar had it. ")
                         + (f"No answer from {', '.join(missed)}: try again when in range." if missed else ""), 1.0)
            return {"removed": removed, "missed": missed}

        return start("wifi", f"Remove the Wi-Fi network \"{ssid}\" from the collars", run)

    @app.post("/api/firmware/flash-usb")
    def firmware_flash_usb(body: UsbFlashIn):
        picked = build_or_404(body.build) if body.build else None

        def run(job):
            m = picked or get_latest(job)
            result = fw.flash_usb(job, builds, m["id"], body.port, body.mode, settings.get("mesh_serial", ""))
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
                # A collar that esptool could not restart waits for someone to press RST: allow for that.
                manual = bool(result.get("manual_reset"))
                result.update(fw.setup_collar(job, body.port, getattr(link, "iface", None), name, lock=body.lock,
                                              boot_wait_s=180 if manual else 60, press_reset=manual))
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
                                         open_iface=lambda _port: fw.open_socket_node(stream), boot_wait_s=180,
                                         lock=lock)  # 180 s: the page may have to ask for the collar again
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
        return {"node_id": node_id, "new_collar": new, "collar": public_collar(store.collar(node_id), node_id)}

    @app.post("/api/collars/{node_id}/firmware/flashed")
    def firmware_flashed(node_id: str, body: FlashedIn):
        require(node_id)
        record_flash(node_id, build_or_404(body.build), body.via)
        return store.collar(node_id).get("firmware")

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
        try:
            name = clean_name(body.name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        node_id = "!5a" + secrets.token_hex(3)  # looks like a node ID; 5a = "sim"
        n = 1 + sum(1 for c in store.collars().values() if c.get("sim"))
        store.upsert_collar(node_id, name or f"Simulated dog {n}")
        settings = sim.add(node_id, {"lat": body.lat, "lon": body.lon}).settings()
        push_owner(node_id)
        if phones():
            apps_changed(node_id)
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
    """The page (and everything else) on GEOCOLLAR_PORT; the phone API alone on
    GEOCOLLAR_PHONE_PORT too, so that port can be opened to the internet without the page, which
    has no login of its own."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    app = create_app(settings)
    port = int(os.environ.get("GEOCOLLAR_PORT", "8099"))
    phone_port = int(settings.get("phone_port") or papi.DEFAULT_PORT)
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port))]
    if phone_port and phone_port != port:
        phone = uvicorn.Server(uvicorn.Config(app.state.phone_app, host="0.0.0.0", port=phone_port,
                                              lifespan="off", log_level="warning"))
        phone.capture_signals = contextlib.nullcontext  # the page's server takes Ctrl+C and SIGTERM
        servers.append(phone)

    async def run() -> None:
        main_task = asyncio.ensure_future(servers[0].serve())
        others = [asyncio.ensure_future(s.serve()) for s in servers[1:]]
        await main_task
        for s in servers[1:]:
            s.should_exit = True
        await asyncio.gather(*others, return_exceptions=True)

    asyncio.run(run())
