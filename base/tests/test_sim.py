# SPDX-License-Identifier: GPL-3.0-or-later

import random

from fastapi.testclient import TestClient

from geocollar_base import protocol
from geocollar_base.app import create_app
from geocollar_base.sim import LostBeacon, SimCollar, _move, beacon_name, beacon_text

# A 200 m square keep-in zone around (40.0, -75.0).
YARD = protocol.Zone(protocol.ZoneKind.KEEP_IN, 0, 5, 5, [(39.9991, -75.00117), (39.9991, -74.99883), (40.0009, -74.99883), (40.0009, -75.00117)])
S = protocol.CollarState


def walker():
    sim = SimCollar("!5a000001", {"lat": 40.0, "lon": -75.0, "noise_m": 0}, rng=random.Random(1))
    sim.set_fences([YARD], 7)
    return sim


def test_walk_out_matches_firmware_rules():
    sim = walker()
    sim.update({"target": list(_move(40.0, -75.0, 90, 150)), "mode": "goto", "speed_mps": 5})
    states = []
    for t in range(40):
        r = sim.step(1.0, now=1000 + t)
        states.append(sim.engine.state)
        if r:
            assert r["fence_version"] == 7
    seen = [s for i, s in enumerate(states) if i == 0 or states[i - 1] != s]
    assert seen == [S.INSIDE, S.WARNING, S.OUTSIDE, S.LOST]
    kinds = [e["kind"] for e in sim.events]
    assert "beep" in kinds and "vibrate" in kinds and "lost" in kinds
    assert "shock" not in kinds  # shock is off unless enabled
    assert sim.mode == "still"  # arrived

    # Walk back in: re-arms after 3 fixes at least 5 m inside.
    sim.update({"target": [40.0, -75.0], "mode": "goto"})
    for t in range(60):
        sim.step(1.0, now=2000 + t)
    assert sim.engine.state == S.INSIDE


def test_no_corrections_past_the_line_and_quiet_until_clear():
    out = _move(40.0, -75.0, 90, 102)  # 2 m past the east edge
    sim = walker()
    sim.set_fences([protocol.Zone(YARD.kind, 3, 5, 5, YARD.vertices)], 8)
    sim.update({"shock_enabled": True, "lat": out[0], "lon": out[1]})
    sim.step(1.0, now=1)
    assert sim.engine.state == S.OUTSIDE
    # Back inside, through the danger and warning bands: silent.
    for t, metres_in in enumerate((1.5, 6, 11), start=2):
        lat, lon = _move(40.0, -75.0, 90, 100 - metres_in)
        sim.update({"lat": lat, "lon": lon})
        sim.step(0, now=t)
    kinds = [e["kind"] for e in sim.events]
    assert not {"shock", "vibrate", "beep"} & set(kinds) and sim.engine.state == S.WARNING
    # Clear of every band: corrections come back.
    sim.update({"lat": 40.0, "lon": -75.0})
    sim.step(0, now=10)
    lat, lon = _move(40.0, -75.0, 90, 100 - 11)
    sim.update({"lat": lat, "lon": lon})
    sim.step(0, now=11)
    assert sim.events[0]["kind"] == "beep"


def test_pulses_repeat_beeps():
    sim = walker()
    sim.set_fences([YARD], 7, protocol.Pulses(beep_s=2, vibrate_s=3))
    lat, lon = _move(40.0, -75.0, 90, 100 - 8)  # attention band
    sim.update({"lat": lat, "lon": lon})
    for t in range(7):
        sim.step(0, now=100 + t)
    beeps = [e for e in sim.events if e["kind"] == "beep"]
    assert len(beeps) == 4  # at 0, 2, 4 and 6 s


def test_danger_band_inside_the_line_shocks():
    sim = walker()
    sim.set_fences([protocol.Zone(YARD.kind, 3, 4, 6, YARD.vertices)], 9)
    sim.update({"shock_enabled": True})
    for t, metres_in in enumerate((20, 10, 5, 1.5)):  # clear, attention, warning, danger
        lat, lon = _move(40.0, -75.0, 90, 100 - metres_in)
        sim.update({"lat": lat, "lon": lon})
        sim.step(0, now=t)
    kinds = [e["kind"] for e in reversed(sim.events) if e["kind"] != "fences"]
    assert kinds == ["beep", "vibrate", "shock"] and sim.engine.state == S.WARNING


