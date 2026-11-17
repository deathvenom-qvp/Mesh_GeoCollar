# SPDX-License-Identifier: GPL-3.0-or-later

import time

import pytest
from fastapi.testclient import TestClient

from geocollar_base import protocol
from geocollar_base.app import create_app

FENCE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"kind": "keep_in", "danger_m": 0, "warning_m": 5, "attention_m": 5},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-75.0, 40.0], [-74.998, 40.0], [-74.998, 40.002], [-75.0, 40.002], [-75.0, 40.0]]],
            },
        }
    ],
}


class FakeLink:
    """Stands in for the radio so the API can be tested without hardware."""

    def __init__(self):
        self.connected = True
        self.sent = []
        self.radio_keys = {}  # node ID -> public key the radio has
        self.asked = []  # NodeInfo requests
        self.contacts = []  # keys given to the radio

    def close(self):
        pass

    def radio_key(self, node_id):
        return self.radio_keys.get(node_id, "")

    def request_node_info(self, node_id):
        self.asked.append(node_id)

    def forget_key(self, node_id):
        self.radio_keys.pop(node_id, None)

    def ensure_contact(self, node_id):
        key = self.key_for(node_id)
        if key and self.radio_keys.get(node_id) != key:
            self.radio_keys[node_id] = key
            self.contacts.append((node_id, key))
            return True
        return False

    def forget_wifi(self, node_id, ssid, timeout=90):
        """self.wifi: node ID -> whether that collar holds the network; a collar not in it does
        not answer."""
        self.forgot = getattr(self, "forgot", []) + [(node_id, ssid)]
        if node_id not in getattr(self, "wifi", {}):
            raise TimeoutError("no reply")
        return self.wifi[node_id]

    def send_fences(self, node_id, zones, version, pulses=None):
        chunks = protocol.fence_chunks(zones, version, pulses)
        self.sent += [(node_id, c) for c in chunks]
        return len(chunks)

    def send_command(self, node_id, action, intensity=0, duration_ms=1000, seq=0):
        self.seqs = getattr(self, "seqs", []) + [seq]
        self.sent.append((node_id, protocol.encode_command(action, intensity, duration_ms)))

    # The sequence numbers on Config and Owner messages are kept apart from `sent` (they come
    # from the clock), in config_seqs and owner_seqs.
    def send_config(self, node_id, report_s, seq=0, anchor=None):
        self.sent.append((node_id, protocol.encode_config(report_s)))
        self.config_seqs = getattr(self, "config_seqs", []) + [seq]
        self.anchors = getattr(self, "anchors", []) + [anchor]

    def send_app_keys(self, node_id, keys, seq):
        self.sent.append((node_id, protocol.encode_app_keys(keys, seq)))
        self.app_keys = getattr(self, "app_keys", []) + [(node_id, list(keys), seq)]

    def send_owner(self, node_id, dog, owner, phone, seq=0):
        self.sent.append((node_id, protocol.encode_owner(dog, owner, phone)))
        self.owner_seqs = getattr(self, "owner_seqs", []) + [seq]
        self.key_at_send = getattr(self, "key_at_send", []) + [node_id in self.radio_keys]


def make(tmp_path):
    link = FakeLink()
    return TestClient(create_app({"data_dir": str(tmp_path)}, link=link)), link


def test_collar_fence_and_command_flow(tmp_path):
    client, link = make(tmp_path)
    assert client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"}).status_code == 200
    assert link.sent.pop() == ("!a1b2c3d4", protocol.encode_owner("Rex", "", ""))

    r = client.put("/api/collars/!a1b2c3d4/fences", json=FENCE)
    assert r.status_code == 200 and r.json() == {"fence_version": 1, "chunks_sent": 1}
    assert link.sent[0][1][:2] == b"\x02\x01"

    r = client.put("/api/collars/!a1b2c3d4/fences", json=FENCE)
    assert r.json()["fence_version"] == 2

    r = client.post("/api/collars/!a1b2c3d4/command", json={"action": "vibrate", "intensity": 40, "duration_ms": 1500})
    assert r.status_code == 200
    assert link.sent[-1][0] == "!a1b2c3d4" and link.sent[-1][1][:6] == bytes.fromhex("02040228dc05")

    assert client.post("/api/collars/!a1b2c3d4/command", json={"action": "explode"}).status_code == 400
    assert client.put("/api/collars/!nope/fences", json=FENCE).status_code == 404
    assert client.put("/api/collars/!a1b2c3d4/fences", json={"type": "Point"}).status_code == 400


def test_status_is_recorded(tmp_path):
    client, _ = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.app.state.store.record_status("!a1b2c3d4", {"state": "LOST", "lat": 40.001, "lon": -75.001})
    collars = client.get("/api/collars").json()
    assert collars["!a1b2c3d4"]["status"]["state"] == "LOST"
    assert collars["!a1b2c3d4"]["track"][0][1:] == [40.001, -75.001]


def test_index_served(tmp_path):
    client, _ = make(tmp_path)
    r = client.get("/")
    assert r.status_code == 200 and "Meshtastic" in r.text and "OpenShock" in r.text


def test_vendored_map_libraries_served(tmp_path):
    client, _ = make(tmp_path)
    page = client.get("/").text
    for path in ("static/vendor/leaflet/leaflet.js", "static/vendor/geoman/leaflet-geoman.min.js"):
        assert path in page
        assert client.get("/" + path).status_code == 200


