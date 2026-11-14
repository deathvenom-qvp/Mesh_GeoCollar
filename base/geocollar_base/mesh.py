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


class MeshLink:
    """Sends GeoCollar messages to collars and hands received status reports to a callback.

    Messages go as direct messages to each collar with pkiEncrypted=True, so only that collar can
    read them; relays in between see only the packet header. The collar only accepts them if
    this base node's public key is in the collar's admin keys.
    """

    def __init__(self, on_status: Callable[[str, dict], None], serial_port: str = "", host: str = ""):
        self.on_status = on_status
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
        if protocol.message_type(data) not in (protocol.MsgType.STATUS, protocol.MsgType.BREACH):
            return
        try:
            status = protocol.decode_status(data)
        except ValueError as e:
            log.warning("bad status from %s: %s", packet.get("fromId"), e)
            return
        node_id = packet.get("fromId") or f"!{packet['from']:08x}"
        record = asdict(status)
        record["msg_type"] = status.msg_type.name
        record["state"] = status.state.name
        self.on_status(node_id, record)

    def _send(self, node_id: str, data: bytes) -> None:
        if not self.iface:
            raise RuntimeError("not connected to a Meshtastic node")
        self.iface.sendData(
            data, destinationId=node_id, portNum=protocol.PORT_NUM, wantAck=True, pkiEncrypted=True
        )

    def send_fences(self, node_id: str, zones: list[protocol.Zone], fence_version: int,
                    pulses: protocol.Pulses | None = None) -> int:
        chunks = protocol.fence_chunks(zones, fence_version, pulses)
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(2)  # give the mesh room between chunks
            self._send(node_id, chunk)
        return len(chunks)

    def send_command(self, node_id: str, action: protocol.CommandAction, intensity: int = 0, duration_ms: int = 1000):
        self._send(node_id, protocol.encode_command(action, intensity, duration_ms))
