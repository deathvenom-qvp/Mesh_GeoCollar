# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import importlib.util
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from geocollar_base import firmware as fw
from geocollar_base.app import create_app

REPO = Path(__file__).resolve().parents[2]


def load_package_py():
    spec = importlib.util.spec_from_file_location("package", REPO / "firmware" / "package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def partition_table() -> bytes:
    """The Heltec Wireless Tracker's table (Meshtastic default_8MB.csv) in ESP-IDF binary form."""
    rows = [
        (1, 0x02, 0x9000, 0x5000, b"nvs"),
        (1, 0x00, 0xE000, 0x2000, b"otadata"),
        (0, 0x10, 0x10000, 0x330000, b"app0"),
        (0, 0x11, 0x340000, 0x330000, b"app1"),
        (1, 0x82, 0x670000, 0x180000, b"spiffs"),
    ]
    out = b"".join(struct.pack("<HBBII16sI", 0x50AA, t, st, off, size, label, 0) for t, st, off, size, label in rows)
    return out + b"\xeb\xeb" + b"\xff" * 30


def test_package_reads_partition_offsets(tmp_path):
    pkg = load_package_py()
    (tmp_path / "partitions.bin").write_bytes(partition_table())
    parts = pkg.read_partitions(tmp_path / "partitions.bin")
    assert pkg.by_subtype(parts, 0, 0x10)["offset"] == 0x10000
    assert pkg.by_subtype(parts, 0, 0x11)["offset"] == 0x340000
    assert pkg.by_subtype(parts, 1, 0x00) == {"type": 1, "subtype": 0, "offset": 0xE000, "size": 0x2000}


def make_build(folder: Path, commit: str = "a" * 40, app: bytes = b"APP" * 1000) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    files = {"app": ("geocollar-x.bin", app), "factory": ("geocollar-x.factory.bin", b"FACTORY" + app),
             "loader": ("mt-esp32s3-ota.bin", b"LOADER")}
    for name, data in files.values():
        (folder / name).write_bytes(data)
    m = {
        "format": 1, "board": "heltec-wireless-tracker", "mcu": "esp32s3", "geocollar_commit": commit,
        "geocollar_subject": "Test build", "meshtastic_version": "v2.8.2.d5f1619", "loader_version": "v1.0.1",
        "offsets": {"factory": 0, "app": 0x10000, "loader": 0x340000, "otadata": 0xE000, "otadata_size": 0x2000},
        "files": {k: {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for k, (n, d) in files.items()},
    }
    (folder / "manifest.json").write_text(json.dumps(m))
    return m


def test_build_store_verifies_and_prunes(tmp_path):
    store = fw.BuildStore(tmp_path)
    make_build(tmp_path / "in1")
    m = store.add(tmp_path / "in1", "release")
    assert store.file(m["id"], "app").read_bytes() == b"APP" * 1000
    assert store.find_commit("a" * 40, "release")["id"] == m["id"]

    bad = tmp_path / "in2"
    make_build(bad, "b" * 40)
    (bad / "geocollar-x.bin").write_bytes(b"tampered")
    with pytest.raises(fw.FirmwareError, match="SHA-256"):
        store.add(bad, "release")

    for i in range(5):
        make_build(tmp_path / f"more{i}", f"{i}" * 40)
        store.add(tmp_path / f"more{i}", "local")
        time.sleep(0.01)
    assert len(store.list()) == fw.KEEP_BUILDS
    with pytest.raises(fw.FirmwareError):
        store.path("../etc")


def test_fetch_release_downloads_and_checks(tmp_path, monkeypatch):
    src = tmp_path / "release"
    make_build(src, "c" * 40)
    served = []

    def fake_download(url, dest, job=None, label=""):
        name = url.rsplit("/", 1)[1]
        served.append(url)
        dest.write_bytes((src / name).read_bytes())

    monkeypatch.setattr(fw, "_download", fake_download)
    store = fw.BuildStore(tmp_path / "fw")
    job = fw.Job("download", "t")
    m = fw.fetch_release(job, store, "owner/repo")
    assert m["source"] == "release" and m["geocollar_commit"] == "c" * 40
    assert served[0] == "https://github.com/owner/repo/releases/download/firmware-latest/manifest.json"
    # Same commit again: nothing more to download.
    n = len(served)
    assert fw.fetch_release(fw.Job("download", "t"), store, "owner/repo")["id"] == m["id"]
    assert len(served) == n + 1


def test_flash_usb_command(tmp_path, monkeypatch):
    store = fw.BuildStore(tmp_path)
    make_build(tmp_path / "in")
    m = store.add(tmp_path / "in", "release")
    ran = []
    monkeypatch.setattr(fw, "serial_ports", lambda exclude="": [{"device": "/dev/ttyACM0"}, {"device": "/dev/ttyUSB0"}])
    monkeypatch.setattr(fw, "_run", lambda job, cmd, **kw: ran.append(cmd))

    fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/ttyACM0", "update")
    cmd = ran[-1]
    i = cmd.index("write_flash")
    assert cmd[cmd.index("--port") + 1] == "/dev/ttyACM0"
    assert cmd[i + 1::2] == ["0xe000", "0x10000", "0x340000"]
    blank = Path(cmd[i + 2])
    assert blank.read_bytes() == b"\xff" * 0x2000  # clears otadata so the new app boots

    fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/ttyACM0", "full")
    cmd = ran[-1]
    i = cmd.index("write_flash")
    assert cmd[i + 1] == "--erase-all" and cmd[i + 2::2] == ["0x0", "0x340000"]

    with pytest.raises(fw.FirmwareError, match="base station"):
        fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/ttyUSB0", "update", base_radio="/dev/ttyUSB0")
    with pytest.raises(fw.FirmwareError, match="not a serial port"):
        fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/nope", "update")


class FakeLoader:
    """A stand-in for the esp32-unified-ota loader's TCP side."""

    def __init__(self, nvs_hash: str):
        self.nvs_hash = nvs_hash
        self.received = b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                f = conn.makefile("rb")
                line = f.readline().decode().strip()
                if line == "VERSION":
                    conn.sendall(b"OK 0 2.8.2 3 v1.0.1\n")
                    continue
                _, size, sha = line.split()
                if sha != self.nvs_hash:
                    conn.sendall(b"ERR Hash Rejected (NVS Mismatch)\n")
                    continue
                conn.sendall(b"ERASING\nOK\n")
                data = f.read(int(size))
                self.received = data
                ok = hashlib.sha256(data).hexdigest() == sha
                conn.sendall(b"OK\n" if ok else b"ERR Hash Mismatch\n")

    def close(self):
        self.sock.close()


def test_upload_and_probe(monkeypatch):
    image = bytes(range(256)) * 50
    sha = hashlib.sha256(image).hexdigest()
    loader = FakeLoader(sha)
    monkeypatch.setattr(fw, "OTA_PORT", loader.port)
    try:
        assert fw.probe_loader("127.0.0.1") == "OK 0 2.8.2 3 v1.0.1"
        job = fw.Job("ota", "t")
        fw.upload(job, "127.0.0.1", image, sha)
        assert loader.received == image and job.step == "Collar is checking the image"
        loader.nvs_hash = "0" * 64
        with pytest.raises(fw.FirmwareError, match="Hash Rejected"):
            fw.upload(fw.Job("ota", "t"), "127.0.0.1", image, sha)
    finally:
        loader.close()


class FakeLink:
    connected = True

    def __init__(self):
        self.calls = []

    def get_metadata(self, node_id, timeout=90):
        self.calls.append("metadata")
        return type("Meta", (), {"firmware_version": "2.8.2.d5f1619", "hasWifi": True})()

    def set_wifi(self, node_id, ssid, psk, timeout=90):
        self.calls.append(("wifi", ssid, psk))

    def request_ota(self, node_id, sha256, timeout=90):
        self.calls.append(("ota", sha256.hex()))


def test_wifi_ota_end_to_end(tmp_path, monkeypatch):
    store = fw.BuildStore(tmp_path)
    make_build(tmp_path / "in")
    m = store.add(tmp_path / "in", "release")
    loader = FakeLoader(m["files"]["app"]["sha256"])
    monkeypatch.setattr(fw, "OTA_PORT", loader.port)
    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    link = FakeLink()
    entered = []
    try:
        result = fw.wifi_ota(fw.Job("ota", "t"), store, m["id"], link, "!12345678", {"ssid": "home", "psk": "pw"},
                             [], manual_ip="127.0.0.1", on_update_mode=entered.append)
    finally:
        loader.close()
    assert result["ip"] == "127.0.0.1"
    assert link.calls == ["metadata", ("wifi", "home", "pw"), "metadata", ("ota", m["files"]["app"]["sha256"])]
    assert entered == [m["id"]]
    assert loader.received == b"APP" * 1000


@pytest.fixture
def client(tmp_path):
    class NoLink:
        connected = False
        last_error = ""

        def start(self):
            pass

        def close(self):
            pass

    app = create_app({"data_dir": str(tmp_path), "mesh_serial": "/dev/ttyUSB0", "ota_subnet": "192.168.5.0/24"}, link=NoLink())
    with TestClient(app) as c:
        yield c, app


def wait_job(c, job_id):
    for _ in range(100):
        j = c.get(f"/api/firmware/jobs/{job_id}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_firmware_api(client, tmp_path):
    c, app = client
    r = c.get("/api/firmware").json()
    assert r["builds"] == [] and r["subnets"] == ["192.168.5.0/24"] and r["wifi"] == {"ssid": "", "has_psk": False}

    assert c.put("/api/firmware/wifi", json={"ssid": "home", "psk": "secret"}).json() == {"ssid": "home", "has_psk": True}
    assert c.put("/api/firmware/wifi", json={"ssid": "home2"}).json() == {"ssid": "home2", "has_psk": True}  # keeps password
    assert "secret" not in json.dumps(c.get("/api/firmware").json())

    make_build(tmp_path / "in")
    m = app.state.builds.add(tmp_path / "in", "release")
    assert c.get(f"/api/firmware/builds/{m['id']}/app").content == b"APP" * 1000
    assert c.get("/api/firmware/builds/nope/app").status_code == 404

    # Wi-Fi update of a simulated collar walks through the steps and records the build.
    sim_id = c.post("/api/sim", json={"lat": 40, "lon": -75}).json()["node_id"]
    job = c.post(f"/api/collars/{sim_id}/firmware/ota", json={"build": m["id"]}).json()
    assert wait_job(c, job["id"])["state"] == "done"
    collar = c.get("/api/collars").json()[sim_id]
    assert collar["firmware"]["commit"] == "a" * 40 and "update_mode" not in collar

    # A real collar needs the radio; USB flashing refuses the base radio's own port.
    c.post("/api/collars", json={"node_id": "!0000abcd"})
    j = c.post("/api/collars/!0000abcd/firmware/ota", json={"build": m["id"]}).json()
    assert wait_job(c, j["id"])["error"] == "the base station's radio is not connected"
    assert c.post("/api/collars/!0000abcd/firmware/ota", json={"build": m["id"], "resume": True}).status_code == 409
    assert c.post("/api/collars/!0000abcd/firmware/ota", json={"build": m["id"], "ip": "nope"}).status_code == 400
    j = c.post("/api/firmware/flash-usb", json={"build": m["id"], "port": "/dev/ttyUSB0"}).json()
    assert "base station" in wait_job(c, j["id"])["error"]