def test_fence_properties_validated(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4"})

    def with_props(**props):
        f = {**FENCE["features"][0], "properties": {**FENCE["features"][0]["properties"], **props}}
        return {"type": "FeatureCollection", "features": [f]}

    # Extra properties such as the page's zone name are kept but not sent.
    r = client.put("/api/collars/!a1b2c3d4/fences", json=with_props(name="Yard"))
    assert r.status_code == 200
    assert client.get("/api/collars").json()["!a1b2c3d4"]["fences"]["features"][0]["properties"]["name"] == "Yard"

    for bad in ({"kind": "keep_near"}, {"danger_m": -1}, {"warning_m": 300}, {"attention_m": None}):
        assert client.put("/api/collars/!a1b2c3d4/fences", json=with_props(**bad)).status_code == 400, bad

    # An empty set clears the collar's fences.
    r = client.put("/api/collars/!a1b2c3d4/fences", json={"type": "FeatureCollection", "features": []})
    assert r.status_code == 200 and link.sent[-1][1][6:] == b"\x00\x00\x00"


def test_parcels_round_trip(tmp_path):
    from fastapi.testclient import TestClient

    from geocollar_base.app import create_app

    class NoLink:
        connected = False
        last_error = ""

        def start(self):
            pass

        def close(self):
            pass

    c = TestClient(create_app({"data_dir": str(tmp_path)}, link=NoLink()))
    assert c.get("/api/parcels").json()["features"] == []
    square = [[-75, 40], [-75, 40.001], [-74.999, 40.001], [-74.999, 40], [-75, 40]]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"PIN": "12-34", "nested": {"x": 1}, "AREA": 1.5},
         "geometry": {"type": "Polygon", "coordinates": [square]}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [-75, 40]}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": [[square], [square]]}},
    ]}
    assert c.put("/api/parcels", json=fc).json() == {"count": 2}
    got = c.get("/api/parcels").json()["features"]
    assert got[0]["properties"] == {"PIN": "12-34", "AREA": 1.5}
    assert got[1]["geometry"]["type"] == "MultiPolygon"
    projected = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[500000, 4400000], [500010, 4400000], [500010, 4400010], [500000, 4400000]]]}}]}
    assert "longitude/latitude" in c.put("/api/parcels", json=projected).json()["detail"]
    assert c.put("/api/parcels", json={"type": "FeatureCollection", "features": []}).json() == {"count": 0}
    assert c.get("/api/parcels").json()["features"] == []


