# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import importlib.util
import json
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
    assert cmd[i + 1::2] == ["0xe000", "0x10000"]  # otadata and the app; never the update loader
    blank = Path(cmd[i + 2])
    assert blank.read_bytes() == b"\xff" * 0x2000  # clears otadata so the new app boots

    fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/ttyACM0", "full")
    cmd = ran[-1]
    i = cmd.index("write_flash")
    assert cmd[i + 1] == "--erase-all" and cmd[i + 2::2] == ["0x0"]
    assert not any("mt-esp32s3-ota" in part for part in cmd)

    # esptool cannot restart an ESP32-S3 that was put into download mode with its buttons. The
    # result says so, and the last step tells the owner to press RST.
    def stuck(job, cmd, on_line=None, **kw):
        for line in ("Wrote 3000 bytes at 0x00010000", "Hard resetting via RTS pin...",
                     "WARNING: ESP32-S3 chip was placed into download mode using GPIO0.",
                     "esptool.py can not exit the download mode over USB. To run the app, reset the chip manually."):
            on_line(line)

    monkeypatch.setattr(fw, "_run", stuck)
    job = fw.Job("usb", "t")
    assert fw.flash_usb(job, store, m["id"], "/dev/ttyACM0", "update")["manual_reset"] is True
    assert "RST" in job.step
    monkeypatch.setattr(fw, "_run", lambda job, cmd, **kw: ran.append(cmd))
    job = fw.Job("usb", "t")
    assert "manual_reset" not in fw.flash_usb(job, store, m["id"], "/dev/ttyACM0", "update")
    assert "restarting" in job.step

    with pytest.raises(fw.FirmwareError, match="base station"):
        fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/ttyUSB0", "update", base_radio="/dev/ttyUSB0")
    with pytest.raises(fw.FirmwareError, match="not a serial port"):
        fw.flash_usb(fw.Job("usb", "t"), store, m["id"], "/dev/nope", "update")


@pytest.fixture
def client(tmp_path):
    class NoLink:
        connected = False
        last_error = ""

        def start(self):
            pass

        def close(self):
            pass

    app = create_app({"data_dir": str(tmp_path), "mesh_serial": "/dev/ttyUSB0"}, link=NoLink())
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
    assert r["builds"] == [] and r["wifi_leftover"] is None
    assert "wifi" not in r and "subnets" not in r  # no Wi-Fi updates

    make_build(tmp_path / "in")
    m = app.state.builds.add(tmp_path / "in", "release")
    assert c.get(f"/api/firmware/builds/{m['id']}/app").content == b"APP" * 1000
    assert c.get("/api/firmware/builds/nope/app").status_code == 404

    # There is no way to update over Wi-Fi: the old endpoints are gone.
    sim_id = c.post("/api/sim", json={"lat": 40, "lon": -75}).json()["node_id"]
    assert c.post(f"/api/collars/{sim_id}/firmware/ota", json={"build": m["id"]}).status_code in (404, 405)
    assert c.put("/api/firmware/wifi", json={"ssid": "home", "psk": "secret"}).status_code in (404, 405)

    # USB flashing refuses the base radio's own port.
    c.post("/api/collars", json={"node_id": "!0000abcd"})
    j = c.post("/api/firmware/flash-usb", json={"build": m["id"], "port": "/dev/ttyUSB0"}).json()
    assert "base station" in wait_job(c, j["id"])["error"]


def test_same_firmware_goes_by_source_not_by_commit():
    a = {"commit": "a" * 40, "source": "s1", "meshtastic": "v2.8.2"}
    assert fw.same_firmware(a, dict(a)) is True
    # A base-only commit: another commit, the same collar source and Meshtastic version.
    assert fw.same_firmware(a, {"commit": "b" * 40, "source": "s1", "meshtastic": "v2.8.2"}) is True
    assert fw.same_firmware(a, {"commit": "b" * 40, "source": "s2", "meshtastic": "v2.8.2"}) is False
    assert fw.same_firmware(a, {"commit": "b" * 40, "source": "s1", "meshtastic": "v2.8.3"}) is False
    # A collar flashed before builds carried a source ID: the commit is all there is.
    assert fw.same_firmware(a, {"commit": "b" * 40, "meshtastic": "v2.8.2"}) is False
    assert fw.same_firmware(a, {"commit": "a" * 40}) is True
    assert fw.same_firmware(a, None) is None and fw.same_firmware(None, a) is None
    assert fw.same_firmware(a, {"flashed_at": 1}) is None
    m = make_build_manifest()
    assert fw.identity(m) == {"commit": "a" * 40, "source": "tree1", "meshtastic": "v2.8.2.d5f1619"}


