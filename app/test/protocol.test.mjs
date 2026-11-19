// SPDX-License-Identifier: GPL-3.0-or-later
// The app's codecs against the same bytes the collar's C++ tests (firmware/test/test_main.cpp)
// and the base's Python tests (base/tests/test_protocol.py) check.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Action, AppSession, ErrorCode, Msg, Result, State, messageType } from '../src/applink.js';
import { hex, unhex } from '../src/bytes.js';
import * as mesh from '../src/meshtastic.js';
import { collarKey, pairingCode, parsePairingCode } from '../src/pairing.js';

const KEY_ID = 0x1a2b3c4d;
const KEY = unhex('000102030405060708090a0b0c0d0e0f');
const APP_NONCE = unhex('a0a1a2a3a4a5a6a7');
const HELLO = '02104d3c2b1aa0a1a2a3a4a5a6a7';
const CHALLENGE = '02114d3c2b1ac0c1c2c3c4c5c6c7c66cfaa9f4fe36cb';
const SESSION_KEY = '4b4ad99bb6e9aa4a581e9cc9defdb082abb994605cae8f691fde30c639488fce';
const REQUEST = '02124d3c2b1a010000000f000000d2b8917361311478';
const STATUS = '02134d3c2b1a010000000003000457d288d7179fd74bd3b40009f4ff012a0017682af07f6aecce';

const session = () => new AppSession(KEY_ID, KEY, () => APP_NONCE);

test('a session matches the collar byte for byte', async () => {
  const s = session();
  assert.equal(hex(s.hello()), HELLO);
  assert.equal(s.ready, false);
  assert.equal(await s.challenge(unhex(CHALLENGE)), true);
  assert.equal(hex(s.sessionKey), SESSION_KEY);
  const r = await s.request(Action.SILENCE_LOST);
  assert.equal(r.counter, 1);
  assert.equal(hex(r.bytes), REQUEST);
  const st = await s.status(unhex(STATUS));
  assert.deepEqual(st, {
    counter: 1, result: Result.DONE, fenceVersion: 3, state: State.LOST, batteryPct: 87,
    lat: 40.0001234, lon: -75.0004321, pdop: 1.8, sats: 9, clearanceM: -12,
    corrections: true, shock: false, beacon: false, beaconSilenced: false, noFix: false, apps: false,
    fixAgeS: 42,
  });
  assert.equal((await s.request(Action.BEEP, 0, 1000)).counter, 2);
});

test('a challenge from something without the key is refused', async () => {
  for (const at of [1, 3, 8, 15, 21]) {
    const bad = unhex(CHALLENGE);
    bad[at] ^= 1;
    const s = session();
    s.hello();
    assert.equal(await s.challenge(bad), false, `byte ${at}`);
    assert.equal(s.ready, false);
  }
  const s = session();
  assert.equal(await s.challenge(unhex(CHALLENGE)), false, 'no hello yet');
  s.hello();
  assert.equal(await s.challenge(unhex(CHALLENGE).subarray(0, 21)), false, 'short');
  await assert.rejects(s.request(Action.BEEP), /no session/);
});

test('a status with a wrong signature, or before a session, is refused', async () => {
  const s = session();
  s.hello();
  assert.equal(await s.status(unhex(STATUS)), null, 'no session yet');
  await s.challenge(unhex(CHALLENGE));
  for (const at of [0, 6, 10, 12, 20, 29, 31, 38]) {
    const bad = unhex(STATUS);
    bad[at] ^= 1;
    assert.equal(await s.status(bad), null, `byte ${at}`);
  }
  assert.ok(await s.status(unhex(STATUS)));
  s.end();
  assert.equal(await s.status(unhex(STATUS)), null);
});

test('errors and message types', () => {
  const s = session();
  assert.equal(s.error(unhex('02144d3c2b1a02')), ErrorCode.NO_SESSION);
  assert.equal(s.error(unhex('02144d3c2b1a01')), ErrorCode.UNKNOWN_KEY);
  assert.equal(s.error(unhex('02144d3c2b1b02')), 0, 'another phone');
  assert.equal(s.error(unhex(CHALLENGE)), 0);
  assert.equal(messageType(unhex(CHALLENGE), KEY_ID), Msg.CHALLENGE);
  assert.equal(messageType(unhex(STATUS), KEY_ID), Msg.STATUS);
  assert.equal(messageType(unhex(STATUS), KEY_ID + 1), 0);
  assert.equal(messageType(unhex('0102030405060708'), KEY_ID), 0, 'another protocol version');
  assert.equal(messageType(unhex('0202'), KEY_ID), 0);
});

