# SPDX-License-Identifier: GPL-3.0-or-later
"""GeoCollar wire format. Must match firmware/GeoCollar/Protocol.cpp; see docs/protocol.md."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

PORT_NUM = 300
VERSION = 1
MAX_CHUNK_DATA = 180
MAX_ZONES = 4
MAX_VERTICES = 32
MAX_CHUNKS = 8


class MsgType(IntEnum):
    FENCE_CHUNK = 1
    STATUS = 2
    BREACH = 3
    COMMAND = 4


class CommandAction(IntEnum):
    SHOCK = 1
    VIBRATE = 2
    BEEP = 3
    CORRECTIONS_OFF = 10
    CORRECTIONS_ON = 11
    FORCE_LOST = 12
    CLEAR_LOST = 13
    REQUEST_STATUS = 14


class CollarState(IntEnum):
    NO_FENCE = 0
    INSIDE = 1
    WARNING = 2
    OUTSIDE = 3
    LOST = 4


class ZoneKind(IntEnum):
    KEEP_IN = 0
    KEEP_OUT = 1


@dataclass
class Zone:
    kind: ZoneKind
    warn_margin_m: int
    vertices: list[tuple[float, float]]  # (lat, lon) in degrees


@dataclass
class Status:
    msg_type: MsgType
    fence_version: int
    state: CollarState
    battery_pct: int
    lat: float
    lon: float
    pdop: float
    sats: int
    clearance_m: int
    corrections_enabled: bool
    shock_enabled: bool


_STATUS = struct.Struct("<BBHBBiiHBhB")
_COMMAND = struct.Struct("<BBBBH")
_CHUNK_HEADER = struct.Struct("<BBHBB")


def _to_i(deg: float) -> int:
    return int(round(deg * 1e7))


def encode_command(action: CommandAction, intensity: int = 0, duration_ms: int = 1000) -> bytes:
    if not 0 <= intensity <= 99:
        raise ValueError("intensity must be 0..99")
    return _COMMAND.pack(VERSION, MsgType.COMMAND, action, intensity, duration_ms)


def decode_status(data: bytes) -> Status:
    if len(data) != _STATUS.size:
        raise ValueError(f"status must be {_STATUS.size} bytes, got {len(data)}")
    v, t, fv, st, bat, lat, lon, pdop, sats, clr, flags = _STATUS.unpack(data)
    if v != VERSION or t not in (MsgType.STATUS, MsgType.BREACH):
        raise ValueError("not a GeoCollar status message")
    return Status(
        msg_type=MsgType(t),
        fence_version=fv,
        state=CollarState(st),
        battery_pct=bat,
        lat=lat / 1e7,
        lon=lon / 1e7,
        pdop=pdop / 100,
        sats=sats,
        clearance_m=clr,
        corrections_enabled=bool(flags & 1),
        shock_enabled=bool(flags & 2),
    )


def encode_status(s: Status) -> bytes:
    """Mostly for tests and simulators: the collar is what normally sends these."""
    flags = (1 if s.corrections_enabled else 0) | (2 if s.shock_enabled else 0)
    return _STATUS.pack(
        VERSION, s.msg_type, s.fence_version, s.state, s.battery_pct, _to_i(s.lat), _to_i(s.lon),
        int(round(s.pdop * 100)), s.sats, s.clearance_m, flags,
    )


def message_type(data: bytes) -> MsgType | None:
    if len(data) < 2 or data[0] != VERSION or data[1] not in (1, 2, 3, 4):
        return None
    return MsgType(data[1])


def encode_fence_set(zones: Iterable[Zone]) -> bytes:
    zones = list(zones)
    if len(zones) > MAX_ZONES:
        raise ValueError(f"at most {MAX_ZONES} zones")
    out = bytearray([len(zones)])
    for z in zones:
        if not 3 <= len(z.vertices) <= MAX_VERTICES:
            raise ValueError(f"a zone needs 3..{MAX_VERTICES} vertices")
        out += struct.pack("<BBH", z.kind, len(z.vertices), z.warn_margin_m)
        for lat, lon in z.vertices:
            out += struct.pack("<ii", _to_i(lat), _to_i(lon))
    return bytes(out)


def fence_chunks(zones: Iterable[Zone], fence_version: int) -> list[bytes]:
    """Split a fence set into packets the collar reassembles."""
    blob = encode_fence_set(zones)
    count = max(1, math.ceil(len(blob) / MAX_CHUNK_DATA))
    if count > MAX_CHUNKS:
        raise ValueError("fence set too large")
    return [
        _CHUNK_HEADER.pack(VERSION, MsgType.FENCE_CHUNK, fence_version & 0xFFFF, i, count)
        + blob[i * MAX_CHUNK_DATA:(i + 1) * MAX_CHUNK_DATA]
        for i in range(count)
    ]


def zones_from_geojson(geojson: dict) -> list[Zone]:
    """Accept a FeatureCollection (or one Feature) of Polygons.

    Feature properties: kind = "keep_in" (default) or "keep_out"; warn_margin_m (default 10);
    any others (such as the web page's zone name) are kept by the store but not sent.
    GeoJSON uses [lon, lat]; the closing vertex is dropped.
    """
    features = geojson["features"] if geojson.get("type") == "FeatureCollection" else [geojson]
    zones = []
    for f in features:
        geom = f.get("geometry") or {}
        if geom.get("type") != "Polygon":
            raise ValueError("only Polygon features are supported")
        ring = geom["coordinates"][0]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        for lon, lat in ring:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"coordinate out of range: {lat}, {lon}")
        props = f.get("properties") or {}
        kind_name = props.get("kind", "keep_in")
        if kind_name not in ("keep_in", "keep_out"):
            raise ValueError(f"unknown zone kind {kind_name!r}")
        kind = ZoneKind.KEEP_OUT if kind_name == "keep_out" else ZoneKind.KEEP_IN
        margin = props.get("warn_margin_m", 10)
        if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not 0 <= margin <= 0xFFFF:
            raise ValueError("warn_margin_m must be 0..65535")
        zones.append(Zone(kind, int(margin), [(lat, lon) for lon, lat in ring]))
    return zones