def test_dog_name_and_contact_reach_the_collar(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4"})
    assert link.sent[-1] == ("!a1b2c3d4", protocol.encode_owner("", "", ""))  # unnamed: collar says "Dog"

    r = client.put("/api/contact", json={"name": "Ken", "phone": "555-123-4567"})
    assert r.status_code == 200 and r.json()["sent_to"] == ["!a1b2c3d4"]
    assert client.get("/api/contact").json() == {"name": "Ken", "phone": "555-123-4567"}

    r = client.patch("/api/collars/!a1b2c3d4", json={"name": "  Rex  "})
    assert r.status_code == 200 and r.json()["name"] == "Rex"
    assert link.sent[-1] == ("!a1b2c3d4", bytes.fromhex("020503526578034b656e0c3535352d3132332d34353637"))

    assert client.patch("/api/collars/!a1b2c3d4", json={"name": "x" * 25}).status_code == 400
    assert client.patch("/api/collars/!a1b2c3d4", json={"name": " "}).status_code == 400
    assert client.patch("/api/collars/!nope", json={"name": "Rex"}).status_code == 404
    assert client.put("/api/contact", json={"name": "Ken", "phone": "call me"}).status_code == 400
    assert client.put("/api/contact", json={"name": "Ken", "phone": "1" * 21}).status_code == 400

    r = client.post("/api/collars/!a1b2c3d4/command", json={"action": "silence_lost"})
    assert r.status_code == 200 and link.sent[-1][1] == protocol.encode_command(protocol.CommandAction.SILENCE_LOST)


def test_collar_missing_owner_info_gets_it_on_next_report(tmp_path):
    link = FakeLink()
    link.connected = False
    client = TestClient(create_app({"data_dir": str(tmp_path)}, link=link))
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    assert link.sent == []
    link.connected = True
    # The link hands reports to the app; this is what MeshLink calls.
    status = {"state": "INSIDE", "lat": 40.0, "lon": -75.0, "beacon_active": False, "pki": True, "sender_key": "a2V5"}
    client.app.state.on_status("!a1b2c3d4", status)
    assert link.sent == [("!a1b2c3d4", protocol.encode_owner("Rex", "", ""))]
    client.app.state.on_status("!a1b2c3d4", status)
    assert len(link.sent) == 1  # only once


def test_shock_switch_needs_confirmation(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    url = "/api/collars/!a1b2c3d4/command"
    assert client.post(url, json={"action": "shock_on", "intensity": 10}).status_code == 400  # not confirmed
    assert client.post(url, json={"action": "shock_on", "intensity": 31, "confirm": True}).status_code == 400
    assert client.post(url, json={"action": "shock_on", "intensity": 0, "confirm": True}).status_code == 400
    assert client.post(url, json={"action": "shock_on", "intensity": 12, "confirm": True}).status_code == 200
    assert link.sent[-1][1] == protocol.encode_command(protocol.CommandAction.SHOCK_ON, 12)
    assert link.sent[-1][1][:6].hex() == "0204100ce803"
    assert client.get("/api/collars").json()["!a1b2c3d4"]["shock_level"] == 12
    assert client.post(url, json={"action": "shock_off"}).status_code == 200
    assert link.sent[-1][1] == protocol.encode_command(protocol.CommandAction.SHOCK_OFF)


def test_simulated_shock_switch(tmp_path):
    client, _ = make(tmp_path)
    node = client.post("/api/sim", json={"lat": 40, "lon": -75}).json()["node_id"]
    url = f"/api/collars/{node}/command"
    client.post(url, json={"action": "shock_on", "intensity": 8, "confirm": True})
    c = client.get("/api/collars").json()[node]
    assert c["status"]["shock_enabled"] and c["sim"]["shock_level"] == 8
    client.post(url, json={"action": "shock_off"})
    assert not client.get("/api/collars").json()[node]["status"]["shock_enabled"]


def test_reports_must_come_from_the_collar(tmp_path):
    client, link = make(tmp_path)
    on_status = client.app.state.on_status
    good = {"state": "INSIDE", "lat": 40.0, "lon": -75.0, "fence_version": 0, "pki": True, "sender_key": "S0VZMQ=="}
    on_status("!99999999", good)  # not a collar on this base: dropped, not added
    assert "!99999999" not in client.get("/api/collars").json()

    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    on_status("!a1b2c3d4", {**good, "pki": False, "state": "LOST"})  # channel-encrypted: anyone could send it
    assert client.get("/api/collars").json()["!a1b2c3d4"]["status"] is None
    on_status("!a1b2c3d4", good)  # first real report: key remembered
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["status"]["state"] == "INSIDE" and c["public_key"] == "S0VZMQ==" and "pki" not in c["status"]
    on_status("!a1b2c3d4", {**good, "sender_key": "T1RIRVI=", "state": "LOST"})  # another node using its ID
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["status"]["state"] == "INSIDE" and c.get("key_mismatch")


def test_commands_carry_growing_sequence_numbers(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    for _ in range(3):
        client.post("/api/collars/!a1b2c3d4/command", json={"action": "beep"})
    assert link.seqs[0] > 1_700_000_000 and link.seqs == sorted(set(link.seqs))


def test_new_fence_version_catches_up_with_the_collar(tmp_path):
    client, _ = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.app.state.on_status("!a1b2c3d4", {"state": "INSIDE", "fence_version": 41, "pki": True, "sender_key": "S0VZ"})
    assert client.put("/api/collars/!a1b2c3d4/fences", json=FENCE).json()["fence_version"] == 42


def test_a_known_collar_saying_hello_gets_an_answer(tmp_path):
    # A collar reports only to a base that has talked to it. If the base's first message was lost
    # (say, while the collar restarted after setup), its hello is the cue to talk to it again.
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    link.sent.clear()
    on_hello = client.app.state.on_hello
    on_hello("!a1b2c3d4", {"has_admin_key": True, "has_fences": False, "battery_pct": 80, "long_name": "Rex"})
    assert link.sent == [("!a1b2c3d4", protocol.encode_owner("Rex", "", ""))]
    assert client.get("/api/collars").json()["!a1b2c3d4"]["hello"]["battery_pct"] == 80
    on_hello("!a1b2c3d4", {"has_admin_key": True})  # again within 30 s: ignored
    assert len(link.sent) == 1
    assert client.get("/api/discovered").json() == {}  # known collars are not "new"

    # Its first report clears the hello.
    client.app.state.on_status("!a1b2c3d4", {"state": "NO_FENCE", "lat": 0, "lon": 0, "pki": True, "sender_key": "a2V5"})
    assert "hello" not in client.get("/api/collars").json()["!a1b2c3d4"]


def test_a_collar_whose_key_the_radio_lacks_is_asked_for_it(tmp_path):
    # Set up by hand (or before the base kept the key from setup): the radio can't encrypt to it.
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    st = client.app.state
    st.on_hello("!a1b2c3d4", {"has_admin_key": True})
    assert link.asked == ["!a1b2c3d4"]
    st.on_send_error("!a1b2c3d4", "PKI_SEND_FAIL_PUBLIC_KEY")
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["send_error"]["reason"] == "PKI_SEND_FAIL_PUBLIC_KEY" and link.asked == ["!a1b2c3d4"]  # 10 min apart
    # Once the radio has its key, the hello needs no NodeInfo swap; a report clears the error.
    link.radio_keys["!a1b2c3d4"] = "a2V5"
    st.on_status("!a1b2c3d4", {"state": "NO_FENCE", "lat": 0, "lon": 0, "pki": True, "sender_key": "a2V5"})
    assert "send_error" not in client.get("/api/collars").json()["!a1b2c3d4"]


def test_setup_gives_the_radio_the_collars_key(tmp_path, monkeypatch):
    # "Set up only" on a collar that is already locked to this base still teaches the base its key.
    from geocollar_base import firmware as fw

    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    monkeypatch.setattr(fw, "serial_ports", lambda: [{"device": "/dev/ttyACM0"}])
    monkeypatch.setattr(fw, "setup_collar", lambda job, port, base, name="", **kw: {
        "node_id": "!a1b2c3d4", "already_locked": True, "public_key": "S0VZMQ=="})
    job = client.post("/api/firmware/setup-usb", json={"port": "/dev/ttyACM0"})
    assert job.status_code == 200
    for _ in range(100):
        if client.get(f"/api/firmware/jobs/{job.json()['id']}").json()["state"] != "running":
            break
        time.sleep(0.05)
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["public_key"] == "S0VZMQ==" and c["locked"] and link.contacts == [("!a1b2c3d4", "S0VZMQ==")]
    assert link.key_at_send[-1]  # and then the base talks to it, so it starts reporting
    # Reports must now carry that key.
    client.app.state.on_status("!a1b2c3d4", {"state": "LOST", "pki": True, "sender_key": "T1RIRVI="})
    assert client.get("/api/collars").json()["!a1b2c3d4"].get("key_mismatch")

    # A collar the base didn't have yet: added, its key given to the radio, and only then written to.
    monkeypatch.setattr(fw, "setup_collar", lambda job, port, base, name="", **kw: {
        "node_id": "!0000beef", "already_locked": True, "public_key": "QkVFRg=="})
    job = client.post("/api/firmware/setup-usb", json={"port": "/dev/ttyACM0", "name": "Biscuit"}).json()
    for _ in range(100):
        if client.get(f"/api/firmware/jobs/{job['id']}").json()["state"] != "running":
            break
        time.sleep(0.05)
    assert client.get("/api/collars").json()["!0000beef"]["name"] == "Biscuit"
    assert link.sent[-1][0] == "!0000beef" and link.key_at_send[-1]

    # Meshtastic 2.7 says PKI_FAILED when its radio has no key for the collar: give it again, ask.
    client.app.state.on_send_error("!0000beef", "PKI_FAILED")
    assert "!0000beef" not in link.radio_keys and link.asked == ["!0000beef"]


def test_rename_and_report_interval(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    link.sent.clear()
    r = client.patch("/api/collars/!a1b2c3d4", json={"name": "Biscuit", "report_s": 300}).json()
    assert r["name"] == "Biscuit" and r["report_s"] == 300 and r["owner_sent"] and r["config_sent"]
    assert link.sent == [("!a1b2c3d4", protocol.encode_owner("Biscuit", "", "")),
                         ("!a1b2c3d4", protocol.encode_config(300))]
    # Same interval again: only the name goes.
    link.sent.clear()
    client.patch("/api/collars/!a1b2c3d4", json={"name": "Biscuit", "report_s": 300})
    assert link.sent == [("!a1b2c3d4", protocol.encode_owner("Biscuit", "", ""))]
    for bad in (10, 7200):
        assert client.patch("/api/collars/!a1b2c3d4", json={"report_s": bad}).status_code == 400
    assert client.patch("/api/collars/!a1b2c3d4", json={"name": " "}).status_code == 400

    # Not sent while the radio is away: it goes with the collar's next report.
    link.connected = False
    r = client.patch("/api/collars/!a1b2c3d4", json={"report_s": 60}).json()
    assert r["config_sent"] is False and r["report_s"] == 60
    link.connected = True
    link.sent.clear()
    client.app.state.on_status("!a1b2c3d4", {"state": "INSIDE", "pki": True, "sender_key": "a2V5"})
    assert ("!a1b2c3d4", protocol.encode_config(60)) in link.sent
    assert client.get("/api/collars").json()["!a1b2c3d4"]["config_sent"] == 60


def test_a_collar_that_does_not_trust_the_base_is_not_sent_anything(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    link.sent.clear()
    client.app.state.on_hello("!a1b2c3d4", {"has_admin_key": False})
    assert link.sent == []
    assert client.get("/api/collars").json()["!a1b2c3d4"]["hello"]["has_admin_key"] is False


GOOD = {"state": "INSIDE", "lat": 40.0, "lon": -75.0, "pki": True, "sender_key": "a2V5"}


def test_owner_and_config_are_numbered_for_firmware_that_reads_numbers(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    assert link.owner_seqs == [0]  # nothing heard from the collar yet: the form every firmware reads

    # Firmware before 0.16 (no "numbered" flag in its reports) keeps getting the plain form.
    client.app.state.on_status("!a1b2c3d4", GOOD)
    client.patch("/api/collars/!a1b2c3d4", json={"name": "Biscuit"})
    assert link.owner_seqs == [0, 0]
    # The report interval is always numbered: older firmware skips the number.
    client.patch("/api/collars/!a1b2c3d4", json={"report_s": 300})
    assert len(link.config_seqs) == 1 and link.config_seqs[0] > 1_700_000_000

    # The collar is updated and says it reads numbers: the name goes again, numbered this time, so
    # the collar stops accepting an unnumbered (replayable) one. The interval already had a number.
    link.sent.clear()
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "numbered": True})
    assert link.sent == [("!a1b2c3d4", protocol.encode_owner("Biscuit", "", ""))]
    assert link.owner_seqs[-1] > link.config_seqs[0]
    # Nothing more on later reports, and every later message carries a larger number.
    link.sent.clear()
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "numbered": True})
    assert link.sent == []
    client.patch("/api/collars/!a1b2c3d4", json={"name": "Rex", "report_s": 60})
    assert link.owner_seqs[-1] > link.owner_seqs[-2] and link.config_seqs[-1] > link.owner_seqs[-1]
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["numbered"] is True and c["owner_seq"] == link.owner_seqs[-1] and c["config_seq"] == link.config_seqs[-1]

    # Back on old firmware: plain again.
    client.app.state.on_status("!a1b2c3d4", GOOD)
    client.patch("/api/collars/!a1b2c3d4", json={"name": "Max"})
    assert link.owner_seqs[-1] == 0


def test_a_page_on_another_site_cannot_change_anything(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    link.sent.clear()
    url, beep = "/api/collars/!a1b2c3d4/command", {"action": "beep"}

    # A browser names the page a request comes from. Another site: refused, whatever the method.
    for origin in ("https://evil.example", "http://evil.example:8099", "null"):
        r = client.post(url, json=beep, headers={"Origin": origin})
        assert r.status_code == 403 and "another website" in r.json()["detail"]
    # Also in the form a page can send without the browser asking first (no JSON content type).
    r = client.post(url, content=b'{"action":"beep"}', headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert client.delete("/api/collars/!a1b2c3d4", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/contact", json={"name": "x", "phone": ""}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert link.sent == [] and "!a1b2c3d4" in client.get("/api/collars").json()

    # The app's own page (the Origin is the address it was reached on), a proxy in front of it that
    # says which address that was, Home Assistant's ingress, and anything that is not a browser.
    assert client.post(url, json=beep, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post(url, json=beep, headers={"Origin": "https://dogs.example", "X-Forwarded-Host": "dogs.example"}).status_code == 200
    assert client.post(url, json=beep, headers={"Origin": "https://ha.example:8123", "X-Ingress-Path": "/api/hassio_ingress/abc"}).status_code == 200
    assert client.post(url, json=beep).status_code == 200
    assert len(link.sent) == 4
    # Reading is not restricted: the browser already keeps another site from seeing the answer.
    assert client.get("/api/collars", headers={"Origin": "https://evil.example"}).status_code == 200


def test_bad_input_is_refused_not_stored(tmp_path):
    client, link = make(tmp_path)
    for bad in ("<img src=x onerror=alert(1)>", "a1b2c3d4", "!a1b2c3d", "!a1b2c3d4e", "!g1b2c3d4", ""):
        assert client.post("/api/collars", json={"node_id": bad, "name": "Rex"}).status_code == 400
    assert client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "x" * 25}).status_code == 400
    assert client.get("/api/collars").json() == {}
    # Upper case and stray spaces are tidied, as is the name.
    r = client.post("/api/collars", json={"node_id": " !A1B2C3D4 ", "name": "  Rex \n the  dog "})
    assert r.status_code == 200 and list(client.get("/api/collars").json()) == ["!a1b2c3d4"]
    assert client.get("/api/collars").json()["!a1b2c3d4"]["name"] == "Rex the dog"
    assert client.post("/api/sim", json={"name": "x" * 25, "lat": 40, "lon": -75}).status_code == 400

    link.sent.clear()
    url = "/api/collars/!a1b2c3d4/command"
    for body in ({"action": "vibrate", "intensity": 100}, {"action": "vibrate", "intensity": -1},
                 {"action": "beep", "duration_ms": 0}, {"action": "beep", "duration_ms": 3001},
                 {"action": "beep", "duration_ms": 70000}, {"action": "nope"}):
        assert client.post(url, json=body).status_code == 400, body
    assert link.sent == []
    assert client.post(url, json={"action": "vibrate", "intensity": 99, "duration_ms": 3000}).status_code == 200

    # Fences that are not the GeoJSON the page sends: a 400 that says so, never a crash.
    ring = [[-75.0, 40.0], [-74.998, 40.0], [-74.998, 40.002], [-75.0, 40.002], [-75.0, 40.0]]
    poly = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
    for bad in ({"type": "FeatureCollection", "features": None},
                {"type": "FeatureCollection", "features": [None]},
                {"type": "FeatureCollection", "features": ["x"]},
                {"type": "FeatureCollection", "features": [{**poly, "geometry": {"type": "Polygon", "coordinates": []}}]},
                {"type": "FeatureCollection", "features": [{**poly, "geometry": {"type": "Polygon", "coordinates": [[1, 2, 3]]}}]},
                {"type": "FeatureCollection", "features": [{**poly, "geometry": {"type": "Polygon", "coordinates": [[["a", "b"]] * 4]}}]},
                {"type": "Feature"}):
        r = client.put("/api/collars/!a1b2c3d4/fences", json=bad)
        assert r.status_code == 400 and "invalid fences" in r.json()["detail"], bad
    assert client.get("/api/collars").json()["!a1b2c3d4"]["fences"] is None
    assert client.put("/api/collars/!a1b2c3d4/fences", json={"type": "FeatureCollection", "features": [poly]}).status_code == 200


def test_a_report_without_a_gps_fix_does_not_extend_the_trail(tmp_path):
    client, _ = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.app.state.on_status("!a1b2c3d4", GOOD)
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "no_fix": True})  # the same position, repeated
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert len(c["track"]) == 1 and c["status"]["no_fix"] is True and c["status"]["lat"] == 40.0

    # The collar restarts indoors: no fix since boot, so it has no position to report. The base
    # keeps the last one it knew instead of taking the dog off the map.
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "no_fix": True, "lat": 0.0, "lon": 0.0})
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["status"]["lat"] == 40.0 and c["status"]["lon"] == GOOD["lon"] and len(c["track"]) == 1
    # A collar this base has never had a position from stays without one.
    client.post("/api/collars", json={"node_id": "!0000beef", "name": "Biscuit"})
    client.app.state.on_status("!0000beef", {**GOOD, "no_fix": True, "lat": 0.0, "lon": 0.0})
    b = client.get("/api/collars").json()["!0000beef"]
    assert b["status"]["lat"] == 0.0 and b["status"]["lon"] == 0.0 and not b.get("track")