def make_build_manifest(commit="a" * 40, source="tree1"):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        m = make_build(Path(d), commit)
    m["geocollar_source"] = source
    m["built_at"] = "2026-11-14T16:37:00+00:00"
    return m


def test_latest_manifest_and_source_lookup(monkeypatch):
    urls = []

    def get(url, timeout=30):
        urls.append(url)
        if "contents/firmware" in url:
            return [{"name": "GeoCollar", "type": "dir", "sha": "0fbfcfe8a35c27cc5fa2f9190e7179f88df9a34a"},
                    {"name": "build.sh", "type": "file", "sha": "4" * 40}]
        return make_build_manifest()

    monkeypatch.setattr(fw, "_get_json", get)
    assert fw.latest_manifest("owner/repo")["geocollar_commit"] == "a" * 40
    assert urls == ["https://github.com/owner/repo/releases/download/firmware-latest/manifest.json"]
    assert fw.source_of_commit("owner/repo", "7546315") == "0fbfcfe8a35c27cc5fa2f9190e7179f88df9a34a"
    assert urls[-1] == "https://api.github.com/repos/owner/repo/contents/firmware?ref=7546315"
    # Nothing is asked for a commit ID or repository name that could change the URL.
    n = len(urls)
    assert fw.source_of_commit("owner/repo", "main&x=1") is None and fw.source_of_commit("owner/repo/x", "7546315") is None
    assert len(urls) == n

    def down(url, timeout=30):
        raise OSError("no route to host")

    monkeypatch.setattr(fw, "_get_json", down)
    with pytest.raises(fw.FirmwareError, match="could not reach github.com/owner/repo"):
        fw.latest_manifest("owner/repo")
    assert fw.source_of_commit("owner/repo", "7546315") is None
    monkeypatch.setattr(fw, "_get_json", lambda url, timeout=30: {"format": 1})
    with pytest.raises(fw.FirmwareError, match="incomplete manifest"):
        fw.latest_manifest("owner/repo")


