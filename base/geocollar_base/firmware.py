# SPDX-License-Identifier: GPL-3.0-or-later
"""Collar firmware: get a build, then flash it over USB.

Builds come from the "firmware-latest" release that CI publishes from main, or are compiled on
this machine from the repository's source (slow, and needs a few GB of disk). Each build is a
folder with manifest.json, written by firmware/package.py. Flashing downloads the newest build by
itself; checking for updates only reads the release's manifest, to tell the owner about it.

Flashing is over USB only:
- USB on this machine: esptool, through the app's serial access.
- USB from the browser: done in the page with esptool-js; the page fetches the files from here.

There are no updates over Wi-Fi. A build still carries Meshtastic's update loader (older versions
of this app expect the file), but nothing here writes it to a collar: a full install erases it,
and without it a collar cannot be sent into an over-the-air update at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import protocol

log = logging.getLogger(__name__)

DEFAULT_REPO = "deathvenom-qvp/Mesh_GeoCollar"
RELEASE_TAG = "firmware-latest"
KEEP_BUILDS = 3
USER_AGENT = "geocollar-base"
FIXED_PIN = 1  # meshtastic Config.BluetoothConfig.PairingMode.FIXED_PIN (checked in tests)
# How often the base may check for newer collar firmware by itself, in seconds.
CHECK_EVERY = {"never": None, "daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}


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


def identity(m: dict | None) -> dict | None:
    """What tells one collar firmware from another, from a build's manifest: the commit it was
    built from, git's ID of the firmware/GeoCollar folder at that commit (builds made before 0.18
    lack it), and the Meshtastic version."""
    if not m:
        return None
    return {"commit": m.get("geocollar_commit"), "source": m.get("geocollar_source"),
            "meshtastic": m.get("meshtastic_version")}


def identity_of_latest(latest: dict | None) -> dict | None:
    """The identity kept from the last update check, or None before the first one."""
    return latest if latest and (latest.get("commit") or latest.get("source")) else None


def same_firmware(a: dict | None, b: dict | None) -> bool | None:
    """Whether two identities are the same collar firmware; None when that cannot be told.
    Every change to this repository makes a new build, also one that only touches the base app.
    So the source ID and the Meshtastic version decide; the commit only when a source ID is missing."""
    if not a or not b:
        return None
    if a.get("commit") and a.get("commit") == b.get("commit"):
        return True
    if a.get("source") and b.get("source"):
        return a["source"] == b["source"] and a.get("meshtastic") == b.get("meshtastic")
    if a.get("commit") and b.get("commit"):
        return False
    return None


def _get_json(url: str, timeout: float = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URL built here
        return json.loads(r.read(1 << 20))


def latest_manifest(repo: str = DEFAULT_REPO) -> dict:
    """The manifest of the newest CI build of main, without its images: enough to say whether
    there is newer firmware."""
    try:
        m = _get_json(f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/manifest.json")
    except (OSError, ValueError) as e:
        raise FirmwareError(f"could not reach github.com/{repo} ({e})") from e
    if not isinstance(m, dict):
        raise FirmwareError("the latest build's manifest is not valid")
    check_manifest(m)
    return m


def source_of_commit(repo: str, commit: str) -> str | None:
    """git's ID of the firmware/GeoCollar folder at a commit, asked from GitHub: for a collar
    flashed with a build that did not carry it yet. None if GitHub can't be asked."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or "") or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        return None
    try:
        entries = _get_json(f"https://api.github.com/repos/{repo}/contents/firmware?ref={commit}", timeout=15)
    except (OSError, ValueError):
        return None
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("name") == "GeoCollar" and e.get("type") == "dir":
            sha = e.get("sha")
            return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None
    return None


