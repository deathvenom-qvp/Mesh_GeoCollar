# SPDX-License-Identifier: GPL-3.0-or-later
"""GeoCollar wire format. Must match firmware/GeoCollar/Protocol.cpp; see docs/protocol.md."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable
from urllib.parse import parse_qs, quote, urlsplit

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
MIN_REPORT_S = 30  # how often a collar may be asked to report (kMinReportS..kMaxReportS)
MAX_REPORT_S = 3600
DEFAULT_REPORT_S = 600  # the firmware's GEOCOLLAR_STATUS_INTERVAL_MS
MAX_APP_KEYS = 4  # phones with the companion app per collar (firmware kMaxAppKeys)
APP_KEY_LEN = 16
APP_NONCE_LEN = 8
APP_TAG_LEN = 8
APP_NO_FIX_AGE = 0xFFFF
MIN_ANCHOR_RSSI = -100  # home anchor signal threshold limits, dBm (kMinAnchorRssi..kMaxAnchorRssi)
MAX_ANCHOR_RSSI = -30


class MsgType(IntEnum):
    FENCE_CHUNK = 1
    STATUS = 2
    BREACH = 3
    COMMAND = 4
    OWNER = 5  # base -> collar: the dog's name and the owner's contact, for the lost beacon
    HELLO = 6  # collar -> broadcast: runs GeoCollar, not adopted by a base yet
    CONFIG = 7  # base -> collar: how often to report
    APP_KEYS = 8  # base -> collar: the phones that may connect to the collar with the companion app
    APP_KEYS_ACK = 9  # collar -> base: that list is in force
    # Between the companion app and the collar, over the collar's own Bluetooth (never on the mesh).
    APP_HELLO = 16
    APP_CHALLENGE = 17
    APP_REQUEST = 18
    APP_STATUS = 19
    APP_ERROR = 20


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


# What the companion app may ask a collar for (firmware appActionAllowed). Never shock.
APP_ACTIONS = frozenset({
    CommandAction.REQUEST_STATUS, CommandAction.SILENCE_LOST, CommandAction.BEEP, CommandAction.VIBRATE,
    CommandAction.CORRECTIONS_OFF, CommandAction.CORRECTIONS_ON,
})


class AnchorKind(IntEnum):
    """What the collar listens for to know it is home (firmware: enum class AnchorKind)."""

    NONE = 0  # home mode off
    BEACON = 1  # a Bluetooth beacon, by its address
    BASE_RADIO = 2  # the base station's Meshtastic radio, by its Bluetooth name


@dataclass
class HomeAnchor:
    """Home mode: while the collar hears this anchor at rssi dBm or stronger (and its last fix was
    inside, clear of every band), it turns its GPS off. address is the beacon's Bluetooth address,
    "AA:BB:CC:DD:EE:FF"; unused for the base radio, which the collar knows as the node that
    sends it fences."""

    kind: AnchorKind = AnchorKind.NONE
    address: str = ""
    rssi: int = -75


def parse_bt_address(text: str) -> bytes:
    """A Bluetooth address, "AA:BB:CC:DD:EE:FF" (or with - or nothing between), most significant
    byte first."""
    hexits = "".join(ch for ch in (text or "") if ch not in ":- ").lower()
    if len(hexits) != 12 or any(ch not in "0123456789abcdef" for ch in hexits):
        raise ValueError("a Bluetooth address is six pairs of hex digits, like AA:BB:CC:DD:EE:FF")
    return bytes.fromhex(hexits)


def format_bt_address(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in data)


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
    no_fix: bool = False  # no current GPS fix: lat / lon are the last position the collar knew
    numbered: bool = False  # the collar's firmware reads sequence numbers on Owner and Config
    apps: bool = False  # the collar holds the key of at least one companion app
    home: bool = False  # near the home anchor with the GPS off: lat / lon are the last position


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


def encode_owner(dog: str, owner: str, phone: str, seq: int = 0) -> bytes:
    """The dog's name and owner contact, used by the collar's lost beacon.

    seq is a sequence number that grows with every message (Store.next_command_seq): the collar
    refuses one it has already passed, so a recording cannot put an old name or phone number
    back. 0 leaves it out, which collar firmware before 0.16 needs: it refuses the longer message."""
    out = bytearray([VERSION, MsgType.OWNER])
    for text, limit in ((dog, MAX_DOG_NAME), (owner, MAX_OWNER_NAME), (phone, MAX_PHONE)):
        data = fit_text(text, limit).encode()
        out.append(len(data))
        out += data
    if seq:
        out += struct.pack("<I", seq & 0xFFFFFFFF)
    return bytes(out)


def _cut_utf8(text: str, limit: int) -> str:
    return text.encode()[:limit].decode(errors="ignore")


def mesh_names(dog: str) -> tuple[str, str]:
    """The collar's Meshtastic long and short name for a dog's name, as the collar firmware
    (meshNames) makes them: the name itself, and its initials (several words) or first letters
    (one word), at most 4 bytes, ASCII letters upper-cased."""
    name = " ".join((dog or "").split())
    words = name.split(" ") if name else []
    short = "".join(w[0] for w in words) if len(words) > 1 else name
    short = "".join(c.upper() if c.isascii() else c for c in _cut_utf8(short, 4))  # ASCII only, as the firmware
    return _cut_utf8(name, 39), short


def encode_config(report_s: int, seq: int = 0, anchor: HomeAnchor | None = None) -> bytes:
    """How often the collar reports, in seconds (MIN_REPORT_S..MAX_REPORT_S, or 0 for the
    firmware's default). While lost it reports at least every 30 s whatever this says. Collar
    firmware before 0.15 ignores it.

    seq is a sequence number as for encode_owner (0 leaves it out). Firmware 0.15 reads the
    interval and ignores the number.

    anchor sets home mode (firmware 0.19.2 on; older firmware ignores it). It needs a sequence
    number. AnchorKind.NONE turns home mode off."""
    if report_s != 0 and not MIN_REPORT_S <= report_s <= MAX_REPORT_S:
        raise ValueError(f"report interval must be {MIN_REPORT_S}..{MAX_REPORT_S} s")
    out = bytes([VERSION, MsgType.CONFIG]) + struct.pack("<H", report_s)
    if not seq:
        if anchor is not None:
            raise ValueError("the home anchor needs a sequence number")
        return out
    out += struct.pack("<I", seq & 0xFFFFFFFF)
    if anchor is not None:
        if not MIN_ANCHOR_RSSI <= anchor.rssi <= MAX_ANCHOR_RSSI:
            raise ValueError(f"the anchor's signal threshold must be {MIN_ANCHOR_RSSI}..{MAX_ANCHOR_RSSI} dBm")
        addr = parse_bt_address(anchor.address) if anchor.kind == AnchorKind.BEACON else bytes(6)
        out += bytes([AnchorKind(anchor.kind)]) + addr + struct.pack("<b", anchor.rssi)
    return out


def decode_config(data: bytes) -> int:
    if len(data) < 4 or data[0] != VERSION or data[1] != MsgType.CONFIG:
        raise ValueError("not a GeoCollar config message")
    return struct.unpack_from("<H", data, 2)[0]


def decode_config_anchor(data: bytes) -> HomeAnchor | None:
    """The home anchor on a config message, None if it has none."""
    decode_config(data)
    if len(data) < 8 + 8:
        return None
    kind, rssi = data[8], struct.unpack_from("<b", data, 15)[0]
    return HomeAnchor(AnchorKind(kind), format_bt_address(data[9:15]) if kind == AnchorKind.BEACON else "", rssi)


def decode_config_seq(data: bytes) -> int:
    """The sequence number on a config message, 0 if it has none."""
    decode_config(data)
    return struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0


def decode_hello(data: bytes) -> dict:
    if len(data) != 4 or data[0] != VERSION or data[1] != MsgType.HELLO:
        raise ValueError("not a GeoCollar hello")
    return {"has_admin_key": bool(data[2] & 1), "has_fences": bool(data[2] & 2),
            "battery_pct": None if data[3] == 255 else data[3]}


def encode_hello(has_admin_key: bool, has_fences: bool = False, battery_pct: int | None = None) -> bytes:
    """For tests and simulators."""
    return bytes([VERSION, MsgType.HELLO, (1 if has_admin_key else 0) | (2 if has_fences else 0),
                  255 if battery_pct is None else battery_pct])


def _owner_fields(data: bytes) -> tuple[list[str], int]:
    if len(data) < 5 or data[0] != VERSION or data[1] != MsgType.OWNER:
        raise ValueError("not a GeoCollar owner message")
    fields, i = [], 2
    for _ in range(3):
        if i >= len(data) or i + 1 + data[i] > len(data):
            raise ValueError("bad owner message length")
        n = data[i]
        fields.append(data[i + 1 : i + 1 + n].decode())
        i += 1 + n
    if len(data) - i not in (0, 4):  # nothing after the texts but a sequence number
        raise ValueError("bad owner message length")
    return fields, struct.unpack_from("<I", data, i)[0] if len(data) - i == 4 else 0


def decode_owner(data: bytes) -> tuple[str, str, str]:
    fields, _ = _owner_fields(data)
    return fields[0], fields[1], fields[2]


def decode_owner_seq(data: bytes) -> int:
    """The sequence number on an owner message, 0 if it has none."""
    return _owner_fields(data)[1]


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
        no_fix=bool(flags & 0x10),
        numbered=bool(flags & 0x20),
        apps=bool(flags & 0x40),
        home=bool(flags & 0x80),
    )


def encode_status(s: Status) -> bytes:
    """Mostly for tests and simulators: the collar is what normally sends these."""
    flags = (
        (1 if s.corrections_enabled else 0) | (2 if s.shock_enabled else 0)
        | (4 if s.beacon_active else 0) | (8 if s.beacon_silenced else 0)
        | (0x10 if s.no_fix else 0) | (0x20 if s.numbered else 0) | (0x40 if s.apps else 0) | (0x80 if s.home else 0)
    )
    return _STATUS.pack(
        VERSION, s.msg_type, s.fence_version, s.state, s.battery_pct, _to_i(s.lat), _to_i(s.lon),
        int(round(s.pdop * 100)), s.sats, s.clearance_m, flags,
    )


def message_type(data: bytes) -> MsgType | None:
    if len(data) < 2 or data[0] != VERSION or data[1] not in tuple(MsgType):
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


# -------------------------------------------------------------------------------------------------
# The companion app. The base gives each phone a key (in a QR code) and sends the collar the list
# of keys; the phone then talks to the collar over the collar's own Bluetooth. See docs/protocol.md
# and firmware/GeoCollar/AppLink.h.
# -------------------------------------------------------------------------------------------------
def encode_app_keys(keys: Iterable[tuple[int, bytes]], seq: int) -> bytes:
    """The whole list of phones authorized for a collar, as (key id, 16-byte key). A phone left
    out is revoked. seq grows with every message (Store.next_command_seq), so a recording of an
    older list cannot put a removed phone back."""
    keys = list(keys)
    if len(keys) > MAX_APP_KEYS:
        raise ValueError(f"at most {MAX_APP_KEYS} apps per collar")
    if not 0 < seq <= 0xFFFFFFFF:
        raise ValueError("the app key list needs a sequence number")
    out = bytearray([VERSION, MsgType.APP_KEYS]) + struct.pack("<IB", seq, len(keys))
    seen = set()
    for key_id, key in keys:
        if not 0 < key_id <= 0xFFFFFFFF or key_id in seen or len(key) != APP_KEY_LEN:
            raise ValueError("an app key is a number of its own (not 0) and 16 bytes")
        seen.add(key_id)
        out += struct.pack("<I", key_id) + key
    return bytes(out)


def decode_app_keys(data: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """For tests and simulators: the list and its sequence number."""
    if len(data) < 7 or data[0] != VERSION or data[1] != MsgType.APP_KEYS:
        raise ValueError("not a GeoCollar app key list")
    seq, count = struct.unpack_from("<IB", data, 2)
    if count > MAX_APP_KEYS or len(data) != 7 + count * (4 + APP_KEY_LEN) or not seq:
        raise ValueError("bad app key list")
    keys = []
    for i in range(count):
        at = 7 + i * (4 + APP_KEY_LEN)
        keys.append((struct.unpack_from("<I", data, at)[0], data[at + 4:at + 4 + APP_KEY_LEN]))
    return keys, seq


def encode_app_keys_ack(seq: int, count: int) -> bytes:
    """For tests and simulators: the collar is what sends these."""
    return bytes([VERSION, MsgType.APP_KEYS_ACK]) + struct.pack("<IB", seq & 0xFFFFFFFF, count)


def decode_app_keys_ack(data: bytes) -> dict:
    if len(data) != 7 or data[0] != VERSION or data[1] != MsgType.APP_KEYS_ACK:
        raise ValueError("not a GeoCollar app key acknowledgement")
    seq, count = struct.unpack_from("<IB", data, 2)
    return {"seq": seq, "count": count}


PAIRING_SCHEME = "geocollar"


def pairing_code(node_id: str, key_id: int, key: bytes, dog: str = "", pin: int | None = None,
                 ble_name: str = "", sim: bool = False) -> str:
    """What the "Connect app" QR code says: everything a phone needs to find one collar and be
    believed by it.

    geocollar://pair?v=1&n=<node, 8 hex>&i=<key id, 8 hex>&k=<key, base64url>[&p=<Bluetooth PIN>]
    [&b=<start of the collar's Bluetooth name>][&d=<dog's name>][&s=1 for a simulated collar]"""
    if len(key) != APP_KEY_LEN or not 0 < key_id <= 0xFFFFFFFF:
        raise ValueError("bad app key")
    parts = ["v=1", f"n={node_id.lstrip('!')}", f"i={key_id:08x}",
             "k=" + base64.urlsafe_b64encode(key).decode().rstrip("=")]
    if pin:
        parts.append(f"p={int(pin):06d}")
    if ble_name:
        parts.append("b=" + quote(ble_name, safe=""))
    if dog:
        parts.append("d=" + quote(dog, safe=""))
    if sim:
        parts.append("s=1")
    return f"{PAIRING_SCHEME}://pair?" + "&".join(parts)


def parse_pairing_code(code: str) -> dict:
    """The reverse of pairing_code, as the app reads it. For tests."""
    url = urlsplit(code.strip())
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    if url.scheme != PAIRING_SCHEME or url.netloc != "pair" or q.get("v") != "1":
        raise ValueError("not a GeoCollar pairing code")
    key = base64.urlsafe_b64decode(q["k"] + "=" * (-len(q["k"]) % 4))
    if len(key) != APP_KEY_LEN:
        raise ValueError("bad app key")
    return {"node_id": "!" + q["n"], "key_id": int(q["i"], 16), "key": key, "pin": q.get("p"),
            "ble_name": q.get("b", ""), "dog": q.get("d", ""), "sim": q.get("s") == "1"}


# The app's half of a session, for tests: the app itself is JavaScript (app/src/applink.js) and
# the collar's half is firmware/GeoCollar/AppLink.cpp. All three share golden bytes.
def _app_mac(key: bytes, label: bytes, key_id: int, app_nonce: bytes, collar_nonce: bytes) -> bytes:
    return hmac.new(key, label + struct.pack("<I", key_id) + app_nonce + collar_nonce, hashlib.sha256).digest()


def app_challenge_tag(key: bytes, key_id: int, app_nonce: bytes, collar_nonce: bytes) -> bytes:
    return _app_mac(key, b"GCA1", key_id, app_nonce, collar_nonce)[:APP_TAG_LEN]


def app_session_key(key: bytes, key_id: int, app_nonce: bytes, collar_nonce: bytes) -> bytes:
    return _app_mac(key, b"GCS1", key_id, app_nonce, collar_nonce)


def app_tag(session_key: bytes, message: bytes) -> bytes:
    return hmac.new(session_key, message, hashlib.sha256).digest()[:APP_TAG_LEN]


def encode_app_hello(key_id: int, app_nonce: bytes) -> bytes:
    return bytes([VERSION, MsgType.APP_HELLO]) + struct.pack("<I", key_id) + app_nonce


def encode_app_request(session_key: bytes, key_id: int, counter: int, action: CommandAction, intensity: int = 0,
                       duration_ms: int = 0) -> bytes:
    msg = bytes([VERSION, MsgType.APP_REQUEST]) + struct.pack("<IIBBH", key_id, counter, action, intensity, duration_ms)
    return msg + app_tag(session_key, msg)


def decode_app_status(session_key: bytes, data: bytes) -> dict:
    """An AppStatus whose signature fits: the request it answers, whether that was done, the
    collar's report and how old its position is (None: no fix since the collar started)."""
    if len(data) != 39 or data[0] != VERSION or data[1] != MsgType.APP_STATUS:
        raise ValueError("not a GeoCollar app status")
    if not hmac.compare_digest(app_tag(session_key, data[:-APP_TAG_LEN]), data[-APP_TAG_LEN:]):
        raise ValueError("the app status is not signed by the collar")
    key_id, counter, result = struct.unpack_from("<IIB", data, 2)
    status = decode_status(bytes([VERSION, MsgType.STATUS]) + data[11:29])
    age = struct.unpack_from("<H", data, 29)[0]
    return {"key_id": key_id, "counter": counter, "done": result == 0, "status": status,
            "fix_age_s": None if age == APP_NO_FIX_AGE else age}