test('a status says when the battery or the fix is unknown', async () => {
  const { CollarHalf } = await import('../src/demo.js');
  const report = { state: State.INSIDE, batteryPct: null, lat: -33.5, lon: 151.25, noFix: true, fixAgeS: null, corrections: true };
  const half = new CollarHalf(new Map([[KEY_ID, KEY]]), { act() {}, report: () => report }, () => unhex('c0c1c2c3c4c5c6c7'));
  const s = session();
  assert.equal(hex(await half.handle(s.hello())), CHALLENGE, 'the demo collar signs as a real one does');
  assert.equal(await s.challenge(unhex(CHALLENGE)), true);
  const st = await s.status(await half.handle((await s.request(Action.REQUEST_STATUS)).bytes));
  assert.equal(st.batteryPct, null);
  assert.equal(st.fixAgeS, null);
  assert.equal(st.noFix, true);
  assert.equal(st.apps, true);
  assert.equal(st.lat, -33.5);
  assert.equal(st.lon, 151.25);
});

// Bytes made with Meshtastic's own Python library (meshtastic.protobuf).
test('ToRadio messages match Meshtastic', () => {
  assert.equal(hex(mesh.encodeWantConfig()), '18ac9e04');
  assert.equal(hex(mesh.encodeHeartbeat()), '3a00');
  assert.equal(hex(mesh.encodeToRadioPacket({ to: 0xa1b2c3d4, id: 0x01020304, portnum: 300, payload: unhex(HELLO) })),
    '0a1f15d4c3b2a1221308ac02120e02104d3c2b1aa0a1a2a3a4a5a6a73504030201');
  assert.deepEqual(mesh.decodeToRadio(unhex('18ac9e04')), { wantConfig: 69420 });
  assert.deepEqual(mesh.decodeToRadio(unhex('3a00')), { heartbeat: true });
  const p = mesh.decodeToRadio(unhex('0a1f15d4c3b2a1221308ac02120e02104d3c2b1aa0a1a2a3a4a5a6a73504030201')).packet;
  assert.deepEqual({ ...p, payload: hex(p.payload) }, { to: 0xa1b2c3d4, id: 0x01020304, portnum: 300, payload: HELLO });
});

test('FromRadio messages from Meshtastic are understood', () => {
  assert.deepEqual(mesh.decodeFromRadio(unhex('080738ac9e04')), { configComplete: 69420 });
  assert.deepEqual(mesh.decodeFromRadio(unhex('08031a2108d487cb8d0a40056a1768656c7465632d776972656c6573732d747261636b6572')),
    { myNodeNum: 0xa1b2c3d4 });
  const p = mesh.decodeFromRadio(unhex(
    '086312380dd4c3b2a115d4c3b2a1221b08ac02121602114d3c2b1ac0c1c2c3c4c5c6c7c66cfaa9f4fe36cb35efbeadde3d208d1669450000b0404803')).packet;
  assert.deepEqual({ ...p, payload: hex(p.payload) },
    { from: 0xa1b2c3d4, to: 0xa1b2c3d4, id: 0xdeadbeef, portnum: 300, payload: CHALLENGE });
  assert.deepEqual(mesh.decodeFromRadio(unhex('086412140d4433221115ffffffff2a030102033505000000')), {}, 'still encrypted');
  const text = mesh.decodeFromRadio(unhex('086512120d4433221115d4c3b2a12206080112026869')).packet;
  assert.equal(text.portnum, 1);
  assert.equal(text.from, 0x11223344);
  assert.deepEqual(mesh.decodeFromRadio(unhex('08665a09100a18102084868808')), {}, 'queue status');
  assert.deepEqual(mesh.decodeFromRadio(new Uint8Array(0)), {});
  assert.throws(() => mesh.decodeFromRadio(unhex('0a05ff')), /bad protobuf/);
  assert.throws(() => mesh.decodeFromRadio(unhex('ffffffffffffffffffffff01')), /bad protobuf/);
});

