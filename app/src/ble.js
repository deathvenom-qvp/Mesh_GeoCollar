// SPDX-License-Identifier: GPL-3.0-or-later
// The Bluetooth transport: finds the collar among the devices the phone is already connected to,
// or else by its advertised name, pairs with it (the phone asks for the collar's Bluetooth PIN
// once) and reads and writes Meshtastic's three characteristics. Everything it does goes in the
// debug log (log.js).
//
// Not yet run against a real collar: see the README.

import { BleClient } from '@capacitor-community/bluetooth-le';
import { Capacitor } from '@capacitor/core';
import { FROM_NUM, FROM_RADIO, SERVICE, TO_RADIO } from './meshtastic.js';
import { Link } from './collar.js';
import { describe, log } from './log.js';
import { matchesCollar, pickConnected } from './match.js';

const SCAN_MS = 25000;
const CONNECTED_EVERY_MS = 3000;
const PAIR_MS = 90000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const needsPairing = e => /encrypt|authenti|authoriz|insufficient|bond|pair/i.test(String(e && e.message || e));
const ios = () => Capacitor.getPlatform() === 'ios';
const brief = d => `${d.deviceId} "${d.name || ''}"${d.uuids && d.uuids.length ? ` uuids ${d.uuids.join(',')}` : ''}`;

let ready = null;
export function initBluetooth() {
  if (!ready) {
    log('ble', `initialize on ${Capacitor.getPlatform()}`);
    ready = BleClient.initialize({ androidNeverForLocation: true })
      .then(() => log('ble', 'initialized'))
      .catch(e => { log('ble', 'initialize failed:', describe(e)); ready = null; throw e; });
  }
  return ready;
}

/// A collar that is already connected to the phone (through this app's last run, the Meshtastic
/// app, or the phone's Bluetooth settings) does not advertise, so no scan can see it. The phone
/// still lists it among its connected devices, and the app can use that connection too.
let lastListed = '';
async function connectedCollar(collar, rejected) {
  let devices;
  try {
    devices = await BleClient.getConnectedDevices([SERVICE]);
  } catch (e) {
    if (lastListed !== 'error') log('ble', 'getConnectedDevices failed:', describe(e));
    lastListed = 'error';
    return null; // an older phone or plugin without the call: scanning still works
  }
  const listed = devices.map(brief).join('; ') || 'none';
  if (listed !== lastListed) log('ble', `connected devices: ${listed}`); // only when it changes
  lastListed = listed;
  const id = pickConnected(collar, devices, rejected, ios());
  if (id) log('ble', `trying connected device ${id}`);
  return id;
}

function scanFor(collar, rejected, onCancel) {
  return new Promise((resolve, reject) => {
    let done = false;
    let poll = 0;
    let started = Promise.resolve();
    const seen = new Map(); // deviceId -> the names it was seen with, to log each one once
    const finish = async (fn, arg) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      clearInterval(poll);
      // A connected collar can turn up before the scan has got going: stop it only once it has.
      try { await started; } catch { /* never started */ }
      try { await BleClient.stopLEScan(); } catch { /* not scanning */ }
      log('ble', `scan stopped, ${seen.size} devices seen`);
      fn(arg);
    };
    const timer = setTimeout(() => finish(reject, new Error('The collar is not in Bluetooth range. '
      + 'If the Meshtastic app is connected to it, disconnect it there.')), SCAN_MS);
    onCancel(() => finish(reject, new Error('stopped')));
    // Look among the connected devices now and every few seconds: the phone may connect to the
    // collar while this scan runs, and from then on the collar no longer advertises.
    const lookConnected = () => connectedCollar(collar, rejected).then(id => { if (id) finish(resolve, id); });
    poll = setInterval(lookConnected, CONNECTED_EVERY_MS);
    log('ble', `scanning for node ${collar.node} (name "${collar.bleName || ''}_${collar.node.slice(-4)}"`
      + `${collar.dog ? `, or "${collar.dog} is lost"` : ''}${collar.deviceId ? `, or address ${collar.deviceId}` : ''})`
      + (rejected.size ? `, skipping ${[...rejected].join(', ')}` : ''));
    // No service filter: while the lost beacon is on, the collar advertises the dog's name
    // without Meshtastic's service in it.
    started = BleClient.requestLEScan({ allowDuplicates: true }, r => {
      const id = r.device && r.device.deviceId;
      if (!id) return;
      const names = [r.localName, r.device.name];
      const key = names.filter(Boolean).join('|');
      if (seen.get(id) !== key) {
        seen.set(id, key);
        const uuids = r.uuids && r.uuids.length ? ` uuids ${r.uuids.join(',')}` : '';
        if (key || uuids) log('ble', `seen ${id} "${key}" rssi ${r.rssi}${uuids}`);
      }
      if (rejected.has(id)) return;
      if (matchesCollar(collar, { deviceId: id, names })) {
        log('ble', `${id} matches the collar`);
        finish(resolve, id);
      }
    });
    started.then(() => log('ble', 'scan started'), e => {
      log('ble', 'scan failed to start:', describe(e));
      finish(reject, e);
    });
    lookConnected();
  });
}

