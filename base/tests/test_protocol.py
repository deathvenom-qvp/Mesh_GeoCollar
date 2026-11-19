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


def test_config_matches_the_firmware():
    # Same bytes as firmware/test/test_main.cpp: [version][7][report interval s, little-endian]
    data = p.encode_config(300)
    assert data.hex() == "02072c01" and p.message_type(data) == p.MsgType.CONFIG
    assert p.decode_config(data) == 300
    for bad in (p.MIN_REPORT_S - 1, p.MAX_REPORT_S + 1):
        with pytest.raises(ValueError):
            p.encode_config(bad)


def test_mesh_names_match_the_firmware():
    # The same cases as meshNames in firmware/test/test_main.cpp.
    assert p.mesh_names("Rex") == ("Rex", "REX")
    assert p.mesh_names("  Biscuit  ") == ("Biscuit", "BISC")
    assert p.mesh_names("Old Yeller the Dog Star") == ("Old Yeller the Dog Star", "OYTD")
    assert p.mesh_names("Zoë") == ("Zoë", "ZOë")
    assert p.mesh_names("Chloé") == ("Chloé", "CHLO")


def test_numbered_owner_and_config_match_the_firmware():
    # Golden vectors shared with firmware/test/test_main.cpp.
    plain = p.encode_owner("Rex", "Ken", "5551234567")
    assert plain.hex() == "0205" "03526578" "034b656e" "0a35353531323334353637"
    assert p.decode_owner_seq(plain) == 0
    numbered = p.encode_owner("Rex", "Ken", "5551234567", 1763085600)
    assert numbered.hex() == plain.hex() + "208d1669"
    assert p.decode_owner(numbered) == ("Rex", "Ken", "5551234567") and p.decode_owner_seq(numbered) == 1763085600
    for bad in (numbered[:-1], numbered + b"\0", plain[:-1]):
        with pytest.raises(ValueError):
            p.decode_owner(bad)

    assert p.encode_config(300).hex() == "02072c01"
    assert p.encode_config(300, 1763085600).hex() == "02072c01208d1669"
    assert p.decode_config(p.encode_config(300, 1763085600)) == 300
    assert p.decode_config_seq(p.encode_config(300, 1763085600)) == 1763085600
    assert p.decode_config_seq(p.encode_config(300)) == 0


def test_config_home_anchor_matches_the_firmware():
    # Golden vector shared with firmware/test/test_main.cpp (testProtocol, the home anchor).
    a = p.HomeAnchor(p.AnchorKind.BEACON, "aa:bb:cc:01:02:03", -72)
    data = p.encode_config(0, 77, a)
    assert data.hex() == "020700004d000000" "01aabbcc010203b8"
    assert p.decode_config(data) == 0 and p.decode_config_seq(data) == 77
    assert p.decode_config_anchor(data) == p.HomeAnchor(p.AnchorKind.BEACON, "AA:BB:CC:01:02:03", -72)
    assert p.decode_config_anchor(p.encode_config(300, 77)) is None
    # The base radio needs no address: the collar knows it as the node that sends its fences.
    base = p.encode_config(600, 78, p.HomeAnchor(p.AnchorKind.BASE_RADIO, "ignored", -85))
    assert base[8:].hex() == "02" "000000000000" "ab"
    off = p.decode_config_anchor(p.encode_config(0, 79, p.HomeAnchor()))
    assert off.kind == p.AnchorKind.NONE
    with pytest.raises(ValueError):
        p.encode_config(0, 0, a)  # the anchor needs a sequence number
    for bad in (p.HomeAnchor(p.AnchorKind.BEACON, "aa:bb:cc", -72), p.HomeAnchor(p.AnchorKind.BEACON, "aa:bb:cc:01:02:0g", -72),
                p.HomeAnchor(p.AnchorKind.BASE_RADIO, "", -20), p.HomeAnchor(p.AnchorKind.BASE_RADIO, "", -101)):
        with pytest.raises(ValueError):
            p.encode_config(0, 80, bad)
    assert p.parse_bt_address("AA-BB-CC-01-02-03") == p.parse_bt_address("aabbcc010203") == bytes.fromhex("aabbcc010203")


def test_config_gps_inside_matches_the_firmware():
    # Golden vector shared with firmware/test/test_main.cpp (testProtocol, the GPS interval inside).
    data = p.encode_config(0, 77, p.HomeAnchor(p.AnchorKind.BASE_RADIO, "", -75), 3)
    assert data.hex() == "020700004d000000" "02000000000000b5" "03"
    assert p.decode_config_gps_inside(data) == 3
    assert p.decode_config_anchor(data).rssi == -75
    assert p.decode_config_gps_inside(p.encode_config(0, 77, p.HomeAnchor())) is None
    assert p.encode_config(0, 77, p.HomeAnchor(), 0)[-1] == 0  # always on
    assert p.encode_config(0, 77, p.HomeAnchor(), p.MAX_GPS_INSIDE_S)[-1] == 30
    for bad in (-1, 31):
        with pytest.raises(ValueError):
            p.encode_config(0, 77, p.HomeAnchor(), bad)
    with pytest.raises(ValueError):
        p.encode_config(0, 77, None, 3)  # it goes after the anchor
    # Bit 7: that interval everywhere inside.
    every = p.encode_config(0, 77, p.HomeAnchor(p.AnchorKind.BASE_RADIO, "", -75), 3, True)
    assert every[-1:].hex() == "83"
    assert p.decode_config_gps_inside(every) == 3 and p.decode_config_gps_everywhere(every)
    assert not p.decode_config_gps_everywhere(data)