def test_update_check_only_reports(client, tmp_path, monkeypatch):
    c, app = client
    store = app.state.store
    latest = [make_build_manifest("c" * 40, "tree2")]
    sources = {"1" * 40: "tree2", "2" * 40: "tree1"}
    asked, notes = [], []
    monkeypatch.setattr(fw, "latest_manifest", lambda repo: latest[0])
    monkeypatch.setattr(fw, "source_of_commit", lambda repo, commit: asked.append(commit) or sources.get(commit))
    monkeypatch.setattr(fw, "notify_home_assistant", lambda title, message, nid: notes.append(message) or True)
    for node_id, name in (("!00000001", "Rex"), ("!00000002", "Biscuit"), ("!00000003", "Pip"), ("!00000004", "Dot")):
        c.post("/api/collars", json={"node_id": node_id, "name": name})
    # Rex: an old build without a source ID whose collar source is the latest one (only the base
    # changed since). Biscuit: older collar firmware. Pip: never flashed from here. Dot: up to date.
    store.set_collar_info("!00000001", "firmware", {"build": "x", "commit": "1" * 40, "meshtastic": "v2.8.2.d5f1619"})
    store.set_collar_info("!00000002", "firmware", {"build": "y", "commit": "2" * 40, "meshtastic": "v2.8.2.d5f1619"})
    store.set_collar_info("!00000004", "firmware", {"build": "z", "commit": "c" * 40, "source": "tree2",
                                                    "meshtastic": "v2.8.2.d5f1619"})
    sim_id = c.post("/api/sim", json={"lat": 40, "lon": -75}).json()["node_id"]

    u = c.get("/api/firmware").json()["update"]
    assert u == {"every": "weekly", "latest": None, "error": None, "outdated": [], "unknown": []}
    assert app.state.check_due()  # never checked
    assert not any(x["update_available"] for x in c.get("/api/collars").json().values())

    u = c.post("/api/firmware/check").json()
    assert u["latest"]["commit"] == "c" * 40 and u["latest"]["subject"] == "Test build" and u["error"] is None
    assert u["latest"]["built_at"] == "2026-11-14T16:37:00+00:00" and u["latest"]["checked_at"] > 0
    assert [x["name"] for x in u["outdated"]] == ["Biscuit"] and [x["name"] for x in u["unknown"]] == ["Pip"]
    assert sorted(asked) == ["1" * 40, "2" * 40]
    assert store.collar("!00000001")["firmware"]["source"] == "tree2"  # learned, not asked again
    collars = c.get("/api/collars").json()
    assert [k for k, x in collars.items() if x["update_available"]] == ["!00000002"]
    assert not collars[sim_id]["update_available"]
    assert app.state.builds.list() == [] and notes == []  # nothing downloaded; a check by hand does not notify
    assert not app.state.check_due()  # next one in a week

    # The automatic check tells Home Assistant once per new firmware.
    store.set_setting("fw_check_next", 1)
    assert app.state.check_due()
    app.state.check_updates(auto=True)
    assert len(notes) == 1 and "Biscuit" in notes[0] and "Rex" not in notes[0]
    latest[0] = make_build_manifest("d" * 40, "tree2")  # a base-only commit: a new build of the same firmware
    app.state.check_updates(auto=True)
    assert len(notes) == 1 and c.get("/api/firmware").json()["update"]["latest"]["commit"] == "d" * 40
    assert [x["name"] for x in c.get("/api/firmware").json()["update"]["outdated"]] == ["Biscuit"]
    latest[0] = make_build_manifest("e" * 40, "tree3")  # new collar firmware
    u = app.state.check_updates(auto=True)
    assert len(notes) == 2 and [x["name"] for x in u["outdated"]] == ["Rex", "Biscuit", "Dot"]
    assert sorted(set(asked)) == ["1" * 40, "2" * 40] and len(asked) == 2

    # How often: daily, weekly, monthly or never.
    assert c.put("/api/firmware/check-every", json={"every": "hourly"}).status_code == 400
    assert c.put("/api/firmware/check-every", json={"every": "monthly"}).json()["every"] == "monthly"
    checked = store.setting("fw_latest")["checked_at"]
    assert abs(store.setting("fw_check_next") - (checked + 30 * 86400)) < 1 and not app.state.check_due()
    store.set_setting("fw_latest", {**store.setting("fw_latest"), "checked_at": checked - 2 * 86400})
    assert c.put("/api/firmware/check-every", json={"every": "daily"}).json()["every"] == "daily"
    assert app.state.check_due()  # the last check is two days old
    assert c.put("/api/firmware/check-every", json={"every": "never"}).json()["every"] == "never"
    assert not app.state.check_due()
    c.put("/api/firmware/check-every", json={"every": "daily"})

    # GitHub can't be reached: the page is told, what was known is kept, and the next automatic
    # try waits some hours.
    def down(repo):
        raise fw.FirmwareError("could not reach github.com/x (timed out)")

    monkeypatch.setattr(fw, "latest_manifest", down)
    u = c.post("/api/firmware/check").json()
    assert "could not reach" in u["error"] and u["latest"]["commit"] == "e" * 40 and len(u["outdated"]) == 3
    assert not app.state.check_due() and store.setting("fw_check_next") > time.time() + 5 * 3600