/// A transport for CollarLink. `remember(deviceId)` is called once the collar behind that
/// Bluetooth address has proved to be the right one.
export function bleTransport(collar, remember) {
  const rejected = new Set();
  let deviceId = null;
  let open = false;
  let cancelScan = null;
  let wasConnected = false; // found among the phone's connected devices: another app may hold it
  const view = b => new DataView(b.buffer, b.byteOffset, b.byteLength);
  const bytesOf = v => new Uint8Array(v.buffer, v.byteOffset, v.byteLength);

  const step = async (what, fn) => {
    log('ble', what);
    try {
      return await fn();
    } catch (e) {
      log('ble', `${what} failed:`, describe(e));
      throw e;
    }
  };

  return {
    async open({ onNotify, onDisconnect, onState }) {
      await initBluetooth();
      const enabled = await BleClient.isEnabled();
      if (!enabled) {
        log('ble', 'Bluetooth is off');
        throw new Error('Bluetooth is off. Turn it on to reach the collar.');
      }
      onState(Link.SEARCHING);
      lastListed = '';
      try {
        deviceId = await scanFor(collar, rejected, c => { cancelScan = c; });
      } finally {
        cancelScan = null;
      }
      const listed = await BleClient.getConnectedDevices([SERVICE]).catch(() => []);
      wasConnected = listed.some(d => d.deviceId === deviceId);
      log('ble', `using ${deviceId}${wasConnected ? ', which the phone is already connected to' : ''}`);
      if (Capacitor.getPlatform() === 'android') {
        const bonded = await BleClient.isBonded(deviceId).catch(e => { log('ble', 'isBonded failed:', describe(e)); return false; });
        log('ble', `bonded: ${bonded}`);
        if (!bonded) {
          onState(Link.PAIRING);
          await step('createBond', () => BleClient.createBond(deviceId, { timeout: PAIR_MS }));
        }
      }
      onState(Link.CONNECTING);
      await step(`connect ${deviceId}`, () => BleClient.connect(deviceId, id => {
        log('ble', `disconnected from ${id}`);
        open = false;
        onDisconnect();
      }, { timeout: 20000 }));
      open = true;
      // Is it a Meshtastic node at all? On Android a guess from the connected devices may not be.
      let services = [];
      try {
        services = await BleClient.getServices(deviceId);
        log('ble', `services: ${services.map(s => s.uuid).join(', ') || 'none'}`);
      } catch (e) {
        log('ble', 'getServices failed:', describe(e));
      }
      const mesh = services.find(s => String(s.uuid).toLowerCase() === SERVICE);
      if (services.length && !mesh) {
        rejected.add(deviceId);
        const err = new Error('That was not the collar. Looking again.');
        err.code = 'wrong';
        throw err;
      }
      if (mesh) log('ble', `characteristics: ${(mesh.characteristics || []).map(c => `${c.uuid} ${Object.keys(c.properties || {}).filter(k => c.properties[k]).join('|')}`).join('; ')}`);
      if (Capacitor.getPlatform() === 'android') {
        try { log('ble', `MTU ${await BleClient.getMtu(deviceId)}`); } catch { /* not on every version */ }
      }
      await step('subscribe to FromNum', () => BleClient.startNotifications(deviceId, SERVICE, FROM_NUM, v => {
        log('ble', `FromNum notify ${v && v.byteLength >= 4 ? v.getUint32(0, true) : ''}`);
        onNotify();
      }));
      // The collar's data needs an encrypted link. On iOS the first read is what makes the phone
      // ask for the PIN, and it fails until the owner has typed it. It reads FromNum, not
      // FromRadio, so that it takes nothing from the queue of messages.
      const end = Date.now() + PAIR_MS;
      for (;;) {
        try {
          const v = await BleClient.read(deviceId, SERVICE, FROM_NUM, { timeout: 10000 });
          log('ble', `FromNum read ${v && v.byteLength >= 4 ? v.getUint32(0, true) : `(${v ? v.byteLength : 0} bytes)`}: the link is encrypted`);
          break;
        } catch (e) {
          log('ble', 'FromNum read failed:', describe(e));
          if (!open || !needsPairing(e) || Date.now() > end) throw e;
          onState(Link.PAIRING);
          await sleep(2000);
        }
      }
      onState(Link.CONNECTING);
    },
    async write(bytes) {
      try {
        await BleClient.write(deviceId, SERVICE, TO_RADIO, view(bytes), { timeout: 10000 });
      } catch (e) {
        log('ble', `write ToRadio (${bytes.length} bytes) failed:`, describe(e));
        throw e;
      }
    },
    async read() {
      try {
        return bytesOf(await BleClient.read(deviceId, SERVICE, FROM_RADIO, { timeout: 10000 }));
      } catch (e) {
        log('ble', 'read FromRadio failed:', describe(e));
        throw e;
      }
    },
    async close() {
      if (cancelScan) cancelScan();
      const id = deviceId;
      deviceId = null;
      open = false;
      if (!id) return;
      log('ble', `closing ${id}`);
      try { await BleClient.stopNotifications(id, SERVICE, FROM_NUM); } catch { /* gone */ }
      try { await BleClient.disconnect(id); } catch { /* gone */ }
    },
    /// More to say about a failure, for the screen.
    hint(e) {
      if (!wasConnected) return '';
      if (e && (e.code === 'timeout' || /did not answer/i.test(e.message || '')))
        return 'Another app may be using this collar\'s connection: if the Meshtastic app is connected to it, disconnect it there.';
      return '';
    },
    wrongDevice() { if (deviceId) { log('ble', `${deviceId} is not this collar: skipping it`); rejected.add(deviceId); } },
    confirmed() { if (deviceId) remember(deviceId); },
  };
}