def test_a_damaged_data_file_falls_back_to_the_backup(tmp_path, monkeypatch):
    from geocollar_base import store as store_mod

    s = store_mod.Store(tmp_path)
    s.upsert_collar("!a1b2c3d4", "Rex")
    monkeypatch.setattr(store_mod, "BACKUP_EVERY_S", 0)  # back up on the next save
    s.set_collar_info("!a1b2c3d4", "firmware", "0.16.0")  # the backup holds the file before this save
    assert (tmp_path / "geocollar.json.bak").exists()

    # A power cut left half a file: the backup is used and the damaged file kept for a look.
    (tmp_path / "geocollar.json").write_text('{"collars": {"!a1b2')
    again = store_mod.Store(tmp_path)
    assert again.collar("!a1b2c3d4")["name"] == "Rex"
    assert len(list(tmp_path.glob("geocollar.json.damaged-*"))) == 1
    again.upsert_collar("!0000beef", "Biscuit")  # and saving works again
    assert set(store_mod.Store(tmp_path).collars()) == {"!a1b2c3d4", "!0000beef"}

    # Valid JSON of the wrong shape, and no backup: a fresh start, not a crash.
    other = tmp_path / "other"
    other.mkdir()
    (other / "geocollar.json").write_text("[1, 2, 3]")
    assert store_mod.Store(other).collars() == {}
    # A collar saved by an old version without a trail still takes reports.
    (other / "geocollar.json").write_text('{"collars": {"!a1b2c3d4": {"name": "Rex"}}}')
    old = store_mod.Store(other)
    old.record_status("!a1b2c3d4", {"lat": 40.0, "lon": -75.0})
    assert len(old.collar("!a1b2c3d4")["track"]) == 1