def test_flash_gets_the_latest_build_by_itself(client, tmp_path, monkeypatch):
    c, app = client
    builds = app.state.builds
    fetched, flashed = [], []

    def fetch(job, store, repo):
        fetched.append(repo)
        have = store.find_commit("f" * 40, "release")
        if have:
            return have
        d = tmp_path / f"dl{len(fetched)}"
        m = make_build(d, "f" * 40)
        m["geocollar_source"] = "tree9"
        (d / "manifest.json").write_text(json.dumps(m))
        return store.add(d, "release")

    monkeypatch.setattr(fw, "fetch_release", fetch)
    monkeypatch.setattr(fw, "flash_usb", lambda job, store, b, port, mode, base_radio="":
                        flashed.append(b) or {"node_id": "!22222222", "mode": mode})
    c.post("/api/collars", json={"node_id": "!22222222", "name": "Biscuit"})

    # No build named: the newest is downloaded, flashed and recorded, and the page's "latest" follows.
    job = wait_job(c, c.post("/api/firmware/flash-usb", json={"port": "/dev/ttyACM0"}).json()["id"])
    assert job["state"] == "done", job
    m = builds.list()[0]
    assert flashed == [m["id"]] and m["geocollar_commit"] == "f" * 40
    rec = c.get("/api/collars").json()["!22222222"]
    assert rec["firmware"]["build"] == m["id"] and rec["firmware"]["source"] == "tree9" and not rec["update_available"]
    assert c.get("/api/firmware").json()["update"]["latest"]["commit"] == "f" * 40
    # Again: the build is here already, so it is not kept twice.
    wait_job(c, c.post("/api/firmware/flash-usb", json={"port": "/dev/ttyACM0"}).json()["id"])
    assert len(builds.list()) == 1 and len(fetched) == 2

    # The browser asks for the same thing before it flashes; the job's result is the manifest.
    job = wait_job(c, c.post("/api/firmware/download").json()["id"])
    assert job["state"] == "done" and job["result"]["id"] == m["id"] and job["title"] == "Get the latest firmware"

    # A build picked by hand is flashed as it is, without asking GitHub.
    older = make_build(tmp_path / "older", "9" * 40)
    old = builds.add(tmp_path / "older", "local")
    assert older["geocollar_commit"] == "9" * 40
    wait_job(c, c.post("/api/firmware/flash-usb", json={"port": "/dev/ttyACM0", "build": old["id"]}).json()["id"])
    assert flashed[-1] == old["id"] and len(fetched) == 3
    assert c.post("/api/firmware/flash-usb", json={"port": "/dev/ttyACM0", "build": "nope"}).status_code == 404

    # GitHub can't be reached: the newest build already here is used, and the log says so.
    def down(job, store, repo):
        raise fw.FirmwareError("could not download the latest build (timed out)")

    monkeypatch.setattr(fw, "fetch_release", down)
    job = wait_job(c, c.post("/api/firmware/flash-usb", json={"port": "/dev/ttyACM0"}).json()["id"])
    full = c.get(f"/api/firmware/jobs/{job['id']}").json()
    assert job["state"] == "done" and flashed[-1] == old["id"]
    assert any("Using the newest build already here instead: 9999999, built here" in line for line in full["log"])
    # With nothing here either, the flash fails and says why.
    for b in builds.list():
        builds.delete(b["id"])
    job = wait_job(c, c.post("/api/firmware/flash-usb", json={"port": "/dev/ttyACM0"}).json()["id"])
    assert job["state"] == "failed" and "no firmware on this machine" in job["error"]

    # Cancelled while downloading: not turned into "use an older build".
    j = fw.Job("usb", "t")
    j.cancel.set()
    with pytest.raises(fw.FirmwareError, match="cancelled"):
        fw.latest_build(j, builds, "owner/repo")


def test_package_adds_the_source_id():
    text = (REPO / "firmware" / "package.py").read_text()
    assert '"geocollar_source": git("rev-parse", "HEAD:firmware/GeoCollar", cwd=repo)' in text


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


