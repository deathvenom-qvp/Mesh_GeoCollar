// SPDX-License-Identifier: GPL-3.0-or-later
// Keeps one collar connected: finds it, proves to it that this phone is linked, asks for its
// status every few seconds, and carries the owner's commands. Reconnects by itself.

import { Action, AppSession, ErrorCode, PORT, Result } from './applink.js';
import { MeshRadio } from './radio.js';
import { randomBytes } from './bytes.js';

// What the link is doing, for the screen.
export const Link = {
  IDLE: 'idle',
  SEARCHING: 'searching',   // scanning for the collar's Bluetooth
  PAIRING: 'pairing',       // the phone is asking for the collar's Bluetooth PIN
  CONNECTING: 'connecting',
  ONLINE: 'online',
  WAITING: 'waiting',       // it failed; trying again shortly
  UNKNOWN: 'unknown',       // connected, but the collar does not know this phone's key
};

class LinkError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}

export class CollarLink {
  constructor(collar, transport, opts = {}) {
    this.collar = collar;
    this.t = transport;
    this.onState = opts.onState || (() => {});
    this.onStatus = opts.onStatus || (() => {});
    this.sleep = opts.sleep || (ms => new Promise(r => setTimeout(r, ms)));
    this.now = opts.now || Date.now;
    this.pollMs = opts.pollMs ?? 5000;
    this.replyMs = opts.replyMs ?? 6000;
    this.retryMs = opts.retryMs ?? 2000;
    this.session = new AppSession(collar.keyId, collar.key, opts.random || randomBytes);
    this.state = Link.IDLE;
    this.detail = '';
    this.stopped = true;
    this.inbox = [];
    this._wakers = new Set();
    this._dead = null;
    this._queue = Promise.resolve();
    this._running = null;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this._running = this._run();
  }

  async stop() {
    if (this.stopped) return;
    this.stopped = true;
    this._kill(new LinkError('stopped', 'stopped'));
    try { await this.t.close(); } catch { /* nothing open */ } // also ends a search in progress
    await this._running;
    this._set(Link.IDLE);
  }

  /// Ask the collar to do something. Resolves with its report afterwards.
  command(action, intensity = 0, durationMs = 0) {
    if (this.state !== Link.ONLINE) return Promise.reject(new LinkError('The collar is not connected.', 'offline'));
    return this._enqueue(() => this._request(action, intensity, durationMs));
  }

  _set(state, detail = '') {
    if (state === this.state && detail === this.detail) return;
    this.state = state;
    this.detail = detail;
    this.onState(state, detail);
  }

  _enqueue(job) {
    const run = this._queue.then(job, job);
    this._queue = run.catch(() => {});
    return run;
  }

  _kill(err) {
    if (!this._dead) this._dead = err;
    this._poke();
  }

  _poke() {
    const all = [...this._wakers];
    this._wakers.clear();
    for (const w of all) w();
  }

  /// Sleep that ends early when something arrives or the link dies.
  _nap(ms) {
    let waker;
    const woken = new Promise(r => { waker = r; this._wakers.add(r); });
    return Promise.race([this.sleep(ms), woken]).finally(() => this._wakers.delete(waker));
  }

  async _run() {
    let wait = this.retryMs;
    while (!this.stopped) {
      this._dead = null;
      this.inbox.length = 0;
      let unknown = false;
      try {
        await this._session(() => { wait = this.retryMs; });
      } catch (e) {
        if (this.stopped) break;
        unknown = e.code === 'unknown';
        if (unknown)
          this._set(Link.UNKNOWN);
        else
          this._set(Link.WAITING, e && e.message ? e.message : String(e));
      } finally {
        this.session.end();
        try { await this.t.close(); } catch { /* already gone */ }
      }
      if (this.stopped) break;
      // A collar that does not know this phone yet learns of it from the base over the mesh,
      // which takes a report or two: no need to hurry.
      await this._nap(unknown ? Math.max(wait, 5 * this.retryMs) : wait);
      wait = Math.min(Math.round(wait * 1.5), 15000);
    }
  }

