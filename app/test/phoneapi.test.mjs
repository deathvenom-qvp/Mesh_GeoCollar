// SPDX-License-Identifier: GPL-3.0-or-later
// The phone API's bytes, against golden values the base's Python makes
// (golden() in base/tests/test_phoneapi.py, which checks they are all in this file).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { fromBase64Url, hex, toBase64Url, unhex } from '../src/bytes.js';
import {
  ApiError, BaseApi, apiKey, enroll, enrollProof, fromBase, isApiCode, readCode, relayBody, replyAad, requestAad, seal,
  unseal, welcomeAad,
} from '../src/phoneapi.js';

const G = {
  secret: Uint8Array.from({ length: 16 }, (_, i) => i),
  token: Uint8Array.from({ length: 8 }, (_, i) => 100 + i),
  phoneD: '1f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c5b6a7988',
  phonePub: 'BL18c7iLLptM7aYgIrLai-Exk6W1btwm5994QuJM0LXrBgWtp72oOsaiuA1-MUBA-kf_Frg7rIXO2wFEUbt85xo',
  basePub: 'BNjNEupcZ_L4oAwRJIk-3PpnVMTWzt5r4TvfIpXIEKl_paidLSo2DAyppNbHye1LKNPhmdZify5pbWicMQpbD0g',
  proof: 'ZUJA_2hmIvJWG8FtbYB-XWj3QxkS9Z2kxSPyL-0kmGk',
  apiKey: '9fe358f2649593a2a290f8fd08ec89b2bf07d8f576adc4ee95bb0cd7f073488b',
  code: 'AwABAgMEBQYHCAkKCwwNDg9OyU4h39amam1n8lmdgD70TB5_v8fNNw4V_X1a7IE4QcYtpeaNauu8-pADRUgBBGAaGw--Hiseu4l-q8nD9AwqWLuABmXMzL4DdQSmeP5XUW3u',
  welcome: { iv: 'AAAAAAAAAAAAAAAA', ct: 'k2K-xN5CLBIP2hfKJ7MLxFmTVJeJQcVNmnPSoC0hBMuJ4njeYZjMPCoDY01_Ql1yuvqGfWrIXwD0k1TC6QT_BuIAdwMQSwysIPA' },
  request: { iv: 'AQEBAQEBAQEBAQEB', ct: 'i9tdmxPkouN8Q0E7hkgdn4_U4c2mwiVVW8vGqPtQ-dXBghM6V-Q9pvlGbctABw' },
  reply: { iv: 'AgICAgICAgICAgIC', ct: 'Ol3Qbi6s5xDotj7RNLfsrPoEZAKxrAi67KYf' },
};

