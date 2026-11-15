// SPDX-License-Identifier: GPL-3.0-or-later
// The app's half of the link to the collar. Messages travel inside Meshtastic packets on port
// 300, over the collar's own Bluetooth service. Being paired over Bluetooth is not enough to
// command a collar: every request is signed with the key from the base station's QR code.
//
// The collar's half is firmware/GeoCollar/AppLink.cpp; docs/protocol.md has the layouts. Both
// sides, and the base's Python, are tested against the same bytes.

import { concat, sameBytes, u32le } from './bytes.js';

export const PORT = 300;
const VERSION = 2;
const NONCE_LEN = 8;
const TAG_LEN = 8;

export const Msg = { HELLO: 16, CHALLENGE: 17, REQUEST: 18, STATUS: 19, ERROR: 20 };
export const Action = { VIBRATE: 2, BEEP: 3, CORRECTIONS_OFF: 10, CORRECTIONS_ON: 11, REQUEST_STATUS: 14, SILENCE_LOST: 15 };
export const Result = { DONE: 0, NOT_ALLOWED: 1 };
export const ErrorCode = { UNKNOWN_KEY: 1, NO_SESSION: 2 };
export const State = { NO_FENCE: 0, INSIDE: 1, WARNING: 2, OUTSIDE: 3, LOST: 4 };
export const NO_FIX_AGE = 0xffff;

const CHALLENGE_LEN = 2 + 4 + NONCE_LEN + TAG_LEN;
const STATUS_LEN = 2 + 4 + 4 + 1 + 18 + 2 + TAG_LEN;
const ERROR_LEN = 2 + 4 + 1;

export async function hmac(key, message) {
  const subtle = globalThis.crypto.subtle;
  const k = await subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await subtle.sign('HMAC', k, message));
}

const head = (type, keyId) => concat(Uint8Array.of(VERSION, type), u32le(keyId));

/// Which app message this is, or 0 if it is not one for this key.
export function messageType(buf, keyId) {
  if (!buf || buf.length < 6 || buf[0] !== VERSION) return 0;
  if (new DataView(buf.buffer, buf.byteOffset, buf.byteLength).getUint32(2, true) !== keyId >>> 0) return 0;
  const t = buf[1];
  return t >= Msg.HELLO && t <= Msg.ERROR ? t : 0;
}

/// The 18 bytes of a collar report, as the base station gets them too.
export function decodeStatusBody(b) {
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const flags = v.getUint8(17);
  return {
    fenceVersion: v.getUint16(0, true),
    state: v.getUint8(2),
    batteryPct: v.getUint8(3) === 255 ? null : v.getUint8(3),
    lat: v.getInt32(4, true) / 1e7,
    lon: v.getInt32(8, true) / 1e7,
    pdop: v.getUint16(12, true) / 100,
    sats: v.getUint8(14),
    clearanceM: v.getInt16(15, true),
    corrections: !!(flags & 0x01),
    shock: !!(flags & 0x02),
    beacon: !!(flags & 0x04),         // the lost beacon is on
    beaconSilenced: !!(flags & 0x08),
    noFix: !!(flags & 0x10),          // the position is the last one known, not a current one
    apps: !!(flags & 0x40),
  };
}

/// One session with a collar. Start with hello(), give the collar's answer to challenge(), then
/// sign requests with request() and check the answers with status().
export class AppSession {
  constructor(keyId, key, random) {
    this.keyId = keyId >>> 0;
    this.key = key;
    this.random = random;
    this.sessionKey = null;
    this.counter = 0;
    this.appNonce = null;
  }

  get ready() { return !!this.sessionKey; }

  hello() {
    this.sessionKey = null;
    this.counter = 0;
    this.appNonce = this.random(NONCE_LEN);
    return concat(head(Msg.HELLO, this.keyId), this.appNonce);
  }

  /// The collar's challenge. True if the collar proved it holds our key; the session is then open.
  async challenge(buf) {
    if (!this.appNonce || buf.length !== CHALLENGE_LEN || messageType(buf, this.keyId) !== Msg.CHALLENGE) return false;
    const collarNonce = buf.subarray(6, 6 + NONCE_LEN);
    const tail = concat(u32le(this.keyId), this.appNonce, collarNonce);
    const tag = (await hmac(this.key, concat(new TextEncoder().encode('GCA1'), tail))).subarray(0, TAG_LEN);
    if (!sameBytes(tag, buf.subarray(6 + NONCE_LEN))) return false;
    this.sessionKey = await hmac(this.key, concat(new TextEncoder().encode('GCS1'), tail));
    this.counter = 0;
    return true;
  }

  /// A signed request. Returns { counter, bytes }.
  async request(action, intensity = 0, durationMs = 0) {
    if (!this.sessionKey) throw new Error('no session');
    const counter = ++this.counter;
    const body = concat(head(Msg.REQUEST, this.keyId), u32le(counter),
      Uint8Array.of(action, intensity, durationMs & 0xff, (durationMs >> 8) & 0xff));
    const tag = (await hmac(this.sessionKey, body)).subarray(0, TAG_LEN);
    return { counter, bytes: concat(body, tag) };
  }

  /// The collar's signed answer, or null if it is not one or its signature is wrong.
  async status(buf) {
    if (!this.sessionKey || buf.length !== STATUS_LEN || messageType(buf, this.keyId) !== Msg.STATUS) return null;
    const tag = (await hmac(this.sessionKey, buf.subarray(0, STATUS_LEN - TAG_LEN))).subarray(0, TAG_LEN);
    if (!sameBytes(tag, buf.subarray(STATUS_LEN - TAG_LEN))) return null;
    const v = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const age = v.getUint16(29, true);
    return {
      counter: v.getUint32(6, true),
      result: v.getUint8(10),
      ...decodeStatusBody(buf.subarray(11, 29)),
      fixAgeS: age === NO_FIX_AGE ? null : age,
    };
  }

  /// The code in an AppError, or 0. An error is not signed, so it only ever ends a session: the
  /// worst a forger can do is make the app say hello again.
  error(buf) {
    if (buf.length !== ERROR_LEN || messageType(buf, this.keyId) !== Msg.ERROR) return 0;
    return buf[6];
  }

  end() { this.sessionKey = null; }
}