def test_wifi_updates_are_gone_and_the_saved_network_is_forgotten(tmp_path, monkeypatch):
    from geocollar_base import store as store_mod

    def wait(client, job_id):
        for _ in range(200):
            j = client.get(f"/api/firmware/jobs/{job_id}").json()
            if j["state"] != "running":
                return j
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    # An older version kept the home Wi-Fi name and password for Wi-Fi updates, and its hourly
    # backup has them too.
    monkeypatch.setattr(store_mod, "BACKUP_EVERY_S", 0)
    old = store_mod.Store(tmp_path)
    old.upsert_collar("!a1b2c3d4", "Rex")
    old.upsert_collar("!0000beef", "Biscuit")
    old.set_setting("ota_wifi", {"ssid": "home", "psk": "hunter2-secret"})
    old.set_setting("home", [40.0, -75.0])
    assert "hunter2-secret" in (tmp_path / "geocollar.json.bak").read_text()
    monkeypatch.setattr(store_mod, "BACKUP_EVERY_S", 3600)

    client, link = make(tmp_path)
    # The password is gone from the data file and from its backup. Only the network's name is
    # kept, until the collars have been asked to forget it as well.
    files = list(tmp_path.glob("geocollar.json*"))
    assert len(files) >= 2
    for f in files:
        assert "hunter2-secret" not in f.read_text(), f
    assert client.get("/api/firmware").json()["wifi_leftover"] == "home"

    # The collars are asked over the mesh. Rex had the network; Biscuit does not answer.
    link.wifi = {"!a1b2c3d4": True}
    done = wait(client, client.post("/api/firmware/forget-wifi").json()["id"])
    assert done["state"] == "done" and "Removed from Rex" in done["step"] and "No answer from Biscuit" in done["step"]
    assert link.forgot == [("!a1b2c3d4", "home"), ("!0000beef", "home")]
    assert client.get("/api/firmware").json()["wifi_leftover"] == "home"  # Biscuit is still to do

    # A second run asks only Biscuit, which never had the network. Then nothing is left to do.
    link.wifi = {"!0000beef": False}
    link.forgot = []
    done = wait(client, client.post("/api/firmware/forget-wifi").json()["id"])
    assert done["state"] == "done" and "No collar had it" in done["step"]
    assert link.forgot == [("!0000beef", "home")]
    assert client.get("/api/firmware").json()["wifi_leftover"] is None
    assert client.post("/api/firmware/forget-wifi").status_code == 409

    # A base that never saved a network has nothing to offer.
    other, _ = make(tmp_path / "fresh")
    other.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    assert other.get("/api/firmware").json()["wifi_leftover"] is None
    assert other.post("/api/firmware/forget-wifi").status_code == 409


