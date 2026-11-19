// SPDX-License-Identifier: GPL-3.0-or-later
// The collars this phone is linked to, kept in the app's private storage. Each entry holds the
// phone's key for that collar: whoever has it can mute the collar's lost alert and make it beep,
// so it never leaves the phone.

import { Preferences } from '@capacitor/preferences';
import { fromBase64Url, toBase64Url } from './bytes.js';

const KEY = 'geocollar.collars.v1';

export async function loadCollars() {
  try {
    const { value } = await Preferences.get({ key: KEY });
    return (value ? JSON.parse(value) : []).map(c => ({ ...c, key: fromBase64Url(c.key) }));
  } catch {
    return [];
  }
}

export async function saveCollars(collars) {
  const value = JSON.stringify(collars.map(c => ({ ...c, key: toBase64Url(c.key) })));
  await Preferences.set({ key: KEY, value });
}

// The bases this phone is linked to as a whole: { id, keyId, key }. The key is the phone's key
// for that base, which each collar's key is made from, so a collar the base adds later can be
// added from the base's collar list without a new key.
const HOMES = 'geocollar.homes.v1';

export async function loadHomes() {
  try {
    const { value } = await Preferences.get({ key: HOMES });
    return (value ? JSON.parse(value) : []).map(h => ({ ...h, key: fromBase64Url(h.key) }));
  } catch {
    return [];
  }
}

export async function saveHomes(homes) {
  await Preferences.set({ key: HOMES, value: JSON.stringify(homes.map(h => ({ ...h, key: toBase64Url(h.key) }))) });
}