async function phoneKeyPair() {
  const pub = fromBase64Url(G.phonePub);
  const jwk = { kty: 'EC', crv: 'P-256', d: toBase64Url(unhex(G.phoneD)), x: toBase64Url(pub.slice(1, 33)), y: toBase64Url(pub.slice(33)) };
  const privateKey = await crypto.subtle.importKey('jwk', jwk, { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  return { privateKey, publicKey: pub };
}

test('the QR code opens to the base, its one-time token and its addresses', async () => {
  assert.ok(isApiCode(G.code));
  assert.ok(!isApiCode('geocollar://pair?v=2&h=a1b2c3d4&n=a1b2c3d4'));
  const c = await readCode(G.code);
  assert.deepEqual([...c.secret], [...G.secret]);
  assert.deepEqual([...c.token], [...G.token]);
  assert.equal(c.home, 'a1b2c3d4');
  assert.deepEqual(c.urls, ['http://192.168.1.5:8100']);
  // A changed character is caught.
  const bad = G.code.slice(0, 40) + (G.code[40] === 'A' ? 'B' : 'A') + G.code.slice(41);
  await assert.rejects(readCode(bad), /damaged/);
  await assert.rejects(readCode('hello'), /not a GeoCollar code/);
});

test('the phone works out the same key as the base, and reads its answers', async () => {
  const pair = await phoneKeyPair();
  assert.equal(toBase64Url(await enrollProof(G.secret, G.token, pair.publicKey)), G.proof);
  const key = await apiKey(pair.privateKey, pair.publicKey, fromBase64Url(G.basePub), G.secret, G.token);
  assert.equal(hex(key), G.apiKey);
  assert.deepEqual(await unseal(key, welcomeAad(G.token), G.welcome), { phone_id: '0000002a', ble_key: 'AAECAwQFBgcICQoLDA0ODw' });
  // A request sealed by the phone is the base's golden one.
  const iv = new Uint8Array(12).fill(1);
  assert.deepEqual(await seal(key, requestAad('collars', '0000002a'), { ts: 1700000000000, body: {} }, iv), G.request);
  assert.deepEqual(await unseal(key, replyAad('collars', '0000002a', iv), G.reply), { ok: true });
  // The answer is bound to the request it answers.
  await assert.rejects(unseal(key, replyAad('collars', '0000002a', new Uint8Array(12)), G.reply), e => e.code === 'not_base');
});

/// A pretend base: it plays the golden enrolment and then answers with the golden key.
function fakeBase({ down = [], clockMs = null } = {}) {
  const calls = [];
  const key = unhex(G.apiKey);
  const http = async (url, body) => {
    calls.push(url);
    if (down.some(d => url.startsWith(d))) throw new Error('connect ECONNREFUSED');
    if (url.endsWith('/enroll')) {
      assert.equal(body.proof, G.proof);
      const welcome = await seal(key, welcomeAad(G.token), {
        phone_id: '0000002a', home: 'a1b2c3d4', ble_key_id: 42, ble_key: 'AAECAwQFBgcICQoLDA0ODw',
        urls: ['https://dogs.example.net', 'http://192.168.1.5:8100'], collars: [], server_time: 1,
      });
      return { status: 200, data: { base_key: G.basePub, ...welcome } };
    }
    const op = url.split('/').pop();
    const plain = await unseal(key, requestAad(op, body.phone), body);
    if (clockMs !== null && Math.abs(plain.ts - clockMs) > 600000) return { status: 401, data: { detail: 'clock', code: 'clock', server_time: clockMs } };
    return { status: 200, data: await seal(key, replyAad(op, body.phone, fromBase64Url(body.iv)), { op, body: plain.body, urls: ['https://dogs.example.net', 'http://192.168.1.5:8100'] }) };
  };
  return { http, calls };
}

test('enrolling, then asking the base, over whichever address works', async () => {
  const base = fakeBase({ down: ['https://dogs.example.net'] });
  const r = await enroll(await readCode(G.code), base.http, await phoneKeyPair());
  assert.equal(r.api.phone, '0000002a');
  assert.equal(hex(r.api.key), G.apiKey);
  assert.equal(r.welcome.ble_key_id, 42);
  assert.deepEqual(r.api.urls, ['https://dogs.example.net', 'http://192.168.1.5:8100']);
  let saved = 0;
  const api = new BaseApi(r.api, base.http, { onChange: () => { saved++; } });
  const reply = await api.call('relay', { node_id: '!a1b2c3d4' });
  assert.deepEqual(reply.body, { node_id: '!a1b2c3d4' });
  // The address that worked is tried first next time.
  assert.equal(r.api.good, 'http://192.168.1.5:8100');
  base.calls.length = 0;
  await api.call('collars');
  assert.deepEqual(base.calls, ['http://192.168.1.5:8100/api/phone/v1/collars']);
  assert.equal(saved, 0);
});

test('a phone whose clock is off takes the base\'s', async () => {
  const base = fakeBase({ clockMs: 1800000000000 });
  const api = new BaseApi({ phone: '0000002a', key: unhex(G.apiKey), urls: ['http://b'] }, base.http, { now: () => 1700000000000 });
  assert.equal((await api.call('collars')).op, 'collars');
  assert.equal(api.offset, 100000000000);
});

test('errors the app acts on', async () => {
  const removed = async () => ({ status: 401, data: { detail: 'x', code: 'unknown_phone' } });
  const api = new BaseApi({ phone: '0000002a', key: unhex(G.apiKey), urls: ['http://a', 'http://b'] }, removed);
  await assert.rejects(api.call('collars'), e => e.code === 'removed');
  const offline = new BaseApi({ phone: '0000002a', key: unhex(G.apiKey), urls: ['http://a'] }, async () => { throw new Error('down'); });
  await assert.rejects(offline.call('collars'), e => e instanceof ApiError && e.code === 'offline');
  // A server that is not the base (a proxy's error page) is skipped for the next address.
  const calls = [];
  const proxy = async url => { calls.push(url); return url.startsWith('http://a') ? { status: 404, data: null } : { status: 502, data: null }; };
  await assert.rejects(new BaseApi({ phone: '0000002a', key: unhex(G.apiKey), urls: ['http://a', 'http://b'] }, proxy).call('collars'),
    e => e.code === 'offline');
  assert.equal(calls.length, 2);
  const used = { status: 410, data: { detail: 'used', code: 'used' } };
  await assert.rejects(enroll(await readCode(G.code), async () => used, await phoneKeyPair()), e => e.code === 'used');
});

test('reports go back and forth in the shapes both sides use', () => {
  const now = 1700000000000;
  const s = fromBase({ state: 'LOST', lat: 40, lon: -75, battery_pct: 255, sats: 7, received: now / 1000 - 90, via: 'phone',
    via_phone: 'Sam', fix_age_s: 4, beacon_active: true }, now, now);
  assert.equal(s.state, 4);
  assert.equal(s.batteryPct, null);
  assert.equal(s.at, now - 90000);
  assert.equal(s.viaPhone, 'Sam');
  assert.equal(s.beacon, true);
  assert.equal(fromBase({ state: 'INSIDE', lat: 1, lon: 2, received: 5, no_fix: true }, 5000, now).fixAgeS, null);
  const body = relayBody('a1b2c3d4', { ...s, at: now - 2000, pdop: 1.25, clearanceM: -40, fenceVersion: 3 }, { lat: 40.1, lon: -75.1, accuracy: 5 }, now);
  assert.deepEqual(body, {
    node_id: '!a1b2c3d4',
    status: { state: 'LOST', lat: 40, lon: -75, battery_pct: null, sats: 7, pdop: 1.25, clearance_m: -40, fence_version: 3,
      corrections_enabled: true, shock_enabled: false, beacon_active: true, beacon_silenced: false, no_fix: false, home: false },
    fix_age_s: 4, age_ms: 2000, phone_position: { lat: 40.1, lon: -75.1, accuracy_m: 5 },
  });
});
