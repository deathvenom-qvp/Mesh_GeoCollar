// SPDX-License-Identifier: GPL-3.0-or-later
// The whole stack below the screen: CollarLink over a pretend Meshtastic node, with the demo
// collar behind it and then with the firmware's real C++ collar half (firmware/test/applink_collar).

import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { Action, State } from '../src/applink.js';
import { hex, unhex } from '../src/bytes.js';
import { CollarLink, Link } from '../src/collar.js';
import { CollarHalf, DemoDog } from '../src/demo.js';
import { FakeNode } from '../src/fakenode.js';
import { matchesCollar } from '../src/match.js';
import { bearingDeg, compass, distanceM, formatAge, formatDistance, offset } from '../src/geo.js';

const NODE = 0xa1b2c3d4;
const KEY_ID = 0x1a2b3c4d;
const KEY = unhex('000102030405060708090a0b0c0d0e0f');
const collar = { node: 'a1b2c3d4', nodeNum: NODE, keyId: KEY_ID, key: KEY, dog: 'Bella', bleName: 'BELL' };
const sleep = ms => new Promise(r => setTimeout(r, ms));

/// A link with short timings, and promises for the states and reports it passes through.
function makeLink(transport, who = collar) {
  const states = [], reports = [], waiting = [];
  const check = () => {
    for (const w of [...waiting]) if (w.test()) { waiting.splice(waiting.indexOf(w), 1); w.resolve(); }
  };
  const link = new CollarLink(who, transport, {
    pollMs: 20, replyMs: 300, retryMs: 20,
    onState: (s, d) => { states.push(d ? `${s}: ${d}` : s); check(); },
    onStatus: st => { reports.push(st); check(); },
  });
  const until = (testFn, what) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for ${what}; states: ${states.join(', ')}`)), 3000);
    waiting.push({ test: testFn, resolve: () => { clearTimeout(timer); resolve(); } });
    check();
  });
  return { link, states, reports, until, state: s => until(() => link.state === s, s) };
}

function demoNode(keys = new Map([[KEY_ID, KEY]])) {
  const dog = new DemoDog(() => ({ lat: 40, lon: -75 }));
  const half = new CollarHalf(keys, dog, n => crypto.getRandomValues(new Uint8Array(n)));
  const node = new FakeNode(NODE, buf => half.handle(buf));
  return { dog, half, node, keys };
}

test('connects, reports, and mutes the lost alert', async () => {
  const { dog, node } = demoNode();
  const { link, states, reports, state, until } = makeLink(node.transport());
  link.start();
  await state(Link.ONLINE);
  assert.deepEqual(states, [Link.SEARCHING, Link.CONNECTING, Link.ONLINE]);
  await until(() => reports.length >= 2, 'two reports');
  assert.equal(reports[0].state, State.LOST);
  assert.equal(reports[0].beacon, true);
  assert.ok(Math.abs(reports[0].lat - 40) < 0.01 && Math.abs(reports[0].lon + 75) < 0.01);

  const st = await link.command(Action.SILENCE_LOST);
  assert.equal(st.beacon, false);
  assert.equal(st.beaconSilenced, true);
  assert.equal(dog.silenced, true);
  await link.command(Action.BEEP, 0, 1000);
  assert.equal(dog.lastAction.action, Action.BEEP);
  assert.equal((await link.command(Action.CORRECTIONS_OFF)).corrections, false);
  assert.equal((await link.command(Action.CORRECTIONS_ON)).corrections, true);

  await link.stop();
  assert.equal(link.state, Link.IDLE);
  assert.equal(node.isOpen, false);
  await assert.rejects(link.command(Action.BEEP), /not connected/);
});

test('a collar that does not know the phone yet is asked again later', async () => {
  const { node, keys } = demoNode(new Map());
  const { link, state } = makeLink(node.transport());
  link.start();
  await state(Link.UNKNOWN);
  keys.set(KEY_ID, KEY); // the base's list reached the collar
  await state(Link.ONLINE);
  await link.stop();
});

test('a phone the base removed is told so, and a wrong key gets nowhere', async () => {
  const { node, keys } = demoNode();
  const { link, state } = makeLink(node.transport());
  link.start();
  await state(Link.ONLINE);
  keys.delete(KEY_ID);
  node.handle = (half => buf => half.handle(buf))(new CollarHalf(keys, new DemoDog(() => ({ lat: 0, lon: 0 })), n => new Uint8Array(n)));
  await state(Link.UNKNOWN);
  await link.stop();

  // Same key id, another key: the collar's challenge does not verify, so the app never trusts it.
  const other = demoNode(new Map([[KEY_ID, unhex('ff0102030405060708090a0b0c0d0e0f')]]));
  const b = makeLink(other.node.transport());
  b.link.start();
  await b.until(() => b.states.some(s => s.startsWith(Link.WAITING)), 'a refusal');
  assert.equal(b.reports.length, 0);
  assert.ok(!b.states.includes(Link.ONLINE));
  await b.link.stop();
});

test('says hello again when the collar has restarted', async () => {
  const { node, half, dog } = demoNode();
  const { link, state, states } = makeLink(node.transport());
  link.start();
  await state(Link.ONLINE);
  half.session = null; // the collar restarted: its sessions live in RAM
  const st = await link.command(Action.SILENCE_LOST);
  assert.equal(st.beaconSilenced, true);
  assert.equal(dog.silenced, true);
  assert.deepEqual(states.filter(s => s !== Link.ONLINE), [Link.SEARCHING, Link.CONNECTING], 'without reconnecting');
  await link.stop();
});

test('refuses a different collar and reconnects after a drop', async () => {
  const wrong = new FakeNode(0x11111111, () => null);
  let rejected = 0;
  const t = wrong.transport();
  t.wrongDevice = () => { rejected++; };
  const a = makeLink(t);
  a.link.start();
  await a.until(() => rejected > 0, 'the wrong collar to be refused');
  assert.ok(!a.states.includes(Link.ONLINE));
  await a.link.stop();

  const { node } = demoNode();
  let dropped;
  const real = node.transport();
  const t2 = { ...real, open: async o => { dropped = o.onDisconnect; await real.open(o); } };
  let confirmed = 0;
  t2.confirmed = () => { confirmed++; };
  const b = makeLink(t2);
  b.link.start();
  await b.state(Link.ONLINE);
  assert.equal(confirmed, 1);
  dropped();
  await b.until(() => b.states.some(s => s.startsWith(Link.WAITING)), 'the drop to be noticed');
  await b.state(Link.ONLINE);
  assert.equal(confirmed, 2);
  await b.link.stop();
});

test('ignores GeoCollar messages that did not come from the collar itself', async () => {
  // Anything the collar hears on the mesh is passed to the phone too. A forged "unknown key"
  // from another node must not make the app give up.
  const { node } = demoNode();
  const { encodeFromRadioPacket } = await import('../src/meshtastic.js');
  const { link, state, reports, until } = makeLink(node.transport());
  link.start();
  await state(Link.ONLINE);
  const forged = unhex('02144d3c2b1a01');
  for (let i = 0; i < 5; i++) {
    node.push(encodeFromRadioPacket({ from: 0x22222222, to: NODE, id: 100 + i, portnum: 300, payload: forged }));
    await sleep(15);
  }
  const n = reports.length;
  await until(() => reports.length > n + 1, 'more reports');
  assert.equal(link.state, Link.ONLINE);
  await link.stop();
});

// The real thing: the collar's C++ session code, built for the PC, behind the pretend node.
const here = dirname(fileURLToPath(import.meta.url));
const firmwareTests = join(here, '..', '..', 'firmware', 'test');
const binary = join(firmwareTests, 'applink_collar');
let proc, lines, pending = [];

function ask(line) {
  return new Promise(resolve => { pending.push(resolve); proc.stdin.write(line + '\n'); });
}

before(() => {
  if (!existsSync(binary)) execFileSync('make', ['-C', firmwareTests, 'applink_collar'], { stdio: 'inherit' });
  proc = spawn(binary, { stdio: ['pipe', 'pipe', 'inherit'] });
  lines = createInterface({ input: proc.stdout });
  lines.on('line', l => { const r = pending.shift(); if (r) r(l.trim()); });
});

after(() => { if (proc) proc.kill(); });

const firmwareNode = () => new FakeNode(NODE, async buf => {
  const answer = await ask(`msg ${hex(buf)}`);
  return answer.startsWith('reply ') ? unhex(answer.slice(6)) : null;
});

test('the firmware collar half accepts the app and answers it', async () => {
  assert.equal(await ask(`key ${KEY_ID.toString(16)} ${hex(KEY)}`), 'ok');
  const { link, state, reports, until } = makeLink(firmwareNode().transport());
  link.start();
  await state(Link.ONLINE);
  await until(() => reports.length >= 1, 'a report');
  const r = reports[0];
  assert.deepEqual([r.fenceVersion, r.state, r.batteryPct, r.lat, r.lon, r.pdop, r.sats, r.clearanceM, r.fixAgeS],
    [3, State.LOST, 87, 40.0001234, -75.0004321, 1.8, 9, -12, 7]);
  assert.deepEqual([r.beacon, r.beaconSilenced, r.corrections, r.apps], [true, false, true, true]);

  const muted = await link.command(Action.SILENCE_LOST);
  assert.deepEqual([muted.beacon, muted.beaconSilenced], [false, true]);
  assert.equal((await link.command(Action.CORRECTIONS_OFF)).corrections, false);
  assert.equal((await link.command(Action.CORRECTIONS_ON)).corrections, true);
  await link.command(Action.BEEP, 0, 1000);
  await link.command(Action.VIBRATE, 40, 1000);
  // Shock is not something a phone may ask for, however well it signs.
  await assert.rejects(link.command(1, 50, 1000), /does not let phones/);
  assert.equal(link.state, Link.ONLINE, 'a refusal does not end the session');
  await link.stop();
});

test('the firmware collar half drops a phone the base removed', async () => {
  assert.equal(await ask(`key ${KEY_ID.toString(16)} ${hex(KEY)}`), 'ok');
  const { link, state } = makeLink(firmwareNode().transport());
  link.start();
  await state(Link.ONLINE);
  assert.equal(await ask('revoke'), 'ok');
  await state(Link.UNKNOWN);
  assert.equal(await ask(`key ${KEY_ID.toString(16)} ${hex(KEY)}`), 'ok');
  await state(Link.ONLINE);
  await link.stop();
});

test('picks the collar out of the Bluetooth names around', () => {
  const seen = (names, deviceId = 'AA:BB') => matchesCollar(collar, { deviceId, names });
  assert.equal(seen(['BELL_c3d4']), true);
  assert.equal(seen(['Rex_c3d4']), true, 'renamed, but the node number shows');
  assert.equal(seen(['BELL_1234']), true, 'same short name: worth a try, the node number is checked after');
  assert.equal(seen([undefined, 'bell_C3D4']), true);
  assert.equal(seen(['Bella is lost 5551234']), true);
  assert.equal(seen(['Bella lost +15551234567']), true);
  assert.equal(seen(['Bella is lost']), true);
  assert.equal(seen(['Bel lost +15551234567890']), true, 'a name cut short by the beacon');
  assert.equal(seen(['Rex is lost 5551234']), false);
  assert.equal(seen(['Meshtastic_1234']), false);
  assert.equal(seen(['BELLY']), false);
  assert.equal(seen([]), false);
  assert.equal(seen(['lost']), false);
  assert.equal(matchesCollar({ ...collar, deviceId: 'AA:BB' }, { deviceId: 'AA:BB', names: [] }), true);
  assert.equal(matchesCollar({ ...collar, bleName: '', dog: '' }, { deviceId: 'x', names: ['_1234', ' is lost'] }), false);
});

test('distances and directions', () => {
  const a = { lat: 40, lon: -75 };
  const b = offset(a.lat, a.lon, 300, 400);
  assert.ok(Math.abs(distanceM(a, b) - 500) < 1);
  assert.ok(Math.abs(bearingDeg(a, b) - 36.87) < 0.5);
  assert.equal(compass(bearingDeg(a, b)), 'north-east');
  assert.equal(compass(359), 'north');
  assert.equal(compass(181), 'south');
  assert.equal(formatDistance(42.4), '42 m');
  assert.equal(formatDistance(147), '150 m');
  assert.equal(formatDistance(2340), '2.3 km');
  assert.equal(formatDistance(100, true), '330 ft');
  assert.equal(formatDistance(5000, true), '3.1 mi');
  assert.equal(formatAge(2), 'just now');
  assert.equal(formatAge(42), '42 s ago');
  assert.equal(formatAge(600), '10 min ago');
  assert.equal(formatAge(7200), '2 h ago');
  assert.equal(formatAge(86400 * 3), '3 days ago');
});
