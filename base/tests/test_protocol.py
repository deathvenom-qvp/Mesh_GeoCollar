# SPDX-License-Identifier: GPL-3.0-or-later
# Golden vectors are shared with firmware/test/test_main.cpp, so both sides stay in step.

import pytest

from geocollar_base import protocol as p

SQUARE = [(40.0, -75.0), (40.0, -74.998), (40.002, -74.998), (40.002, -75.0)]


def test_command_golden():
    assert p.encode_command(p.CommandAction.VIBRATE, 40, 1500).hex() == "01040228dc05"


def test_command_rejects_bad_intensity():
    with pytest.raises(ValueError):
        p.encode_command(p.CommandAction.SHOCK, 120)


def test_status_golden_round_trip():
    raw = bytes.fromhex("010303000457d288d7179fd74bd3b40009f4ff01")
    s = p.decode_status(raw)
    assert s.msg_type == p.MsgType.BREACH
    assert s.fence_version == 3 and s.state == p.CollarState.LOST and s.battery_pct == 87
    assert s.lat == pytest.approx(40.0001234) and s.lon == pytest.approx(-75.0004321)
    assert s.pdop == pytest.approx(1.8) and s.sats == 9 and s.clearance_m == -12
    assert s.corrections_enabled and not s.shock_enabled
    assert p.encode_status(s) == raw


def test_fence_set_golden():
    blob = p.encode_fence_set([p.Zone(p.ZoneKind.KEEP_IN, 10, SQUARE)])
    assert blob.hex() == (
        "0100040a00"
        "0084d71780e84bd3"
        "0084d717a0364cd3"
        "20d2d717a0364cd3"
        "20d2d71780e84bd3"
    )


def test_chunking_matches_firmware_rules():
    big = p.Zone(p.ZoneKind.KEEP_OUT, 5, [(40.0 + i * 1e-5, -75.0 + i * 1e-5) for i in range(32)])
    small = p.Zone(p.ZoneKind.KEEP_IN, 12, SQUARE)
    chunks = p.fence_chunks([small, big], fence_version=9)
    assert len(chunks) == 2
    assert all(c[:2] == b"\x01\x01" for c in chunks)
    assert len(chunks[0]) == 6 + p.MAX_CHUNK_DATA  # every chunk but the last is full
    blob = b"".join(c[6:] for c in chunks)
    assert blob == p.encode_fence_set([small, big])


def test_geojson_import():
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "keep_out", "warn_margin_m": 4},
                "geometry": {"type": "Polygon", "coordinates": [[[-75.0, 40.0], [-74.998, 40.0], [-74.998, 40.002], [-75.0, 40.0]]]},
            }
        ],
    }
    (z,) = p.zones_from_geojson(gj)
    assert z.kind == p.ZoneKind.KEEP_OUT and z.warn_margin_m == 4
    assert z.vertices == [(40.0, -75.0), (40.0, -74.998), (40.002, -74.998)]


def test_message_type():
    assert p.message_type(bytes.fromhex("01040228dc05")) == p.MsgType.COMMAND
    assert p.message_type(b"\x02\x04") is None
