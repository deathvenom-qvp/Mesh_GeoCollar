// SPDX-License-Identifier: GPL-3.0-or-later
// The app's debug log: what Bluetooth and the link to the collar did, with times, for the owner
// to save and send when something does not work. It keeps the last few thousand lines in memory;
// the screen code saves them on the phone (see setLogSaver) so they outlive a restart.
//
// Never log the collar's key or its Bluetooth PIN.

const MAX_LINES = 3000;
const lines = [];
const listeners = new Set();
let saver = null;
let saveTimer = 0;

const pad = (n, w = 2) => String(n).padStart(w, '0');
function stamp(d = new Date()) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/// An error, or anything thrown, as one line of text.
export function describe(e) {
  if (e === undefined || e === null) return String(e);
  if (typeof e === 'string') return e;
  const parts = [];
  if (e.name && e.name !== 'Error') parts.push(e.name);
  if (e.message) parts.push(e.message);
  if (e.code !== undefined) parts.push(`(code ${e.code})`);
  if (!parts.length) {
    try { return JSON.stringify(e); } catch { return String(e); }
  }
  return parts.join(': ');
}

/// log('ble', 'connecting to', id): one line, the parts joined by spaces.
export function log(tag, ...parts) {
  const text = parts.map(p => (typeof p === 'string' ? p : p instanceof Error ? describe(p) : safeJson(p))).join(' ');
  add(`${stamp()} [${tag}] ${text}`);
}

function safeJson(v) {
  if (v === undefined) return 'undefined';
  try { return JSON.stringify(v); } catch { return String(v); }
}

function add(line) {
  lines.push(line);
  if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
  if (typeof console !== 'undefined' && globalThis.GEOCOLLAR_LOG_CONSOLE) console.log(line);
  for (const fn of listeners) {
    try { fn(line); } catch { /* a screen that went away */ }
  }
  if (saver && !saveTimer) saveTimer = setTimeout(flushLog, 2000);
}

export function logLines() {
  return lines.slice();
}

/// Called with each new line. Returns a function that stops it.
export function onLog(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function clearLog() {
  lines.length = 0;
  for (const fn of listeners) {
    try { fn(null); } catch { /* gone */ }
  }
  flushLog();
}

/// `save(lines)` stores the lines somewhere that outlives the app; `saved` are the lines it
/// stored last time, which go in front, marked as an earlier run.
export function setLogSaver(save, saved = []) {
  if (saved && saved.length) {
    const earlier = saved.slice(-Math.floor(MAX_LINES / 2));
    lines.unshift(...earlier, `---- earlier run above, this run below ----`);
    if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
  }
  saver = save;
}

export function flushLog() {
  clearTimeout(saveTimer);
  saveTimer = 0;
  if (!saver) return;
  try {
    const r = saver(lines.slice());
    if (r && r.catch) r.catch(() => {});
  } catch { /* nowhere to save it */ }
}
