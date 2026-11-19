# SPDX-License-Identifier: GPL-3.0-or-later
"""The phone API: how a linked phone reaches the base over the network (at home or over the
internet), to see where the dogs are and to pass on a collar's report it got over Bluetooth.

Everything a phone sends and gets is encrypted and authenticated end to end with a key of that
phone's own, so plain HTTP is as safe as HTTPS here and a reverse proxy in between can't read it.

Linking (enrolment):

1. "Connect app" on the base page shows a QR code. It is one opaque blob: a version byte, a
   one-time secret, and the rest encrypted with a key made from that secret. A QR reader shows
   random characters; there is no address, name or key in readable form. The code is good for
   ENROLL_TTL_S and for one phone only.
2. The app makes a fresh P-256 key pair and posts its public key to /api/phone/v1/enroll with the
   code's token and an HMAC proving it read the code. The base answers with a fresh public key of
   its own. Both work out the phone's API key from the ECDH secret, salted with the code's secret
   (HKDF-SHA256). Neither the API key nor the phone's Bluetooth key for the collars is ever in the
   QR code: the API key is never sent at all, and the Bluetooth key comes in the encrypted answer.
   Someone who photographs the code after the phone has used it has nothing; someone who also
   recorded the network traffic still can't work out the key (ECDH).
3. From then on each request is AES-256-GCM under the phone's API key (see seal / open_request).
   Removing the phone on the base deletes its key: its next request is refused.

The same bytes are produced by the app (app/src/phoneapi.js); tests share golden values.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import time
import urllib.request
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, Field

QR_VERSION = 3  # pairing code versions 1 and 2 are the older readable geocollar://pair?... codes
SECRET_LEN = 16
TOKEN_LEN = 8
API_KEY_LEN = 32
IV_LEN = 12
ENROLL_TTL_S = 600  # how long a code shown under Connect app can be used
CLOCK_SKEW_S = 600  # how far a phone's clock may be from the base's
MAX_BODY = 64 * 1024
PREFIX = "/api/phone/v1"
DEFAULT_PORT = 8100  # the port the phone API listens on, besides the page's own


API_DESCRIPTION = """\
How the GeoCollar phone app reaches its base station over the network, at home or over the
internet. A phone is linked with the one-time code in the QR code under **Connect app** on the
base page, and removed there too.

**Every request and answer is encrypted** (AES-256-GCM) with a key of that phone's own, so plain
HTTP is safe here and a proxy in between can't read or change anything.

1. **Enrol** (`POST /api/phone/v1/enroll`): the phone posts a fresh P-256 public key with the
   code's token and an HMAC proving it read the code. The answer carries the base's fresh public
   key. API key = HKDF-SHA256(ECDH secret, salt = code secret, info = "GeoCollar phone API v1" ||
   token || phone key || base key), 32 bytes. The answer's `ct` opens with it to a `Welcome`.
2. **Every other operation** is `POST /api/phone/v1/<op>` with a `PhoneRequest`: `ct` opens with
   associated data `GCP1 <op> <phone id>` to `{"ts": <ms>, "body": <request>}`. The answer is an
   `Envelope` whose `ct` opens with associated data `GCR1 <op> <phone id> ` followed by the
   request's 12-byte IV. Each operation's `x-plaintext` names the schemas of `body` and of the
   opened answer. A request more than 10 minutes off the base's clock, or seen before, is refused.