def test_sim_api_flow(tmp_path):
    client = TestClient(create_app({"data_dir": str(tmp_path)}))
    r = client.post("/api/sim", json={"lat": 40.0, "lon": -75.0})
    assert r.status_code == 200
    node_id = r.json()["node_id"]
    c = client.get("/api/collars").json()[node_id]
    assert c["name"] == "Simulated dog 1" and c["sim"]["mode"] == "still" and c["status"]["lat"]

    fence = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"kind": "keep_in", "warning_m": 5, "attention_m": 5},
             "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lat, lon in YARD.vertices + YARD.vertices[:1]]]}}]}
    r = client.put(f"/api/collars/{node_id}/fences", json=fence)
    assert r.json() == {"fence_version": 1, "chunks_sent": 1}
    client.app.state.sim.tick(1.0)
    assert client.get("/api/collars").json()[node_id]["status"]["state"] == "INSIDE"

    assert client.patch(f"/api/sim/{node_id}", json={"mode": "goto"}).status_code == 400  # no target yet
    assert client.patch(f"/api/sim/{node_id}", json={"speed_mps": 99}).status_code == 400
    assert client.patch(f"/api/sim/{node_id}", json={"mode": "wander", "speed_mps": 3}).json()["mode"] == "wander"
    assert client.post(f"/api/collars/{node_id}/command", json={"action": "force_lost"}).status_code == 200
    assert client.get("/api/collars").json()[node_id]["status"]["state"] == "LOST"
    assert client.patch("/api/sim/!a1b2c3d4", json={}).status_code == 404

    # Survives a restart, with its fences.
    client2 = TestClient(create_app({"data_dir": str(tmp_path)}))
    assert node_id in client2.app.state.sim and client2.app.state.sim.collars[node_id].fence_version == 1

    assert client.delete(f"/api/collars/{node_id}").status_code == 200
    assert node_id not in client.app.state.sim


def test_lost_beacon_in_the_simulator():
    sim = walker()
    sim.set_owner("Rex", "Ken", "555-123-4567")
    sim.update({"lat": 40.0, "lon": -74.9975})  # about 50 m outside the east side
    t = 1000
    sim.step(1.0, now=t)
    assert not sim.beacon.active
    for t in range(1001, 1000 + LostBeacon.TRIGGER_S + 1):
        r = sim.step(1.0, now=t)
    assert sim.beacon.active and r["beacon_active"]
    texts = [e["text"] for e in sim.events]
    assert 'Bluetooth on as "Rex is lost 555-123-4567"' in texts
    assert any(x.startswith("mesh plea: Please help get Rex home. Call Ken 555-123-4567. Last seen 40.") for x in texts)
    n_mesh = sum(e["kind"] == "mesh" for e in sim.events)
    for t in range(t + 1, t + 100):
        sim.step(1.0, now=t)
    assert any(e["text"] == "Bluetooth off for 90 s" for e in sim.events)
    assert sum(e["kind"] == "mesh" for e in sim.events) == n_mesh  # next plea after 5 minutes

    sim.command(protocol.CommandAction.SILENCE_LOST)
    assert not sim.beacon.active and sim.report(t)["beacon_silenced"]
    for t in range(t + 1, t + 200):
        sim.step(1.0, now=t)
    assert not sim.beacon.active  # stays silent while still out

    sim.update({"lat": 40.0, "lon": -75.0})  # back home
    sim.step(1.0, now=t + 1)
    assert not sim.beacon.silenced


def test_beacon_strings_match_firmware():
    # Same cases as firmware/test/test_main.cpp testBeacon().
    assert beacon_name("Rex", "(555) 123-4567") == "Rex is lost 555123-4567"
    name = beacon_name("Maximilian Barksworth", "+44 7700 900123")
    assert len(name) <= 29 and name.startswith("Maximilian ") and name.endswith(" lost +447700900123")
    assert beacon_name("Rex", "") == "Rex is lost"
    assert beacon_name("Rex", "5551234567", cap=27) == "Rex is lost 5551234567"
    assert beacon_text("Rex", "Ken", "555-123-4567", 40.1234567, -75.1234567) == (
        "Please help get Rex home. Call Ken 555-123-4567. Last seen 40.12345,-75.12345")
    assert beacon_text("Rex", "", "") == "Please help get Rex home."
