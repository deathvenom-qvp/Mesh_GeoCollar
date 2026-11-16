// SPDX-License-Identifier: GPL-3.0-or-later
// Just enough of Meshtastic's phone API to carry GeoCollar messages: the few protobuf fields the
// app needs, written and read by hand so the app does not ship Meshtastic's whole schema.
// Field numbers are from meshtastic/mesh.proto (Meshtastic 2.8). The tests check these bytes
// against ones made with Meshtastic's own Python library.

import { concat } from './bytes.js';

// The Bluetooth service every Meshtastic node offers to a phone.
export const SERVICE = '6ba1b218-15a8-461f-9fa8-5dcae273eafd';
export const TO_RADIO = 'f75c76d2-129e-4dad-a1dd-7866124401e7';
export const FROM_RADIO = '2c55e69e-4993-11ed-b878-0242ac120002';
export const FROM_NUM = 'ed9da18c-a800-4f66-a670-aa7547e34453';

/// "Send me my own node's details only": skips the list of every other node on the mesh.
export const CONFIG_NONCE = 69420;

function varint(v) {
  const out = [];
  let n = BigInt.asUintN(64, BigInt(v));
  do {
    let b = Number(n & 0x7fn);
    n >>= 7n;
    if (n) b |= 0x80;
    out.push(b);
  } while (n);
  return Uint8Array.from(out);
}

const tag = (field, wire) => varint((field << 3) | wire);
const fVarint = (field, v) => concat(tag(field, 0), varint(v));
const fBytes = (field, bytes) => concat(tag(field, 2), varint(bytes.length), bytes);
function fFixed32(field, v) {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, v >>> 0, true);
  return concat(tag(field, 5), out);
}

/// Every field of a protobuf message as { field, wire, value }: a number for varints (as a
/// Number, which is enough for the fields used here) and fixed32, bytes for the rest.
export function fields(buf) {
  const out = [];
  let at = 0;
  const readVarint = () => {
    let v = 0n, shift = 0n;
    for (;;) {
      if (at >= buf.length || shift > 63n) throw new Error('bad protobuf');
      const b = buf[at++];
      v |= BigInt(b & 0x7f) << shift;
      if (!(b & 0x80)) return Number(BigInt.asUintN(64, v));
      shift += 7n;
    }
  };
  while (at < buf.length) {
    const key = readVarint();
    const field = Math.floor(key / 8), wire = key % 8;
    let value;
    if (wire === 0) value = readVarint();
    else if (wire === 1) { value = buf.subarray(at, at + 8); at += 8; }
    else if (wire === 2) { const n = readVarint(); value = buf.subarray(at, at + n); at += n; }
    else if (wire === 5) {
      if (at + 4 > buf.length) throw new Error('bad protobuf');
      value = new DataView(buf.buffer, buf.byteOffset + at, 4).getUint32(0, true); at += 4;
    } else throw new Error('bad protobuf');
    if (at > buf.length) throw new Error('bad protobuf');
    out.push({ field, wire, value });
  }
  return out;
}

const pick = (list, field, wire) => { const f = list.find(x => x.field === field && x.wire === wire); return f ? f.value : undefined; };

export const encodeWantConfig = (nonce = CONFIG_NONCE) => fVarint(3, nonce);     // ToRadio.want_config_id
export const encodeHeartbeat = () => fBytes(7, new Uint8Array(0));                // ToRadio.heartbeat

/// ToRadio.packet: a GeoCollar message for node `to`, which is the node we are connected to.
export function encodeToRadioPacket({ to, id, portnum, payload }) {
  const data = concat(fVarint(1, portnum), fBytes(2, payload));                   // Data
  const packet = concat(fFixed32(2, to), fBytes(4, data), fFixed32(6, id));       // MeshPacket
  return fBytes(1, packet);
}

/// What a FromRadio message holds, as far as the app cares:
///   { configComplete: nonce } | { myNodeNum } | { packet: { from, to, id, portnum, payload } } | {}
export function decodeFromRadio(buf) {
  const top = fields(buf);
  const done = pick(top, 7, 0);
  if (done !== undefined) return { configComplete: done };
  const info = pick(top, 3, 2);
  if (info) return { myNodeNum: pick(fields(info), 1, 0) >>> 0 };
  const packet = pick(top, 2, 2);
  if (packet) {
    const p = fields(packet);
    const decoded = pick(p, 4, 2);
    if (!decoded) return {};          // still encrypted: not for us
    const d = fields(decoded);
    return {
      packet: {
        from: (pick(p, 1, 5) || 0) >>> 0,
        to: (pick(p, 2, 5) || 0) >>> 0,
        id: (pick(p, 6, 5) || 0) >>> 0,
        portnum: pick(d, 1, 0) || 0,
        payload: pick(d, 2, 2) || new Uint8Array(0),
      },
    };
  }
  return {};
}

// The other direction, for the demo collar and the tests' pretend radio.
export function decodeToRadio(buf) {
  const top = fields(buf);
  const want = pick(top, 3, 0);
  if (want !== undefined) return { wantConfig: want };
  const packet = pick(top, 1, 2);
  if (packet) {
    const p = fields(packet);
    const d = fields(pick(p, 4, 2) || new Uint8Array(0));
    return { packet: { to: (pick(p, 2, 5) || 0) >>> 0, id: (pick(p, 6, 5) || 0) >>> 0, portnum: pick(d, 1, 0) || 0, payload: pick(d, 2, 2) || new Uint8Array(0) } };
  }
  if (pick(top, 7, 2)) return { heartbeat: true };
  return {};
}

export const encodeFromRadioMyInfo = nodeNum => fBytes(3, fVarint(1, nodeNum));
export const encodeFromRadioConfigComplete = nonce => fVarint(7, nonce);
export function encodeFromRadioPacket({ from, to, id, portnum, payload }) {
  const data = concat(fVarint(1, portnum), fBytes(2, payload));
  return fBytes(2, concat(fFixed32(1, from), fFixed32(2, to), fBytes(4, data), fFixed32(6, id)));
}