test('the pretend node round-trips what the app writes', () => {
  const packet = { from: 0xa1b2c3d4, to: 0xa1b2c3d4, id: 7, portnum: 300, payload: unhex(STATUS) };
  const back = mesh.decodeFromRadio(mesh.encodeFromRadioPacket(packet)).packet;
  assert.deepEqual({ ...back, payload: hex(back.payload) }, { ...packet, payload: STATUS });
  assert.deepEqual(mesh.decodeFromRadio(mesh.encodeFromRadioMyInfo(0xfedcba98)), { myNodeNum: 0xfedcba98 });
  assert.deepEqual(mesh.decodeFromRadio(mesh.encodeFromRadioConfigComplete(69420)), { configComplete: 69420 });
});

test('pairing codes from the base station', () => {
  // Version 1, for one collar, as base/tests/test_protocol.py makes it.
  const code = 'geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw&p=004711&b=BE&d=Bella%20%26%20Co';
  const c = parsePairingCode(`  ${code}\n`);
  assert.deepEqual({ ...c, key: hex(c.key) }, {
    home: '', keyId: KEY_ID, key: hex(KEY), collars: [{
      node: 'a1b2c3d4', nodeNum: 0xa1b2c3d4, pin: '004711', bleName: 'BE', dog: 'Bella & Co', sim: false }],
  });
  assert.equal(pairingCode(c), code);
  const sim = parsePairingCode('geocollar://pair?v=1&n=000000AA&i=00000001&k=AAECAwQFBgcICQoLDA0ODw&s=1').collars[0];
  assert.deepEqual([sim.node, sim.sim, sim.pin, sim.dog, sim.bleName], ['000000aa', true, '', '', '']);
  for (const bad of [
    '', 'hello', 'https://example.com/?v=1&n=a1b2c3d4', 'geocollar://pair?v=3&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw',
    'geocollar://pair?v=1&n=a1b2c3&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw',
    'geocollar://pair?v=1&n=a1b2c3d4&i=00000000&k=AAECAwQFBgcICQoLDA0ODw',
    'geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0O',
    'geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=!!!',
    'geocollar://pair?v=1&n=a1b2c3d4&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw&p=12345',
  ]) assert.throws(() => parsePairingCode(bad), Error, bad);
});

test('pairing codes for every collar on the base', async () => {
  // As base/tests/test_protocol.py makes it: the phone's key, then each collar's fields after its n=.
  const code = 'geocollar://pair?v=2&h=0badc0de&i=1a2b3c4d&k=AAECAwQFBgcICQoLDA0ODw'
    + '&n=a1b2c3d4&p=004711&b=BE&d=Bella%20%26%20Co&n=5a000001&d=Sim&s=1';
  const c = parsePairingCode(code);
  assert.deepEqual({ ...c, key: hex(c.key) }, {
    home: '0badc0de', keyId: KEY_ID, key: hex(KEY), collars: [
      { node: 'a1b2c3d4', nodeNum: 0xa1b2c3d4, pin: '004711', bleName: 'BE', dog: 'Bella & Co', sim: false },
      { node: '5a000001', nodeNum: 0x5a000001, pin: '', bleName: '', dog: 'Sim', sim: true }],
  });
  assert.equal(pairingCode(c), code);
  // Each collar's key is made from the phone's: golden bytes shared with the base's tests.
  assert.equal(hex(await collarKey(KEY, 0xa1b2c3d4)), 'faabcb3c8ddf0f9da74afdae47c96531');
  assert.notEqual(hex(await collarKey(KEY, 0x5a000001)), hex(await collarKey(KEY, 0xa1b2c3d4)));
  // The collar list: the same base, no key.
  const list = parsePairingCode('geocollar://pair?v=2&h=0badc0de&n=5a000001&d=Sim&s=1');
  assert.deepEqual([list.home, list.key, list.keyId, list.collars.map(x => x.dog)], ['0badc0de', null, 0, ['Sim']]);
  assert.equal(pairingCode(list), 'geocollar://pair?v=2&h=0badc0de&n=5a000001&d=Sim&s=1');
  for (const bad of [
    'geocollar://pair?v=2&h=0badc0d&n=a1b2c3d4',
    'geocollar://pair?v=2&h=0badc0de',
    'geocollar://pair?v=2&h=0badc0de&n=a1b2c3d4&n=a1b2c3d4',
    'geocollar://pair?v=2&h=0badc0de&i=1a2b3c4d&k=AAEC&n=a1b2c3d4',
    'geocollar://pair?v=2&h=0badc0de&i=00000000&k=AAECAwQFBgcICQoLDA0ODw&n=a1b2c3d4',
    'geocollar://pair?v=2&h=0badc0de&n=a1b2c3d4&p=1',
  ]) assert.throws(() => parsePairingCode(bad), Error, bad);
});