class FakeMeshtasticDevice:
    """A stand-in for a collar on USB that behaves like Meshtastic 2.8 where it matters: settings
    sent to another node number go nowhere; setting the region on a fresh device makes its key and
    moves its node number to a hash of the key; managed mode ignores all later USB admin (commit
    included); changes inside an edit transaction are lost unless committed; a Bluetooth or security
    change shuts Bluetooth down and restarts the collar, and a second one in the same boot resets
    it before the save. Each open() is a new USB connection, through the same SocketStream bridge
    the browser relay uses, with the real Meshtastic library on top. drop_security loses that many
    security writes, to check the setup's second try."""

    def __init__(self, num=0xAB52A4E5, region=0, managed=False, admin_keys=(), bt=False, drop_security=0):
        from meshtastic.protobuf import channel_pb2, localonly_pb2

        self.num = num
        self.saved = localonly_pb2.LocalConfig()
        self.saved.lora.region = region
        self.saved.security.is_managed = managed
        self.saved.security.admin_key.extend(admin_keys)
        if region:
            self.saved.security.public_key = b"P" * 32
        self.saved.bluetooth.enabled = bt
        self.saved_channels = [channel_pb2.Channel(index=i, role=1 if i == 0 else 0) for i in range(8)]
        self.long_name = "GeoCollar a4e5"
        self.ram = None
        self.txn = False
        self.ignored = []
        self.restart_pending = False  # a write asked for a restart; it happens before the next open()
        self.bt_down = False  # Bluetooth was shut down in this boot
        self.dead = False  # reset mid-connection: nothing more arrives on it
        self.restarts = 0
        self.drop_security = drop_security

    def open(self, _port=None):
        from meshtastic.protobuf import localonly_pb2

        # A new connection after a restart sees what was saved.
        if self.restart_pending:
            self.restarts += 1
            self.restart_pending = self.bt_down = False
        self.dead = False
        self.ram = localonly_pb2.LocalConfig()
        self.ram.CopyFrom(self.saved)
        self.ram_channels = [type(c)() for c in self.saved_channels]
        for a, b in zip(self.ram_channels, self.saved_channels):
            a.CopyFrom(b)
        self.txn = False
        self.buf = b""
        self.stream = fw.SocketStream(self.on_bytes, read_timeout=0.05)
        return fw.open_socket_node(self.stream)

    def save(self):
        if not self.txn:
            self.saved.CopyFrom(self.ram)
            for a, b in zip(self.saved_channels, self.ram_channels):
                a.CopyFrom(b)

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
                self.admin(tr.packet.to, m)

    def admin(self, to, m):
        import zlib

        kind = m.WhichOneof("payload_variant")
        if self.dead:
            self.ignored.append(("after a reset", kind))
            return
        if to != self.num:
            self.ignored.append(("to another node", kind))
            return
        if self.ram.security.is_managed:
            self.ignored.append(("managed", kind))
            return
        if kind == "begin_edit_settings":
            self.txn = True
        elif kind == "commit_edit_settings":
            self.txn = False
            self.save()
        elif kind == "set_config":
            which = m.set_config.WhichOneof("payload_variant")
            if which in ("bluetooth", "security") and not self.txn:
                if self.bt_down:  # shutting Bluetooth down twice resets the collar before it saves
                    self.ignored.append(("lost in a reset", which))
                    self.dead = self.restart_pending = True
                    return
                self.bt_down = self.restart_pending = True
            if which == "security" and self.drop_security:
                self.drop_security -= 1
                self.ignored.append(("dropped", which))
                return
            if which == "lora" and not self.ram.lora.region and m.set_config.lora.region:
                self.ram.security.public_key = b"P" * 32  # the key is made now...
                self.num = zlib.crc32(b"P" * 32)  # ...and the node number follows it
            if which == "security":
                keep = bytes(self.ram.security.public_key)
                getattr(self.ram, which).CopyFrom(m.set_config.security)
                self.ram.security.public_key = keep
            elif which in ("lora", "bluetooth"):
                getattr(self.ram, which).CopyFrom(getattr(m.set_config, which))
            self.save()
        elif kind == "set_channel":
            self.ram_channels[m.set_channel.index].CopyFrom(m.set_channel)
            self.save()
        elif kind == "set_owner":
            self.long_name = m.set_owner.long_name or self.long_name
            self.restart_pending = True

    def send_config(self, config_id):
        from meshtastic.protobuf import mesh_pb2

        out = [mesh_pb2.FromRadio()]
        out[0].my_info.my_node_num = self.num
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = self.num
        fr.node_info.user.id = f"!{self.num:08x}"
        fr.node_info.user.long_name = self.long_name
        out.append(fr)
        for name in ("device", "lora", "security", "bluetooth"):
            fr = mesh_pb2.FromRadio()
            getattr(fr.config, name).CopyFrom(getattr(self.ram, name))
            out.append(fr)
        for c in self.ram_channels:
            fr = mesh_pb2.FromRadio()
            fr.channel.CopyFrom(c)
            out.append(fr)
        fr = mesh_pb2.FromRadio()
        fr.config_complete_id = config_id
        out.append(fr)
        for f in out:
            self.stream.feed(FakeMeshtasticDevice.frame(f))

    @staticmethod
    def frame(msg) -> bytes:
        b = msg.SerializeToString()
        return bytes([0x94, 0xC3, len(b) >> 8, len(b) & 255]) + b


