# SPDX-License-Identifier: GPL-3.0-or-later

import math
import random

import pytest

from fastapi.testclient import TestClient

from geocollar_base import protocol
from geocollar_base.app import create_app
from geocollar_base.sim import (VERIFY_STALE_S, FenceEngine, LostBeacon, SimCollar, _bearing_and_distance, _move,
                                beacon_name, beacon_text)

# A 200 m square keep-in zone around (40.0, -75.0).
YARD = protocol.Zone(protocol.ZoneKind.KEEP_IN, 0, 5, 5, [(39.9991, -75.00117), (39.9991, -74.99883), (40.0009, -74.99883), (40.0009, -75.00117)])
S = protocol.CollarState


def walker():
    sim = SimCollar("!5a000001", {"lat": 40.0, "lon": -75.0, "noise_m": 0}, rng=random.Random(1))
    sim.set_fences([YARD], 7)
    return sim


def test_walk_out_matches_firmware_rules():
    sim = walker()
    # At 2 m/s, so that each 5 m band has the two fixes a second apart that a correction takes.
    sim.update({"target": list(_move(40.0, -75.0, 90, 150)), "mode": "goto", "speed_mps": 2})
    states = []
    for t in range(80):
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
    for t in range(90):
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
    assert sim.events[0]["text"].startswith("in a band: checking the position again")  # the first reading only waits
    sim.step(0, now=12)
    assert sim.events[0]["kind"] == "beep"


def test_pulses_repeat_beeps():
    sim = walker()
    sim.set_fences([YARD], 7, protocol.Pulses(beep_s=2, vibrate_s=3))
    lat, lon = _move(40.0, -75.0, 90, 100 - 8)  # attention band
    sim.update({"lat": lat, "lon": lon})
    for t in range(8):
        sim.step(0, now=100 + t)
    beeps = [e for e in sim.events if e["kind"] == "beep"]
    assert len(beeps) == 4  # at 1 s (the second look), 3, 5 and 7 s


def test_danger_band_inside_the_line_shocks():
    sim = walker()
    sim.set_fences([protocol.Zone(YARD.kind, 3, 4, 6, YARD.vertices)], 9)
    sim.update({"shock_enabled": True})
    for t, metres_in in enumerate((20, 10, 10, 5, 5, 1.5, 1.5)):  # clear, then two fixes each in attention, warning, danger
        lat, lon = _move(40.0, -75.0, 90, 100 - metres_in)
        sim.update({"lat": lat, "lon": lon})
        sim.step(0, now=t)
    kinds = [e["kind"] for e in reversed(sim.events) if e["kind"] not in ("fences", "info")]
    assert kinds == ["beep", "vibrate", "shock"] and sim.engine.state == S.WARNING
    # One fix in each band is not enough for any of them.
    sim = walker()
    sim.set_fences([protocol.Zone(YARD.kind, 3, 4, 6, YARD.vertices)], 9)
    sim.update({"shock_enabled": True})
    for t, metres_in in enumerate((20, 1.5, 20, 5, 20, 10, 20)):
        lat, lon = _move(40.0, -75.0, 90, 100 - metres_in)
        sim.update({"lat": lat, "lon": lon})
        sim.step(0, now=t)
    assert not {"shock", "vibrate", "beep"} & {e["kind"] for e in sim.events}