def test_collar_log_and_clearing_the_path(tmp_path):
    client, link = make(tmp_path)
    app = client.app
    for node_id, name in (("!a1b2c3d4", "Rex"), ("!0000beef", "Biscuit")):
        client.post("/api/collars", json={"node_id": node_id, "name": name})
    assert client.get("/api/collars/!a1b2c3d4/log").json() == {"total": 0, "entries": []}

    def report(node_id, **over):
        status = {"state": "INSIDE", "fence_version": 0, "battery_pct": 88, "lat": 40.001, "lon": -75.001, "pdop": 1.2,
                  "sats": 11, "clearance_m": 14, "no_fix": False, "pki": True, "sender_key": "a2V5" + node_id[1:3]}
        app.state.on_status(node_id, {**status, **over})

    t0 = time.time()
    report("!a1b2c3d4")
    report("!a1b2c3d4", lat=40.002, lon=-75.002, state="WARNING", battery_pct=87)
    report("!a1b2c3d4", lat=0.0, lon=0.0, no_fix=True, sats=0, state="NO_FENCE")  # no fix: logged without a position
    report("!0000beef", lat=41.0, lon=-76.0)
    report("!deadbeef")  # not a collar on this base: nothing is kept
    log = client.get("/api/collars/!a1b2c3d4/log").json()
    assert log["total"] == 3 and [e.get("lat") for e in log["entries"]] == [40.001, 40.002, None]
    first, second, third = log["entries"]
    assert first == {"t": first["t"], "state": "INSIDE", "bat": 88, "sats": 11, "lat": 40.001, "lon": -75.001}
    assert second["state"] == "WARNING" and second["bat"] == 87 and t0 - 1 <= first["t"] <= second["t"] <= time.time() + 1
    assert third == {"t": third["t"], "state": "NO_FENCE", "bat": 88, "sats": 0}
    assert client.get("/api/collars/!0000beef/log").json()["total"] == 1
    assert client.get("/api/collars/!deadbeef/log").status_code == 404
    assert not (tmp_path / "log" / "deadbeef.jsonl").exists() and (tmp_path / "log" / "a1b2c3d4.jsonl").exists()
    # From a time on, and only the newest when there are more than asked for.
    assert client.get(f"/api/collars/!a1b2c3d4/log?since={time.time() + 5}").json() == {"total": 0, "entries": []}
    newest = client.get("/api/collars/!a1b2c3d4/log?limit=2").json()
    assert newest["total"] == 3 and [e.get("lat") for e in newest["entries"]] == [40.002, None]
    # What is new since the page last asked: only the later entries, with the count of them all.
    fresh = client.get(f"/api/collars/!a1b2c3d4/log?after={second['t']}").json()
    assert fresh["total"] == 3 and fresh["entries"] == [e for e in log["entries"] if e["t"] > second["t"]]

    # Clearing the path on the map leaves the log, and the other collar, alone.
    assert len(client.get("/api/collars").json()["!a1b2c3d4"]["track"]) == 2
    assert client.delete("/api/collars/!a1b2c3d4/track").json() == {"cleared": "!a1b2c3d4"}
    collars = client.get("/api/collars").json()
    assert collars["!a1b2c3d4"]["track"] == [] and len(collars["!0000beef"]["track"]) == 1
    assert collars["!a1b2c3d4"]["status"]["lat"] == 40.002  # the collar's last known position stays
    assert client.get("/api/collars/!a1b2c3d4/log").json()["total"] == 3
    assert client.delete("/api/collars/!nope0000/track").status_code == 404
    report("!a1b2c3d4", lat=40.003, lon=-75.003)
    assert [p[1] for p in client.get("/api/collars").json()["!a1b2c3d4"]["track"]] == [40.003]  # and starts again

    # Clearing the log; removing a collar removes its log too.
    assert client.delete("/api/collars/!a1b2c3d4/log").json() == {"cleared": "!a1b2c3d4"}
    assert client.get("/api/collars/!a1b2c3d4/log").json()["total"] == 0
    assert len(client.get("/api/collars").json()["!a1b2c3d4"]["track"]) == 1
    client.delete("/api/collars/!0000beef")
    assert not (tmp_path / "log" / "0000beef.jsonl").exists()
    # Another site's page can't clear either (the same rule as every other change).
    assert client.delete("/api/collars/!a1b2c3d4/log", headers={"origin": "https://evil.example"}).status_code == 403


def test_collar_log_file(tmp_path):
    from geocollar_base.collarlog import CollarLog

    clog = CollarLog(tmp_path, keep=8)
    for i in range(10):
        clog.add("!a1b2c3d4", {"t": 1000 + i, "lat": 40.0 + i, "lon": -75.0})
    assert clog.read("!a1b2c3d4")["total"] == 10  # may grow a quarter past the limit...
    clog.add("!a1b2c3d4", {"t": 1010})
    got = clog.read("!a1b2c3d4")
    assert got["total"] == 8 and [e["t"] for e in got["entries"]] == list(range(1003, 1011))  # ...then the oldest go
    # It is read from the file, so it survives a restart, and a line cut short by a power cut is skipped.
    with open(tmp_path / "log" / "a1b2c3d4.jsonl", "a") as f:
        f.write('{"t": 1011, "lat": 4')
    again = CollarLog(tmp_path, keep=8)
    assert [e["t"] for e in again.read("!a1b2c3d4", since=1008)["entries"]] == [1008, 1009, 1010]
    again.add("!a1b2c3d4", {"t": 1012})
    assert again.read("!a1b2c3d4", since=1010)["total"] == 2
    # A collar that got a new node ID during setup keeps its log.
    again.move("!a1b2c3d4", "!0badf00d")
    assert again.read("!a1b2c3d4")["total"] == 0 and again.read("!0badf00d", since=1010)["total"] == 2
    again.move("!11111111", "!22222222")  # nothing to move
    again.clear("!0badf00d")
    again.clear("!0badf00d")
    assert again.read("!0badf00d") == {"total": 0, "entries": []}
    # A node ID becomes a file name: anything else is refused.
    for bad in ("../../etc/passwd", "!a1b2c3d", "a1b2c3d4", "!A1B2C3D4", ""):
        with pytest.raises(ValueError):
            clog.add(bad, {"t": 1})
        with pytest.raises(ValueError):
            clog.read(bad)