  async _session(onOnline) {
    this._set(Link.SEARCHING);
    await this.t.open({
      onNotify: () => { if (this.radio) this.radio.drain().catch(e => this._kill(e)); },
      onDisconnect: () => this._kill(new LinkError('The Bluetooth connection dropped.', 'dropped')),
      onState: s => { if (!this.stopped) this._set(s); },
    });
    if (this._dead) throw this._dead;
    this._set(Link.CONNECTING);
    this.radio = new MeshRadio(this.t, { sleep: this.sleep, now: this.now });
    this.radio.onPacket = p => {
      // Only the collar's own answers: it hands those to the phone as from itself, to itself.
      if (p.portnum !== PORT || p.from !== this.collar.nodeNum || p.to !== this.collar.nodeNum) return;
      this.inbox.push(Uint8Array.from(p.payload));
      this._poke();
    };
    const me = await this.radio.start();
    if (me !== this.collar.nodeNum) {
      if (this.t.wrongDevice) this.t.wrongDevice();
      throw new LinkError('That was a different collar. Looking again.', 'wrong');
    }
    if (this.t.confirmed) this.t.confirmed();
    await this._enqueue(() => this._hello());
    this._set(Link.ONLINE);
    onOnline();
    while (!this.stopped) {
      await this._enqueue(() => this._request(Action.REQUEST_STATUS));
      await this._nap(this.pollMs);
      if (this._dead) throw this._dead;
    }
  }

  /// Wait for a message `check` accepts (it returns something other than undefined).
  async _await(check) {
    const end = this.now() + this.replyMs;
    for (;;) {
      while (this.inbox.length) {
        const r = await check(this.inbox.shift());
        if (r !== undefined) return r;
      }
      if (this._dead) throw this._dead;
      const left = end - this.now();
      if (left <= 0) throw new LinkError('The collar did not answer.', 'timeout');
      await this._nap(Math.min(left, 400));
      // In case a notification went missing, look anyway.
      if (!this.inbox.length && !this._dead) await this.radio.drain();
    }
  }

  async _hello() {
    const hello = this.session.hello();
    this.inbox.length = 0;
    await this.radio.send(this.collar.nodeNum, PORT, hello);
    await this._await(async buf => {
      if (await this.session.challenge(buf)) return true;
      if (this.session.error(buf) === ErrorCode.UNKNOWN_KEY)
        throw new LinkError('The collar does not know this phone.', 'unknown');
      return undefined;
    });
  }

  async _request(action, intensity = 0, durationMs = 0, again = true) {
    if (this._dead) throw this._dead;
    if (!this.session.ready) throw new LinkError('The collar is not connected.', 'offline');
    const { counter, bytes } = await this.session.request(action, intensity, durationMs);
    this.inbox.length = 0;
    try {
      await this.radio.send(this.collar.nodeNum, PORT, bytes);
      const st = await this._await(async buf => {
        const s = await this.session.status(buf);
        if (s && s.counter === counter) return s;
        const code = this.session.error(buf);
        if (code === ErrorCode.UNKNOWN_KEY) throw new LinkError('The collar does not know this phone.', 'unknown');
        if (code === ErrorCode.NO_SESSION) throw new LinkError('The collar forgot this session.', 'nosession');
        return undefined;
      });
      st.at = this.now();
      this.onStatus(st);
      if (st.result === Result.NOT_ALLOWED) throw new LinkError('The collar does not let phones do that.', 'notallowed');
      return st;
    } catch (e) {
      // The collar restarted, or another phone took its one session: say hello again, once.
      if (e.code === 'nosession' && again) {
        await this._hello();
        return this._request(action, intensity, durationMs, false);
      }
      if (e.code !== 'notallowed') this._kill(e);
      throw e;
    }
  }
}
