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


def test_node_id_from_mac_while_flashing(tmp_path, monkeypatch):
    assert fw.node_id_from_mac("34:85:18:AB:cd:01") == "!18abcd01"
    assert fw.node_id_from_mac("nonsense") is None
    store = fw.BuildStore(tmp_path)
    make_build(tmp_path / "in")
    m = store.add(tmp_path / "in", "release")
    monkeypatch.setattr(fw, "serial_ports", lambda exclude="": [{"device": "/dev/ttyACM0"}])

    def fake_run(job, cmd, on_line=None, **kw):
        for line in ["Chip is ESP32-S3 (QFN56) (revision v0.2)", "MAC: 34:85:18:ab:cd:01", "Wrote 1 bytes"]:
            on_line(line)

    monkeypatch.setattr(fw, "_run", fake_run)
    job = fw.Job("usb", "t")
    r = fw.flash_usb(job, store, m["id"], "/dev/ttyACM0", "full")
    assert r["node_id"] == "!18abcd01" and r["mac"] == "34:85:18:ab:cd:01"
    assert any("Meshtastic node !18abcd01" in line for line in job.view()["log"])


class FakeNode:
    def __init__(self, admin_keys=(), bluetooth=False, public_key=b""):
        from meshtastic.protobuf import channel_pb2, localonly_pb2

        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.security.admin_key.extend(admin_keys)
        self.localConfig.security.public_key = public_key
        self.localConfig.bluetooth.enabled = bluetooth
        self.channels = [channel_pb2.Channel(index=i) for i in range(8)]
        self.calls = []

    def beginSettingsTransaction(self):
        self.calls.append("begin")

    def commitSettingsTransaction(self):
        self.calls.append("commit")

    def writeConfig(self, name):
        self.calls.append(f"config:{name}")

    def writeChannel(self, i):
        self.calls.append(f"channel:{i}")

    def setOwner(self, long_name=None, short_name=None):
        self.calls.append(f"owner:{long_name}/{short_name}")


class FakeIface:
    def __init__(self, node, num=0x18ABCD01):
        self.localNode = node
        self.num = num
        self.closed = False

    def getMyNodeInfo(self):
        return {"num": self.num, "user": {"id": f"!{self.num:08x}"}}

    def close(self):
        self.closed = True


def test_setup_collar_over_usb(monkeypatch):
    from meshtastic.protobuf import channel_pb2, config_pb2

    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    base = FakeNode(public_key=b"B" * 32)
    base.localConfig.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
    base.localConfig.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST
    base.channels[0].role = channel_pb2.Channel.Role.PRIMARY
    base.channels[0].settings.psk = b"\x01"
    collar = FakeNode(admin_keys=[b"1" * 32, b"2" * 32, b"3" * 32])
    iface = FakeIface(collar)
    tries = []

    def open_iface(port):
        tries.append(port)
        if len(tries) < 3:
            raise OSError("still booting")
        return iface

    job = fw.Job("usb", "t")
    r = fw.setup_collar(job, "/dev/ttyACM0", FakeIface(base, 0x11111111), "Rex the Dog", open_iface=open_iface)
    assert r["node_id"] == "!18abcd01" and r["locked"] and 100000 <= r["ble_pin"] <= 999999
    assert len(tries) == 3 and iface.closed
    assert collar.localConfig.security.is_managed and not collar.localConfig.security.admin_channel_enabled
    assert collar.localConfig.bluetooth.fixed_pin == r["ble_pin"] and collar.localConfig.bluetooth.mode == 1  # FIXED_PIN
    keys = [bytes(k) for k in collar.localConfig.security.admin_key]
    assert keys == [b"1" * 32, b"2" * 32, b"B" * 32]  # three at most: the last one gives way
    assert collar.localConfig.lora.region == base.localConfig.lora.region
    assert collar.localConfig.lora.modem_preset == base.localConfig.lora.modem_preset
    assert collar.localConfig.bluetooth.enabled
    assert collar.channels[0].settings.psk == b"\x01"
    assert collar.calls[0] == "begin" and collar.calls[-1] == "commit"
    assert "owner:Rex the Dog/RTD" in collar.calls
    # Locking comes last, after everything else is written.
    assert collar.calls[-2] == "config:security" and collar.calls.index("config:lora") < collar.calls.index("config:security")

    # Again: locked to this base already, so nothing is written.
    collar.calls.clear()
    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", FakeIface(base), "", open_iface=lambda p: iface)
    assert r["already_locked"] and collar.calls == [] and len(collar.localConfig.security.admin_key) == 3

    # Locked to someone else's base: refused; only a full install frees it.
    other = FakeNode(public_key=b"O" * 32)
    with pytest.raises(fw.FirmwareError, match="locked to another base"):
        fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", FakeIface(other), "", open_iface=lambda p: iface)

    # Unlocked set-up when asked.
    free = FakeNode()
    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", FakeIface(base), "", open_iface=lambda p: FakeIface(free), lock=False)
    assert not free.localConfig.security.is_managed and r["ble_pin"] is None

    with pytest.raises(fw.FirmwareError, match="not connected"):
        fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", None)


