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
  constructor(nodeNum, handle) {
    this.nodeNum = nodeNum >>> 0;
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
        await this.toRadio(bytes);
      },
      read: async () => {
        if (!this.isOpen) throw new Error('not connected');
        return this.queue.shift() || new Uint8Array(0);
      },
      close: async () => { this.isOpen = false; this.notify = null; },
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
