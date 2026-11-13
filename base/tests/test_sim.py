# SPDX-License-Identifier: GPL-3.0-or-later

import random

from fastapi.testclient import TestClient

from geocollar_base import protocol
from geocollar_base.app import create_app
from geocollar_base.sim import SimCollar, _move

# A 200 m square keep-in zone around (40.0, -75.0).
YARD = protocol.Zone(protocol.ZoneKind.KEEP_IN, 10, [(39.9991, -75.00117), (39.9991, -74.99883), (40.0009, -74.99883), (40.0009, -75.00117)])
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


def test_shock_when_enabled_is_capped_per_breach():
    sim = walker()
    sim.update({"shock_enabled": True, "lat": _move(40.0, -75.0, 90, 105)[0], "lon": _move(40.0, -75.0, 90, 105)[1]})
    for t in range(5):
        sim.step(1.0, now=t)
    shocks = [e for e in sim.events if e["kind"] == "shock"]
    assert len(shocks) == 2 and sim.engine.state == S.LOST


def test_sim_api_flow(tmp_path):
    client = TestClient(create_app({"data_dir": str(tmp_path)}))
    r = client.post("/api/sim", json={"lat": 40.0, "lon": -75.0})
    assert r.status_code == 200
    node_id = r.json()["node_id"]
    c = client.get("/api/collars").json()[node_id]
    assert c["name"] == "Simulated dog 1" and c["sim"]["mode"] == "still" and c["status"]["lat"]

    fence = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"kind": "keep_in", "warn_margin_m": 10},
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
