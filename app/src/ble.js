// SPDX-License-Identifier: GPL-3.0-or-later
// The Bluetooth transport: finds the collar among the devices the phone is already connected to,
// or else by its advertised name, pairs with it (the phone asks for the collar's Bluetooth PIN
// once) and reads and writes Meshtastic's three characteristics.
//
// Not yet run against a real collar: see the README.

import { BleClient } from '@capacitor-community/bluetooth-le';
import { Capacitor } from '@capacitor/core';
import { FROM_NUM, FROM_RADIO, SERVICE, TO_RADIO } from './meshtastic.js';
import { Link } from './collar.js';
import { matchesCollar, pickConnected } from './match.js';

const SCAN_MS = 25000;
const CONNECTED_EVERY_MS = 3000;
const PAIR_MS = 90000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const needsPairing = e => /encrypt|authenti|authoriz|insufficient|bond|pair/i.test(String(e && e.message || e));

let ready = null;
export function initBluetooth() {
  if (!ready) ready = BleClient.initialize({ androidNeverForLocation: true }).catch(e => { ready = null; throw e; });
  return ready;
}

/// A collar that is already connected to the phone (through this app's last run, the Meshtastic
/// app, or the phone's Bluetooth settings) does not advertise, so no scan can see it. The phone
/// still lists it among its connected devices, and the app can use that connection too.
async function connectedCollar(collar, rejected) {
  let devices;
  try {
    devices = await BleClient.getConnectedDevices([SERVICE]);
  } catch {
    return null; // an older phone or plugin without the call: scanning still works
  }
  return pickConnected(collar, devices, rejected, Capacitor.getPlatform() === 'ios');
}

function scanFor(collar, rejected, onCancel) {
  return new Promise((resolve, reject) => {
    let done = false;
    let poll = 0;
    let started = Promise.resolve();
    const finish = async (fn, arg) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      clearInterval(poll);
      // A connected collar can turn up before the scan has got going: stop it only once it has.
      try { await started; } catch { /* never started */ }
      try { await BleClient.stopLEScan(); } catch { /* not scanning */ }
      fn(arg);
    };
    const timer = setTimeout(() => finish(reject, new Error('The collar is not in Bluetooth range. '
      + 'If the Meshtastic app is connected to it, disconnect it there.')), SCAN_MS);
    onCancel(() => finish(reject, new Error('stopped')));
    // Look among the connected devices now and every few seconds: the phone may connect to the
    // collar while this scan runs, and from then on the collar no longer advertises.
    const lookConnected = () => connectedCollar(collar, rejected).then(id => { if (id) finish(resolve, id); });
    poll = setInterval(lookConnected, CONNECTED_EVERY_MS);
    // No service filter: while the lost beacon is on, the collar advertises the dog's name
    // without Meshtastic's service in it.
    started = BleClient.requestLEScan({ allowDuplicates: true }, r => {
      const id = r.device && r.device.deviceId;
      if (!id || rejected.has(id)) return;
      if (matchesCollar(collar, { deviceId: id, names: [r.localName, r.device.name] })) finish(resolve, id);
    });
    started.catch(e => finish(reject, e));
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
  const view = b => new DataView(b.buffer, b.byteOffset, b.byteLength);

  return {
    async open({ onNotify, onDisconnect, onState }) {
      await initBluetooth();
      if (!(await BleClient.isEnabled())) throw new Error('Bluetooth is off. Turn it on to reach the collar.');
      onState(Link.SEARCHING);
      try {
        deviceId = await scanFor(collar, rejected, c => { cancelScan = c; });
      } finally {
        cancelScan = null;
      }
      if (Capacitor.getPlatform() === 'android' && !(await BleClient.isBonded(deviceId))) {
        onState(Link.PAIRING);
        await BleClient.createBond(deviceId, { timeout: PAIR_MS });
      }
      onState(Link.CONNECTING);
      await BleClient.connect(deviceId, () => { open = false; onDisconnect(); }, { timeout: 20000 });
      open = true;
      await BleClient.startNotifications(deviceId, SERVICE, FROM_NUM, () => onNotify());
      // The collar's data needs an encrypted link. On iOS the first read is what makes the phone
      // ask for the PIN, and it fails until the owner has typed it.
      const end = Date.now() + PAIR_MS;
      for (;;) {
        try {
          await BleClient.read(deviceId, SERVICE, FROM_RADIO, { timeout: 10000 });
          break;
        } catch (e) {
          if (!open || !needsPairing(e) || Date.now() > end) throw e;
          onState(Link.PAIRING);
          await sleep(2000);
        }
      }
      onState(Link.CONNECTING);
    },
    write: bytes => BleClient.write(deviceId, SERVICE, TO_RADIO, view(bytes), { timeout: 10000 }),
    async read() {
      const v = await BleClient.read(deviceId, SERVICE, FROM_RADIO, { timeout: 10000 });
      return new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
    },
    async close() {
      if (cancelScan) cancelScan();
      const id = deviceId;
      deviceId = null;
      open = false;
      if (!id) return;
      try { await BleClient.stopNotifications(id, SERVICE, FROM_NUM); } catch { /* gone */ }
      try { await BleClient.disconnect(id); } catch { /* gone */ }
    },
    wrongDevice() { if (deviceId) rejected.add(deviceId); },
    confirmed() { if (deviceId) remember(deviceId); },
  };
}
