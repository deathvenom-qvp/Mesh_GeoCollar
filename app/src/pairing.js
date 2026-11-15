// SPDX-License-Identifier: GPL-3.0-or-later
// The pairing code the base station shows as a QR code under "Connect app":
//
//   geocollar://pair?v=1&n=<node, 8 hex>&i=<key id, 8 hex>&k=<key, base64url>
//       [&p=<Bluetooth PIN>][&b=<start of the collar's Bluetooth name>][&d=<dog's name>][&s=1]
//
// It must match pairing_code() in base/geocollar_base/protocol.py.

import { fromBase64Url, toBase64Url } from './bytes.js';

export const KEY_LEN = 16;

export function parsePairingCode(text) {
  const code = String(text || '').trim();
  const m = /^geocollar:\/\/pair\/?\?(.*)$/i.exec(code);
  if (!m) throw new Error('This is not a GeoCollar pairing code.');
  const q = new URLSearchParams(m[1]);
  if (q.get('v') !== '1') throw new Error('This pairing code is from a newer GeoCollar. Update the app.');
  const node = (q.get('n') || '').toLowerCase();
  const id = (q.get('i') || '').toLowerCase();
  if (!/^[0-9a-f]{8}$/.test(node) || !/^[0-9a-f]{8}$/.test(id) || id === '00000000')
    throw new Error('The pairing code is damaged. Show a new one on the base station.');
  let key;
  try { key = fromBase64Url(q.get('k') || ''); } catch { key = new Uint8Array(0); }
  if (key.length !== KEY_LEN) throw new Error('The pairing code is damaged. Show a new one on the base station.');
  const pin = q.get('p') || '';
  if (pin && !/^\d{6}$/.test(pin)) throw new Error('The pairing code is damaged. Show a new one on the base station.');
  return {
    node,                               // the collar's Meshtastic node number, 8 hex digits
    nodeNum: parseInt(node, 16),
    keyId: parseInt(id, 16),
    key,
    pin,                                // the collar's Bluetooth PIN, '' if the base does not know it
    bleName: (q.get('b') || '').slice(0, 8),
    dog: (q.get('d') || '').slice(0, 40),
    sim: q.get('s') === '1',            // a simulated collar: nothing to connect to
  };
}

/// The reverse, for tests and for the demo collar.
export function pairingCode({ node, keyId, key, pin = '', bleName = '', dog = '', sim = false }) {
  const parts = ['v=1', `n=${node}`, `i=${(keyId >>> 0).toString(16).padStart(8, '0')}`, `k=${toBase64Url(key)}`];
  if (pin) parts.push(`p=${pin}`);
  if (bleName) parts.push(`b=${encodeURIComponent(bleName)}`);
  if (dog) parts.push(`d=${encodeURIComponent(dog)}`);
  if (sim) parts.push('s=1');
  return 'geocollar://pair?' + parts.join('&');
}