def test_new_collars_from_usb_browser_and_mesh(client, tmp_path, monkeypatch):
    c, app = client
    make_build(tmp_path / "in")
    m = app.state.builds.add(tmp_path / "in", "release")

    # Flashed from the browser, which read the MAC address: added as a new collar.
    r = c.post("/api/firmware/flashed", json={"node_id": "!18ABCD01", "build": m["id"], "name": "Rex"}).json()
    assert r["new_collar"] and r["collar"]["name"] == "Rex" and r["collar"]["firmware"]["build"] == m["id"]
    r = c.post("/api/firmware/flashed", json={"node_id": "!18abcd01", "build": m["id"]}).json()
    assert not r["new_collar"] and r["collar"]["name"] == "Rex"
    assert c.post("/api/firmware/flashed", json={"node_id": "rex", "build": m["id"]}).status_code == 400

    # Flashed on the base: the node ID comes from esptool's MAC line.
    monkeypatch.setattr(fw, "flash_usb", lambda job, builds, b, port, mode, base_radio="": {"node_id": "!22222222", "mode": mode})
    job = c.post("/api/firmware/flash-usb", json={"build": m["id"], "port": "/dev/ttyACM0", "mode": "full", "name": "Biscuit"}).json()
    done = wait_job(c, job["id"])
    assert done["state"] == "done" and done["result"]["new_collar"]
    assert c.get("/api/collars").json()["!22222222"]["name"] == "Biscuit"

    # Heard on the mesh: listed until added.
    app.state.on_hello("!33333333", {"has_admin_key": False, "long_name": "Meshtastic 3333"})
    app.state.on_hello("!18abcd01", {"has_admin_key": True})  # already on the base: ignored
    found = c.get("/api/discovered").json()
    assert list(found) == ["!33333333"] and found["!33333333"]["long_name"] == "Meshtastic 3333"
    c.post("/api/collars", json={"node_id": "!33333333", "name": "Pip"})
    assert c.get("/api/discovered").json() == {}


def test_socket_stream_relays_bytes_both_ways():
    sent = []
    s = fw.SocketStream(sent.append, read_timeout=0.05)
    assert s.read(1) == b""  # nothing yet: times out like pyserial
    s.feed(b"\x94\xc3ab")
    assert s.read(2) == b"\x94\xc3" and s.read(5) == b"ab"
    s.write(b"to the collar")
    s.flush()
    s.close()  # the interface closing doesn't end the relay
    assert sent == [b"to the collar"]
    s.shutdown()
    with pytest.raises(OSError):
        s.read(1)
    with pytest.raises(OSError):
        s.write(b"x")