# -- The companion app -------------------------------------------------------------------------
def test_linking_a_phone_makes_a_key_for_it_and_for_the_collar(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.app.state.store.set_collar_info("!a1b2c3d4", "ble_pin", 482913)
    assert client.get("/api/collars/!a1b2c3d4/apps").json() == {
        "apps": [], "synced": True, "max": 4, "pin_known": True, "sim": False}

    r = client.post("/api/collars/!a1b2c3d4/apps", json={"name": "  Ken's   phone "})
    assert r.status_code == 200
    body = r.json()
    assert body["app"]["name"] == "Ken's phone" and body["synced"] is False and len(body["apps"]) == 1
    # The QR code says how to find the collar and carries the phone's key; the collar got the same key.
    code = protocol.parse_pairing_code(body["code"])
    assert code["node_id"] == "!a1b2c3d4" and code["dog"] == "Rex" and code["ble_name"] == "REX"
    assert code["pin"] == "482913" and code["sim"] is False
    assert f"{code['key_id']:08x}" == body["app"]["id"]
    node, keys, seq = link.app_keys[-1]
    assert node == "!a1b2c3d4" and keys == [(code["key_id"], code["key"])] and seq > 1_700_000_000
    assert link.sent[-1][1][:2] == b"\x02\x08"

    # The key is given out that once: nothing the page can ask for carries it afterwards.
    secret, b64 = code["key"].hex(), body["code"].split("k=")[1].split("&")[0]
    for path in ("/api/collars", "/api/collars/!a1b2c3d4/apps"):
        text = client.get(path).text
        assert secret not in text and b64 not in text
    assert secret not in client.patch("/api/collars/!a1b2c3d4", json={"report_s": 300}).text
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    assert c["apps"] == [body["app"]] and c["apps_synced"] is False

    # The collar confirms the list by its number, in a message that must really be the collar's.
    ack = client.app.state.on_app_ack
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "apps": True})  # the base learns the collar's key
    ack("!a1b2c3d4", {"seq": seq, "count": 1, "pki": False, "sender_key": "a2V5"})  # not PKI: anyone could send it
    ack("!a1b2c3d4", {"seq": seq, "count": 1, "pki": True, "sender_key": "T1RIRVI="})  # another node's key
    ack("!a1b2c3d4", {"seq": seq - 5, "count": 1, "pki": True, "sender_key": "a2V5"})  # an older list
    ack("!99999999", {"seq": seq, "count": 1, "pki": True, "sender_key": "a2V5"})  # not a collar here
    assert client.get("/api/collars/!a1b2c3d4/apps").json()["synced"] is False
    ack("!a1b2c3d4", {"seq": seq, "count": 1, "pki": True, "sender_key": "a2V5"})
    assert client.get("/api/collars/!a1b2c3d4/apps").json()["synced"] is True
    assert client.get("/api/collars").json()["!a1b2c3d4"]["apps_synced"] is True

    # Once confirmed, reports that agree send nothing more.
    sent = len(link.app_keys)
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "apps": True})
    assert len(link.app_keys) == sent

    # A second phone gets its own key and the collar gets both.
    r2 = client.post("/api/collars/!a1b2c3d4/apps", json={})
    code2 = protocol.parse_pairing_code(r2.json()["code"])
    assert r2.json()["app"]["name"] == "Phone 2" and code2["key"] != code["key"] and code2["key_id"] != code["key_id"]
    assert link.app_keys[-1][1] == [(code["key_id"], code["key"]), (code2["key_id"], code2["key"])]

    # Removing the first phone sends the list without it: that is what revokes it on the collar.
    r = client.delete(f"/api/collars/!a1b2c3d4/apps/{body['app']['id']}")
    assert r.status_code == 200 and [a["id"] for a in r.json()["apps"]] == [r2.json()["app"]["id"]]
    assert link.app_keys[-1][1] == [(code2["key_id"], code2["key"])] and link.app_keys[-1][2] > seq
    assert client.delete(f"/api/collars/!a1b2c3d4/apps/{body['app']['id']}").status_code == 404
    # Removing the last one sends an empty list, and the base waits for the collar to confirm that too.
    r = client.delete(f"/api/collars/!a1b2c3d4/apps/{r2.json()['app']['id']}")
    assert r.json()["apps"] == [] and r.json()["synced"] is False and link.app_keys[-1][1] == []
    ack("!a1b2c3d4", {"seq": link.app_keys[-1][2], "count": 0, "pki": True, "sender_key": "a2V5"})
    assert client.get("/api/collars/!a1b2c3d4/apps").json()["synced"] is True

    assert client.get("/api/collars/!nope/apps").status_code == 404
    assert client.post("/api/collars/!nope/apps", json={}).status_code == 404
    assert client.post("/api/collars/!a1b2c3d4/apps", json={"name": "x" * 41}).status_code == 400


def test_at_most_four_phones_per_collar(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    for _ in range(4):
        assert client.post("/api/collars/!a1b2c3d4/apps", json={}).status_code == 200
    assert client.post("/api/collars/!a1b2c3d4/apps", json={}).status_code == 409
    assert len(link.app_keys[-1][1]) == 4
    protocol.decode_app_keys(link.sent[-1][1])  # and it still fits one message


def test_app_keys_are_sent_again_until_the_collar_has_them(tmp_path, monkeypatch):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])

    # Made while the radio was unplugged: nothing could be sent, so the phone counts as not on the collar.
    link.connected = False
    r = client.post("/api/collars/!a1b2c3d4/apps", json={"name": "Phone"})
    assert r.status_code == 200 and r.json()["synced"] is False and not getattr(link, "app_keys", [])
    link.connected = True
    client.app.state.on_status("!a1b2c3d4", GOOD)
    assert len(link.app_keys) == 1

    # No answer (collar firmware before 0.20 never gives one): again on later reports, further and
    # further apart, not with every report.
    client.app.state.on_status("!a1b2c3d4", GOOD)
    assert len(link.app_keys) == 1
    now[0] += 61
    client.app.state.on_status("!a1b2c3d4", GOOD)
    assert len(link.app_keys) == 2
    now[0] += 61
    client.app.state.on_status("!a1b2c3d4", GOOD)
    assert len(link.app_keys) == 2
    now[0] += 60
    client.app.state.on_status("!a1b2c3d4", GOOD)
    assert len(link.app_keys) == 3
    assert [k[2] for k in link.app_keys] == sorted({k[2] for k in link.app_keys})  # each with a larger number

    # Confirmed. Later the collar is erased by a full install and reports that it holds no keys:
    # it gets them again without anyone asking.
    client.app.state.on_app_ack("!a1b2c3d4", {"seq": link.app_keys[-1][2], "count": 1, "pki": True, "sender_key": "a2V5"})
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "apps": True})
    assert len(link.app_keys) == 3
    now[0] += 5
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "apps": False})
    assert len(link.app_keys) == 4 and link.app_keys[-1][1] == link.app_keys[0][1]


