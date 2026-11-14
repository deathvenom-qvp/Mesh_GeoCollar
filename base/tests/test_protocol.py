# SPDX-License-Identifier: GPL-3.0-or-later
# Golden vectors are shared with firmware/test/test_main.cpp, so both sides stay in step.

import pytest

from geocollar_base import protocol as p

SQUARE = [(40.0, -75.0), (40.0, -74.998), (40.002, -74.998), (40.002, -75.0)]


def test_command_golden():
    assert p.encode_command(p.CommandAction.VIBRATE, 40, 1500, 1763085600).hex() == "02040228dc05208d1669"  # as firmware
    assert p.next_fence_version(4, None) == 5 and p.next_fence_version(4, 9) == 10 and p.next_fence_version(9, 4) == 10
    assert p.next_fence_version(65535, None) == 1 and p.next_fence_version(65530, 3) == 4  # wraps, never 0


def test_command_rejects_bad_intensity():
    with pytest.raises(ValueError):
        p.encode_command(p.CommandAction.SHOCK, 120)


def test_status_golden_round_trip():
    raw = bytes.fromhex("020303000457d288d7179fd74bd3b40009f4ff01")
    s = p.decode_status(raw)
    assert s.msg_type == p.MsgType.BREACH
    assert s.fence_version == 3 and s.state == p.CollarState.LOST and s.battery_pct == 87
    assert s.lat == pytest.approx(40.0001234) and s.lon == pytest.approx(-75.0004321)
    assert s.pdop == pytest.approx(1.8) and s.sats == 9 and s.clearance_m == -12
    assert s.corrections_enabled and not s.shock_enabled
    assert p.encode_status(s) == raw


def test_fence_set_golden():
    blob = p.encode_fence_set([p.Zone(p.ZoneKind.KEEP_IN, 3, 5, 7, SQUARE)], p.Pulses(4, 2))
    assert blob.hex() == (
        "010402"
        "000403050700"
        "0084d71780e84bd3"
        "0084d717a0364cd3"
        "20d2d717a0364cd3"
        "20d2d71780e84bd3"
    )


def test_chunking_matches_firmware_rules():
    big = p.Zone(p.ZoneKind.KEEP_OUT, 0, 5, 5, [(40.0 + i * 1e-5, -75.0 + i * 1e-5) for i in range(32)])
    small = p.Zone(p.ZoneKind.KEEP_IN, 2, 4, 6, SQUARE)
    chunks = p.fence_chunks([small, big], fence_version=9)
    assert len(chunks) == 2
    assert all(c[:2] == b"\x02\x01" for c in chunks)
    assert len(chunks[0]) == 6 + p.MAX_CHUNK_DATA  # every chunk but the last is full
    blob = b"".join(c[6:] for c in chunks)
    assert blob == p.encode_fence_set([small, big])


def test_geojson_import():
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "keep_out", "danger_m": 2, "warning_m": 4, "attention_m": 6},
                "geometry": {"type": "Polygon", "coordinates": [[[-75.0, 40.0], [-74.998, 40.0], [-74.998, 40.002], [-75.0, 40.0]]]},
            }
        ],
    }
    (z,) = p.zones_from_geojson(gj)
    assert z.kind == p.ZoneKind.KEEP_OUT and (z.danger_m, z.warning_m, z.attention_m) == (2, 4, 6)
    assert z.vertices == [(40.0, -75.0), (40.0, -74.998), (40.002, -74.998)]


def test_geojson_old_margin_and_defaults():
    def zone(props):
        f = {"type": "Feature", "properties": props,
             "geometry": {"type": "Polygon", "coordinates": [[[-75.0, 40.0], [-74.998, 40.0], [-74.998, 40.002]]]}}
        (z,) = p.zones_from_geojson(f)
        return (z.danger_m, z.warning_m, z.attention_m)

    assert zone({"warn_margin_m": 10}) == (0, 5, 5)  # fences saved before bands existed
    assert zone({}) == (0, 5, 5)
    for bad in ({"danger_m": -1}, {"warning_m": 256}, {"attention_m": 2.5}, {"danger_m": True}):
        with pytest.raises(ValueError):
            zone(bad)


def test_pulses():
    assert p.pulses_from_geojson({"type": "FeatureCollection", "features": []}) == p.Pulses(0, 0)
    assert p.pulses_from_geojson({"pulse": {"beep_s": 2, "vibrate_s": 10}}) == p.Pulses(2, 10)
    for bad in ({"beep_s": 1}, {"vibrate_s": 11}, {"beep_s": 2.5}, {"beep_s": True}):
        with pytest.raises(ValueError):
            p.pulses_from_geojson({"pulse": bad})


def test_message_type():
    assert p.message_type(bytes.fromhex("02040228dc05")) == p.MsgType.COMMAND
    assert p.message_type(b"\x01\x04") is None  # version 1 is no longer understood


def test_owner_golden_and_limits():
    data = p.encode_owner("Rex", "Ken", "555-123-4567")
    assert data.hex() == "020503526578034b656e0c3535352d3132332d34353637"  # firmware test uses the same bytes
    assert p.message_type(data) == p.MsgType.OWNER
    assert p.decode_owner(data) == ("Rex", "Ken", "555-123-4567")
    long = p.encode_owner("Ä" * 20, "Ken\n", "1" * 30)
    dog, owner, phone = p.decode_owner(long)
    assert dog == "Ä" * 12 and owner == "Ken" and phone == "1" * 20


def test_status_beacon_flags():
    s = p.Status(p.MsgType.BREACH, 3, p.CollarState.LOST, 80, 40.0, -75.0, 1.0, 9, -40, True, False, True, False)
    back = p.decode_status(p.encode_status(s))
    assert back.beacon_active and not back.beacon_silenced