def notify_home_assistant(title: str, message: str, notification_id: str) -> bool:
    """A persistent notification in Home Assistant (needs homeassistant_api in config.yaml).
    False outside Home Assistant or when it can't be reached."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    body = json.dumps({"title": title, "message": message, "notification_id": notification_id}).encode()
    req = urllib.request.Request("http://supervisor/core/api/services/persistent_notification/create", data=body,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310 - fixed internal URL
            return True
    except OSError as e:
        log.warning("could not notify Home Assistant: %s", e)
        return False


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


def latest_build(job: Job, builds: BuildStore, repo: str = DEFAULT_REPO) -> tuple[dict, bool]:
    """The build to flash when none was picked: the newest one, downloaded now unless it is here
    already. If GitHub can't be reached, the newest build already on this machine, and the log
    says so. Returns the build and whether it is known to be the latest."""
    try:
        return fetch_release(job, builds, repo), True
    except (FirmwareError, OSError) as e:
        job.check_cancel()
        have = builds.list()
        if not have:
            raise FirmwareError(f"{e}. There is no firmware on this machine to use instead") from e
        m = have[0]
        age = max(0, time.time() - m.get("added_at", 0)) / 86400
        job.log(f"Could not get the latest firmware: {e}")
        job.log(f"Using the newest build already here instead: {(m.get('geocollar_commit') or 'unknown')[:7]}, "
                f"{'built here' if m.get('source') == 'local' else 'downloaded'} {age:.0f} days ago")
        return m, False


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
        # From the repository asked for, also when the copy on disk was cloned from another one.
        _run(job, ["git", "-C", str(src), "remote", "set-url", "origin", url])
        # A branch or a tag only: a bare name could also be another ref, such as an unmerged pull
        # request (refs/pull/N/head), whose build script would then run here.
        try:
            _run(job, ["git", "-C", str(src), "fetch", "--depth", "1", "origin", f"refs/heads/{ref}"])
        except FirmwareError:
            job.check_cancel()
            _run(job, ["git", "-C", str(src), "fetch", "--depth", "1", "origin", f"refs/tags/{ref}"])
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


def usb_identity(port: str) -> tuple | None:
    """What stays the same when a USB serial device comes back under another name. A collar that
    is reset by hand while this app has its port open returns as, say, ttyACM1 instead of ttyACM0."""
    for p in serial_ports():
        if p["device"] == port and p["vid"] is not None and p["serial_number"]:
            return (p["vid"], p["pid"], p["serial_number"])
    return None


def port_now(port: str, identity: tuple | None) -> str:
    """Where that device is now: the port it was on while that still exists."""
    if identity is None:
        return port
    ports = serial_ports()
    if any(p["device"] == port for p in ports):
        return port
    for p in ports:
        if (p["vid"], p["pid"], p["serial_number"]) == identity:
            return p["device"]
    return port


def node_id_from_mac(mac: str) -> str | None:
    """Meshtastic's node number is the last four bytes of the ESP32's base MAC address
    (NodeDB::pickNewNodeNum), so esptool's "MAC: ..." line tells us the node ID before the collar
    even boots. Meshtastic only picks another number if that one clashes with a node it knows."""
    parts = re.findall(r"[0-9a-fA-F]{2}", mac or "")
    if len(parts) != 6:
        return None
    return "!" + "".join(parts[2:]).lower()


def flash_usb(job: Job, builds: BuildStore, build_id: str, port: str, mode: str, base_radio: str = "") -> dict:
    """mode "update": write the app, and clear otadata so the collar boots it (this also gets a
    collar out of Meshtastic's update loader, if an older version of this app left it there).
    Settings, keys and fences are kept.
    mode "full": erase everything first. The collar comes back blank and must be set up again.

    Meshtastic's update loader is not written in either mode (see the top of this file).

    The result has manual_reset set when esptool says it could not restart the chip: an ESP32-S3
    put into download mode with its buttons stays there until RST is pressed."""
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

    job.set_step(f"Flashing {port} ({'full install' if mode == 'full' else 'update, settings kept'})", 0.0)
    files = 2 if mode == "update" else 1
    seen = [0]

    def on_line(line: str) -> None:
        if line.startswith("Writing at"):
            pct = re.search(r"\((\d+) ?%\)", line)
            if pct:
                job.progress = (seen[0] + int(pct.group(1)) / 100) / files
            return  # too chatty for the log
        if line.startswith("Wrote "):
            seen[0] += 1
        if re.search(r"reset the chip manually", line, re.I):
            found["manual_reset"] = True
        mac = re.match(r"MAC:\s*([0-9a-fA-F:]{17})", line.strip())
        if mac and not found.get("node_id"):
            found["mac"] = mac.group(1).lower()
            found["node_id"] = node_id_from_mac(mac.group(1))
            line += f"  (Meshtastic node {found['node_id']})"
        job.log(line)

    found: dict = {}
    _run(job, cmd, on_line=on_line)
    job.set_step("Flashed. Press the collar's RST button once to start it." if found.get("manual_reset")
                 else "Flashed. The collar is restarting.", 1.0)
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


def _open_collar(job: Job, port: str, open_iface, wait_s: float, what: str, identity: tuple | None = None,
                 press_reset: bool = False):
    """Connect to the collar, trying again while it starts. A collar that was just flashed does
    not always restart by itself, so the first wait says what to do about that (press_reset: it is
    known that it will not). identity (see usb_identity) follows the collar to another port name."""
    first = what == "to start"
    hint = ""
    if first:
        hint = (": press its RST button once now" if press_reset
                else ". If nothing happens in a few seconds, press its RST button once")
    job.set_step(f"Waiting for the collar on {port} {what}{hint}", None)
    deadline = time.monotonic() + wait_s
    last = port
    while True:
        job.check_cancel()
        now = port_now(port, identity)
        if now != last:
            job.log(f"The collar is on {now} now")
            last = now
        try:
            return open_iface(now)
        except Exception as e:  # noqa: BLE001 - still booting; any failure means try again
            if time.monotonic() > deadline:
                hint = (". If it had to be restarted by hand, its port may have changed: pick the collar again "
                        "and press \"Set up only\"") if first else ""
                raise FirmwareError(f"could not talk to the collar on {now}: {e}{hint}") from e
            time.sleep(3)


def setup_collar(job: Job, port: str, base_iface, name: str = "", open_iface=open_serial_node,
                 boot_wait_s: float = 60, lock: bool = True, restart_s: float = 9,
                 press_reset: bool = False) -> dict:
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
    - A Bluetooth or security change shuts Bluetooth down and restarts the collar 7 s later. A
      second such change in the same boot gets lost (the collar never saved the key and lock that
      followed the Bluetooth change), so each gets its own boot: Bluetooth, restart, then the key
      and lock, restart. Then the setup reconnects, checks that everything stuck, and writes the
      key and lock once more if they didn't.

    press_reset: the collar was just flashed and could not be restarted over USB, so the first wait
    asks for its RST button.

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

    identity = usb_identity(port)  # None for a collar on the browser's computer: the page follows it
    iface = _open_collar(job, port, open_iface, boot_wait_s, "to start", identity, press_reset)
    try:
        first_id = _node_id(iface)
        job.log(f"Collar is Meshtastic node {first_id}")
        node = iface.localNode
        sec = node.localConfig.security
        if bytes(sec.public_key) == base_key:
            raise FirmwareError("that is the base station's own radio, not a collar")
        first_key = _key_b64(sec)  # empty on a fresh collar: its key is made when the region is set
        if sec.is_managed:
            if base_key in [bytes(k) for k in sec.admin_key]:
                job.set_step("This collar is already locked to this base; nothing to change over USB.", 1.0)
                return {"node_id": first_id, "already_locked": True, "public_key": _key_b64(sec)}
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
        iface = _open_collar(job, port, open_iface, max(30, boot_wait_s), "to come back with its new key", identity)
    else:
        iface = _open_collar(job, port, open_iface, max(30, boot_wait_s), "again", identity)
    pin = None
    locked_msg = ("Added the base's public key to the collar's admin keys"
                  + (" and locked it to this base: its settings can now only be changed by this base, "
                     "or by a full install that erases it" if lock else ""))
    try:
        node_id = _node_id(iface)
        if node_id != first_id:
            job.log(f"Its node ID is now {node_id} (Meshtastic derives it from the key it just made)")
        node = iface.localNode
        _check_same_collar(node, first_key)
        collar_key = _key_b64(node.localConfig.security)  # the base's radio needs it to write to the collar
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
            long_name, short_name = protocol.mesh_names(name)  # as the collar names itself later
            node.setOwner(long_name=long_name, short_name=short_name)
            job.log(f"Named the collar {name}")

        # 3. Bluetooth (the lost beacon needs it), with a random PIN when locking: a fixed one instead
        # of Meshtastic's default 123456 (boards without a screen) or one shown on a screen inside
        # the collar's case. Saving it restarts the collar.
        bt = node.localConfig.bluetooth
        if lock:
            import secrets

            pin = secrets.randbelow(900000) + 100000
        wrote_bt = bool(lock) or not bt.enabled
        if wrote_bt:
            bt.enabled = True
            if lock:
                bt.mode = FIXED_PIN
                bt.fixed_pin = pin
            node.writeConfig("bluetooth")
            job.log("Bluetooth on" + (", pairing PIN set (shown in the collar's panel)" if pin else ""))
        else:
            _write_security(node, base_key, lock)  # nothing restarted yet, so it can go now
            job.log(locked_msg)
    finally:
        _close(iface)

    # 4. The base's key and the lock, alone in the boot after Bluetooth's restart.
    if wrote_bt:
        job.set_step("Waiting for the collar to restart with Bluetooth on", 0.45)
        time.sleep(restart_s)
        iface = _open_collar(job, port, open_iface, boot_wait_s, "to restart", identity)
        try:
            node_id = _node_id(iface) or node_id
            node = iface.localNode
            _check_same_collar(node, collar_key)
            if _bluetooth_problem(node.localConfig.bluetooth, pin):
                raise FirmwareError("the collar restarted without its Bluetooth settings, so it was not locked. "
                                    "Try again; if it keeps failing, do a full install first")
            job.set_step("Locking the collar to this base", 0.55)
            _write_security(node, base_key, lock)
            job.log(locked_msg)
        finally:
            _close(iface)

    # 5. Check, after the restart, that it all stuck. If the key or the lock didn't, the collar still
    # takes settings over USB, so they get one more try.
    problems: list[str] = []
    final_id = None
    for attempt in (1, 2):
        job.set_step("Waiting for the collar to restart, to check its settings", 0.7)
        time.sleep(restart_s)
        try:
            iface = _open_collar(job, port, open_iface, boot_wait_s, "to restart", identity)
        except FirmwareError as e:
            job.log(f"Could not reconnect to check the settings ({e}). The collar shows here once it reports.")
            job.set_step("Done. Unchecked: the collar didn't reconnect over USB after restarting.", 1.0)
            return {"node_id": node_id, "previous_node_id": first_id, "ble_pin": pin, "locked": lock,
                    "checked": False, "public_key": collar_key}
        again = False
        try:
            node = iface.localNode
            _check_same_collar(node, collar_key)
            problems = _setup_problems(node, base_lora, base_chans, base_key, lock, pin)
            final_id = _node_id(iface)
            collar_key = _key_b64(node.localConfig.security) or collar_key
            if (attempt == 1 and problems and set(problems) <= {"the base's key", "lock"}
                    and not node.localConfig.security.is_managed):
                job.log("The collar restarted without " + " and ".join(problems) + "; writing them again")
                _write_security(node, base_key, lock)
                again = True
        finally:
            _close(iface)
        if not again:
            break
    if problems:
        raise FirmwareError("the collar restarted without these settings: " + ", ".join(problems)
                            + ". Try again; if it keeps failing, do a full install first")
    job.log("Checked after the restart: the collar kept its new settings")
    job.set_step("Done. The collar is set up for this base and reports within a minute or two.", 1.0)
    return {"node_id": final_id or node_id, "previous_node_id": first_id, "ble_pin": pin, "locked": lock,
            "checked": True, "public_key": collar_key}


def _key_b64(sec) -> str:
    import base64

    key = bytes(sec.public_key)
    return base64.b64encode(key).decode() if len(key) == 32 else ""


def _check_same_collar(node, key_b64: str) -> None:
    """A collar keeps its key pair across restarts. If what answers after one has another key, or
    is blank, it is a different device: two were plugged in and the reconnect found the other.
    Stop before anything is written to it, above all the lock."""
    now = _key_b64(node.localConfig.security)
    blank = not now and not node.localConfig.lora.region  # never set up, so not the collar being set up
    if key_b64 and (blank or (now and now != key_b64)):
        raise FirmwareError("a different device answered after the collar restarted, so nothing more was written. "
                            "Unplug other Meshtastic devices from that computer and run the setup again")


def _write_security(node, base_key: bytes, lock: bool) -> None:
    """Put the base's key in the collar's admin keys and, with lock, turn on managed mode. Once
    managed, the collar ignores settings over USB, so this is always the last write."""
    sec = node.localConfig.security
    keys = [bytes(k) for k in sec.admin_key]
    if base_key not in keys:
        if len(keys) >= 3:  # Meshtastic keeps three admin keys; replace the last one
            del sec.admin_key[-1]
        sec.admin_key.append(base_key)
    sec.admin_channel_enabled = False  # the old shared-channel admin: off
    sec.is_managed = bool(lock)
    node.writeConfig("security")


def _bluetooth_problem(bt, pin) -> bool:
    # The PIN reads back as 0 on firmware that hides it from USB clients; then only the mode is checked.
    return not bt.enabled or bool(pin and (bt.mode != FIXED_PIN or (bt.fixed_pin and bt.fixed_pin != pin)))


def _setup_problems(node, base_lora, base_chans, base_key: bytes, lock: bool, pin) -> list[str]:
    """What the collar is missing after setup, read back after it restarted."""
    cfg = node.localConfig
    problems = []
    if cfg.lora.region != base_lora.region:
        problems.append("region")
    if cfg.lora.modem_preset != base_lora.modem_preset:
        problems.append("preset")
    if base_chans and node.channels and bytes(node.channels[0].settings.psk) != bytes(base_chans[0].settings.psk):
        problems.append("primary channel")
    if _bluetooth_problem(cfg.bluetooth, pin):
        problems.append("Bluetooth")
    if base_key not in [bytes(k) for k in cfg.security.admin_key]:
        problems.append("the base's key")
    if bool(cfg.security.is_managed) != bool(lock):
        problems.append("lock")
    return problems