Errors are plain JSON (`ApiError`), never encrypted, and say nothing about the base's collars.
"""


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64d(text: str) -> bytes:
    if not isinstance(text, str) or len(text) > 2 * MAX_BODY:
        raise ValueError("not base64")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise ValueError("not base64") from e


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt or None, info=info).derive(ikm)


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


# -- The QR code ------------------------------------------------------------------------------
def _qr_key(secret: bytes) -> tuple[bytes, bytes]:
    k = hkdf(secret, b"", b"GeoCollar QR v3", 16 + IV_LEN)
    return k[:16], k[16:]


def make_code(secret: bytes, token: bytes, home: str, urls: list[str]) -> str:
    """The text of the QR code: base64url(version, secret, AES-128-GCM(payload))."""
    if len(secret) != SECRET_LEN or len(token) != TOKEN_LEN:
        raise ValueError("bad secret or token")
    key, iv = _qr_key(secret)
    payload = _dumps({"h": home, "t": b64e(token), "u": list(urls)})
    return b64e(bytes([QR_VERSION]) + secret + AESGCM(key).encrypt(iv, payload, bytes([QR_VERSION])))


def read_code(code: str) -> dict:
    """The reverse, as the app reads it: {secret, token, home, urls}. For tests."""
    raw = b64d(code.strip())
    if len(raw) < 1 + SECRET_LEN + 16 or raw[0] != QR_VERSION:
        raise ValueError("not a GeoCollar code")
    secret = raw[1:1 + SECRET_LEN]
    key, iv = _qr_key(secret)
    p = json.loads(AESGCM(key).decrypt(iv, raw[1 + SECRET_LEN:], bytes([QR_VERSION])))
    return {"secret": secret, "token": b64d(p["t"]), "home": p["h"], "urls": p["u"]}


def enroll_proof(secret: bytes, token: bytes, phone_pub: bytes) -> bytes:
    return hmac.new(secret, b"GCE1" + token + phone_pub, hashlib.sha256).digest()


def api_key(shared: bytes, secret: bytes, token: bytes, phone_pub: bytes, base_pub: bytes) -> bytes:
    return hkdf(shared, secret, b"GeoCollar phone API v1" + token + phone_pub + base_pub, API_KEY_LEN)


def public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def ecdh(private: ec.EllipticCurvePrivateKey, peer_pub: bytes) -> bytes:
    """The shared secret (the x coordinate, as WebCrypto's deriveBits gives it). Raises ValueError
    for a key that is not a point on P-256."""
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_pub)
    return private.exchange(ec.ECDH(), peer)


def new_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def welcome_aad(token: bytes) -> bytes:
    return b"GCW1" + token


def request_aad(op: str, phone: str) -> bytes:
    return f"GCP1 {op} {phone}".encode()


def reply_aad(op: str, phone: str, request_iv: bytes) -> bytes:
    return f"GCR1 {op} {phone} ".encode() + request_iv


def seal(key: bytes, aad: bytes, obj, iv: bytes | None = None) -> dict:
    iv = iv or secrets.token_bytes(IV_LEN)
    return {"iv": b64e(iv), "ct": b64e(AESGCM(key).encrypt(iv, _dumps(obj), aad))}


def unseal(key: bytes, aad: bytes, env: dict):
    """Decrypt an envelope. Raises ValueError if it was not sealed with this key and label."""
    iv = b64d(env.get("iv", ""))
    if len(iv) != IV_LEN:
        raise ValueError("bad iv")
    try:
        return json.loads(AESGCM(key).decrypt(iv, b64d(env.get("ct", "")), aad))
    except Exception as e:  # InvalidTag, bad JSON
        raise ValueError("cannot be opened with this phone's key") from e


# -- Schemas, for the OpenAPI document -------------------------------------------------------
class ApiError(BaseModel):
    """Errors are not encrypted: they say only what went wrong with the envelope."""
    detail: str
    code: Literal["bad_request", "unknown_phone", "clock", "replay", "expired", "used", "not_found",
                  "too_large"] = Field(description="What to do about it, for the app.")
    server_time: int | None = Field(None, description="The base's clock in milliseconds, with code \"clock\".")


class EnrollRequest(BaseModel):
    token: str = Field(description="The code's one-time token, base64url (8 bytes).")
    phone_key: str = Field(description="The phone's fresh P-256 public key, uncompressed (65 bytes), base64url.")
    proof: str = Field(description="HMAC-SHA256(code secret, \"GCE1\" || token || phone_key), base64url.")


class Envelope(BaseModel):
    """AES-256-GCM with a random 12-byte IV. The plaintext is JSON."""
    iv: str = Field(description="base64url, 12 bytes")
    ct: str = Field(description="base64url: ciphertext followed by the 16-byte tag")


class EnrollReply(Envelope):
    """ct opens with the new API key and associated data \"GCW1\" || token, to a Welcome."""
    base_key: str = Field(description="The base's fresh P-256 public key, uncompressed, base64url.")


class PhoneRequest(Envelope):
    """ct opens with the phone's API key and associated data \"GCP1 <op> <phone>\" to a
    {\"ts\": <phone's clock, ms>, \"body\": <the operation's request>}. The answer is an Envelope
    whose ct opens with associated data \"GCR1 <op> <phone> \" || this request's IV."""
    phone: str = Field(description="The phone's id, 8 hex digits, as the Welcome gave it.", pattern="^[0-9a-f]{8}$")


class CollarCode(BaseModel):
    node_id: str = Field(description="Meshtastic node ID, like !a1b2c3d4")
    dog: str = ""
    pin: int | None = Field(None, description="The collar's Bluetooth PIN, when the base knows it")
    ble_name: str = Field("", description="The start of the collar's Bluetooth name")
    sim: bool = Field(False, description="A simulated collar: nothing to connect to")


class Welcome(BaseModel):
    """What the phone gets once linked (the plaintext of EnrollReply.ct)."""
    phone_id: str
    home: str = Field(description="The base's id, 8 hex digits")
    ble_key_id: int = Field(description="The phone's key id on the collars")
    ble_key: str = Field(description="The phone's key for the collars, base64url (16 bytes); each collar's own key is made from it")
    urls: list[str] = Field(description="Where the phone API can be reached, best first")
    collars: list[CollarCode]
    server_time: int


class CollarStatus(BaseModel):
    state: Literal["NO_FENCE", "INSIDE", "WARNING", "OUTSIDE", "LOST"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    battery_pct: int | None = Field(None, ge=0, le=255, description="255 or null: unknown")
    sats: int = Field(0, ge=0, le=255)
    pdop: float = Field(0, ge=0, le=655.35)
    clearance_m: int = Field(0, ge=-32768, le=32767, description="Distance inside (+) or outside (-) the line")
    fence_version: int = Field(0, ge=0, le=65535)
    corrections_enabled: bool = True
    shock_enabled: bool = False
    beacon_active: bool = False
    beacon_silenced: bool = False
    no_fix: bool = False
    home: bool = False


class CollarState(CollarStatus):
    received: float = Field(description="When the base got it, seconds since 1970 (base's clock)")
    via: Literal["mesh", "phone", "sim"] = "mesh"
    via_phone: str | None = Field(None, description="Which phone passed it on, with via \"phone\"")
    fix_age_s: int | None = Field(None, description="How old the GPS fix was when the report was made, when known")


class CollarView(CollarCode):
    status: CollarState | None = None


class CollarsRequest(BaseModel):
    """Nothing to say yet: an empty object."""


class CollarsReply(BaseModel):
    home: str
    urls: list[str] = Field(description="Where the phone API can be reached now, best first: keep them")
    collars: list[CollarView]
    server_time: int


class PhonePosition(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0)


class RelayRequest(BaseModel):
    """A collar's report the phone got over Bluetooth, for a collar the base has not heard from
    over the mesh lately."""
    node_id: str = Field(pattern="^![0-9a-f]{8}$")
    status: CollarStatus
    fix_age_s: int | None = Field(None, ge=0, le=65535, description="Age of the collar's GPS fix when it answered, if it has had one")
    age_ms: int = Field(0, ge=0, description="How long ago the phone got it")
    phone_position: PhonePosition | None = Field(None, description="Where the phone was: within Bluetooth range of the dog")


class RelayReply(BaseModel):
    recorded: bool
    reason: Literal["recorded", "heard_over_mesh", "too_soon", "too_old", "unknown_collar"]
    next_s: int = Field(description="Offer the next report after this many seconds")


OPERATIONS = {  # op name -> (request schema, reply schema), for the OpenAPI document
    "collars": (CollarsRequest, CollarsReply),
    "relay": (RelayRequest, RelayReply),
}


def plaintext_components() -> dict:
    """JSON schemas of everything that travels inside the envelopes."""
    out: dict = {}
    for model in (Welcome, CollarsRequest, CollarsReply, RelayRequest, RelayReply):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        out.update(schema.pop("$defs", {}))
        out[model.__name__] = schema
    return out


# -- Where the phone can reach the base ----------------------------------------------------
def _supervisor(path: str, fetch=None, body: dict | None = None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token and fetch is None:
        return None
    if fetch is not None:
        return fetch(f"http://supervisor{path}") if body is None else fetch(f"http://supervisor{path}", body)
    req = urllib.request.Request(f"http://supervisor{path}", headers={"Authorization": f"Bearer {token}",
                                                                      "Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - fixed internal URL
            return json.load(r)
    except (OSError, ValueError):
        return None


def ha_lookup(fetch=None) -> dict:
    """What Home Assistant knows: its own URLs, the host port this app's phone API is published
    on (None if it is not) and the machine's address on the home network. Empty outside HA."""
    out: dict = {}
    cfg = _supervisor("/core/api/config", fetch)
    if not isinstance(cfg, dict):
        return out
    out["external_url"] = cfg.get("external_url") or ""
    out["internal_url"] = cfg.get("internal_url") or ""
    info = _supervisor("/addons/self/info", fetch)
    net = ((info or {}).get("data") or {}).get("network") or {}
    out["port"] = net.get(f"{DEFAULT_PORT}/tcp")
    out["port_known"] = bool(info)
    netinfo = _supervisor("/network/info", fetch)
    for iface in (((netinfo or {}).get("data") or {}).get("interfaces") or []):
        addrs = (iface.get("ipv4") or {}).get("address") or []
        if addrs and (iface.get("primary") or "ip" not in out):
            out["ip"] = str(addrs[0]).split("/")[0]
    return out


# -- The Home Assistant integration (ha_integration/geocollar): the phone API through Home
# Assistant's own address, at HA_PATH, with no port or proxy host of its own.
HA_PATH = "/api/geocollar/phone"
INTEGRATION = Path(__file__).parent / "ha_integration" / "geocollar"


def integration_version(folder: Path) -> str | None:
    try:
        m = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    return m.get("version") if m.get("domain") == "geocollar" else None


def install_integration(config_dir: Path) -> str:
    """Copy the integration into Home Assistant's custom_components, if this app may write there
    and it is missing or older. Returns what happened: installed, updated, current, not_ours or
    no_access."""
    if not config_dir.is_dir() or not os.access(config_dir, os.W_OK):
        return "no_access"
    dest = config_dir / "custom_components" / "geocollar"
    have, want = integration_version(dest), integration_version(INTEGRATION)
    if dest.exists() and have is None:
        return "not_ours"  # someone else's folder of that name: leave it alone
    if have == want:
        return "current"
    tmp = dest.with_name(".geocollar.new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(INTEGRATION, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    if dest.exists():
        shutil.rmtree(dest)
    tmp.replace(dest)
    return "updated" if have else "installed"


def announce(fetch=None, phone_port: int = DEFAULT_PORT, page_port: int = 8099) -> bool:
    """Tell Home Assistant, through the Supervisor, where this app is: it then offers to set up
    the GeoCollar integration ("Discovered")."""
    info = _supervisor("/addons/self/info", fetch)
    host = ((info or {}).get("data") or {}).get("hostname")
    if not host:
        return False
    r = _supervisor("/discovery", fetch, {"service": "geocollar",
                                          "config": {"host": host, "phone_port": phone_port, "page_port": page_port}})
    return bool(r) and (r.get("result") == "ok" if isinstance(r, dict) else True)


def local_ip() -> str | None:
    """This machine's address on the network it would reach the internet by (no packet is sent).
    In a Docker bridge network that is the container's own address, which a phone can't use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def clean_url(text: str) -> str:
    """A Base URL as typed: http(s)://host[:port][/path], without a trailing slash. Raises ValueError."""
    t = (text or "").strip()
    if not t:
        return ""
    if "://" not in t:
        t = "http://" + t
    u = urlsplit(t)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("give an address like https://geocollar.example.com or http://203.0.113.5:8100")
    try:
        u.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as e:
        raise ValueError("the port is not a number from 1 to 65535") from e
    if len(t) > 200:
        raise ValueError("the address is too long")
    return t.rstrip("/")


def with_port(host: str, port: int, scheme: str = "http") -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")


def is_private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local") or host.endswith(".lan") or host.endswith(".home.arpa") or "." not in host
    return ip.is_private or ip.is_loopback or ip.is_link_local


# -- The app's half, for tests ----------------------------------------------------------------
class TestPhone:
    """Does what the app does (app/src/phoneapi.js), against a test client."""

    __test__ = False  # not a pytest class

    def __init__(self, client, code: str, private: ec.EllipticCurvePrivateKey | None = None):
        self.client = client
        self.code = read_code(code)
        self.private = private or new_private_key()
        self.key = b""
        self.phone = ""
        self.welcome: dict = {}
        self.clock_ms = None  # fixed clock for tests, else the real one

    def enroll(self, path: str = PREFIX + "/enroll"):
        pub = public_bytes(self.private)
        c = self.code
        r = self.client.post(path, json={"token": b64e(c["token"]), "phone_key": b64e(pub),
                                         "proof": b64e(enroll_proof(c["secret"], c["token"], pub))})
        if r.status_code != 200:
            return r
        body = r.json()
        base_pub = b64d(body["base_key"])
        self.key = api_key(ecdh(self.private, base_pub), c["secret"], c["token"], pub, base_pub)
        self.welcome = unseal(self.key, welcome_aad(c["token"]), body)
        self.phone = self.welcome["phone_id"]
        return r

    def call(self, op: str, body: dict | None = None, ts_ms: int | None = None, iv: bytes | None = None):
        """Returns (status code, the opened answer or the error JSON)."""
        iv = iv or secrets.token_bytes(IV_LEN)
        ts = ts_ms if ts_ms is not None else (self.clock_ms or int(time.time() * 1000))
        env = seal(self.key, request_aad(op, self.phone), {"ts": ts, "body": body or {}}, iv)
        r = self.client.post(f"{PREFIX}/{op}", json={"phone": self.phone, **env})
        if r.status_code != 200:
            return r.status_code, r.json()
        return 200, unseal(self.key, reply_aad(op, self.phone, iv), r.json())
