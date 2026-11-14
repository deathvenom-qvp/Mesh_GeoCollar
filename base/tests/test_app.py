# SPDX-License-Identifier: GPL-3.0-or-later

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

    def close(self):
        pass

    def send_fences(self, node_id, zones, version, pulses=None):
        chunks = protocol.fence_chunks(zones, version, pulses)
        self.sent += [(node_id, c) for c in chunks]
        return len(chunks)

    def send_command(self, node_id, action, intensity=0, duration_ms=1000, seq=0):
        self.seqs = getattr(self, "seqs", []) + [seq]
        self.sent.append((node_id, protocol.encode_command(action, intensity, duration_ms)))

    def send_owner(self, node_id, dog, owner, phone):
        self.sent.append((node_id, protocol.encode_owner(dog, owner, phone)))


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


def test_a_collar_that_does_not_trust_the_base_is_not_sent_anything(tmp_path):
    client, link = make(tmp_path)
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    link.sent.clear()
    client.app.state.on_hello("!a1b2c3d4", {"has_admin_key": False})
    assert link.sent == []
    assert client.get("/api/collars").json()["!a1b2c3d4"]["hello"]["has_admin_key"] is False