def test_a_collar_holding_keys_this_base_never_gave_it_is_told_to_drop_them(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.app.state.on_status("!a1b2c3d4", GOOD)
    assert not getattr(link, "app_keys", [])  # no phones, and the collar agrees
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "apps": True})  # set up on another base before
    assert link.app_keys[-1][1] == []


def test_removing_a_collar_takes_its_phones_off_it(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.post("/api/collars/!a1b2c3d4/apps", json={})
    assert client.delete("/api/collars/!a1b2c3d4").status_code == 200
    assert link.app_keys[-1] == ("!a1b2c3d4", [], link.app_keys[-1][2]) and len(link.app_keys) == 2


def test_a_simulated_collar_can_be_linked_for_trying_the_app(tmp_path):
    client, link = make(tmp_path)
    node_id = client.post("/api/sim", json={"name": "Sim", "lat": 40.0, "lon": -75.0}).json()["node_id"]
    r = client.post(f"/api/collars/{node_id}/apps", json={"name": "Phone"})
    assert r.status_code == 200 and r.json()["synced"] is True and r.json()["sim"] is True
    assert protocol.parse_pairing_code(r.json()["code"])["sim"] is True
    assert not getattr(link, "app_keys", [])  # nothing goes on the air for a simulated collar


def test_home_anchor_is_sent_to_collars_and_checked(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    client.put("/api/collars/!a1b2c3d4/fences", json=FENCE)
    # Nothing set yet: home mode is off and no settings message goes to the collar.
    assert client.get("/api/home-anchor").json()["kind"] == "off"
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "numbered": True})
    assert not getattr(link, "anchors", [])

    bad = [{"kind": "beacon", "address": "nope"}, {"kind": "moon"}, {"kind": "base", "rssi": -20},
           {"kind": "base", "lat": 40.0}, {"kind": "base", "gps_restart_s": 5}]
    for body in bad:
        assert client.put("/api/home-anchor", json=body).status_code == 400, body

    r = client.put("/api/home-anchor", json={"kind": "beacon", "address": "aa-bb-cc-01-02-03", "rssi": -85,
                                             "lat": 40.001, "lon": -74.999})  # the yard's middle
    assert r.status_code == 200
    a = r.json()
    assert a["sent_to"] == ["!a1b2c3d4"] and a["address"] == "AA:BB:CC:01:02:03" and a["position"] == [40.001, -74.999]
    assert link.anchors[-1] == protocol.HomeAnchor(protocol.AnchorKind.BEACON, "AA:BB:CC:01:02:03", -85)
    assert link.config_seqs[-1] > 0  # the anchor always goes numbered
    # The check: how far it is from the anchor to the bands, against how far a running dog gets.
    row = a["collars"][0]
    assert row["name"] == "Rex" and row["room_m"] > 0 and a["need_m"] > a["range_m"] > 0
    assert row["ok"] == (row["room_m"] >= a["need_m"])

    # Sent once: later reports do not send it again, until it changes.
    n = len(link.anchors)
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "numbered": True})
    assert len(link.anchors) == n
    client.put("/api/home-anchor", json={"kind": "base", "rssi": -75})
    assert link.anchors[-1].kind == protocol.AnchorKind.BASE_RADIO
    # The report interval goes with it, and the anchor with the interval.
    client.patch("/api/collars/!a1b2c3d4", json={"report_s": 300})
    assert link.anchors[-1].kind == protocol.AnchorKind.BASE_RADIO
    # Turning it off tells the collars.
    client.put("/api/home-anchor", json={"kind": "off"})
    assert link.anchors[-1].kind == protocol.AnchorKind.NONE
    assert client.get("/api/home-anchor").json()["collars"] == []

    # A collar reporting that it is home.
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "numbered": True, "home": True})
    assert client.get("/api/collars").json()["!a1b2c3d4"]["status"]["home"] is True


def test_the_home_anchor_follows_home_assistants_home(tmp_path, monkeypatch):
    from geocollar_base import app as app_mod
    from geocollar_base import geo

    home = [{"lat": 40.001, "lon": -74.999}]
    monkeypatch.setattr(geo, "ha_home", lambda: home[0])
    client, _ = make(tmp_path)
    node_id = client.post("/api/sim", json={"name": "Sim", "lat": 40.0, "lon": -75.0}).json()["node_id"]
    client.put(f"/api/collars/{node_id}/fences", json=FENCE)
    client.put("/api/home-anchor", json={"kind": "base", "rssi": -75})  # at the home location
    sim_collar = client.app.state.sim.collars[node_id]
    a = client.get("/api/home-anchor").json()
    assert a["position"] == [40.001, -74.999] and a["position_set"] is False
    assert sim_collar.anchor_at == (40.001, -74.999)
    room = a["collars"][0]["room_m"]

    # The home is moved in Home Assistant: the page shows the anchor there straight away, the
    # distance to the bands is checked from there, and the simulated collar listens there.
    home[0] = {"lat": 40.0015, "lon": -74.9985}
    a = client.get("/api/home-anchor").json()
    assert a["position"] == [40.0015, -74.9985] and a["collars"][0]["room_m"] != room
    assert sim_collar.anchor_at == (40.0015, -74.9985)

    # Without the page open: the app looks again every HOME_MAX_AGE_S while it runs.
    home[0] = {"lat": 40.0012, "lon": -74.9988}
    monkeypatch.setattr(app_mod, "HOME_MAX_AGE_S", 0.02)
    with client:  # runs the app's background loops
        deadline = time.monotonic() + 3
        while sim_collar.anchor_at != (40.0012, -74.9988) and time.monotonic() < deadline:
            time.sleep(0.02)
    assert sim_collar.anchor_at == (40.0012, -74.9988)

    # Home Assistant out of reach: the last home it gave stands.
    home[0] = None
    assert client.get("/api/home-anchor").json()["position"] == [40.0012, -74.9988]

    # An anchor the owner placed on the map stays where it was put.
    home[0] = {"lat": 41.0, "lon": -74.0}
    client.put("/api/home-anchor", json={"kind": "base", "rssi": -75, "lat": 40.001, "lon": -74.999})
    assert client.get("/api/home-anchor").json()["position"] == [40.001, -74.999]
    assert sim_collar.anchor_at == (40.001, -74.999)
