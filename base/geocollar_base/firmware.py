# SPDX-License-Identifier: GPL-3.0-or-later
"""Collar firmware: get a build, then flash it over USB or Wi-Fi.

Builds come from the "firmware-latest" release that CI publishes from main, or are compiled on
this machine from the repository's source (slow, and needs a few GB of disk). Each build is a
folder with manifest.json, written by firmware/package.py.

Flashing:
- USB on this machine: esptool, through the app's serial access.
- Wi-Fi OTA: Meshtastic 2.7.18+ reboots into its OTA loader (meshtastic/esp32-unified-ota) when
  an admin ota_request with the image's SHA-256 arrives. The loader joins the Wi-Fi network saved
  on the collar, announces itself by UDP broadcast on port 3232, and takes the image over TCP 3232:
  "OTA <size> <sha256>\\n", "ERASING", "OK", raw bytes, "OK". It stays in the loader until it gets
  a good image, so a collar in update mode does not enforce fences.
- USB from the browser: done in the page with esptool-js; the page fetches the files from here.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_REPO = "deathvenom-qvp/Mesh_GeoCollar"
RELEASE_TAG = "firmware-latest"
OTA_PORT = 3232
KEEP_BUILDS = 3
USER_AGENT = "geocollar-base"


class FirmwareError(Exception):
    pass


# -------------------------------------------------------------------------------------------------
# Jobs: one long task at a time (download, build, flash), with a log the page polls.
# -------------------------------------------------------------------------------------------------
@dataclass
class Job:
    kind: str
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "running"  # running, done, failed, cancelled
    step: str = ""
    progress: float | None = None  # 0..1, or None when unknown
    lines: deque = field(default_factory=lambda: deque(maxlen=500))
    result: dict = field(default_factory=dict)
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def log(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self.lines.append(line)
            log.info("[%s] %s", self.kind, line)

    def set_step(self, step: str, progress: float | None = None) -> None:
        self.step = step
        self.progress = progress
        self.log(step)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise FirmwareError("cancelled")

    def view(self, since: int = 0) -> dict:
        lines = list(self.lines)
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "state": self.state, "step": self.step,
            "progress": self.progress, "error": self.error, "result": self.result,
            "started": self.started, "finished": self.finished, "log": lines[since:] if since < len(lines) else [],
            "log_total": len(lines),
        }


class JobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=10)

    def busy(self) -> bool:
        return bool(self.current and self.current.state == "running")

    def start(self, kind: str, title: str, fn: Callable[[Job], dict | None]) -> Job:
        with self._lock:
            if self.busy():
                raise FirmwareError(f"busy: {self.current.title}")
            job = Job(kind, title)
            self.current = job
            self.history.appendleft(job)

        def run():
            try:
                job.result = fn(job) or {}
                job.state = "done"
                if job.progress is not None:
                    job.progress = 1.0
            except Exception as e:  # noqa: BLE001 - reported to the page
                job.state = "cancelled" if job.cancel.is_set() else "failed"
                job.error = str(e) or type(e).__name__
                job.log(f"Error: {job.error}")
                if not isinstance(e, FirmwareError):
                    log.exception("firmware job %s failed", kind)
            finally:
                job.finished = time.time()

        threading.Thread(target=run, name=f"fw-{kind}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return next((j for j in self.history if j.id == job_id), None)


# -------------------------------------------------------------------------------------------------
# Builds on disk
# -------------------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def check_manifest(m: dict) -> None:
    try:
        if m.get("format") != 1:
            raise FirmwareError("unknown manifest format")
        for k in ("app", "factory", "loader"):
            f = m["files"][k]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", f["name"]) or not re.fullmatch(r"[0-9a-f]{64}", f["sha256"]):
                raise FirmwareError(f"bad {k} entry in manifest")
        for k in ("factory", "app", "loader", "otadata", "otadata_size"):
            if not isinstance(m["offsets"][k], int) or m["offsets"][k] < 0:
                raise FirmwareError(f"bad {k} offset in manifest")
    except (KeyError, TypeError) as e:
        raise FirmwareError(f"incomplete manifest ({e})") from e


class BuildStore:
    def __init__(self, root: Path):
        self.root = root / "builds"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, build_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", build_id or ""):
            raise FirmwareError("bad build id")
        p = self.root / build_id
        if not (p / "manifest.json").exists():
            raise FirmwareError("no such build")
        return p

    def manifest(self, build_id: str) -> dict:
        m = json.loads((self.path(build_id) / "manifest.json").read_text())
        m["id"] = build_id
        return m

    def file(self, build_id: str, kind: str) -> Path:
        m = self.manifest(build_id)
        if kind not in m["files"]:
            raise FirmwareError("no such file")
        return self.path(build_id) / m["files"][kind]["name"]

    def list(self) -> list[dict]:
        out = []
        for p in self.root.iterdir():
            try:
                out.append(self.manifest(p.name))
            except (FirmwareError, OSError, ValueError):
                continue
        return sorted(out, key=lambda m: m.get("added_at", 0), reverse=True)

    def find_commit(self, commit: str, source: str) -> dict | None:
        return next((m for m in self.list() if m.get("geocollar_commit") == commit and m.get("source") == source), None)

    def add(self, folder: Path, source: str) -> dict:
        """Verify a packaged build folder and move it into the store."""
        m = json.loads((folder / "manifest.json").read_text())
        check_manifest(m)
        for k, f in m["files"].items():
            if sha256_file(folder / f["name"]) != f["sha256"]:
                raise FirmwareError(f"{f['name']} does not match its SHA-256 in the manifest")
        m["source"] = source
        m["added_at"] = time.time()
        (folder / "manifest.json").write_text(json.dumps(m, indent=2))
        build_id = f"{source}-{(m.get('geocollar_commit') or 'unknown')[:7]}-{int(m['added_at'])}"
        dest = self.root / build_id
        shutil.move(str(folder), dest)
        self.prune()
        return self.manifest(build_id)

    def delete(self, build_id: str) -> None:
        shutil.rmtree(self.path(build_id))

    def prune(self, keep: int = KEEP_BUILDS) -> None:
        for m in self.list()[keep:]:
            shutil.rmtree(self.root / m["id"], ignore_errors=True)


def _download(url: str, dest: Path, job: Job | None = None, label: str = "") -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:  # noqa: S310 - https URL built here
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if job:
                job.check_cancel()
                if total:
                    job.progress = done / total
    if job and label:
        job.log(f"Downloaded {label} ({done // 1024} KB)")


def fetch_release(job: Job, builds: BuildStore, repo: str = DEFAULT_REPO) -> dict:
    """Download the newest CI build of main."""
    base = f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/"
    job.set_step(f"Checking the latest build on github.com/{repo}")
    tmp = Path(tempfile.mkdtemp(dir=builds.root, prefix=".dl-"))
    try:
        try:
            _download(base + "manifest.json", tmp / "manifest.json")
        except OSError as e:
            raise FirmwareError(f"could not download the latest build ({e}). Has CI published one yet?") from e
        m = json.loads((tmp / "manifest.json").read_text())
        check_manifest(m)
        commit = m.get("geocollar_commit", "")
        have = builds.find_commit(commit, "release") if commit else None
        if have:
            job.log(f"Already have this build ({commit[:7]})")
            return have
        job.log(f"Latest build: {commit[:7]} {m.get('geocollar_subject', '')} (Meshtastic {m.get('meshtastic_version')})")
        for k in ("app", "factory", "loader"):
            name = m["files"][k]["name"]
            job.set_step(f"Downloading {name}", 0.0)
            for attempt in range(3):
                _download(base + name, tmp / name, job, name)
                if sha256_file(tmp / name) == m["files"][k]["sha256"]:
                    break
                job.log(f"{name} does not match the manifest yet (CI may be uploading); retrying")
                time.sleep(10 * (attempt + 1))
            else:
                raise FirmwareError(f"{name} does not match the manifest. Try again in a few minutes.")
        job.set_step("Saved")
        return builds.add(tmp, "release")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(job: Job, cmd: list[str], cwd: Path | None = None, env: dict | None = None,
         on_line: Callable[[str], None] | None = None) -> None:
    job.log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, errors="replace")
    try:
        assert proc.stdout
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf:
                    (on_line or job.log)(buf)
                buf = ""
            else:
                buf += ch
            if job.cancel.is_set():
                proc.terminate()
        if buf:
            (on_line or job.log)(buf)
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    job.check_cancel()
    if rc:
        raise FirmwareError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} failed (exit code {rc})".strip())


def build_from_source(job: Job, builds: BuildStore, work: Path, repo: str = DEFAULT_REPO, ref: str = "main") -> dict:
    """Clone or update the repository and run firmware/build.sh and firmware/package.py.
    PlatformIO and the ESP32 toolchain are installed into the data folder on first use."""
    if not shutil.which("git"):
        raise FirmwareError("git is not installed in this image")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ref.startswith("-") or ".." in ref or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        raise FirmwareError("bad repository or branch name")
    work.mkdir(parents=True, exist_ok=True)
    src, venv, pio_home = work / "src", work / "venv", work / "platformio"
    env = dict(os.environ, PLATFORMIO_CORE_DIR=str(pio_home), PIP_DISABLE_PIP_VERSION_CHECK="1",
               PATH=f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    free = shutil.disk_usage(work).free
    job.log(f"Free disk space: {free / 1e9:.1f} GB")
    if free < 3e9 and not pio_home.exists():
        raise FirmwareError("building needs about 3 GB of free disk space for the ESP32 toolchain")

    job.set_step("Getting the source")
    url = f"https://github.com/{repo}.git"
    if (src / ".git").exists():
        _run(job, ["git", "-C", str(src), "fetch", "--depth", "1", "origin", ref])
        _run(job, ["git", "-C", str(src), "reset", "--hard", "FETCH_HEAD"])
    else:
        _run(job, ["git", "clone", "--depth", "1", "--branch", ref, url, str(src)])

    if not (venv / "bin" / "pio").exists():
        job.set_step("Installing PlatformIO (first build only)")
        _run(job, [sys.executable, "-m", "venv", str(venv)])
        _run(job, [str(venv / "bin" / "pip"), "install", "-q", "platformio==6.2.0"], env=env)

    job.set_step("Compiling (the first build downloads the toolchain and can take 30 minutes or more)")
    compiled = [0]

    def on_line(line: str) -> None:
        job.log(line)
        if line.startswith("Compiling "):
            compiled[0] += 1
            job.step = f"Compiling ({compiled[0]} files so far)"

    _run(job, ["bash", str(src / "firmware" / "build.sh"), "heltec-wireless-tracker"], cwd=src, env=env, on_line=on_line)

    job.set_step("Packaging")
    out = Path(tempfile.mkdtemp(dir=builds.root, prefix=".build-"))
    try:
        _run(job, [str(venv / "bin" / "python"), str(src / "firmware" / "package.py"), str(out), "heltec-wireless-tracker"],
             cwd=src, env=env)
        return builds.add(out, "local")
    finally:
        shutil.rmtree(out, ignore_errors=True)


# -------------------------------------------------------------------------------------------------
# USB flashing on this machine
# -------------------------------------------------------------------------------------------------
def serial_ports(exclude: str = "") -> list[dict]:
    from serial.tools import list_ports

    skip = os.path.realpath(exclude) if exclude else ""
    out = []
    for p in list_ports.comports():
        if not p.device or p.device.startswith("/dev/ttyS"):
            continue  # built-in UARTs, never a collar
        out.append({
            "device": p.device, "description": p.description or "", "vid": p.vid, "pid": p.pid,
            "serial_number": p.serial_number or "", "in_use": bool(skip) and os.path.realpath(p.device) == skip,
        })
    return out


def node_id_from_mac(mac: str) -> str | None:
    """Meshtastic's node number is the last four bytes of the ESP32's base MAC address
    (NodeDB::pickNewNodeNum), so esptool's "MAC: ..." line tells us the node ID before the collar
    even boots. Meshtastic only picks another number if that one clashes with a node it knows."""
    parts = re.findall(r"[0-9a-fA-F]{2}", mac or "")
    if len(parts) != 6:
        return None
    return "!" + "".join(parts[2:]).lower()


def flash_usb(job: Job, builds: BuildStore, build_id: str, port: str, mode: str, base_radio: str = "") -> dict:
    """mode "update": write the app and OTA loader, and clear otadata so the collar boots the new
    app (also gets a collar out of update mode). Settings, keys and fences are kept.
    mode "full": erase everything first. The collar comes back blank and must be set up again."""
    if mode not in ("update", "full"):
        raise FirmwareError("mode must be update or full")
    if base_radio and os.path.realpath(port) == os.path.realpath(base_radio):
        raise FirmwareError("that port is the base station's own Meshtastic radio")
    if not any(p["device"] == port for p in serial_ports()):
        raise FirmwareError(f"{port} is not a serial port on this machine")
    m = builds.manifest(build_id)
    d = builds.path(build_id)
    off = m["offsets"]
    cmd = [sys.executable, "-m", "esptool", "--chip", m.get("mcu", "esp32s3"), "--port", port, "--baud", "460800",
           "--before", "default_reset", "--after", "hard_reset", "write_flash"]
    if mode == "full":
        cmd += ["--erase-all", hex(off["factory"]), str(d / m["files"]["factory"]["name"])]
    else:
        blank = d / "otadata-blank.bin"
        if not blank.exists():
            blank.write_bytes(b"\xff" * off["otadata_size"])
        cmd += [hex(off["otadata"]), str(blank), hex(off["app"]), str(d / m["files"]["app"]["name"])]
    cmd += [hex(off["loader"]), str(d / m["files"]["loader"]["name"])]

    job.set_step(f"Flashing {port} ({'full install' if mode == 'full' else 'update, settings kept'})", 0.0)
    files = 3 if mode == "update" else 2
    seen = [0]

    def on_line(line: str) -> None:
        if line.startswith("Writing at"):
            pct = re.search(r"\((\d+) ?%\)", line)
            if pct:
                job.progress = (seen[0] + int(pct.group(1)) / 100) / files
            return  # too chatty for the log
        if line.startswith("Wrote "):
            seen[0] += 1
        mac = re.match(r"MAC:\s*([0-9a-fA-F:]{17})", line.strip())
        if mac and not found.get("node_id"):
            found["mac"] = mac.group(1).lower()
            found["node_id"] = node_id_from_mac(mac.group(1))
            line += f"  (Meshtastic node {found['node_id']})"
        job.log(line)

    found: dict = {}
    _run(job, cmd, on_line=on_line)
    job.set_step("Done. The collar is restarting.", 1.0)
    return {"build": build_id, "commit": m.get("geocollar_commit"), "mode": mode, **found}


# -------------------------------------------------------------------------------------------------
# Setting up a collar over USB: what the owner would otherwise do by hand in the Meshtastic app.
# -------------------------------------------------------------------------------------------------
def open_serial_node(port: str):
    from meshtastic.serial_interface import SerialInterface

    return SerialInterface(devPath=port)


class SocketStream:
    """A collar's USB serial port, relayed by the browser over a WebSocket (Web Serial), that looks
    enough like pyserial for Meshtastic's StreamInterface. feed() takes bytes from the browser;
    write() hands bytes to send(), which delivers them to the browser."""

    def __init__(self, send: Callable[[bytes], None], read_timeout: float = 0.5):
        self._send = send
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._shut = False
        self.timeout = read_timeout

    def feed(self, data: bytes) -> None:
        with self._cv:
            self._buf += data
            self._cv.notify_all()

    def shutdown(self) -> None:
        """The browser went away: reads fail from now on, so the interface stops."""
        with self._cv:
            self._shut = True
            self._cv.notify_all()

    def read(self, n: int = 1) -> bytes:
        with self._cv:
            if not self._buf and not self._shut:
                self._cv.wait(self.timeout)
            if not self._buf and self._shut:
                raise OSError("the browser closed the USB connection")
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def write(self, data: bytes) -> int:
        if self._shut:
            raise OSError("the browser closed the USB connection")
        self._send(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        """Called by the interface when it is done; the WebSocket stays open for a retry."""


def open_socket_node(stream: SocketStream):
    """A Meshtastic interface over a SocketStream (each attempt gets a fresh interface)."""
    from meshtastic.stream_interface import StreamInterface

    class SocketInterface(StreamInterface):
        def connect(self) -> None:
            self.stream = stream
            super().connect()

    return SocketInterface(timeout=60)


def _node_id(iface) -> str | None:
    info = iface.getMyNodeInfo() or {}
    if "num" in info:
        return f"!{info['num']:08x}"
    return (info.get("user") or {}).get("id")


def _close(iface) -> None:
    try:
        iface.close()
    except Exception:  # noqa: BLE001
        pass


def _open_collar(job: Job, port: str, open_iface, wait_s: float, what: str):
    job.set_step(f"Waiting for the collar on {port} {what}", None)
    deadline = time.monotonic() + wait_s
    while True:
        job.check_cancel()
        try:
            return open_iface(port)
        except Exception as e:  # noqa: BLE001 - still booting; any failure means try again
            if time.monotonic() > deadline:
                raise FirmwareError(f"could not talk to the collar on {port}: {e}") from e
            time.sleep(3)


def setup_collar(job: Job, port: str, base_iface, name: str = "", open_iface=open_serial_node,
                 boot_wait_s: float = 60, lock: bool = True, restart_s: float = 9) -> dict:
    """Over USB, make a collar trust and talk to this base: put the base's public key in its admin
    keys, give it the base's LoRa settings and channels, keep Bluetooth on for the lost beacon, and
    name it after the dog.

    With lock (the default) the collar is also locked to this base: Meshtastic's managed mode
    refuses settings changes from Bluetooth, USB and the network, so only this base (by its key)
    can change the collar, and Bluetooth pairing needs a random PIN. Undoing it takes a full
    install, which erases it.

    Three things about Meshtastic 2.8 shape the order:
    - A fresh collar has no region. Setting one mints its key pair, and its node number becomes a
      hash of the public key, so messages addressed to the old number go nowhere. So the region
      goes first, on its own, and the setup reconnects to learn the final node number.
    - Managed mode blocks every later message over USB, including the commit of an edit
      transaction. So no transaction: each setting saves as it is written, and the lock goes last.
    - Bluetooth and security changes restart the collar 7 s after the last one. After that the
      setup reconnects and checks that the settings stuck.

    Returns the node ID (and the one before setup, if it changed), the Bluetooth PIN, and whether
    the settings were checked after the restart."""
    if base_iface is None:
        raise FirmwareError("the base's own Meshtastic radio is not connected, so there is nothing to copy")
    base = base_iface.localNode
    base_key = bytes(base.localConfig.security.public_key)
    if not base_key:
        raise FirmwareError("the base radio has no public key yet (Meshtastic 2.5 or later makes one)")
    base_lora = base.localConfig.lora
    base_chans = list(base.channels or [])

    iface = _open_collar(job, port, open_iface, boot_wait_s, "to start")
    try:
        first_id = _node_id(iface)
        job.log(f"Collar is Meshtastic node {first_id}")
        node = iface.localNode
        sec = node.localConfig.security
        if sec.is_managed:
            if base_key in [bytes(k) for k in sec.admin_key]:
                job.set_step("This collar is already locked to this base; nothing to change over USB.", 1.0)
                return {"node_id": first_id, "already_locked": True}
            raise FirmwareError("this collar is locked to another base. Only a full install (which erases it) "
                                "can free it; then set it up again")

        # 1. Region and preset, on their own (see above).
        job.set_step("Setting the collar's region and preset", 0.1)
        moved = not node.localConfig.lora.region or not bytes(sec.public_key)
        if node.localConfig.lora.SerializeToString() != base_lora.SerializeToString():
            node.localConfig.lora.CopyFrom(base_lora)
            node.writeConfig("lora")
            job.log("Copied the base's LoRa settings (region and preset)")
        else:
            moved = False
            job.log("The collar already has the base's LoRa settings")
    finally:
        _close(iface)

    if moved:
        # The new key, and with it the final node number, arrive with a fresh connection.
        time.sleep(2)
        iface = _open_collar(job, port, open_iface, 30, "to come back with its new key")
    else:
        iface = _open_collar(job, port, open_iface, 30, "again")
    try:
        node_id = _node_id(iface)
        if node_id != first_id:
            job.log(f"Its node ID is now {node_id} (Meshtastic derives it from the key it just made)")
        node = iface.localNode
        job.set_step("Setting up the collar", 0.3)

        # 2. Channels and name: saved as written, no restart.
        for i, ch in enumerate(base_chans):
            if i >= len(node.channels):
                break
            node.channels[i].CopyFrom(ch)
            node.writeChannel(i)
        job.log("Copied the base's channels")
        if not any(c.role and bytes(c.settings.psk) in (b"", b"\x01") for c in base_chans):
            job.log("Note: no channel with the default public key, so the lost-dog message reaches only your own nodes")
        if name:
            short = "".join(w[0] for w in name.split()[:4]).upper() or name[:4]
            node.setOwner(long_name=name[:39], short_name=short[:4])
            job.log(f"Named the collar {name}")

        # 3. Bluetooth, then security with the lock: each restarts the collar 7 s later.
        from meshtastic.protobuf import config_pb2

        bt = node.localConfig.bluetooth
        bt.enabled = True  # the lost beacon needs it
        pin = None
        if lock:
            # A fixed random PIN instead of Meshtastic's default 123456 (boards without a screen) or
            # a PIN shown on a screen inside the collar's case.
            import secrets

            pin = secrets.randbelow(900000) + 100000
            bt.mode = config_pb2.Config.BluetoothConfig.PairingMode.FIXED_PIN
            bt.fixed_pin = pin
        node.writeConfig("bluetooth")
        job.log("Bluetooth on" + (", pairing PIN set (shown in the collar's panel)" if pin else ""))

        sec = node.localConfig.security
        keys = [bytes(k) for k in sec.admin_key]
        if base_key not in keys:
            if len(keys) >= 3:  # Meshtastic keeps three admin keys; replace the last one
                del sec.admin_key[-1]
            sec.admin_key.append(base_key)
        sec.admin_channel_enabled = False  # the old shared-channel admin: off
        sec.is_managed = bool(lock)
        node.writeConfig("security")  # last: once managed, the collar ignores USB settings
        job.log("Added the base's public key to the collar's admin keys"
                + (" and locked it to this base: its settings can now only be changed by this base, "
                   "or by a full install that erases it" if lock else ""))
    finally:
        _close(iface)

    # 4. Check, after the restart, that it all stuck.
    job.set_step("Waiting for the collar to restart, to check its settings", 0.7)
    time.sleep(restart_s)
    try:
        iface = _open_collar(job, port, open_iface, boot_wait_s, "to restart")
    except FirmwareError as e:
        job.log(f"Could not reconnect to check the settings ({e}). The collar shows here once it reports.")
        job.set_step("Done. Unchecked: the collar didn't reconnect over USB after restarting.", 1.0)
        return {"node_id": node_id, "previous_node_id": first_id, "ble_pin": pin, "locked": lock, "checked": False}
    try:
        node = iface.localNode
        problems = []
        if node.localConfig.lora.region != base_lora.region:
            problems.append("region")
        if node.localConfig.lora.modem_preset != base_lora.modem_preset:
            problems.append("preset")
        if base_key not in [bytes(k) for k in node.localConfig.security.admin_key]:
            problems.append("the base's key")
        if base_chans and node.channels and bytes(node.channels[0].settings.psk) != bytes(base_chans[0].settings.psk):
            problems.append("primary channel")
        if bool(node.localConfig.security.is_managed) != bool(lock):
            problems.append("lock")
        final_id = _node_id(iface)
    finally:
        _close(iface)
    if problems:
        raise FirmwareError("the collar restarted without these settings: " + ", ".join(problems)
                            + ". Try again; if it keeps failing, do a full install first")
    job.log("Checked after the restart: the collar kept its new settings")
    job.set_step("Done. The collar is set up for this base and reports within a minute or two.", 1.0)
    return {"node_id": final_id or node_id, "previous_node_id": first_id, "ble_pin": pin, "locked": lock,
            "checked": True}


# -------------------------------------------------------------------------------------------------
# Wi-Fi OTA
# -------------------------------------------------------------------------------------------------
def probe_loader(ip: str, timeout: float = 1.5) -> str | None:
    """Ask a host on TCP 3232 for its version. Returns the reply if it is a Meshtastic OTA loader."""
    try:
        with socket.create_connection((ip, OTA_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"VERSION\n")
            data = b""
            while not data.endswith(b"\n") and len(data) < 200:
                chunk = s.recv(200)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return None
    reply = data.decode("ascii", "replace").strip()
    return reply if reply.startswith("OK ") else None


def supervisor_subnets() -> list[str]:
    """The Home Assistant host's IPv4 networks, from the Supervisor API (needs hassio_api)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return []
    try:
        req = urllib.request.Request("http://supervisor/network/info", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - fixed internal URL
            info = json.load(r)["data"]
    except (OSError, ValueError, KeyError):
        return []
    nets = []
    for iface in info.get("interfaces", []):
        for addr in (iface.get("ipv4") or {}).get("address", []):
            try:
                net = ipaddress.ip_interface(addr).network
            except ValueError:
                continue
            if net.prefixlen >= 22 and not net.is_loopback and str(net) not in nets:
                nets.append(str(net))
    return nets


def local_subnets() -> list[str]:
    """Fallback when not under Home Assistant: the /24 of this machine's outbound address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            ip = s.getsockname()[0]
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        return [] if net.is_loopback or ip.startswith("172.") else [str(net)]
    except OSError:
        return []


def find_loader(job: Job, subnets: list[str], manual_ip: str = "", timeout: float = 240) -> tuple[str, str]:
    """Wait for the OTA loader to appear: listen for its UDP broadcasts, try the given address,
    and scan the subnets on TCP 3232. Returns (ip, VERSION reply)."""
    heard: list[str] = []
    stop = threading.Event()

    def listen():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                u.bind(("", OTA_PORT))
                u.settimeout(1)
                while not stop.is_set():
                    try:
                        msg, (ip, _port) = u.recvfrom(256)
                    except socket.timeout:
                        continue
                    if msg.startswith(b"Meshtastic_") and ip not in heard:
                        job.log(f"Heard {msg.decode('ascii', 'replace')} at {ip}")
                        heard.append(ip)
        except OSError as e:
            job.log(f"Not listening for loader broadcasts ({e})")

    threading.Thread(target=listen, daemon=True).start()
    hosts: list[str] = []
    for net in subnets:
        hosts += [str(h) for h in ipaddress.ip_network(net, strict=False).hosts()][:1024]
    deadline = time.time() + timeout
    try:
        with ThreadPoolExecutor(max_workers=64) as pool:
            while time.time() < deadline:
                job.check_cancel()
                for ip in ([manual_ip] if manual_ip else []) + heard:
                    reply = probe_loader(ip)
                    if reply:
                        return ip, reply
                if hosts:
                    for ip, reply in zip(hosts, pool.map(lambda h: probe_loader(h, 0.6), hosts)):
                        if reply:
                            return ip, reply
                time.sleep(3)
    finally:
        stop.set()
    raise FirmwareError("the collar's update loader did not show up on the network")


def upload(job: Job, ip: str, image: bytes, sha_hex: str) -> None:
    """Stream an app image to the OTA loader (protocol of meshtastic/esp32-unified-ota)."""
    with socket.create_connection((ip, OTA_PORT), timeout=15) as s:
        f = s.makefile("rb")

        def line(timeout: float) -> str:
            s.settimeout(timeout)
            raw = f.readline(200)
            if not raw:
                raise FirmwareError("the collar closed the connection")
            return raw.decode("ascii", "replace").strip()

        s.sendall(f"OTA {len(image)} {sha_hex}\n".encode())
        while True:
            reply = line(120)
            if reply == "OK":
                break
            if reply == "ERASING":
                job.set_step("Collar is erasing its app slot", 0.0)
            elif reply.startswith("ERR"):
                raise FirmwareError(f"collar refused the image: {reply[4:]}")
        job.set_step(f"Sending {len(image) // 1024} KB", 0.0)
        s.settimeout(30)
        for i in range(0, len(image), 1024):
            job.check_cancel()
            s.sendall(image[i:i + 1024])
            job.progress = min(1.0, (i + 1024) / len(image))
        job.set_step("Collar is checking the image", None)
        while True:
            reply = line(180)
            if reply == "OK":
                return
            if reply.startswith("ERR"):
                raise FirmwareError(f"collar rejected the image: {reply[4:]}")


def wifi_ota(job: Job, builds: BuildStore, build_id: str, link, node_id: str, wifi: dict | None,
             subnets: list[str], manual_ip: str = "", resume: bool = False,
             on_update_mode: Callable[[str], None] | None = None) -> dict:
    """Update one collar over Wi-Fi. With resume=True the collar is already waiting in its
    loader for this build (an earlier attempt sent it there), so go straight to finding it."""
    image_path = builds.file(build_id, "app")
    m = builds.manifest(build_id)
    image = image_path.read_bytes()
    sha_hex = m["files"]["app"]["sha256"]
    if hashlib.sha256(image).hexdigest() != sha_hex:
        raise FirmwareError("the stored image is corrupt; download it again")

    if not resume:
        if not link or not link.connected:
            raise FirmwareError("the base station's radio is not connected")
        job.set_step("Asking the collar for its firmware details (over the mesh, can take a minute)")
        meta = link.get_metadata(node_id)
        job.log(f"Collar runs Meshtastic {meta.firmware_version}")
        if not meta.hasWifi:
            raise FirmwareError("this collar's firmware has no Wi-Fi, so it can only be updated over USB")
        if wifi:
            job.set_step(f"Saving the Wi-Fi network \"{wifi['ssid']}\" on the collar (it restarts to apply it)")
            link.set_wifi(node_id, wifi["ssid"], wifi["psk"])
            time.sleep(20)
            for attempt in range(6):
                job.check_cancel()
                try:
                    link.get_metadata(node_id, timeout=45)
                    break
                except (TimeoutError, RuntimeError):
                    job.log("Waiting for the collar to come back")
            else:
                raise FirmwareError("the collar did not come back after saving the Wi-Fi settings")
        job.set_step("Sending the collar into update mode")
        link.request_ota(node_id, bytes.fromhex(sha_hex))
        if on_update_mode:
            on_update_mode(build_id)
        time.sleep(5)

    job.set_step("Looking for the collar on Wi-Fi" + (f" ({', '.join(subnets)})" if subnets else ""))
    ip, version = find_loader(job, subnets, manual_ip)
    job.log(f"Found the update loader at {ip}: {version}")
    upload(job, ip, image, sha_hex)
    job.set_step("Image accepted. The collar is restarting into the new firmware.", 1.0)
    return {"build": build_id, "commit": m.get("geocollar_commit"), "ip": ip}