def test_second_look_matches_firmware():
    # Same cases as firmware/test/test_main.cpp testSecondLook().
    zone = protocol.Zone(YARD.kind, 3, 4, 6, YARD.vertices)  # shock 0-3 m, vibrate 3-7 m, beep 7-13 m
    beep, buzz, shock, clear = (_move(40.0, -75.0, 90, 100 - m) for m in (10, 5, 1.5, 50))
    out = _move(40.0, -75.0, 90, 102)

    def fresh():
        e = FenceEngine()
        e.settings.shock_enabled = True
        e.settings.lost_confirm_fixes = 10
        e.set_fences([zone])
        return e

    def fix(e, where, now):
        d = e.on_fix(*where, now)
        return d.correction, d.verify

    # The first reading in a band starts the wait; one before the second is over does not end it.
    e = fresh()
    assert fix(e, beep, 5.0) == (None, True) and e.state == S.WARNING
    assert fix(e, beep, 5.6) == (None, True)
    assert fix(e, beep, 6.0) == ("beep", False)
    assert fix(e, beep, 6.1) == ("beep", False)

    # The second look finds the dog clear, or past the line: no correction.
    e = fresh()
    assert fix(e, shock, 5) == (None, True)
    assert fix(e, clear, 6) == (None, False) and e.state == S.INSIDE
    assert fix(e, shock, 7) == (None, True)
    assert fix(e, out, 8) == (None, False) and e.state == S.OUTSIDE
    assert fix(e, buzz, 9) == (None, False)  # on the way back in: silent

    # A step up takes a second look too, and the agreed correction goes on meanwhile. A step
    # down counts at once.
    e = fresh()
    fix(e, beep, 5)
    assert fix(e, beep, 6) == ("beep", False)
    assert fix(e, buzz, 7) == ("beep", True)
    assert fix(e, buzz, 8) == ("vibrate", False)
    assert fix(e, shock, 9) == ("vibrate", True)
    assert fix(e, shock, 10) == ("shock", False)
    assert fix(e, beep, 11) == ("beep", False)
    assert fix(e, shock, 12) == ("beep", True)

    # Two readings in different bands agree on the milder one.
    e = fresh()
    fix(e, shock, 5)
    assert fix(e, buzz, 6) == ("vibrate", False)
    e = fresh()
    fix(e, buzz, 5)
    assert fix(e, shock, 6) == ("vibrate", True)
    assert fix(e, shock, 7) == ("shock", False)
    e = fresh()
    fix(e, shock, 5)
    fix(e, buzz, 5.4)
    assert fix(e, shock, 6) == ("vibrate", True)

    # A first reading that has got old is not agreed with.
    e = fresh()
    fix(e, buzz, 5)
    assert fix(e, buzz, 5 + VERIFY_STALE_S + 1) == (None, True)
    assert fix(e, buzz, 5 + VERIFY_STALE_S + 2) == ("vibrate", False)

    # With corrections off the readings still count; new fences and lost commands start afresh.
    e = fresh()
    fix(e, shock, 5)
    assert fix(e, shock, 6) == ("shock", False)
    e.settings.corrections_enabled = False
    assert fix(e, beep, 7) == (None, False)
    e.settings.corrections_enabled = True
    assert fix(e, shock, 8) == ("beep", True)
    e.set_fences([zone])
    assert fix(e, buzz, 9) == (None, True)
    assert fix(e, buzz, 10) == ("vibrate", False)
    e.force_lost()
    e.clear_lost()
    assert fix(e, buzz, 11) == (None, True)


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


def test_home_mode_in_the_simulator():
    from geocollar_base import sim as simmod

    sim = walker()
    anchor = protocol.HomeAnchor(protocol.AnchorKind.BEACON, "AA:BB:CC:01:02:03", -85)
    sim.set_anchor(anchor, (40.0, -75.0))
    t = 1000
    for t in range(1000, 1000 + simmod.HOME_ENTER_S - 1):
        sim.step(1.0, now=t)
    assert not sim.home  # in range, but not for long enough yet
    for t in range(t + 1, t + 4):
        r = sim.step(1.0, now=t)
    assert sim.home and any("GPS off" in e["text"] for e in sim.events)
    assert sim.report(t)["home"] is True
    # No fixes while home: walking around inside range changes nothing.
    fix = sim._fix
    sim.step(1.0, now=t + 1)
    assert sim._fix == fix
    # Walking off: once the anchor has faded for HOME_LEAVE_S the GPS is back on.
    sim.update({"target": list(_move(40.0, -75.0, 90, 60)), "mode": "goto", "speed_mps": 4})
    left = None
    for u in range(t + 2, t + 40):
        sim.step(1.0, now=u)
        if not sim.home and left is None:
            left = u
    assert left is not None and sim.engine.state == S.INSIDE
    # The anchor alone decides, even one in a band: the GPS goes off there too.
    edge = (39.9991 + 3 / 111_000, -75.0)  # 3 m inside the south line: the warning band
    sim.set_anchor(anchor, edge)
    sim.update({"lat": edge[0], "lon": edge[1]})
    for u in range(left + 40, left + 80):
        sim.step(1.0, now=u)
    assert sim.engine.state == S.WARNING and sim.home


