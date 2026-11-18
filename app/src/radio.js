// SPDX-License-Identifier: GPL-3.0-or-later
// Talks Meshtastic's phone API to one node over a transport (Bluetooth, the demo collar, or a
// test's pretend radio). A transport is { open(), write(bytes), read() -> bytes, close() }:
// write goes to the node's ToRadio characteristic, read takes the next message from FromRadio
// (empty when there is none), and the node says "there is more" through its FromNum notification.

import {
  CONFIG_NONCE, decodeFromRadio, describeFromRadio, encodeHeartbeat, encodeToRadioPacket, encodeWantConfig,
} from './meshtastic.js';
import { randomBytes } from './bytes.js';
import { log } from './log.js';

const random32 = () => new DataView(randomBytes(4).buffer).getUint32(0, true) >>> 0;

export class MeshRadio {
  constructor(transport, { sleep, now = Date.now } = {}) {
    this.t = transport;
    this.sleep = sleep || (ms => new Promise(r => setTimeout(r, ms)));
    this.now = now;
    this.onPacket = null;
    this.myNodeNum = null;
    this.configDone = false;
    this._draining = null;
    this._again = false;
    this.reads = 0;
  }

  /// Ask the node who it is and wait for the answer. Returns its node number.
  async start(timeoutMs = 20000) {
    // The collar ignores a write that is the same as the one before it on the same Bluetooth link,
    // and the link outlives this app's connection when another app or the phone itself holds it.
    // A heartbeat that is different every time goes first, so "want config" is never a repeat.
    const nonce = (random32() | 2) >>> 0; // never 0 (no nonce) or 1 (a node info broadcast)
    log('mesh', `heartbeat ${nonce}, then want_config ${CONFIG_NONCE}`);
    await this.t.write(encodeHeartbeat(nonce));
    await this.t.write(encodeWantConfig());
    const end = this.now() + timeoutMs;
    for (;;) {
      const n = await this.drain();
      if (this.configDone) break;
      if (this.now() > end) {
        log('mesh', `no config_complete within ${timeoutMs} ms`
          + ` (${this.myNodeNum === null ? 'no my_info either' : 'my_info came'}, ${this.reads} messages read)`);
        throw new Error('The collar did not answer.');
      }
      // The node fills its answer from another thread: an empty read can just mean "not yet".
      if (!n) await this.sleep(150);
    }
    if (this.myNodeNum === null) throw new Error('The collar did not say who it is.');
    return this.myNodeNum;
  }

  async send(to, portnum, payload) {
    const id = (random32() | 1) >>> 0; // never 0
    log('mesh', `send port ${portnum} to ${to.toString(16).padStart(8, '0')}, ${payload.length} bytes, id ${id.toString(16)}`);
    await this.t.write(encodeToRadioPacket({ to, id, portnum, payload }));
    return id;
  }

  /// Read everything the node has waiting. One reader at a time: a call during a drain makes
  /// that drain go round again instead of starting a second one.
  drain() {
    if (this._draining) { this._again = true; return this._draining; }
    this._draining = (async () => {
      let count = 0;
      try {
        do {
          this._again = false;
          for (;;) {
            const buf = await this.t.read();
            if (!buf || !buf.length) break;
            count++;
            this._handle(buf);
          }
        } while (this._again);
      } finally {
        this._draining = null;
      }
      return count;
    })();
    return this._draining;
  }

  _handle(buf) {
    this.reads++;
    log('mesh', 'from radio:', describeFromRadio(buf));
    let m;
    try { m = decodeFromRadio(buf); } catch { return; } // not ours to understand
    if (m.myNodeNum !== undefined) this.myNodeNum = m.myNodeNum;
    else if (m.configComplete === CONFIG_NONCE) this.configDone = true;
    else if (m.packet && this.onPacket) this.onPacket(m.packet);
  }
}
