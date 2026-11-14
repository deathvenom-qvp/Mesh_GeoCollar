# SPDX-License-Identifier: GPL-3.0-or-later
import time

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2

from geocollar_base import radio
from geocollar_base.app import create_app

L = config_pb2.Config.LoRaConfig


class Node:
    def __init__(self):
        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.lora.use_preset = True
        self.localConfig.lora.region = L.RegionCode.US
        self.localConfig.lora.modem_preset = L.ModemPreset.LONG_FAST
        self.localConfig.lora.hop_limit = 3
        self.channels = [channel_pb2.Channel(index=i) for i in range(8)]
        self.channels[0].role = channel_pb2.Channel.Role.PRIMARY
        self.channels[0].settings.psk = b"\x01"


def test_plan_and_describe():
    n = Node()
    d = radio.describe(n, {"longName": "Base", "shortName": "BASE"})
    assert d["region"] == "US" and d["preset"] == "LONG_FAST" and d["channel_key"] == "default" and not d["public_secondary"]

    lora, chans, mesh = radio.plan(n, {"hop_limit": 5})
    assert lora.hop_limit == 5 and not mesh  # the base's own business

    lora, chans, mesh = radio.plan(n, {"channel_key": "new", "channel_name": "Smiths", "public_secondary": True})
    assert mesh and len(bytes(chans[0].settings.psk)) == 32 and chans[0].settings.name == "Smiths"
    assert chans[1].role == channel_pb2.Channel.Role.SECONDARY and bytes(chans[1].settings.psk) == b"\x01"
    assert chans[1].settings.name == "LongFast"

    assert radio.primary_moves(n, lora, chans)

    lora, chans, mesh = radio.plan(n, {"preset": "MEDIUM_FAST", "region": "EU_868"})
    assert mesh and lora.region == L.RegionCode.EU_868 and lora.modem_preset == L.ModemPreset.MEDIUM_FAST
    # The default channel is known by the preset's name, so a new preset renames it.
    assert radio.primary_moves(n, lora, chans)

    # A new private key with no name keeps the default frequency slot.
    lora, chans, mesh = radio.plan(n, {"channel_key": "new"})
    assert chans[0].settings.name == "" and len(bytes(chans[0].settings.psk)) == 32

    # A named private channel keeps its identity across a preset change.
    n.channels[0].settings.psk = bytes(range(32))
    n.channels[0].settings.name = "Smiths"
    lora, chans, mesh = radio.plan(n, {"preset": "MEDIUM_FAST"})
    assert mesh and not radio.primary_moves(n, lora, chans)
    assert radio.describe(n, {})["channel_key"] == "private"

    with pytest.raises(radio.RadioError):
        radio.validate({"region": "MARS"})
    with pytest.raises(radio.RadioError):
        radio.validate({"channel_name": "has space"})
    with pytest.raises(radio.RadioError):
        radio.validate({"short_name": "TOOLONG"})


class FakeLink:
    def __init__(self, deaf=()):
        self.connected = True
        self.iface = type("I", (), {"localNode": Node()})()
        self.deaf = set(deaf)
        self.pushed, self.applied, self.sent = [], [], []
        self.fail_apply = 0

    def close(self):
        pass

    def send_owner(self, *a):
        pass

    def radio_settings(self):
        return radio.describe(self.iface.localNode, {"longName": "Base", "shortName": "BASE"})

    def get_metadata(self, node_id, timeout=90):
        if node_id in self.deaf:
            raise TimeoutError("no answer")

    def push_radio(self, node_id, lora, chans, primary_moves=True):
        self.pushed.append((node_id, lora.modem_preset))
        return not primary_moves

    def apply_local(self, lora=None, chans=None, long_name=None, short_name=None):
        if self.fail_apply:
            self.fail_apply -= 1
            raise RuntimeError("serial port gone")
        self.applied.append((lora is not None, chans is not None, long_name))


def wait(c, job_id):
    for _ in range(100):
        j = c.get(f"/api/firmware/jobs/{job_id}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def make(tmp_path, link):
    c = TestClient(create_app({"data_dir": str(tmp_path)}, link=link))
    c.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    c.post("/api/collars", json={"node_id": "!b1b2c3d4", "name": "Pip"})
    c.post("/api/sim", json={"lat": 40, "lon": -75})
    return c


def test_collars_move_first_then_the_base(tmp_path):
    link = FakeLink()
    c = make(tmp_path, link)
    r = c.get("/api/base/settings").json()
    assert r["settings"]["preset"] == "LONG_FAST" and r["collars"] == 2 and "US" in r["options"]["regions"]

    job = c.put("/api/base/settings", json={"preset": "MEDIUM_FAST"}).json()
    j = wait(c, job["id"])
    assert j["state"] == "done" and j["result"]["moved"] == ["!a1b2c3d4", "!b1b2c3d4"]
    assert link.pushed == [("!a1b2c3d4", L.ModemPreset.MEDIUM_FAST), ("!b1b2c3d4", L.ModemPreset.MEDIUM_FAST)]
    assert link.applied == [(True, True, None)]

    # A name change touches only the base.
    link.pushed.clear()
    wait(c, c.put("/api/base/settings", json={"long_name": "Farm base"}).json()["id"])
    assert link.pushed == [] and link.applied[-1] == (False, False, "Farm base")
    assert c.put("/api/base/settings", json={"region": "MARS"}).status_code == 400


def test_nothing_changes_if_a_collar_does_not_answer(tmp_path):
    link = FakeLink(deaf={"!b1b2c3d4"})
    c = make(tmp_path, link)
    j = wait(c, c.put("/api/base/settings", json={"preset": "SHORT_FAST"}).json()["id"])
    assert j["state"] == "failed" and "Pip did not answer" in j["error"]
    assert link.pushed == [] and link.applied == []

    j = wait(c, c.put("/api/base/settings", json={"preset": "SHORT_FAST", "skip_unreachable": True}).json()["id"])
    assert j["state"] == "done" and j["result"]["left_behind"] == ["!b1b2c3d4"] and link.pushed == [("!a1b2c3d4", L.ModemPreset.SHORT_FAST)]


def test_reports_clear_the_moved_mark(tmp_path):
    link = FakeLink()
    c = make(tmp_path, link)
    wait(c, c.put("/api/base/settings", json={"preset": "MEDIUM_FAST"}).json()["id"])
    rex = c.get("/api/collars").json()["!a1b2c3d4"]
    assert rex["radio_changed"]["moved"] is True
    c.app.state.on_status("!a1b2c3d4", {"state": "INSIDE", "lat": 40.0, "lon": -75.0, "pki": True,
                                        "sender_key": "a2V5"})
    assert "radio_changed" not in c.get("/api/collars").json()["!a1b2c3d4"]


def test_finish_a_change_the_base_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    link = FakeLink()
    c = make(tmp_path, link)
    link.fail_apply = 6
    j = wait(c, c.put("/api/base/settings", json={"channel_key": "new"}).json()["id"])
    assert j["state"] == "failed" and "Finish the change" in j["error"] and len(link.pushed) == 2
    assert c.get("/api/base/settings").json()["pending"] is True
    assert c.put("/api/base/settings", json={"preset": "SHORT_FAST"}).status_code == 409
    j = wait(c, c.post("/api/base/settings/finish").json()["id"])
    assert j["state"] == "done" and link.applied == [(True, True, None)]
    assert c.get("/api/base/settings").json()["pending"] is False
    assert c.get("/api/collars").json()["!b1b2c3d4"]["radio_changed"]["moved"] is True
    assert c.post("/api/base/settings/finish").status_code == 409