def fake_base():
    from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2

    node = type("N", (), {})()
    node.localConfig = localonly_pb2.LocalConfig()
    node.localConfig.security.public_key = b"B" * 32
    node.localConfig.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
    node.localConfig.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST
    node.localConfig.lora.tx_enabled = True
    node.channels = [channel_pb2.Channel(index=i) for i in range(8)]
    node.channels[0].role = channel_pb2.Channel.Role.PRIMARY
    node.channels[0].settings.psk = b"\x01"
    return type("I", (), {"localNode": node})()


def test_setup_collar_sticks_on_a_fresh_meshtastic_28_collar():
    import zlib

    dev = FakeMeshtasticDevice()  # fresh install: no region, no key, node number from the MAC
    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "Rex the Dog", open_iface=dev.open,
                        boot_wait_s=5, restart_s=0)
    new_id = f"!{zlib.crc32(b'P' * 32):08x}"
    assert r["checked"] and r["node_id"] == new_id and r["previous_node_id"] == "!ab52a4e5"
    assert r["locked"] and 100000 <= r["ble_pin"] <= 999999
    assert r["public_key"] == "UFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFA="  # b"P" * 32, for the base's radio
    sec = dev.saved.security
    assert sec.is_managed and not sec.admin_channel_enabled and [bytes(k) for k in sec.admin_key] == [b"B" * 32]
    assert dev.saved.lora == fake_base().localNode.localConfig.lora
    assert dev.saved.bluetooth.enabled and dev.saved.bluetooth.fixed_pin == r["ble_pin"]
    assert dev.long_name == "Rex the Dog"
    assert ("to another node", "set_config") not in dev.ignored  # nothing was lost to the old number
    assert dev.ignored == [] and dev.restarts == 2  # Bluetooth, then the key and lock, each in its own boot

    # Again: locked to this base already, so nothing is written.
    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "", open_iface=dev.open, boot_wait_s=5)
    assert r["already_locked"] and r["node_id"] == new_id and r["public_key"].startswith("UFBQ")

    # Locked to someone else's base: refused; only a full install frees it.
    other = FakeMeshtasticDevice(region=1, managed=True, admin_keys=[b"O" * 32])
    with pytest.raises(fw.FirmwareError, match="locked to another base"):
        fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "", open_iface=other.open, boot_wait_s=5)

    # Unlocked set-up when asked; a collar that already had a region keeps its node number. Three
    # admin keys at most: the last one gives way.
    free = FakeMeshtasticDevice(region=1, admin_keys=[b"1" * 32, b"2" * 32, b"3" * 32])
    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "", open_iface=free.open, lock=False,
                        boot_wait_s=5, restart_s=0)
    assert r["checked"] and r["node_id"] == "!ab52a4e5" and r["ble_pin"] is None
    assert not free.saved.security.is_managed
    assert [bytes(k) for k in free.saved.security.admin_key] == [b"1" * 32, b"2" * 32, b"B" * 32]

    # Bluetooth already on and no lock: no Bluetooth change, so the key goes in the same boot.
    on = FakeMeshtasticDevice(region=1, bt=True)
    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "", open_iface=on.open, lock=False,
                        boot_wait_s=5, restart_s=0)
    assert r["checked"] and on.restarts == 1 and [bytes(k) for k in on.saved.security.admin_key] == [b"B" * 32]


def test_setup_writes_the_lock_again_if_it_did_not_stick():
    dev = FakeMeshtasticDevice(region=1, drop_security=1)
    job = fw.Job("usb", "t")
    r = fw.setup_collar(job, "/dev/ttyACM0", fake_base(), "", open_iface=dev.open, boot_wait_s=5, restart_s=0)
    assert r["checked"] and dev.saved.security.is_managed and dev.ignored == [("dropped", "security")]
    assert any("writing them again" in line for line in job.lines)

    # Lost twice: it says so, and the collar is not locked.
    dev = FakeMeshtasticDevice(region=1, drop_security=2)
    with pytest.raises(fw.FirmwareError, match="without these settings: the base's key, lock"):
        fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "", open_iface=dev.open, boot_wait_s=5,
                        restart_s=0)
    assert not dev.saved.security.is_managed


def test_fixed_pin_matches_meshtastic():
    from meshtastic.protobuf import config_pb2

    assert fw.FIXED_PIN == config_pb2.Config.BluetoothConfig.PairingMode.FIXED_PIN

    with pytest.raises(fw.FirmwareError, match="not connected"):
        fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", None)


