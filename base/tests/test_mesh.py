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


def test_base_info_reports_settings_and_problems():
    from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2

    class Node:
        localConfig = localonly_pb2.LocalConfig()
        channels = [channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)]

    Node.localConfig.lora.use_preset = True
    Node.localConfig.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
    Node.channels[0].settings.psk = b"\x01"

    class Meta:
        firmware_version = "2.7.15.567b8ea"

    class Iface:
        localNode = Node()
        metadata = Meta()

        def getMyNodeInfo(self):
            return {"user": {"id": "!0badbase", "longName": "Base", "hwModel": "HELTEC_V3", "publicKey": "a2V5"}}

    link = MeshLink(lambda *_: None, serial_port="/dev/ttyUSB0")
    link.iface = Iface()
    info = link.base_info()
    assert info["node_id"] == "!0badbase" and info["public_key"] == "a2V5" and info["preset"] == "LONG_FAST"
    assert info["region"] == "UNSET" and info["public_channel"] and info["connection"] == "USB /dev/ttyUSB0"
    assert any("region is not set" in i for i in info["issues"])
    Node.localConfig.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
    Node.localConfig.lora.tx_enabled = True
    assert link.base_info()["issues"] == []


def test_radio_gets_the_collars_key_and_reports_failed_sends(monkeypatch):
    import base64

    from geocollar_base import mesh

    monkeypatch.setattr(mesh.time, "sleep", lambda s: None)

    from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

    from geocollar_base import protocol

    admin, sent, errors = [], [], []

    class LocalNode:
        localConfig = None

        def _sendAdmin(self, m, wantResponse=True):
            admin.append((m, wantResponse))

    class Iface:
        localNode = LocalNode()
        nodesByNum = {}

        def _getOrCreateByNum(self, num):
            return self.nodesByNum.setdefault(num, {"num": num, "user": {"id": f"!{num:08x}"}})

        def getMyNodeInfo(self):
            return {"user": {"id": "!0badbase", "longName": "Base", "shortName": "BASE", "hwModel": "HELTEC_WIRELESS_TRACKER"}}

        def sendData(self, data, destinationId, portNum, wantAck=False, wantResponse=False, onResponse=None,
                     onResponseAckPermitted=False, pkiEncrypted=False):
            sent.append((data, destinationId, portNum, wantResponse, pkiEncrypted))
            if onResponse:
                onResponse({"decoded": {"routing": {"errorReason": "PKI_SEND_FAIL_PUBLIC_KEY"}}})

    key = base64.b64encode(b"K" * 32).decode()
    link = MeshLink(lambda *_: None, key_for=lambda n: key if n == "!7f5ffdcc" else None,
                    on_send_error=lambda n, r: errors.append((n, r)))
    link.iface = Iface()
    assert link.radio_key("!7f5ffdcc") == ""

    # The first message to the collar adds it to the radio's node list, with the key from setup.
    link.send_owner("!7f5ffdcc", "Testy", "Ken", "")
    (m, want), = admin
    assert not want and m.add_contact.node_num == 0x7F5FFDCC and m.add_contact.manually_verified
    assert m.add_contact.user.public_key == b"K" * 32 and m.add_contact.user.id == "!7f5ffdcc"
    assert link.radio_key("!7f5ffdcc") == key
    link.send_owner("!7f5ffdcc", "Testy", "Ken", "")
    assert len(admin) == 1  # only once
    assert sent[-1][1:] == ("!7f5ffdcc", protocol.PORT_NUM, False, True)
    assert errors[-1] == ("!7f5ffdcc", "PKI_SEND_FAIL_PUBLIC_KEY")  # what the radio said is passed on
    link.forget_key("!7f5ffdcc")  # the radio turned out not to have it: given again next time
    link.send_owner("!7f5ffdcc", "Testy", "Ken", "")
    assert len(admin) == 2

    # No key from setup: nothing to add; the base can swap NodeInfo with it instead.
    link.send_owner("!11112222", "", "", "")
    assert len(admin) == 2
    link.request_node_info("!11112222")
    data, dest, port, want_response, pki = sent[-1]
    assert dest == 0x11112222 and port == portnums_pb2.PortNum.NODEINFO_APP and want_response and not pki
    assert isinstance(data, mesh_pb2.User) and data.id == "!0badbase"
    assert data.hw_model == mesh_pb2.HardwareModel.HELTEC_WIRELESS_TRACKER
    assert isinstance(m, admin_pb2.AdminMessage)