def test_home_mode_without_a_gps_fix_in_the_simulator():
    """Indoors a collar may never get a fix that counts. Hearing the anchor is enough."""
    from geocollar_base import sim as simmod

    sim = walker()
    sim.update({"sats": 0})
    sim.set_anchor(protocol.HomeAnchor(protocol.AnchorKind.BEACON, "AA:BB:CC:01:02:03", -85), (40.0, -75.0))
    for t in range(1000, 1000 + simmod.HOME_ENTER_S + 3):
        sim.step(1.0, now=t)
    assert sim.home
    # Lost, the anchor does not count: the GPS stays on until fixes bring the dog back in.
    sim = walker()
    sim.update({"sats": 0})
    sim.set_anchor(protocol.HomeAnchor(protocol.AnchorKind.BEACON, "AA:BB:CC:01:02:03", -85), (40.0, -75.0))
    sim.command(protocol.CommandAction.FORCE_LOST)
    for t in range(1000, 1000 + simmod.HOME_ENTER_S + 3):
        sim.step(1.0, now=t)
    assert not sim.home


def test_home_room_check():
    from geocollar_base import sim as simmod

    B = protocol.AnchorKind
    assert simmod.anchor_range_m(B.BEACON, -75) < simmod.anchor_range_m(B.BEACON, -85)
    assert simmod.anchor_range_m(B.BEACON, -75) < simmod.anchor_range_m(B.BASE_RADIO, -75)
    assert simmod.home_room_needed_m(B.BEACON, -75, 30) > simmod.home_room_needed_m(B.BEACON, -75, 10)
    # The 200 m yard's bands are 10 m wide: about 90 m from its middle to the nearest band.
    assert 85 < simmod.room_from_anchor_m([YARD], 40.0, -75.0) < 95
    assert simmod.room_from_anchor_m([YARD], 40.0, -76.0) < 0


def _walk_states(client, node_id, start: float):
    """Step the simulator a second at a time; returns a function that walks to a point and records
    every change of (state, home, beacon) the base shows for the collar."""
    sim = client.app.state.sim
    seen: list[tuple] = []
    t = [start]

    def run(secs: float) -> None:
        for _ in range(int(secs)):
            t[0] += 1
            sim.tick(1.0, now=t[0])
            s = client.get("/api/collars").json()[node_id]["status"]
            key = (s["state"], s["home"], s["beacon_active"])
            if not seen or seen[-1] != key:
                seen.append(key)

    def walk(bearing: float, metres: float, speed: float, then: float = 5) -> None:
        sim.update(node_id, {"target": list(_move(40.0, -75.0, bearing, metres)), "mode": "goto", "speed_mps": speed})
        run(metres_from(node_id) / speed + then)

    def metres_from(nid):
        c = sim.collars[nid]
        return max(1.0, _bearing_and_distance(c.lat, c.lon, *c.target)[1])

    return run, walk, seen


