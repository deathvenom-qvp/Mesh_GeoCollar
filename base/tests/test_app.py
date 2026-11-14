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

    def send_command(self, node_id, action, intensity=0, duration_ms=1000):
        self.sent.append((node_id, protocol.encode_command(action, intensity, duration_ms)))


def make(tmp_path):
    link = FakeLink()
    return TestClient(create_app({"data_dir": str(tmp_path)}, link=link)), link


def test_collar_fence_and_command_flow(tmp_path):
    client, link = make(tmp_path)
    assert client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"}).status_code == 200

    r = client.put("/api/collars/!a1b2c3d4/fences", json=FENCE)
    assert r.status_code == 200 and r.json() == {"fence_version": 1, "chunks_sent": 1}
    assert link.sent[0][1][:2] == b"\x02\x01"

    r = client.put("/api/collars/!a1b2c3d4/fences", json=FENCE)
    assert r.json()["fence_version"] == 2

    r = client.post("/api/collars/!a1b2c3d4/command", json={"action": "vibrate", "intensity": 40, "duration_ms": 1500})
    assert r.status_code == 200
    assert link.sent[-1] == ("!a1b2c3d4", bytes.fromhex("02040228dc05"))

    assert client.post("/api/collars/!a1b2c3d4/command", json={"action": "explode"}).status_code == 400
    assert client.put("/api/collars/!nope/fences", json=FENCE).status_code == 404
    assert client.put("/api/collars/!a1b2c3d4/fences", json={"type": "Point"}).status_code == 400


def test_status_is_recorded(tmp_path):
    client, _ = make(tmp_path)
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