def test_config_min_sats_matches_the_firmware():
    # Golden vector shared with firmware/test/test_main.cpp (testProtocol, the satellites a fix needs).
    data = p.encode_config(0, 77, p.HomeAnchor(p.AnchorKind.BASE_RADIO, "", -75), 3, False, 12)
    assert data.hex() == "020700004d000000" "02000000000000b5" "03" "0c"
    assert p.decode_config_gps_inside(data) == 3 and p.MIN_FIX_SATS == 12
    for bad in (p.MIN_SATS_LOW - 1, p.MIN_SATS_HIGH + 1):
        with pytest.raises(ValueError):
            p.encode_config(0, 77, p.HomeAnchor(), 3, False, bad)
    with pytest.raises(ValueError):
        p.encode_config(0, 77, p.HomeAnchor(), None, False, 12)  # it goes after the GPS interval


def test_status_home_flag():
    s = p.Status(p.MsgType.STATUS, 3, p.CollarState.INSIDE, 80, 40.0, -75.0, 1.0, 9, 40, True, False, home=True)
    raw = p.encode_status(s)
    assert raw[-1] == 0x01 | 0x80
    assert p.decode_status(raw).home and not p.decode_status(raw[:-1] + bytes([0x01])).home


def test_status_fix_and_firmware_flags():
    s = p.Status(p.MsgType.STATUS, 3, p.CollarState.INSIDE, 80, 40.0, -75.0, 1.0, 9, 40, True, False,
                 no_fix=True, numbered=True)
    raw = p.encode_status(s)
    assert raw[-1] == 0x01 | 0x10 | 0x20
    back = p.decode_status(raw)
    assert back.no_fix and back.numbered and not back.beacon_active
    # Firmware before 0.16 sets neither bit.
    old = p.decode_status(raw[:-1] + bytes([0x03]))
    assert not old.no_fix and not old.numbered and old.shock_enabled


# -- The companion app: the same golden bytes as firmware/test/test_main.cpp and app/test ---------
APP_KEY, APP_KEY_ID = bytes(range(16)), 0x1A2B3C4D
APP_NONCE, COLLAR_NONCE = bytes(range(0xA0, 0xA8)), bytes(range(0xC0, 0xC8))


def test_app_keys_golden_and_limits():
    raw = p.encode_app_keys([(APP_KEY_ID, APP_KEY)], 1763085600)
    assert raw.hex() == "0208208d1669014d3c2b1a000102030405060708090a0b0c0d0e0f"
    assert p.message_type(raw) == p.MsgType.APP_KEYS
    assert p.decode_app_keys(raw) == ([(APP_KEY_ID, APP_KEY)], 1763085600)
    assert p.encode_app_keys([], 7).hex() == "02080700000000"  # every phone removed
    assert len(p.encode_app_keys([(i + 1, APP_KEY) for i in range(4)], 7)) == 87
    for bad in ([(i + 1, APP_KEY) for i in range(5)], [(0, APP_KEY)], [(1, APP_KEY), (1, APP_KEY)], [(1, b"short")]):
        with pytest.raises(ValueError):
            p.encode_app_keys(bad, 7)
    with pytest.raises(ValueError):
        p.encode_app_keys([], 0)  # unnumbered: a recording could bring a removed phone back

    ack = p.encode_app_keys_ack(1763085600, 1)
    assert ack.hex() == "0209208d166901" and p.decode_app_keys_ack(ack) == {"seq": 1763085600, "count": 1}
    assert p.message_type(ack) == p.MsgType.APP_KEYS_ACK
    with pytest.raises(ValueError):
        p.decode_app_keys_ack(ack + b"\x00")


