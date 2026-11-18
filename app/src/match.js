// SPDX-License-Identifier: GPL-3.0-or-later
// Is this Bluetooth advertisement our collar? A collar shows up under one of three names:
//   - Meshtastic's own, "<short name>_<last four hex digits of its node number>"
//   - the lost beacon's, "<dog> is lost <phone>" (or "<dog> lost <phone>" when that is too long)
// It is only a first guess: once connected, the app checks the collar's node number, and the
// collar checks the app's key.

import { SERVICE } from './meshtastic.js';

export function matchesCollar(collar, { deviceId, names = [] }) {
  if (collar.deviceId && deviceId === collar.deviceId) return true;
  const suffix = '_' + collar.node.slice(-4);
  const prefix = collar.bleName ? collar.bleName.toLowerCase() + '_' : '';
  const dog = (collar.dog || '').trim().toLowerCase();
  for (const raw of names) {
    const name = String(raw || '').trim().toLowerCase();
    if (!name) continue;
    if (name.endsWith(suffix)) return true;
    if (prefix && name.startsWith(prefix) && /_[0-9a-f]{4}$/.test(name)) return true;
    // The beacon cuts a long name short, so the advertised part may be only its beginning.
    const lost = /^(.+?) (?:is )?lost(?: |$)/.exec(name);
    if (lost && dog && dog.startsWith(lost[1])) return true;
  }
  return false;
}

/// Which of the devices the phone is already connected to is our collar? A collar that is
/// connected stops advertising, so a scan never finds it: the phone's own list of connected
/// devices is the only way to see it. `meshtasticOnly` is true when that list holds only devices
/// with Meshtastic's service (iOS filters it so); then any of them is worth a try, because the
/// app checks the node number once connected. Android lists every connected device, earbuds and
/// watches too, so there a device is tried only if its name matches, if the phone remembers it
/// offering Meshtastic's service, or if its name looks like a Meshtastic node's ("Name_ab12"):
/// the phone may remember the collar under an older name. Once connected, the app checks for the
/// service and the node number, and a device that fails goes into `rejected`.
export function pickConnected(collar, devices, rejected = new Set(), meshtasticOnly = false, service = SERVICE) {
  const open = (devices || []).filter(d => d && d.deviceId && !rejected.has(d.deviceId));
  const named = open.find(d => matchesCollar(collar, { deviceId: d.deviceId, names: [d.name] }));
  if (named) return named.deviceId;
  if (meshtasticOnly) return open.length ? open[0].deviceId : null;
  const offers = open.find(d => (d.uuids || []).some(u => String(u).toLowerCase() === service));
  if (offers) return offers.deviceId;
  const meshy = open.find(d => /_[0-9a-f]{4}$/i.test(String(d.name || '').trim()));
  return meshy ? meshy.deviceId : null;
}
