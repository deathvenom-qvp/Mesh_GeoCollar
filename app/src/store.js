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