def test_setup_refuses_the_base_radio_and_a_device_that_swapped_in():
    # The base's own radio, picked by mistake in the browser's list: its key is the base's key.
    radio = FakeMeshtasticDevice(region=1)
    radio.saved.security.public_key = b"B" * 32
    with pytest.raises(fw.FirmwareError, match="base station's own radio"):
        fw.setup_collar(fw.Job("usb", "t"), "this computer's USB", fake_base(), "", open_iface=radio.open, boot_wait_s=5)
    assert radio.ignored == [] and not radio.saved.security.admin_key and not radio.saved.bluetooth.enabled

    # Two collars on one computer. The setup starts on the first; after its restart the reconnect
    # finds the second, which has another key. Nothing is written to it, above all not the lock.
    first, second = FakeMeshtasticDevice(region=1), FakeMeshtasticDevice(num=0x22222222, region=1)
    second.saved.security.public_key = b"Q" * 32
    opens = []

    def open_iface(port):
        opens.append(port)
        return (first if len(opens) <= 2 else second).open(port)

    with pytest.raises(fw.FirmwareError, match="different device answered"):
        fw.setup_collar(fw.Job("usb", "t"), "this computer's USB", fake_base(), "Rex", open_iface=open_iface,
                        boot_wait_s=5, restart_s=0)
    assert len(opens) == 3 and first.saved.bluetooth.enabled  # Bluetooth went to the first one
    sec = second.saved.security
    assert not sec.is_managed and not sec.admin_key and not second.saved.bluetooth.enabled
    assert second.long_name == "GeoCollar a4e5" and second.restarts == 0

    # A blank device (fresh install, no region, no key) in its place is refused as well.
    first, blank = FakeMeshtasticDevice(region=1), FakeMeshtasticDevice(num=0x33333333)
    opens.clear()

    def open_blank(port):
        opens.append(port)
        return (first if len(opens) <= 2 else blank).open(port)

    with pytest.raises(fw.FirmwareError, match="different device answered"):
        fw.setup_collar(fw.Job("usb", "t"), "this computer's USB", fake_base(), "", open_iface=open_blank,
                        boot_wait_s=5, restart_s=0)
    assert not blank.saved.lora.region and not blank.saved.security.admin_key


def test_setup_follows_a_collar_to_another_port_name(monkeypatch):
    """Reset by hand while its port is open, a collar comes back as ttyACM1: found by its USB
    serial number. The base's own radio (another serial number) is never opened."""
    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    dev = FakeMeshtasticDevice(region=1)
    radio = {"device": "/dev/ttyUSB0", "description": "", "vid": 0x303A, "pid": 0x1001, "serial_number": "BASE", "in_use": True}

    def port(name):
        return {"device": name, "description": "", "vid": 0x303A, "pid": 0x1001, "serial_number": "AA:BB:CC", "in_use": False}

    listing = [[port("/dev/ttyACM0"), radio]]
    monkeypatch.setattr(fw, "serial_ports", lambda exclude="": listing[0])
    tries = []

    def open_iface(p):
        tries.append(p)
        if len(tries) == 1:  # in download mode: no answer; then someone presses RST
            listing[0] = [radio, port("/dev/ttyACM1")]
            raise OSError("timed out")
        if p != "/dev/ttyACM1":
            raise OSError("no such port")
        return dev.open(p)

    job = fw.Job("usb", "t")
    r = fw.setup_collar(job, "/dev/ttyACM0", fake_base(), "", open_iface=open_iface, boot_wait_s=5, restart_s=0,
                        press_reset=True)
    view = job.view()
    assert r["checked"] and tries[0] == "/dev/ttyACM0" and set(tries[1:]) == {"/dev/ttyACM1"}
    assert any("The collar is on /dev/ttyACM1 now" in line for line in view["log"])
    assert fw.usb_identity("/dev/ttyUSB1") is None and fw.port_now("/dev/ttyUSB0", None) == "/dev/ttyUSB0"

    # The first wait says to press RST when the flash could not restart the collar.
    steps = []
    monkeypatch.setattr(fw.Job, "set_step", lambda self, step, progress=None: steps.append(step))
    listing[0] = [port("/dev/ttyACM1")]
    fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM1", fake_base(), "", open_iface=dev.open, boot_wait_s=5, press_reset=True)
    assert steps[0] == "Waiting for the collar on /dev/ttyACM1 to start: press its RST button once now"


