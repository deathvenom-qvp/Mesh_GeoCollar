// SPDX-License-Identifier: GPL-3.0-or-later
// The pairing code the base station shows as a QR code under "Connect app".
//
// Version 2, for every collar on the base:
//
//   geocollar://pair?v=2&h=<base, 8 hex>[&i=<key id, 8 hex>&k=<phone key, base64url>]
//       then for each collar: n=<node, 8 hex>[&p=<Bluetooth PIN>][&b=<start of its Bluetooth name>][&d=<dog>][&s=1]
//
// With a key it links this phone to the base. Without one it is the base's list of collars, for a
// phone that is linked already: it adds the collars added since. Each collar's own key is made
// from the phone key (collarKey).
//
// Version 1, from bases before this, for one collar, with that collar's own key:
//
//   geocollar://pair?v=1&n=<node, 8 hex>&i=<key id, 8 hex>&k=<key, base64url>
//       [&p=<Bluetooth PIN>][&b=<start of the collar's Bluetooth name>][&d=<dog's name>][&s=1]
//
// It must match pairing_code() and home_pairing_code() in base/geocollar_base/protocol.py.

import { hmac } from './applink.js';
import { concat, fromBase64Url, toBase64Url, u32le } from './bytes.js';

export const KEY_LEN = 16;

const DAMAGED = 'The pairing code is damaged. Show a new one on the base station.';
const hex8 = v => (v >>> 0).toString(16).padStart(8, '0');

function readKey(text) {
  let key;
  try { key = fromBase64Url(text || ''); } catch { key = new Uint8Array(0); }
  if (key.length !== KEY_LEN) throw new Error(DAMAGED);
  return key;
}

function readCollar(fields) {
  const node = (fields.n || '').toLowerCase();
  if (!/^[0-9a-f]{8}$/.test(node)) throw new Error(DAMAGED);
  const pin = fields.p || '';
  if (pin && !/^\d{6}$/.test(pin)) throw new Error(DAMAGED);
  return {
    node,                               // the collar's Meshtastic node number, 8 hex digits
    nodeNum: parseInt(node, 16),
    pin,                                // the collar's Bluetooth PIN, '' if the base does not know it
    bleName: (fields.b || '').slice(0, 8),
    dog: (fields.d || '').slice(0, 40),
    sim: fields.s === '1',              // a simulated collar: nothing to connect to
  };
}

/// What a code says: { home, keyId, key, collars }. `home` is '' for a version 1 code, whose key
/// is the one collar's own; in a version 2 code `key` is the phone's key for the whole base, or
/// null in a collar list.
export function parsePairingCode(text) {
  const code = String(text || '').trim();
  const m = /^geocollar:\/\/pair\/?\?(.*)$/i.exec(code);
  if (!m) throw new Error('This is not a GeoCollar pairing code.');
  const pairs = [...new URLSearchParams(m[1])];
  const q = new URLSearchParams(m[1]);
  const v = q.get('v');
  if (v === '1') {
    const id = (q.get('i') || '').toLowerCase();
    if (!/^[0-9a-f]{8}$/.test(id) || id === '00000000') throw new Error(DAMAGED);
    return { home: '', keyId: parseInt(id, 16), key: readKey(q.get('k')), collars: [readCollar(Object.fromEntries(q))] };
  }
  if (v !== '2') throw new Error('This pairing code is from a newer GeoCollar. Update the app.');
  const home = (q.get('h') || '').toLowerCase();
  if (!/^[0-9a-f]{8}$/.test(home)) throw new Error(DAMAGED);
  let keyId = 0, key = null;
  if (q.has('k')) {
    const id = (q.get('i') || '').toLowerCase();
    if (!/^[0-9a-f]{8}$/.test(id) || id === '00000000') throw new Error(DAMAGED);
    keyId = parseInt(id, 16);
    key = readKey(q.get('k'));
  }
  // A collar's fields follow its n=, in order.
  const groups = [];
  for (const [k, val] of pairs) {
    if (k === 'n') groups.push({ n: val });
    else if (groups.length && ['p', 'b', 'd', 's'].includes(k)) groups[groups.length - 1][k] = val;
  }
  if (!groups.length) throw new Error('The code lists no collars. Add a collar on the base station first.');
  const collars = groups.map(readCollar);
  if (new Set(collars.map(c => c.node)).size !== collars.length) throw new Error(DAMAGED);
  return { home, keyId, key, collars };
}

/// A collar's key for a phone linked to the whole base: HMAC-SHA256(phone key, "GCK1" + node
/// number), first 16 bytes. The base works out the same one and sends it to the collar.
export async function collarKey(phoneKey, nodeNum) {
  const mac = await hmac(phoneKey, concat(new TextEncoder().encode('GCK1'), u32le(nodeNum)));
  return mac.slice(0, KEY_LEN);
}

function collarParts(c) {
  const parts = [`n=${c.node}`];
  if (c.pin) parts.push(`p=${c.pin}`);
  if (c.bleName) parts.push(`b=${encodeURIComponent(c.bleName)}`);
  if (c.dog) parts.push(`d=${encodeURIComponent(c.dog)}`);
  if (c.sim) parts.push('s=1');
  return parts;
}

/// The reverse, for tests and for the demo collar.
export function pairingCode({ home = '', keyId = 0, key = null, collars }) {
  if (!home) {
    const [c] = collars;
    const [n, ...rest] = collarParts(c);
    return 'geocollar://pair?' + ['v=1', n, `i=${hex8(keyId)}`, `k=${toBase64Url(key)}`, ...rest].join('&');
  }
  const parts = ['v=2', `h=${home}`];
  if (key) parts.push(`i=${hex8(keyId)}`, `k=${toBase64Url(key)}`);
  for (const c of collars) parts.push(...collarParts(c));
  return 'geocollar://pair?' + parts.join('&');
}
