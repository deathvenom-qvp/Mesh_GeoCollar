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