def test_setup_retries_while_the_collar_boots(monkeypatch):
    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    dev = FakeMeshtasticDevice(region=1)
    tries = []

    def open_iface(port):
        tries.append(port)
        if len(tries) < 3:
            raise OSError("still booting")
        return dev.open(port)

    r = fw.setup_collar(fw.Job("usb", "t"), "/dev/ttyACM0", fake_base(), "", open_iface=open_iface, boot_wait_s=5)
    assert r["checked"] and len(tries) >= 5  # 2 failures, then: setup, after the region, after the restart


def test_new_collars_from_usb_browser_and_mesh(client, tmp_path, monkeypatch):
    c, app = client
    make_build(tmp_path / "in")
    m = app.state.builds.add(tmp_path / "in", "release")

    # Flashed from the browser, which read the MAC address. After an update that is not the node
    # ID of a set-up Meshtastic 2.8 collar (a hash of its key), so nothing is added...
    r = c.post("/api/firmware/flashed", json={"node_id": "!18ABCD01", "build": m["id"], "name": "Rex"}).json()
    assert not r["new_collar"] and c.get("/api/collars").json() == {}
    # ...but after a full install it is, until setup gives the collar its key.
    r = c.post("/api/firmware/flashed", json={"node_id": "!18ABCD01", "build": m["id"], "name": "Rex", "mode": "full"}).json()
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
        return {"node_id": "!a1b2c3d4", "previous_node_id": "!0badf00d", "ble_pin": None, "locked": False}

    monkeypatch.setattr(fw, "setup_collar", fake_setup)
    monkeypatch.setattr(fw, "open_socket_node", lambda stream: stream)
    c = TestClient(create_app({"data_dir": str(tmp_path)}))
    # Flashed (full install) as !0badf00d; setup gives it a key, and with it a new node ID.
    c.post("/api/collars", json={"node_id": "!0badf00d", "name": "Rex"})
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
    collars = c.get("/api/collars").json()
    assert list(collars) == ["!a1b2c3d4"] and collars["!a1b2c3d4"]["name"] == "Rex"

    # Another site's page can't open it: WebSockets ignore CORS.
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/firmware/setup-web", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()



def test_build_fetches_only_branches_and_tags(tmp_path, monkeypatch):
    """A name such as refs/pull/7/head or a commit ID must not be fetched: the build runs the
    fetched tree's own build script."""
    class Stop(Exception):
        pass

    calls = []

    def run(job, cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "refs/heads/v0.16.0":
            raise fw.FirmwareError("git fetch failed (exit code 128)")  # not a branch
        if "reset" in cmd:
            raise Stop

    monkeypatch.setattr(fw, "_run", run)
    monkeypatch.setattr(fw.shutil, "which", lambda name: "/usr/bin/git")
    (tmp_path / "src" / ".git").mkdir(parents=True)
    (tmp_path / "platformio").mkdir()
    store = fw.BuildStore(tmp_path / "builds")

    with pytest.raises(Stop):
        fw.build_from_source(fw.Job("build", "t"), store, tmp_path, "owner/repo", ref="main")
    # The copy on disk is pointed at the repository asked for before anything is fetched.
    assert calls[0][-3:] == ["set-url", "origin", "https://github.com/owner/repo.git"]
    assert calls[1][-3:] == ["1", "origin", "refs/heads/main"] and "reset" in calls[2]

    calls.clear()
    with pytest.raises(Stop):
        fw.build_from_source(fw.Job("build", "t"), store, tmp_path, ref="v0.16.0")
    assert [c[-1] for c in calls[1:3]] == ["refs/heads/v0.16.0", "refs/tags/v0.16.0"]

    # A pull request's head is asked for as a branch of that name, which does not exist.
    calls.clear()
    with pytest.raises(Stop):
        fw.build_from_source(fw.Job("build", "t"), store, tmp_path, ref="refs/pull/7/head")
    assert calls[1][-1] == "refs/heads/refs/pull/7/head"
    for bad in ("--upload-pack=x", "a..b", "main;rm -rf /", ""):
        with pytest.raises(fw.FirmwareError):
            fw.build_from_source(fw.Job("build", "t"), store, tmp_path, ref=bad)
