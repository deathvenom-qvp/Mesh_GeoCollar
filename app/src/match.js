// SPDX-License-Identifier: GPL-3.0-or-later
// Is this Bluetooth advertisement our collar? A collar shows up under one of three names:
//   - Meshtastic's own, "<short name>_<last four hex digits of its node number>"
//   - the lost beacon's, "<dog> is lost <phone>" (or "<dog> lost <phone>" when that is too long)
// It is only a first guess: once connected, the app checks the collar's node number, and the
// collar checks the app's key.

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