def _yard_with_pond(client, node_id):
    pond_c = _move(40.0, -75.0, 0, 50)  # a 20 m keep-out square 50 m north of the middle
    dl, do = 10 / 111_195, 10 / (111_195 * math.cos(math.radians(40)))
    pond = [(pond_c[0] - dl, pond_c[1] - do), (pond_c[0] - dl, pond_c[1] + do), (pond_c[0] + dl, pond_c[1] + do),
            (pond_c[0] + dl, pond_c[1] - do)]

    def feat(kind, verts, d, w, a):
        return {"type": "Feature", "properties": {"kind": kind, "danger_m": d, "warning_m": w, "attention_m": a},
                "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lat, lon in verts + verts[:1]]]}}

    fence = {"type": "FeatureCollection", "features": [feat("keep_in", YARD.vertices, 3, 5, 5), feat("keep_out", pond, 0, 4, 4)]}
    assert client.put(f"/api/collars/{node_id}/fences", json=fence).status_code == 200


def test_zone_states_as_the_base_shows_them(tmp_path):
    """A simulated collar walked through every zone, with home mode on: what the base records."""
    client = TestClient(create_app({"data_dir": str(tmp_path), "home": "40.0, -75.0"}))
    node_id = client.post("/api/sim", json={"lat": 40.0, "lon": -75.0}).json()["node_id"]
    _yard_with_pond(client, node_id)
    r = client.put("/api/home-anchor", json={"kind": "beacon", "address": "AA:BB:CC:01:02:03", "rssi": -85})
    assert r.status_code == 200 and node_id in r.json()["sent_to"]
    client.patch(f"/api/sim/{node_id}", json={"noise_m": 0})
    run, walk, seen = _walk_states(client, node_id, 10_000)
    S_ = "INSIDE", "WARNING", "OUTSIDE", "LOST"
    IN, BAND, OUT, LOST = S_

    run(30)  # at the anchor: home after 20 s
    walk(90, 85, 2)  # east, to 15 m from the line: away from home, still clear of the bands
    walk(90, 98, 1)  # into the bands
    walk(90, 140, 2, then=100)  # past the line and lost; the beacon after 90 s outside
    walk(90, 0, 2, then=40)  # back to the middle: re-armed, then home again
    assert seen == [(IN, False, False), (IN, True, False), (IN, False, False), (BAND, False, False),
                    (OUT, False, False), (LOST, False, False), (LOST, False, True), (LOST, False, False),
                    (IN, False, False), (IN, True, False)]

    seen.clear()
    walk(0, 50, 2, then=10)  # into the pond: a keep-out zone
    walk(0, 0, 2, then=40)  # and back home
    # The pond's bands start 32 m from the anchor, nearer than a running dog gets before its GPS has
    # a fix again (the home mode card warns about it): the first fix after home is already in them.
    assert seen == [(IN, True, False), (BAND, False, False), (OUT, False, False),
                    (LOST, False, False), (IN, False, False), (IN, True, False)]
    assert any(not c["ok"] for c in client.get("/api/home-anchor").json()["collars"])
    # While home the GPS was off: the trail did not grow.
    c = client.get("/api/collars").json()[node_id]
    assert c["status"]["home"]
    n = len(c["track"])
    run(30)
    assert len(client.get("/api/collars").json()[node_id]["track"]) == n


def test_new_fences_end_home_mode_in_the_simulator():
    from geocollar_base import sim as simmod

    sim = walker()
    sim.set_anchor(protocol.HomeAnchor(protocol.AnchorKind.BEACON, "AA:BB:CC:01:02:03", -85), (40.0, -75.0))
    for t in range(1000, 1000 + simmod.HOME_ENTER_S + 3):
        sim.step(1.0, now=t)
    assert sim.home
    sim.set_fences([YARD], 8)  # as the firmware does: away, and a clear fix needed first
    assert not sim.home and sim.events[1]["text"] == "new fences: GPS on"
    sim.step(1.0, now=t + 1)
    assert not sim.home  # the anchor's 20 s start again
    for t in range(t + 2, t + 2 + simmod.HOME_ENTER_S + 2):
        sim.step(1.0, now=t)
    assert sim.home


def test_too_few_satellites_in_the_simulator(tmp_path):
    """Fixes from fewer than 12 satellites are not used: no fence decision, the position stays the
    last good one, and the base hears "no GPS fix" once that is old. A dog that walks out under
    trees is not noticed until a fix from 12 or more."""
    sim = walker()
    for t in range(1000, 1005):
        sim.step(1.0, now=t)
    good = sim.report(1005)
    assert good["sats"] >= protocol.MIN_FIX_SATS and not good["no_fix"]
    sim.update({"sats": 10, "target": list(_move(40.0, -75.0, 90, 150)), "mode": "goto", "speed_mps": 3})
    for t in range(1006, 1080):
        sim.step(1.0, now=t)
    assert sim.engine.state == S.INSIDE  # out past the line, and the collar does not know
    rep = sim.report(1080)
    assert rep["no_fix"] and (rep["lat"], rep["lon"]) == (good["lat"], good["lon"])
    assert any("satellites ignored" in e["text"] for e in sim.events)
    sim.update({"sats": 12})
    for t in range(1081, 1090):
        sim.step(1.0, now=t)
    assert sim.engine.state == S.LOST and not sim.report(1090)["no_fix"]
    with pytest.raises(ValueError):
        sim.update({"sats": 41})


def test_the_base_ignores_positions_from_too_few_satellites(tmp_path):
    client = TestClient(create_app({"data_dir": str(tmp_path)}))
    node_id = client.post("/api/sim", json={"lat": 40.0, "lon": -75.0}).json()["node_id"]
    store = client.app.state.store
    good = {"state": "INSIDE", "lat": 40.001, "lon": -75.001, "sats": 12, "no_fix": False}
    assert store.record_status(node_id, good)["lat"] == 40.001
    weak = store.record_status(node_id, {**good, "lat": 40.009, "lon": -75.009, "sats": 11})
    assert weak["no_fix"] and weak["few_sats"] and (weak["lat"], weak["lon"]) == (40.001, -75.001)
    track = client.get("/api/collars").json()[node_id]["track"]
    assert [p[1] for p in track][-1] == 40.001 and 40.009 not in [p[1] for p in track]
