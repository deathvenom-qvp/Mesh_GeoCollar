# SPDX-License-Identifier: GPL-3.0-or-later

import time

from geocollar_base.mesh import MeshLink


def test_link_retries_until_the_node_answers():
    attempts = []

    class Iface:
        def close(self):
            pass

    def connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("no node")
        link.iface = Iface()

    link = MeshLink(lambda *_: None, host="192.0.2.1")
    link.connect = connect
    link.start(retry_secs=0.01)
    deadline = time.time() + 2
    while not link.connected and time.time() < deadline:
        time.sleep(0.01)
    assert link.connected and len(attempts) == 3 and link.last_error == ""

    link._on_lost()  # the node went away: drop it so the loop reconnects
    assert not link.connected and link.last_error == "connection lost"
    deadline = time.time() + 2
    while not link.connected and time.time() < deadline:
        time.sleep(0.01)
    assert link.connected
    link.close()


def test_remote_admin_for_ota():
    from meshtastic.protobuf import admin_pb2, portnums_pb2

    sent = []

    class Iface:
        nodes = {0x12345678: {"adminSessionPassKey": b"k" * 8}}

        def _getOrCreateByNum(self, num):
            return self.nodes.setdefault(num, {})

        def sendData(self, data, destinationId, portNum, wantAck, wantResponse, onResponse, onResponseAckPermitted, pkiEncrypted):
            sent.append((data, destinationId, portNum, wantResponse, pkiEncrypted))
            if wantResponse:
                reply = admin_pb2.AdminMessage()
                reply.get_device_metadata_response.firmware_version = "2.8.2.d5f1619"
                reply.get_device_metadata_response.hasWifi = True
                onResponse({"decoded": {"admin": {"raw": reply}}})
            else:
                onResponse({"decoded": {"routing": {"errorReason": "NONE"}}})

    link = MeshLink(lambda *_: None)
    link.iface = Iface()
    meta = link.get_metadata("!12345678")
    assert meta.firmware_version == "2.8.2.d5f1619" and meta.hasWifi
    link.request_ota("!12345678", b"\x01" * 32)
    msg, dest, port, want_response, pki = sent[-1]
    assert dest == 0x12345678 and port == portnums_pb2.PortNum.ADMIN_APP and pki and not want_response
    assert msg.ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_WIFI and msg.ota_request.ota_hash == b"\x01" * 32
    assert msg.session_passkey == b"k" * 8


def test_hello_from_a_new_collar_is_passed_on():
    from geocollar_base import protocol

    heard = []
    link = MeshLink(lambda *_: None, on_hello=lambda n, h: heard.append((n, h)))

    class Iface:
        nodes = {"!18abcd01": {"user": {"longName": "Meshtastic cd01", "shortName": "cd01"}}}

    link.iface = Iface()
    link._on_receive({"fromId": "!18abcd01", "from": 0x18ABCD01, "rxRssi": -90, "hopStart": 3, "hopLimit": 2,
                      "decoded": {"portnum": "300", "payload": protocol.encode_hello(False, False, 80)}})
    assert heard == [("!18abcd01", {"has_admin_key": False, "has_fences": False, "battery_pct": 80,
                                    "long_name": "Meshtastic cd01", "short_name": "cd01", "rssi": -90, "snr": None, "hops": 1})]
