// SPDX-License-Identifier: GPL-3.0-or-later
// A pretend Meshtastic node behind the transport interface: it answers the phone API's "who are
// you" and hands GeoCollar messages to a collar half. The demo collar runs on it, and so do the
// tests, with the firmware's real C++ collar half behind it.

import { PORT } from './applink.js';
import {
  decodeToRadio, encodeFromRadioConfigComplete, encodeFromRadioMyInfo, encodeFromRadioPacket,
} from './meshtastic.js';

export class FakeNode {
  /// handle(payload) -> reply bytes or null (may be async): the collar's GeoCollar module.
  /// `sharedLink`: the Bluetooth link stays up when the app closes its connection, as it does
  /// while the Meshtastic app or the phone itself holds the collar.
  constructor(nodeNum, handle, { sharedLink = false } = {}) {
    this.nodeNum = nodeNum >>> 0;
    this.sharedLink = sharedLink;
    // Like the firmware (NimbleBluetooth's lastToRadio): a write whose bytes match the start of
    // this buffer is dropped, and every write taken is copied over its start.
    this.lastWrite = new Uint8Array(512);
    this.handle = handle;
    this.queue = [];
    this.notify = null;
    this.isOpen = false;
    this.nextId = 1;
  }

  transport() {
    return {
      open: async ({ onNotify }) => { this.queue.length = 0; this.notify = onNotify; this.isOpen = true; },
      write: async bytes => {
        if (!this.isOpen) throw new Error('not connected');
        if (bytes.every((b, i) => b === this.lastWrite[i])) return; // "Drop duplicate ToRadio packet"
        this.lastWrite.set(bytes);
        await this.toRadio(bytes);
      },
      read: async () => {
        if (!this.isOpen) throw new Error('not connected');
        return this.queue.shift() || new Uint8Array(0);
      },
      close: async () => {
        this.isOpen = false;
        this.notify = null;
        if (!this.sharedLink) this.lastWrite.fill(0); // the link went down with it
      },
    };
  }

  push(bytes) {
    this.queue.push(bytes);
    if (this.notify) this.notify();
  }

  async toRadio(bytes) {
    const m = decodeToRadio(bytes);
    if (m.wantConfig !== undefined) {
      this.push(encodeFromRadioMyInfo(this.nodeNum));
      this.push(encodeFromRadioConfigComplete(m.wantConfig));
    } else if (m.packet && m.packet.to === this.nodeNum && m.packet.portnum === PORT) {
      const reply = await this.handle(Uint8Array.from(m.packet.payload));
      if (reply && reply.length)
        this.push(encodeFromRadioPacket({ from: this.nodeNum, to: this.nodeNum, id: this.nextId++, portnum: PORT, payload: reply }));
    }
  }
}