def test_setup_from_the_browser_over_a_websocket(tmp_path, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    def fake_setup(job, port, base_iface, name="", open_iface=None, boot_wait_s=60, lock=True):
        stream = open_iface(port)  # patched below to hand back the stream itself
        stream.write(b"ping")
        reply = b""
        while len(reply) < 4:
            reply += stream.read(4 - len(reply))
        assert reply == b"pong" and name == "Rex" and lock is False
        return {"node_id": "!a1b2c3d4", "ble_pin": None, "locked": False}

    monkeypatch.setattr(fw, "setup_collar", fake_setup)
    monkeypatch.setattr(fw, "open_socket_node", lambda stream: stream)
    c = TestClient(create_app({"data_dir": str(tmp_path)}))
    with c.websocket_connect("/api/firmware/setup-web?name=Rex&lock=false",
                             headers={"origin": "http://testserver"}) as ws:
        job = ws.receive_json()["job"]
        assert ws.receive_bytes() == b"ping"
        ws.send_bytes(b"pong")
        assert ws.receive_json() == {"done": True}
    for _ in range(100):
        j = c.get(f"/api/firmware/jobs/{job}").json()
        if j["state"] != "running":
            break
        time.sleep(0.05)
    assert j["state"] == "done", j
    assert c.get("/api/collars").json()["!a1b2c3d4"]["name"] == "Rex"

    # Another site's page can't open it: WebSockets ignore CORS.
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/firmware/setup-web", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


class FakeSerialDevice:
    """Speaks just enough of Meshtastic's serial API (framed ToRadio / FromRadio) for the real
    Meshtastic library to connect over a SocketStream and send admin messages."""

    def __init__(self):
        self.buf = b""
        self.stream = fw.SocketStream(self.on_bytes, read_timeout=0.05)
        self.admin = []

    @staticmethod
    def frame(msg) -> bytes:
        b = msg.SerializeToString()
        return bytes([0x94, 0xC3, len(b) >> 8, len(b) & 255]) + b

    def on_bytes(self, data):
        from meshtastic.protobuf import admin_pb2, mesh_pb2

        self.buf += data
        while (i := self.buf.find(b"\x94\xc3")) >= 0 and len(self.buf) >= i + 4:
            n = (self.buf[i + 2] << 8) | self.buf[i + 3]
            if len(self.buf) < i + 4 + n:
                return
            tr = mesh_pb2.ToRadio()
            tr.ParseFromString(self.buf[i + 4 : i + 4 + n])
            self.buf = self.buf[i + 4 + n :]
            if tr.HasField("want_config_id"):
                threading.Thread(target=self.send_config, args=(tr.want_config_id,), daemon=True).start()
            elif tr.HasField("packet") and tr.packet.decoded.portnum == 6:  # ADMIN_APP
                m = admin_pb2.AdminMessage()
                m.ParseFromString(tr.packet.decoded.payload)
                self.admin.append(m)

    def send_config(self, config_id):
        from meshtastic.protobuf import mesh_pb2

        out = [mesh_pb2.FromRadio()]
        out[0].my_info.my_node_num = 0xA1B2C3D4
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = 0xA1B2C3D4
        fr.node_info.user.id = "!a1b2c3d4"
        out.append(fr)
        for name in ("device", "lora", "security", "bluetooth"):
            fr = mesh_pb2.FromRadio()
            getattr(fr.config, name).SetInParent()
            out.append(fr)
        for i in range(8):
            fr = mesh_pb2.FromRadio()
            fr.channel.index = i
            fr.channel.role = 1 if i == 0 else 0
            out.append(fr)
        fr = mesh_pb2.FromRadio()
        fr.config_complete_id = config_id
        out.append(fr)
        for f in out:
            self.stream.feed(self.frame(f))


def test_setup_runs_over_the_browser_relay_with_the_real_meshtastic_library():
    from meshtastic.protobuf import channel_pb2, localonly_pb2

    base_node = type("N", (), {})()
    base_node.localConfig = localonly_pb2.LocalConfig()
    base_node.localConfig.security.public_key = b"K" * 32
    base_node.localConfig.lora.region = 1
    base_node.channels = [channel_pb2.Channel(index=i) for i in range(8)]
    base_node.channels[0].role = 1
    base_node.channels[0].settings.psk = b"\x01"
    base_iface = type("I", (), {"localNode": base_node})()

    dev = FakeSerialDevice()
    job = fw.Job("usb", "test")
    result = fw.setup_collar(job, "this computer's USB", base_iface, "Rex",
                             open_iface=lambda _p: fw.open_socket_node(dev.stream), boot_wait_s=5)
    time.sleep(0.3)
    assert result["node_id"] == "!a1b2c3d4" and result["locked"] and 100000 <= result["ble_pin"] <= 999999
    kinds = [m.WhichOneof("payload_variant") for m in dev.admin]
    assert "begin_edit_settings" in kinds and kinds[-1] == "commit_edit_settings"
    assert kinds.index("begin_edit_settings") < kinds.index("set_config")
    security = [m.set_config.security for m in dev.admin if m.WhichOneof("payload_variant") == "set_config"
                and m.set_config.WhichOneof("payload_variant") == "security"][-1]
    assert list(security.admin_key) == [b"K" * 32] and security.is_managed
    assert any(m.WhichOneof("payload_variant") == "set_owner" and m.set_owner.long_name == "Rex" for m in dev.admin)
