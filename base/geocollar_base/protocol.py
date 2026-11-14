# SPDX-License-Identifier: GPL-3.0-or-later
"""GeoCollar wire format. Must match firmware/GeoCollar/Protocol.cpp; see docs/protocol.md."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

PORT_NUM = 300
VERSION = 2  # 2: zones carry danger / warning / attention band widths
MAX_CHUNK_DATA = 180
MAX_ZONES = 4
MAX_VERTICES = 32
MAX_CHUNKS = 8
MAX_BAND_M = 255  # each band width is one byte on the wire
MAX_PULSE_S = 10
MIN_PULSE_S = 2
MAX_DOG_NAME = 24  # bytes of UTF-8, as in firmware/GeoCollar/Protocol.h
MAX_OWNER_NAME = 24
MAX_PHONE = 20
MAX_SHOCK_LEVEL = 30  # the collar firmware's cap (Settings::maxShockLevel)


class MsgType(IntEnum):
    FENCE_CHUNK = 1
    STATUS = 2
    BREACH = 3
    COMMAND = 4
    OWNER = 5  # base -> collar: the dog's name and the owner's contact, for the lost beacon
    HELLO = 6  # collar -> broadcast: runs GeoCollar, not adopted by a base yet


class CommandAction(IntEnum):
    SHOCK = 1
    VIBRATE = 2
    BEEP = 3
    CORRECTIONS_OFF = 10
    CORRECTIONS_ON = 11
    FORCE_LOST = 12
    CLEAR_LOST = 13
    REQUEST_STATUS = 14
    SILENCE_LOST = 15  # stop the lost beacon until the dog is next back inside
    SHOCK_ON = 16  # allow shock at level `intensity`, capped by the collar's maximum
    SHOCK_OFF = 17


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
    """A fence polygon. The line is the outer edge of its bands, which run from the line into
    the allowed side: danger (shock; 0 = none), then warning (vibrate), then attention (beep)."""

    kind: ZoneKind
    danger_m: int
    warning_m: int
    attention_m: int
    vertices: list[tuple[float, float]]  # (lat, lon) in degrees

    @property
    def band_width_m(self) -> int:
        return self.danger_m + self.warning_m + self.attention_m


@dataclass
class Pulses:
    """Repeat beep / vibrate every this many seconds while the dog stays in that band
    (0 = once per GPS fix only). Sent with the fences."""

    beep_s: int = 0
    vibrate_s: int = 0


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
    beacon_active: bool = False  # lost beacon on: Bluetooth name and mesh plea
    beacon_silenced: bool = False


_STATUS = struct.Struct("<BBHBBiiHBhB")
_COMMAND = struct.Struct("<BBBBHI")  # ... duration ms, then a sequence number (anti-replay)
_CHUNK_HEADER = struct.Struct("<BBHBB")


def _to_i(deg: float) -> int:
    return int(round(deg * 1e7))


def encode_command(action: CommandAction, intensity: int = 0, duration_ms: int = 1000, seq: int = 0) -> bytes:
    """seq must grow with every command to a collar: the collar refuses one it has already passed,
    so a command recorded off the air cannot be played back later."""
    if not 0 <= intensity <= 99:
        raise ValueError("intensity must be 0..99")
    if not 0 <= duration_ms <= 0xFFFF:
        raise ValueError("duration must be 0..65535 ms")
    return _COMMAND.pack(VERSION, MsgType.COMMAND, action, intensity, duration_ms, seq & 0xFFFFFFFF)


def next_fence_version(stored: int, reported: int | None) -> int:
    """The version for new fences: one past both the base's own count and what the collar says it
    holds (16-bit, wrapping, never 0). The collar refuses versions older than the one it has, so
    a reinstalled base must catch up with the collar."""
    base = stored or 0
    if reported and (reported - base) & 0xFFFF and ((reported - base) & 0xFFFF) < 0x8000:
        base = reported
    return (base + 1) & 0xFFFF or 1


def fit_text(text: str, limit: int) -> str:
    """Tidy text for the collar: no control characters, at most limit bytes of UTF-8, cut on a
    character boundary."""
    text = "".join(c for c in (text or "") if c.isprintable()).strip()
    data = text.encode()[:limit]
    return data.decode(errors="ignore").strip()


def encode_owner(dog: str, owner: str, phone: str) -> bytes:
    """The dog's name and owner contact, used by the collar's lost beacon."""
    out = bytearray([VERSION, MsgType.OWNER])
    for text, limit in ((dog, MAX_DOG_NAME), (owner, MAX_OWNER_NAME), (phone, MAX_PHONE)):
        data = fit_text(text, limit).encode()
        out.append(len(data))
        out += data
    return bytes(out)


def decode_hello(data: bytes) -> dict:
    if len(data) != 4 or data[0] != VERSION or data[1] != MsgType.HELLO:
        raise ValueError("not a GeoCollar hello")
    return {"has_admin_key": bool(data[2] & 1), "has_fences": bool(data[2] & 2),
            "battery_pct": None if data[3] == 255 else data[3]}


def encode_hello(has_admin_key: bool, has_fences: bool = False, battery_pct: int | None = None) -> bytes:
    """For tests and simulators."""
    return bytes([VERSION, MsgType.HELLO, (1 if has_admin_key else 0) | (2 if has_fences else 0),
                  255 if battery_pct is None else battery_pct])


def decode_owner(data: bytes) -> tuple[str, str, str]:
    if len(data) < 5 or data[0] != VERSION or data[1] != MsgType.OWNER:
        raise ValueError("not a GeoCollar owner message")
    fields, i = [], 2
    for _ in range(3):
        n = data[i]
        fields.append(data[i + 1 : i + 1 + n].decode())
        i += 1 + n
    if i != len(data):
        raise ValueError("bad owner message length")
    return fields[0], fields[1], fields[2]


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
        beacon_active=bool(flags & 4),
        beacon_silenced=bool(flags & 8),
    )


def encode_status(s: Status) -> bytes:
    """Mostly for tests and simulators: the collar is what normally sends these."""
    flags = (
        (1 if s.corrections_enabled else 0) | (2 if s.shock_enabled else 0)
        | (4 if s.beacon_active else 0) | (8 if s.beacon_silenced else 0)
    )
    return _STATUS.pack(
        VERSION, s.msg_type, s.fence_version, s.state, s.battery_pct, _to_i(s.lat), _to_i(s.lon),
        int(round(s.pdop * 100)), s.sats, s.clearance_m, flags,
    )


def message_type(data: bytes) -> MsgType | None:
    if len(data) < 2 or data[0] != VERSION or data[1] not in (1, 2, 3, 4, 5, 6):
        return None
    return MsgType(data[1])


def encode_fence_set(zones: Iterable[Zone], pulses: Pulses | None = None) -> bytes:
    zones = list(zones)
    if len(zones) > MAX_ZONES:
        raise ValueError(f"at most {MAX_ZONES} zones")
    pulses = pulses or Pulses()
    for v in (pulses.beep_s, pulses.vibrate_s):
        if v != 0 and not MIN_PULSE_S <= v <= MAX_PULSE_S:
            raise ValueError(f"pulse intervals must be 0 (off) or {MIN_PULSE_S}..{MAX_PULSE_S} s")
    out = bytearray([len(zones), pulses.beep_s, pulses.vibrate_s])
    for z in zones:
        if not 3 <= len(z.vertices) <= MAX_VERTICES:
            raise ValueError(f"a zone needs 3..{MAX_VERTICES} vertices")
        for w in (z.danger_m, z.warning_m, z.attention_m):
            if not 0 <= w <= MAX_BAND_M:
                raise ValueError(f"band widths must be 0..{MAX_BAND_M} m")
        out += struct.pack("<BBBBBB", z.kind, len(z.vertices), z.danger_m, z.warning_m, z.attention_m, 0)
        for lat, lon in z.vertices:
            out += struct.pack("<ii", _to_i(lat), _to_i(lon))
    return bytes(out)


def fence_chunks(zones: Iterable[Zone], fence_version: int, pulses: Pulses | None = None) -> list[bytes]:
    """Split a fence set into packets the collar reassembles."""
    blob = encode_fence_set(zones, pulses)
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

    Feature properties: kind = "keep_in" (default) or "keep_out"; danger_m (default 0, no danger
    band), warning_m (default 5), attention_m (default 5), in metres. Fences saved before bands
    existed carry warn_margin_m instead, which becomes warning + attention as the old firmware
    split it. Other properties (such as the page's zone name) are kept but not sent.
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
        if "warn_margin_m" in props and not any(k in props for k in ("danger_m", "warning_m", "attention_m")):
            old = _band(props, "warn_margin_m", 10)
            bands = (0, old // 2, old - old // 2)
        else:
            bands = (_band(props, "danger_m", 0), _band(props, "warning_m", 5), _band(props, "attention_m", 5))
        zones.append(Zone(kind, *bands, [(lat, lon) for lon, lat in ring]))
    return zones


def _band(props: dict, key: str, default: int) -> int:
    v = props.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= MAX_BAND_M or v != int(v):
        raise ValueError(f"{key} must be a whole number of metres, 0..{MAX_BAND_M}")
    return int(v)


def pulses_from_geojson(geojson: dict) -> Pulses:
    """The FeatureCollection's "pulse" member: {"beep_s": n, "vibrate_s": n}, 0 = off."""
    raw = geojson.get("pulse") or {}
    if not isinstance(raw, dict):
        raise ValueError("pulse must be an object")
    values = []
    for key in ("beep_s", "vibrate_s"):
        v = raw.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int) or (v != 0 and not MIN_PULSE_S <= v <= MAX_PULSE_S):
            raise ValueError(f"pulse.{key} must be 0 (off) or {MIN_PULSE_S}..{MAX_PULSE_S} seconds")
        values.append(v)
    return Pulses(*values)
