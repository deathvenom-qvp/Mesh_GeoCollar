# SPDX-License-Identifier: GPL-3.0-or-later
"""Link to the base station's Meshtastic node, using the official Meshtastic Python library."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Callable

from . import protocol

log = logging.getLogger(__name__)


def _hops(packet: dict) -> int | None:
    start, left = packet.get("hopStart"), packet.get("hopLimit")
    return start - left if start is not None and left is not None else None


class MeshLink:
    """Sends GeoCollar messages to collars and hands received status reports to a callback.

    Messages go as direct messages to each collar with pkiEncrypted=True, so only that collar can
    read them; relays in between see only the packet header. The collar only accepts them if
    this base node's public key is in the collar's admin keys.
    """

    def __init__(self, on_status: Callable[[str, dict], None], serial_port: str = "", host: str = "",
                 on_hello: Callable[[str, dict], None] | None = None,
                 key_for: Callable[[str], str | None] | None = None,
                 on_send_error: Callable[[str, str], None] | None = None):
        self.on_status = on_status
        self.on_hello = on_hello
        # The collar's public key (base64) as read over USB at setup, or None: the radio needs it
        # in its node list to PKI-encrypt anything to the collar.
        self.key_for = key_for
        self.on_send_error = on_send_error
        self.serial_port = serial_port
        self.host = host
        self.iface = None
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, retry_secs: float = 30) -> None:
        """Connect in the background, and reconnect whenever the node goes away (unplugged,
        rebooted, Wi-Fi dropped), so the web page stays up without a radio."""

        def run():
            while not self._stop.is_set():
                if self.iface is None:
                    try:
                        self.connect()
                        self.last_error = ""
                    except Exception as e:  # noqa: BLE001 - any failure just means retry later
                        self.last_error = str(e) or type(e).__name__
                        log.warning("could not connect to the Meshtastic node: %s", self.last_error)
                        self._drop()
                self._stop.wait(retry_secs)

        self._thread = threading.Thread(target=run, name="meshlink", daemon=True)
        self._thread.start()

    def connect(self) -> None:
        from pubsub import pub  # installed with meshtastic

        if self.host:
            from meshtastic.tcp_interface import TCPInterface

            self.iface = TCPInterface(hostname=self.host)
        else:
            from meshtastic.serial_interface import SerialInterface

            self.iface = SerialInterface(devPath=self.serial_port or None)
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        log.info("connected to base node %s", self.iface.getMyNodeInfo().get("user", {}).get("id"))

    def _on_lost(self, interface=None):  # noqa: ARG002 - pubsub signature
        log.warning("lost the connection to the Meshtastic node; will retry")
        self.last_error = "connection lost"
        self._drop()

    def _drop(self) -> None:
        iface, self.iface = self.iface, None
        if iface:
            try:
                iface.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._stop.set()
        self._drop()

    @property
    def connected(self) -> bool:
        return self.iface is not None

    def _on_receive(self, packet, interface=None):  # noqa: ARG002 - pubsub signature
        decoded = packet.get("decoded") or {}
        if decoded.get("portnum") not in (protocol.PORT_NUM, str(protocol.PORT_NUM)):
            return
        data = decoded.get("payload") or b""
        kind = protocol.message_type(data)
        if kind == protocol.MsgType.HELLO and self.on_hello:
            try:
                hello = protocol.decode_hello(data)
            except ValueError:
                return
            node_id = packet.get("fromId") or f"!{packet['from']:08x}"
            user = ((self.iface.nodes or {}).get(node_id) or {}).get("user", {}) if self.iface else {}
            hello.update(long_name=user.get("longName", ""), short_name=user.get("shortName", ""),
                         rssi=packet.get("rxRssi"), snr=packet.get("rxSnr"), hops=_hops(packet))
            self.on_hello(node_id, hello)
            return
        if kind not in (protocol.MsgType.STATUS, protocol.MsgType.BREACH):
            return
        try:
            status = protocol.decode_status(data)
        except ValueError as e:
            log.warning("bad status from %s: %s", packet.get("fromId"), e)
            return
        node_id = packet.get("fromId") or f"!{packet['from']:08x}"
        record = asdict(status)
        # Who really sent it: a collar's reports are PKI direct messages, so the radio knows the
        # sender's public key. The app checks it against the key it saw first from that collar.
        record["pki"] = bool(packet.get("pkiEncrypted"))
        record["sender_key"] = packet.get("publicKey") or ""
        record["msg_type"] = status.msg_type.name
        record["state"] = status.state.name
        self.on_status(node_id, record)

    def _send(self, node_id: str, data: bytes) -> None:
        if not self.iface:
            raise RuntimeError("not connected to a Meshtastic node")
        self.ensure_contact(node_id)
        self.iface.sendData(
            data, destinationId=node_id, portNum=protocol.PORT_NUM, wantAck=True, pkiEncrypted=True,
            onResponse=lambda p: self._on_routing(node_id, p), onResponseAckPermitted=True,
        )

    def _on_routing(self, node_id: str, packet: dict) -> None:
        """The radio's answer to a message we sent: an ack, or why it failed. The usual failure is
        PKI_SEND_FAIL_PUBLIC_KEY: the radio has no public key for the collar, so it sent nothing."""
        reason = ((packet.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
        if reason in ("NONE", None):
            return
        log.warning("message to %s failed: %s", node_id, reason)
        if self.on_send_error:
            self.on_send_error(node_id, str(reason))

    # ---------------------------------------------------------------------------------------------
    # Public keys. The radio PKI-encrypts to a collar only if its node list has the collar's key.
    # It learns keys from NodeInfo on the air; the base also tells it the key read over USB.
    # ---------------------------------------------------------------------------------------------
    def radio_key(self, node_id: str) -> str:
        """The collar's public key (base64) as the radio has it, or "" if it has none."""
        if not self.iface:
            return ""
        try:
            num = int(node_id.lstrip("!"), 16)
        except ValueError:
            return ""
        node = (getattr(self.iface, "nodesByNum", None) or {}).get(num) or {}
        return (node.get("user") or {}).get("publicKey") or ""

    def ensure_contact(self, node_id: str) -> bool:
        """If the radio lacks the collar's key from setup (or has another), add the collar to its
        node list with that key, marked verified so a NodeInfo on the air can't replace it. Returns
        True if it sent one."""
        key = self.key_for(node_id) if self.key_for else None
        if not key or not self.iface or self.radio_key(node_id) == key:
            return False
        import base64

        from meshtastic.protobuf import admin_pb2

        num = int(node_id.lstrip("!"), 16)
        m = admin_pb2.AdminMessage()
        c = m.add_contact
        c.node_num = num
        c.manually_verified = True
        c.user.id = node_id
        c.user.long_name = f"GeoCollar {node_id[-4:]}"
        c.user.short_name = node_id[-4:]
        c.user.public_key = base64.b64decode(key)
        self.iface.localNode._sendAdmin(m, wantResponse=False)  # noqa: SLF001 - the library's add_contact takes a URL
        # The library's copy of the radio's node list doesn't hear about it; keep it in step.
        node = self.iface._getOrCreateByNum(num)  # noqa: SLF001
        node.setdefault("user", {})["publicKey"] = key
        log.info("gave the base radio the public key of %s from its USB setup", node_id)
        # The radio encrypts an outgoing message as it arrives but applies admin messages a moment
        # later, so give it time before the message that needs the key.
        time.sleep(1.5)
        return True

    def forget_key(self, node_id: str) -> None:
        """The radio turned out not to have the collar's key: forget the library's copy, so the
        next message gives it the key again."""
        if not self.iface:
            return
        try:
            num = int(node_id.lstrip("!"), 16)
        except ValueError:
            return
        node = (getattr(self.iface, "nodesByNum", None) or {}).get(num) or {}
        (node.get("user") or {}).pop("publicKey", None)

    def request_node_info(self, node_id: str) -> None:
        """Swap NodeInfo with a collar: send the base's (name and public key) and ask for its own.
        Meshtastic answers this at most once every 12 hours per asker."""
        if not self.iface:
            raise RuntimeError("not connected to a Meshtastic node")
        from meshtastic.protobuf import mesh_pb2, portnums_pb2

        info = (self.iface.getMyNodeInfo() or {}).get("user", {})
        user = mesh_pb2.User(id=info.get("id", ""), long_name=info.get("longName", ""),
                             short_name=info.get("shortName", ""))
        cfg = getattr(getattr(self.iface, "localNode", None), "localConfig", None)
        if cfg is not None and cfg.security.public_key:
            user.public_key = bytes(cfg.security.public_key)
        try:
            user.hw_model = mesh_pb2.HardwareModel.Value(info.get("hwModel", "UNSET"))
        except ValueError:
            pass
        self.iface.sendData(user, destinationId=int(node_id.lstrip("!"), 16),
                            portNum=portnums_pb2.PortNum.NODEINFO_APP, wantResponse=True)
        log.info("asked %s for its NodeInfo (the base radio has no public key for it)", node_id)

    def send_fences(self, node_id: str, zones: list[protocol.Zone], fence_version: int,
                    pulses: protocol.Pulses | None = None) -> int:
        chunks = protocol.fence_chunks(zones, fence_version, pulses)
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(2)  # give the mesh room between chunks
            self._send(node_id, chunk)
        return len(chunks)

    def base_info(self) -> dict:
        """What the page shows about the base radio: identity, public key (base64, for collars'
        admin keys), firmware and radio settings, and anything that would stop collars working."""
        if not self.iface:
            return {}
        import base64

        info = self.iface.getMyNodeInfo() or {}
        user = info.get("user", {})
        key = user.get("publicKey") or ""
        out = {"node_id": user.get("id", ""), "long_name": user.get("longName", ""),
               "hw_model": user.get("hwModel", ""), "connection": f"network {self.host}" if self.host else f"USB {self.serial_port or '(auto)'}"}
        node = getattr(self.iface, "localNode", None)
        cfg = getattr(node, "localConfig", None)
        try:
            if not key and cfg is not None and cfg.security.public_key:
                key = base64.b64encode(cfg.security.public_key).decode()
        except AttributeError:
            pass
        out["public_key"] = key
        meta = getattr(self.iface, "metadata", None)
        out["firmware"] = getattr(meta, "firmware_version", "") or ""
        issues = []
        if cfg is not None:
            from meshtastic.protobuf import config_pb2

            lora = cfg.lora
            out["region"] = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
            out["preset"] = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset) if lora.use_preset else "custom"
            out["role"] = config_pb2.Config.DeviceConfig.Role.Name(cfg.device.role)
            if lora.region == 0:
                issues.append("The LoRa region is not set, so the radio does not transmit. Set it in the Meshtastic app (Settings > LoRa).")
            if not lora.tx_enabled:
                issues.append("Transmit is turned off in the LoRa settings.")
        chans = getattr(node, "channels", None) or []
        if chans:
            primary = chans[0].settings
            out["channel"] = primary.name or "(default)"
            out["public_channel"] = any(c.role and bytes(c.settings.psk) in (b"", b"\x01") for c in chans)
            # Radios pick their frequency slot from the primary channel's name: only a blank name (shown
            # as the preset's) shares the slot with strangers on the default channel.
            lora = getattr(cfg, "lora", None)
            out["default_slot"] = not primary.name and not (lora and (lora.channel_num or lora.override_frequency))
        if user.get("isLicensed"):
            issues.append("Licensed (ham) mode is on, which turns off encryption; collars cannot trust this base.")
        if not key:
            issues.append("The radio has no public key. Update it to Meshtastic 2.5 or later.")
        ver = out["firmware"].split(".")
        try:
            if ver[0] and (int(ver[0]), int(ver[1])) < (2, 5):
                issues.append(f"Meshtastic {out['firmware']} is too old; collars need 2.5 or later on the base.")
        except (ValueError, IndexError):
            pass
        out["issues"] = issues
        return out

    def send_owner(self, node_id: str, dog: str, owner: str, phone: str, seq: int = 0) -> None:
        """The dog's name and the owner's contact, for the collar's lost beacon. seq as in
        protocol.encode_owner: 0 for collar firmware that does not read it."""
        self._send(node_id, protocol.encode_owner(dog, owner, phone, seq))

    def send_config(self, node_id: str, report_s: int, seq: int = 0) -> None:
        """How often the collar reports."""
        self._send(node_id, protocol.encode_config(report_s, seq))

    def send_command(self, node_id: str, action: protocol.CommandAction, intensity: int = 0, duration_ms: int = 1000,
                     seq: int = 0):
        self._send(node_id, protocol.encode_command(action, intensity, duration_ms, seq))

    # ---------------------------------------------------------------------------------------------
    # Remote admin, for firmware updates. The collar already trusts this base node's public key as
    # an admin key (that is how it accepts fences), so standard Meshtastic admin messages work too.
    # Changes need a session passkey, which the collar hands out in any admin response; the
    # Meshtastic library stores it per node and _admin() copies it into each request.
    # ---------------------------------------------------------------------------------------------
    def _admin(self, node_id: str, message, want_response: bool = True, timeout: float = 90):
        """Send an AdminMessage to a remote node. Returns the reply AdminMessage, or None when
        want_response is False. Raises TimeoutError or RuntimeError."""
        from meshtastic.protobuf import portnums_pb2

        if not self.iface:
            raise RuntimeError("not connected to a Meshtastic node")
        num = int(node_id.lstrip("!"), 16)
        self.ensure_contact(node_id)
        key = self.iface._getOrCreateByNum(num).get("adminSessionPassKey")  # noqa: SLF001 - library-internal store
        if key:
            message.session_passkey = key
        done = threading.Event()
        reply: dict = {}

        def on_response(packet):
            decoded = packet.get("decoded") or {}
            if "admin" in decoded:
                reply["admin"] = decoded["admin"].get("raw")
                done.set()
            elif (decoded.get("routing") or {}).get("errorReason", "NONE") not in ("NONE", None):
                reply["error"] = decoded["routing"]["errorReason"]
                done.set()
            elif not want_response:
                done.set()

        self.iface.sendData(
            message, destinationId=num, portNum=portnums_pb2.PortNum.ADMIN_APP, wantAck=True,
            wantResponse=want_response, onResponse=on_response, onResponseAckPermitted=not want_response,
            pkiEncrypted=True,
        )
        if not done.wait(timeout):
            raise TimeoutError(f"no answer from {node_id} within {int(timeout)} s")
        if "error" in reply:
            raise RuntimeError(f"{node_id} refused the request: {reply['error']}")
        return reply.get("admin")

    # ---------------------------------------------------------------------------------------------
    # The base radio's own settings, and moving collars along with them.
    # ---------------------------------------------------------------------------------------------
    def radio_settings(self) -> dict:
        from . import radio

        if not self.iface:
            raise RuntimeError("not connected to a Meshtastic node")
        user = (self.iface.getMyNodeInfo() or {}).get("user", {})
        return radio.describe(self.iface.localNode, user)

    def apply_local(self, lora=None, channels=None, long_name: str | None = None, short_name: str | None = None) -> None:
        """Write settings to the base radio in one transaction (it reboots once if needed)."""
        if not self.iface:
            raise RuntimeError("not connected to a Meshtastic node")
        node = self.iface.localNode
        licensed = bool((self.iface.getMyNodeInfo() or {}).get("user", {}).get("isLicensed"))
        node.beginSettingsTransaction()
        if long_name is not None or short_name is not None:
            node.setOwner(long_name=long_name, short_name=short_name, is_licensed=licensed)
        if lora is not None:
            node.localConfig.lora.CopyFrom(lora)
            node.writeConfig("lora")
        for ch in channels or []:
            node.channels[ch.index].CopyFrom(ch)
            node.writeChannel(ch.index)
        node.commitSettingsTransaction()

    def push_radio(self, node_id: str, lora, channels, primary_moves: bool = True, timeout: float = 90) -> bool:
        """Give a collar new LoRa settings and channels over the mesh, as its admin (this works on
        a collar locked to this base). All in one edit, so it restarts once, at the end.

        The collar keeps its radio settings until the edit is committed, but switches its primary
        channel the moment it gets it, and acknowledges with that channel's key, which the base can't
        read until it changes too. So the primary channel and the commit go last, and when the primary
        changes their acknowledgements may never arrive. Returns True when every step was
        acknowledged, False when those last two could not be confirmed. Raises if an earlier step
        fails."""
        from meshtastic.protobuf import admin_pb2

        def send(build, blind=False):
            m = admin_pb2.AdminMessage()
            build(m)
            try:
                self._admin(node_id, m, want_response=False, timeout=45 if blind else timeout)
            except (TimeoutError, RuntimeError):
                if not blind:
                    raise
                return False
            return True

        send(lambda m: setattr(m, "begin_edit_settings", True))
        for ch in channels:
            if ch.index != 0:
                send(lambda m, ch=ch: m.set_channel.CopyFrom(ch))
        send(lambda m: m.set_config.lora.CopyFrom(lora))
        ok = send(lambda m: m.set_channel.CopyFrom(channels[0]), blind=primary_moves)
        return send(lambda m: setattr(m, "commit_edit_settings", True), blind=primary_moves) and ok

    def get_metadata(self, node_id: str, timeout: float = 90):
        """DeviceMetadata (firmware_version, hasWifi, hw_model...). Also fetches a session key."""
        from meshtastic.protobuf import admin_pb2

        m = admin_pb2.AdminMessage()
        m.get_device_metadata_request = True
        reply = self._admin(node_id, m, timeout=timeout)
        return reply.get_device_metadata_response

    def get_network_config(self, node_id: str, timeout: float = 90):
        from meshtastic.protobuf import admin_pb2

        m = admin_pb2.AdminMessage()
        m.get_config_request = admin_pb2.AdminMessage.ConfigType.NETWORK_CONFIG
        return self._admin(node_id, m, timeout=timeout).get_config_response.network

    def forget_wifi(self, node_id: str, ssid: str, timeout: float = 90) -> bool:
        """Remove the Wi-Fi network an older version of this app saved on the node for Wi-Fi
        updates. Only if the node holds that network (ssid): one the owner set up is left alone.
        Returns whether there was anything to remove; if so, the node reboots to apply it."""
        from meshtastic.protobuf import admin_pb2

        net = self.get_network_config(node_id, timeout)
        if not net.wifi_ssid or net.wifi_ssid != ssid:
            return False
        m = admin_pb2.AdminMessage()
        m.set_config.network.CopyFrom(net)
        m.set_config.network.wifi_ssid = ""
        m.set_config.network.wifi_psk = ""
        m.set_config.network.wifi_enabled = False
        self._admin(node_id, m, want_response=False, timeout=timeout)
        return True
