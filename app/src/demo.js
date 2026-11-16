// SPDX-License-Identifier: GPL-3.0-or-later
// A pretend collar, so the app can be tried with no hardware: a lost dog wandering a few hundred
// metres away. It speaks the same signed messages as a real collar (this is the collar's half of
// applink.js, as firmware/GeoCollar/AppLink.cpp does it), so the rest of the app cannot tell.

import { Action, ErrorCode, Msg, Result, State, hmac } from './applink.js';
import { concat, sameBytes, u32le } from './bytes.js';
import { offset } from './geo.js';

const enc = new TextEncoder();
const ALLOWED = new Set(Object.values(Action));

export const DEMO_NODE = 'de300001';
export const DEMO_KEY_ID = 0x0de30001;
export const demoKey = () => Uint8Array.from({ length: 16 }, (_, i) => 0xd0 + i);

/// The collar's half of a session. `collar` supplies act(action, intensity, durationMs) and
/// report() -> { state, batteryPct, lat, lon, ..., fixAgeS }.
export class CollarHalf {
  constructor(keys, collar, random) {
    this.keys = keys;         // Map of key id -> key
    this.collar = collar;
    this.random = random;
    this.session = null;
  }

  async handle(buf) {
    if (buf.length < 6 || buf[0] !== 2) return null;
    const v = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const keyId = v.getUint32(2, true);
    const key = this.keys.get(keyId);
    const error = code => concat(Uint8Array.of(2, Msg.ERROR), u32le(keyId), Uint8Array.of(code));

    if (buf[1] === Msg.HELLO && buf.length === 14) {
      if (!key) return error(ErrorCode.UNKNOWN_KEY);
      const appNonce = buf.subarray(6, 14);
      const collarNonce = this.random(8);
      const tail = concat(u32le(keyId), appNonce, collarNonce);
      const tag = (await hmac(key, concat(enc.encode('GCA1'), tail))).subarray(0, 8);
      this.session = { keyId, key: await hmac(key, concat(enc.encode('GCS1'), tail)), last: 0 };
      return concat(Uint8Array.of(2, Msg.CHALLENGE), u32le(keyId), collarNonce, tag);
    }

    if (buf[1] === Msg.REQUEST && buf.length === 22) {
      const why = key ? ErrorCode.NO_SESSION : ErrorCode.UNKNOWN_KEY;
      const s = this.session;
      if (!s || s.keyId !== keyId) return error(why);
      const tag = (await hmac(s.key, buf.subarray(0, 14))).subarray(0, 8);
      const counter = v.getUint32(6, true);
      if (!sameBytes(tag, buf.subarray(14)) || counter <= s.last) return error(why);
      s.last = counter;
      const action = buf[10];
      let result = Result.DONE;
      if (!ALLOWED.has(action)) result = Result.NOT_ALLOWED;
      else if (action !== Action.REQUEST_STATUS) this.collar.act(action, buf[11], v.getUint16(12, true));
      const body = concat(Uint8Array.of(2, Msg.STATUS), u32le(keyId), u32le(counter), Uint8Array.of(result),
        encodeStatusBody(this.collar.report()));
      return concat(body, (await hmac(s.key, body)).subarray(0, 8));
    }
    return null;
  }
}

/// The 18 bytes of a report and the 2 of its fix age.
export function encodeStatusBody(r) {
  const out = new Uint8Array(20);
  const v = new DataView(out.buffer);
  v.setUint16(0, r.fenceVersion || 0, true);
  v.setUint8(2, r.state);
  v.setUint8(3, r.batteryPct ?? 255);
  v.setInt32(4, Math.round(r.lat * 1e7), true);
  v.setInt32(8, Math.round(r.lon * 1e7), true);
  v.setUint16(12, Math.round((r.pdop || 0) * 100), true);
  v.setUint8(14, r.sats || 0);
  v.setInt16(15, r.clearanceM || 0, true);
  v.setUint8(17, (r.corrections ? 1 : 0) | (r.shock ? 2 : 0) | (r.beacon ? 4 : 0) | (r.beaconSilenced ? 8 : 0) |
    (r.noFix ? 0x10 : 0) | 0x20 | 0x40);
  v.setUint16(18, r.fixAgeS ?? 0xffff, true);
  return out;
}

/// The pretend dog: lost, about 250 m from `home`, drifting around slowly.
export class DemoDog {
  constructor(home, now = Date.now) {
    this.home = home;       // () => { lat, lon }: where to be lost near (the phone, once it is known)
    this.now = now;
    this.started = now();
    this.silenced = false;
    this.corrections = true;
    this.lastAction = null; // { action, at }, for the screen to say "the collar beeped"
  }

  act(action) {
    if (action === Action.SILENCE_LOST) this.silenced = true;
    else if (action === Action.CORRECTIONS_OFF) this.corrections = false;
    else if (action === Action.CORRECTIONS_ON) this.corrections = true;
    this.lastAction = { action, at: this.now() };
  }

  report() {
    const t = (this.now() - this.started) / 1000;
    const h = this.home();
    // A slow figure of eight around a point 250 m to the north-east.
    const p = offset(h.lat, h.lon, 180 + 60 * Math.sin(t / 40), 170 + 45 * Math.sin(t / 20));
    return {
      fenceVersion: 3, state: State.LOST, batteryPct: Math.max(5, 76 - Math.floor(t / 600)),
      lat: p.lat, lon: p.lon, pdop: 1.6, sats: 9, clearanceM: -Math.round(120 + 30 * Math.sin(t / 40)),
      corrections: this.corrections, shock: false, beacon: !this.silenced, beaconSilenced: this.silenced,
      noFix: false, fixAgeS: Math.floor(t) % 15,
    };
  }
}