def test_app_session_golden():
    assert p.encode_app_hello(APP_KEY_ID, APP_NONCE).hex() == "02104d3c2b1aa0a1a2a3a4a5a6a7"
    assert p.app_challenge_tag(APP_KEY, APP_KEY_ID, APP_NONCE, COLLAR_NONCE).hex() == "c66cfaa9f4fe36cb"
    sk = p.app_session_key(APP_KEY, APP_KEY_ID, APP_NONCE, COLLAR_NONCE)
    assert sk.hex() == "4b4ad99bb6e9aa4a581e9cc9defdb082abb994605cae8f691fde30c639488fce"
    req = p.encode_app_request(sk, APP_KEY_ID, 1, p.CommandAction.SILENCE_LOST)
    assert req.hex() == "02124d3c2b1a010000000f000000d2b8917361311478"
    raw = bytes.fromhex("02134d3c2b1a010000000003000457d288d7179fd74bd3b40009f4ff012a0017682af07f6aecce")
    s = p.decode_app_status(sk, raw)
    assert s["key_id"] == APP_KEY_ID and s["counter"] == 1 and s["done"] and s["fix_age_s"] == 42
    assert s["status"].state == p.CollarState.LOST and s["status"].clearance_m == -12
    assert abs(s["status"].lat - 40.0001234) < 1e-9
    with pytest.raises(ValueError):
        p.decode_app_status(sk, raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(ValueError):
        p.decode_app_status(bytes(32), raw)
    assert p.CommandAction.SHOCK not in p.APP_ACTIONS and p.CommandAction.SHOCK_ON not in p.APP_ACTIONS


def test_status_says_whether_the_collar_holds_app_keys():
    raw = bytearray.fromhex("020303000457d288d7179fd74bd3b40009f4ff01")
    assert p.decode_status(bytes(raw)).apps is False
    raw[-1] |= 0x40
    s = p.decode_status(bytes(raw))
    assert s.apps is True and p.encode_status(s) == bytes(raw)


def test_pairing_code_round_trip():
    code = p.pairing_code("!a1b2c3d4", APP_KEY_ID, APP_KEY, dog="Zo\u00eb & Max", pin=4821, ble_name="Z&M")
    assert code == ("geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw"
                    "&p=004821&b=Z%26M&d=Zo%C3%AB%20%26%20Max")
    back = p.parse_pairing_code(code)
    assert back == {"node_id": "!a1b2c3d4", "key_id": APP_KEY_ID, "key": APP_KEY, "pin": "004821",
                    "ble_name": "Z&M", "dog": "Zo\u00eb & Max", "sim": False}
    bare = p.pairing_code("!a1b2c3d4", APP_KEY_ID, APP_KEY, sim=True)
    assert bare == "geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw&s=1"
    assert p.parse_pairing_code(bare)["pin"] is None and p.parse_pairing_code(bare)["sim"] is True
    for bad in ("https://example.com/pair?v=1", "geocollar://pair?v=2&n=a1b2c3d4&i=1&k=AAECAwQFBgcICQoLDA0ODw"):
        with pytest.raises(ValueError):
            p.parse_pairing_code(bad)


def test_a_phone_linked_to_the_whole_base_has_a_key_for_each_collar():
    # Golden bytes, shared with app/test/protocol.test.mjs.
    assert p.collar_app_key(APP_KEY, "!a1b2c3d4").hex() == "faabcb3c8ddf0f9da74afdae47c96531"
    assert p.collar_app_key(APP_KEY, 0xA1B2C3D4) == p.collar_app_key(APP_KEY, "!a1b2c3d4")
    assert p.collar_app_key(APP_KEY, "!a1b2c3d5") != p.collar_app_key(APP_KEY, "!a1b2c3d4")
    with pytest.raises(ValueError):
        p.collar_app_key(bytes(15), "!a1b2c3d4")


def test_home_pairing_code_round_trip():
    collars = [{"node_id": "!a1b2c3d4", "pin": 4711, "ble_name": "BE", "dog": "Bella & Co"},
               {"node_id": "!5a000001", "dog": "Sim", "sim": True}]
    code = p.home_pairing_code("0badc0de", collars, APP_KEY_ID, APP_KEY)
    # Shared with app/test/protocol.test.mjs.
    assert code == ("geocollar://pair?v=2&h=0badc0de&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw"
                    "&n=a1b2c3d4&p=004711&b=BE&d=Bella%20%26%20Co&n=5a000001&d=Sim&s=1")
    back = p.parse_home_pairing_code(code)
    assert back == {"home": "0badc0de", "key_id": APP_KEY_ID, "key": APP_KEY, "collars": [
        {"node_id": "!a1b2c3d4", "pin": "004711", "ble_name": "BE", "dog": "Bella & Co", "sim": False},
        {"node_id": "!5a000001", "pin": None, "ble_name": "", "dog": "Sim", "sim": True}]}
    # Without a key: the collar list, for a phone already linked to this base.
    bare = p.home_pairing_code("0badc0de", collars[1:])
    assert bare == "geocollar://pair?v=2&h=0badc0de&n=5a000001&d=Sim&s=1"
    assert p.parse_home_pairing_code(bare)["key"] is None
    for bad in (("0BADC0DE", None, None), ("0badc0d", None, None), ("0badc0de", 0, APP_KEY), ("0badc0de", 1, bytes(3))):
        with pytest.raises(ValueError):
            p.home_pairing_code(bad[0], collars, bad[1], bad[2])
    with pytest.raises(ValueError):
        p.parse_home_pairing_code("geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw")
