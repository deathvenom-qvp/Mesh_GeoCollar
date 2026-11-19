// SPDX-License-Identifier: GPL-3.0-or-later
// The screens: the list of linked collars, adding one from the base station's QR code, and one
// collar's map, status and buttons.

import L from 'leaflet';
import { App } from '@capacitor/app';
import { Capacitor } from '@capacitor/core';
import { Directory, Encoding, Filesystem } from '@capacitor/filesystem';
import { Preferences } from '@capacitor/preferences';
import { Share } from '@capacitor/share';

import { Action, State } from './applink.js';
import { bleTransport } from './ble.js';
import { randomBytes } from './bytes.js';
import { CollarLink, Link } from './collar.js';
import { CollarHalf, DEMO_KEY_ID, DEMO_NODE, DemoDog, demoKey } from './demo.js';
import { FakeNode } from './fakenode.js';
import { bearingDeg, compass, distanceM, formatAge, formatDistance } from './geo.js';
import { collarKey, parsePairingCode } from './pairing.js';
import { startScanner } from './scanner.js';
import { loadCollars, loadHomes, saveCollars, saveHomes } from './store.js';
import { clearLog, describe, flushLog, log, logLines, onLog, setLogSaver } from './log.js';

const $ = id => document.getElementById(id);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const native = Capacitor.isNativePlatform();
const platform = Capacitor.getPlatform();
const imperial = /^en-(US|LR|MM)\b/i.test(navigator.language || '');
// eslint-disable-next-line no-undef
const BUILD = typeof __GEOCOLLAR_BUILD__ !== 'undefined' ? __GEOCOLLAR_BUILD__ : {};
const BUILD_TEXT = BUILD.number ? `Build ${BUILD.number} (${BUILD.commit}${BUILD.branch && BUILD.branch !== 'main' ? `, ${BUILD.branch}` : ''})` : 'Local build';
const LOG_KEY = 'geocollar.log.v1';

const STATE_TEXT = {
  [State.NO_FENCE]: 'No fence set',
  [State.INSIDE]: 'Inside the fence',
  [State.WARNING]: 'Near the fence line',
  [State.OUTSIDE]: 'Outside the fence',
  [State.LOST]: 'Lost',
};
const STATE_CLASS = { [State.INSIDE]: 's-inside', [State.WARNING]: 's-warning', [State.OUTSIDE]: 's-outside', [State.LOST]: 's-lost' };
const PILL = {
  [Link.SEARCHING]: ['Searching…', 'busy'], [Link.PAIRING]: ['Pairing…', 'busy'], [Link.CONNECTING]: ['Connecting…', 'busy'],
  [Link.ONLINE]: ['Connected', 'online'], [Link.WAITING]: ['Not connected', ''], [Link.UNKNOWN]: ['Not linked yet', ''],
  [Link.IDLE]: ['', ''],
};

let collars = [];
let homes = [];       // the bases this phone is linked to as a whole: { id, keyId, key }
let screen = 'home';
let current = null;   // the collar on screen
let link = null;
let status = null;    // its latest report: live over Bluetooth, or the one saved last time
let live = false;
let me = null;        // where the phone is: { lat, lon, accuracy }
let watchId = null;
let stopScanner = null;
let ticker = 0;
let lastSaved = 0;
let toastTimer = 0;
let map, collarMark, meMark, line, follow = true, placed = false;
let logReturn = 'home'; // the screen the debug log goes back to

const nameOf = c => c.dog || 'the collar';
const titleOf = c => c.dog || `Collar ${c.node}`;
const hasPosition = s => !!s && !(s.lat === 0 && s.lon === 0);
const persist = () => saveCollars(collars).catch(() => {});

function show(name) {
  screen = name;
  for (const el of document.querySelectorAll('.screen')) el.hidden = el.id !== name;
}

function toast(text) {
  const t = $('toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---- The list of collars ----

function renderHome() {
  const list = $('collars');
  list.replaceChildren();
  for (const c of collars) {
    const li = el('li');
    const s = c.last;
    li.append(el('span', `dot ${s ? STATE_CLASS[s.state] || '' : ''}`));
    const grow = el('div', 'grow');
    grow.append(el('div', 'name', titleOf(c)));
    const kind = c.demo ? 'Demo collar' : c.sim ? 'Simulated collar' : '';
    const heard = s ? `${STATE_TEXT[s.state] || 'Unknown'}, heard ${formatAge((Date.now() - s.at) / 1000)}` : 'Not heard from yet';
    grow.append(el('div', 'sub', kind ? `${kind}. ${heard}` : heard));
    li.append(grow, el('span', 'chev', '›'));
    li.addEventListener('click', () => openCollar(c));
    list.append(li);
  }
  $('empty').hidden = collars.length > 0;
  $('demo').hidden = collars.some(c => c.demo);
}

function goHome() {
  show('home');
  renderHome();
}

// ---- Adding a collar ----

async function openAdd() {
  show('addScreen');
  $('addMsg').hidden = true;
  $('code').value = '';
  $('camMsg').textContent = 'Starting the camera…';
  try {
    const stop = await startScanner($('video'), text => addFromCode(text).catch(showAddError));
    if (screen !== 'addScreen') { stop(); return; }
    stopScanner = stop;
    $('camMsg').textContent = 'Point the camera at the code on the base station';
  } catch (e) {
    $('camMsg').textContent = 'The camera is not available. Paste the code below instead.';
    $('pasteBox').open = true;
  }
}

function closeAdd() {
  if (stopScanner) { stopScanner(); stopScanner = null; }
}

function showAddError(e) {
  $('addMsg').textContent = e && e.message ? e.message : String(e);
  $('addMsg').hidden = false;
}

const listNames = names => names.join(', ').replace(/, ([^,]*)$/, ' and $1');

/// Link this phone to the collars a pairing code names: every collar on the base, or one collar
/// from an older base. A code without a key (the base's collar list) adds the collars of a base
/// this phone is linked to already, and drops the ones the base no longer has. Throws if the
/// code is not one.
async function addFromCode(text) {
  const p = parsePairingCode(text);
  let keyId = p.keyId, key = p.key;
  if (p.home && !key) {
    const h = homes.find(x => x.id === p.home);
    if (!h) throw new Error('This phone is not linked to that base station yet. On the base, press Connect app and scan that code first.');
    ({ keyId, key } = h);
  }
  const before = new Set(collars.filter(x => p.home && x.home === p.home).map(x => x.node));
  const linked = [];
  for (const e of p.collars) {
    const c = { node: e.node, nodeNum: e.nodeNum, keyId, key: p.home ? await collarKey(key, e.nodeNum) : key,
      pin: e.pin, bleName: e.bleName, dog: e.dog, sim: e.sim };
    if (p.home) c.home = p.home;
    const i = collars.findIndex(x => x.node === c.node && !x.demo);
    if (i >= 0) {
      // Linked again: a new key for the same collar. Keep what the phone knew about it.
      c.last = collars[i].last;
      c.deviceId = collars[i].deviceId;
      collars[i] = c;
    } else {
      collars.push(c);
    }
    linked.push(c);
  }
  // Collars the base no longer has.
  const gone = p.home ? collars.filter(x => x.home === p.home && !p.collars.some(e => e.node === x.node)) : [];
  if (gone.length) collars = collars.filter(x => !gone.includes(x));
  if (p.home && p.key) homes = [...homes.filter(x => x.id !== p.home), { id: p.home, keyId, key }];
  await persist();
  if (p.home && p.key) await saveHomes(homes);
  log('app', `pairing code v${p.home ? 2 : 1}${p.home ? `, base ${p.home}${p.key ? ', with key' : ', collar list'}` : ''}:`
    + ` ${linked.map(c => c.node).join(' ')}${gone.length ? `; dropped ${gone.map(c => c.node).join(' ')}` : ''}`);
  closeAdd();
  await closeCollar();
  if (linked.length === 1) {
    openCollar(linked[0]);
    toast(`${titleOf(linked[0])} is linked to this phone.`);
    return;
  }
  goHome();
  const added = linked.filter(c => !before.has(c.node));
  if (p.key || !p.home) toast(`Linked to ${listNames(linked.map(titleOf))}.`);
  else if (added.length) toast(`Added ${listNames(added.map(titleOf))}.`);
  else toast(gone.length ? `Removed ${listNames(gone.map(titleOf))}.` : 'The collars are up to date.');
}

function addDemo() {
  let c = collars.find(x => x.demo);
  if (!c) {
    c = { demo: true, node: DEMO_NODE, nodeNum: parseInt(DEMO_NODE, 16), keyId: DEMO_KEY_ID, key: demoKey(), pin: '', bleName: '', dog: 'Biscuit' };
    collars.push(c);
    persist();
  }
  openCollar(c);
}

// ---- One collar ----

/// The pretend collar's transport: a lost dog a few hundred metres from the phone.
function demoTransport(c) {
  let home = null;
  const dog = new DemoDog(() => home);
  const half = new CollarHalf(new Map([[c.keyId, c.key]]), dog, randomBytes);
  const node = new FakeNode(c.nodeNum, async buf => { await sleep(120); return half.handle(buf); });
  const t = node.transport();
  return {
    ...t,
    async open(o) {
      // Give the phone a moment to say where it is, so the dog is lost somewhere nearby.
      for (let i = 0; i < 12 && !me && !home; i++) await sleep(250);
      home = home || (me ? { lat: me.lat, lon: me.lon } : { lat: 51.4779, lon: -0.0015 });
      await sleep(500);
      return t.open(o);
    },
  };
}

function transportFor(c) {
  if (c.demo || c.sim) return demoTransport(c);
  if (native) return bleTransport(c, id => { if (c.deviceId !== id) { c.deviceId = id; persist(); } });
  return {
    open: async () => { throw new Error('Bluetooth works in the phone app only. This page can show the demo collar.'); },
    write: async () => {}, read: async () => new Uint8Array(0), close: async () => {},
  };
}

function openCollar(c) {
  log('app', `open collar ${c.node}: Bluetooth name "${c.bleName || ''}_${c.node.slice(-4)}", dog "${c.dog || ''}"`
    + `${c.deviceId ? `, last address ${c.deviceId}` : ''}${c.demo ? ', demo' : c.sim ? ', simulated' : ''}`);
  current = c;
  status = c.last || null;
  live = false;
  follow = true;
  placed = false;
  show('collarScreen');
  ensureMap();
  link = new CollarLink(c, transportFor(c), {
    onState: () => { if (link && link.state !== Link.ONLINE) live = false; render(); },
    onStatus: st => {
      if (current !== c) return;
      status = st;
      live = true;
      c.last = { ...st };
      if (Date.now() - lastSaved > 30000) { lastSaved = Date.now(); persist(); }
      render();
    },
  });
  link.start();
  watchPhone();
  clearInterval(ticker);
  ticker = setInterval(render, 5000);
  render();
  setTimeout(() => map.invalidateSize(), 0);
}

async function closeCollar() {
  if (!current) return;
  log('app', `close collar ${current.node}`);
  const old = link;
  link = null;
  current = null;
  clearInterval(ticker);
  if (watchId !== null && navigator.geolocation) navigator.geolocation.clearWatch(watchId);
  watchId = null;
  persist();
  if (old) await old.stop();
}

function watchPhone() {
  if (!navigator.geolocation || watchId !== null) return;
  watchId = navigator.geolocation.watchPosition(
    p => { me = { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy }; render(); },
    () => { me = null; render(); },
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
}

function linkMessage(c, state, detail) {
  const name = nameOf(c);
  switch (state) {
    case Link.SEARCHING:
      return [`Looking for ${name === 'the collar' ? 'the collar' : name + "'s collar"}. Bluetooth reaches a few dozen metres, so go to where it was last seen.`
        + (hasPosition(status) ? ' The map shows the last position this phone got.' : '')];
    case Link.PAIRING:
      return c.pin
        ? ["Your phone is asking for the collar's Bluetooth PIN. It is ", el('b', '', `${c.pin.slice(0, 3)} ${c.pin.slice(3)}`), '.']
        : ["Your phone is asking for the collar's Bluetooth PIN. Enter the code on the collar's screen, or 123456 on a collar without a screen."];
    case Link.CONNECTING:
      return ['Found it. Connecting…'];
    case Link.WAITING:
      return [`${detail || 'Not connected.'} Trying again…`];
    case Link.UNKNOWN:
      return ['The collar has not been told about this phone yet. The base station tells it over the mesh, usually within a few '
        + 'minutes of linking. If this phone was removed on the base station, link it again there.'];
    case Link.ONLINE:
      if (c.demo) return ['A pretend collar, to try the app with. Nothing here is real.'];
      if (c.sim) return ["One of the base station's simulated collars: there is nothing to connect to, so this is a pretend dog."];
      return null;
    default:
      return null;
  }
}

function fact(name, value) {
  return [el('dt', '', name), el('dd', '', value)];
}

function render() {
  if (screen !== 'collarScreen' || !current || !link) return;
  const c = current, s = status, name = nameOf(c);
  const online = link.state === Link.ONLINE;
  $('dogName').textContent = titleOf(c);
  const [pillText, pillClass] = PILL[link.state] || ['', ''];
  $('pill').textContent = pillText;
  $('pill').className = `pill ${pillClass}`;
  $('pill').hidden = !pillText;

  const msg = linkMessage(c, link.state, link.detail);
  $('linkMsg').hidden = !msg;
  if (msg) $('linkMsg').replaceChildren(...msg);

  // The lost alert.
  const lost = !!s && (s.beacon || s.state === State.LOST);
  $('alert').hidden = !lost;
  if (lost) {
    const Name = c.dog || 'The collar';
    $('alert').classList.toggle('calm', !s.beacon);
    $('mute').hidden = !s.beacon;
    $('mute').disabled = !online;
    if (s.beacon) {
      $('alertTitle').textContent = `${Name} is lost`;
      $('alertText').textContent = 'The collar is showing its name and your phone number to phones nearby'
        + (online ? '.' : '. Get within Bluetooth range to mute it.');
    } else if (s.beaconSilenced) {
      $('alertTitle').textContent = 'Lost alert muted';
      $('alertText').textContent = `The collar stays quiet until ${name} is back inside the fence.`;
    } else {
      $('alertTitle').textContent = `${Name} is lost`;
      $('alertText').textContent = 'The collar is outside its fence and has not come back.';
    }
  }

  // What the collar last said.
  const facts = [];
  if (!s) {
    facts.push(...fact('Status', 'Nothing heard from this collar yet'));
  } else {
    facts.push(...fact('Status', STATE_TEXT[s.state] || 'Unknown'));
    if (s.state !== State.NO_FENCE && hasPosition(s)) {
      const d = formatDistance(Math.abs(s.clearanceM), imperial);
      facts.push(...fact('Fence', s.clearanceM >= 0 ? `${d} inside the line` : `${d} outside the line`));
    }
    facts.push(...fact('Battery', s.batteryPct === null ? 'Unknown' : `${s.batteryPct}%`));
    let gps;
    if (!hasPosition(s) || s.fixAgeS === null) gps = 'No GPS fix yet';
    else if (s.noFix) gps = `No fix right now. Last one ${formatAge(s.fixAgeS + (Date.now() - s.at) / 1000)}`;
    else gps = `Fix ${formatAge(s.fixAgeS + (Date.now() - s.at) / 1000)}, ${s.sats} satellites`;
    facts.push(...fact('GPS', gps));
    if (!s.corrections) facts.push(...fact('Corrections', 'Paused'));
    if (!live) facts.push(...fact('Heard', formatAge((Date.now() - s.at) / 1000)));
  }
  $('facts').replaceChildren(...facts);

  for (const id of ['beep', 'vibrate', 'corrections']) $(id).disabled = !online;
  $('corrections').textContent = s && !s.corrections ? 'Resume corrections' : 'Pause corrections';

  // Where it is, and how far from here.
  const where = $('where');
  const pos = hasPosition(s) ? { lat: s.lat, lon: s.lon } : null;
  where.hidden = !pos;
  if (pos) {
    const small = el('small');
    if (me) {
      where.textContent = `${formatDistance(distanceM(me, pos), imperial)} ${compass(bearingDeg(me, pos))} of you`;
      small.textContent = live ? 'Live position' : `Position from ${formatAge((Date.now() - s.at) / 1000)}`;
    } else {
      where.textContent = live ? 'Live position' : `Position from ${formatAge((Date.now() - s.at) / 1000)}`;
      small.textContent = "Allow location to see how far away it is";
    }
    where.append(small);
  }
  const a = $('openMaps');
  if (pos) {
    const ll = `${pos.lat.toFixed(6)},${pos.lon.toFixed(6)}`;
    a.href = platform === 'android' ? `geo:${ll}?q=${ll}(${encodeURIComponent(titleOf(c))})`
      : platform === 'ios' ? `https://maps.apple.com/?ll=${ll}&q=${encodeURIComponent(titleOf(c))}`
        : `https://www.openstreetmap.org/?mlat=${pos.lat.toFixed(6)}&mlon=${pos.lon.toFixed(6)}#map=18/${pos.lat.toFixed(6)}/${pos.lon.toFixed(6)}`;
    a.removeAttribute('aria-disabled');
  } else {
    a.removeAttribute('href');
    a.setAttribute('aria-disabled', 'true');
  }
  drawMap(pos, s);
}

// ---- The map ----

const icon = (cls, size) => L.divIcon({ className: '', html: `<div class="mark ${cls}" style="width:${size}px;height:${size}px"></div>`, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });

function ensureMap() {
  if (map) return;
  map = L.map('map', { zoomControl: false }).setView([20, 0], 2);
  const tiles = { referrerPolicy: 'strict-origin-when-cross-origin', maxZoom: 21, maxNativeZoom: 19 };
  const layers = {
    Street: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      { ...tiles, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }),
    Satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      { ...tiles, attribution: 'Imagery &copy; Esri' }),
  };
  layers.Street.addTo(map);
  L.control.layers(layers, null, { position: 'topright' }).addTo(map);
  map.on('dragstart', () => { follow = false; });
  line = L.polyline([], { color: '#2563eb', weight: 3, dashArray: '6 8', interactive: false }).addTo(map);
  // The alert and the notes below come and go, and the map's box changes with them.
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      map.invalidateSize();
      if (follow) { placed = false; render(); }
    }).observe($('map'));
  }
}

function drawMap(pos, s) {
  if (!map) return;
  if (pos) {
    const cls = `collar ${STATE_CLASS[s.state] || ''} ${live ? '' : 'stale'}`;
    if (!collarMark) collarMark = L.marker([pos.lat, pos.lon], { icon: icon(cls, 26), zIndexOffset: 1000 }).addTo(map);
    collarMark.setLatLng([pos.lat, pos.lon]);
    collarMark.setIcon(icon(cls, 26));
  } else if (collarMark) {
    collarMark.remove();
    collarMark = null;
  }
  if (me) {
    if (!meMark) meMark = L.marker([me.lat, me.lon], { icon: icon('me', 18), interactive: false }).addTo(map);
    meMark.setLatLng([me.lat, me.lon]);
  } else if (meMark) {
    meMark.remove();
    meMark = null;
  }
  line.setLatLngs(pos && me ? [[me.lat, me.lon], [pos.lat, pos.lon]] : []);
  if (!follow) return;
  // Move the view only when there is something new to show: the first positions, or when the
  // collar or the phone has wandered off the edge.
  const points = [pos && [pos.lat, pos.lon], me && [me.lat, me.lon]].filter(Boolean);
  if (!points.length) return;
  const count = points.length;
  const view = map.getBounds().pad(-0.08);
  if (placed === count && points.every(p => view.contains(p))) return;
  placed = count;
  if (count === 2) map.fitBounds(points, { padding: [56, 56], maxZoom: 18 });
  else map.setView(points[0], 17);
}

// ---- Buttons ----

async function send(action, intensity, durationMs, done) {
  if (!link) return;
  try {
    await link.command(action, intensity, durationMs);
    toast(done);
  } catch (e) {
    log('app', `command ${action} failed:`, describe(e));
    toast(e && e.message ? e.message : 'That did not work.');
  }
}

// ---- The debug log ----

function logHeader() {
  const out = ['GeoCollar debug log',
    `App: ${BUILD_TEXT}${BUILD.date ? `, built ${BUILD.date}` : ''}`,
    `Platform: ${platform}, ${navigator.userAgent}`,
    `Saved: ${new Date().toString()}`];
  for (const c of collars) {
    out.push(`Collar: node ${c.node}, Bluetooth name "${c.bleName || ''}_${c.node.slice(-4)}", dog "${c.dog || ''}"`
      + `${c.deviceId ? `, address ${c.deviceId}` : ''}${c.demo ? ', demo' : c.sim ? ', simulated' : ''}`
      + `${c.last ? `, last heard ${new Date(c.last.at).toISOString()}` : ', never heard'}`);
  }
  if (current && link) out.push(`On screen: ${current.node}, link ${link.state}${link.detail ? `: ${link.detail}` : ''}`);
  return out.join('\n');
}

const logFileText = () => `${logHeader()}\n${'-'.repeat(60)}\n${logLines().join('\n')}\n`;

function openLog() {
  if (screen !== 'logScreen') logReturn = screen;
  show('logScreen');
  const pre = $('logText');
  pre.textContent = logLines().join('\n');
  pre.scrollTop = pre.scrollHeight;
}

function closeLog() {
  show(logReturn);
  if (logReturn === 'home') renderHome();
  else render();
}

onLog(line => {
  if (screen !== 'logScreen') return;
  const pre = $('logText');
  if (line === null) { pre.textContent = ''; return; }
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.append((pre.textContent ? '\n' : '') + line);
  if (atEnd) pre.scrollTop = pre.scrollHeight;
});

async function shareLog() {
  flushLog();
  const text = logFileText();
  const d = new Date();
  const p2 = n => String(n).padStart(2, '0');
  const name = `geocollar-log-${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}.txt`;
  if (!native) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    return;
  }
  try {
    const { uri } = await Filesystem.writeFile({ path: name, data: text, directory: Directory.Cache, encoding: Encoding.UTF8 });
    await Share.share({ title: 'GeoCollar debug log', dialogTitle: 'Save or send the debug log', files: [uri] });
  } catch (e) {
    if (/cancel/i.test(describe(e))) return;
    log('app', 'sharing the log failed:', describe(e));
    toast('Could not share the file. Use Copy instead.');
  }
}

async function copyLog() {
  const text = logFileText();
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const t = document.createElement('textarea');
    t.value = text;
    t.setAttribute('readonly', '');
    t.style.position = 'fixed';
    t.style.opacity = '0';
    document.body.append(t);
    t.select();
    const ok = document.execCommand && document.execCommand('copy');
    t.remove();
    if (!ok) { toast('Could not copy. Use Save or share instead.'); return; }
  }
  toast('The log is copied. Paste it into a message.');
}

async function startLog() {
  let saved = [];
  try {
    const { value } = await Preferences.get({ key: LOG_KEY });
    if (value) saved = JSON.parse(value);
  } catch { /* none yet */ }
  setLogSaver(all => Preferences.set({ key: LOG_KEY, value: JSON.stringify(all.slice(-1500)) }), saved);
  log('app', `start: ${BUILD_TEXT}${BUILD.date ? `, built ${BUILD.date}` : ''}, ${platform}, ${navigator.userAgent}`);
  window.addEventListener('error', e => log('app', 'error:', describe(e.error || e.message)));
  window.addEventListener('unhandledrejection', e => log('app', 'unhandled:', describe(e.reason)));
}

$('add').addEventListener('click', openAdd);
$('demo').addEventListener('click', addDemo);
$('useCode').addEventListener('click', () => addFromCode($('code').value).catch(showAddError));
for (const b of document.querySelectorAll('[data-back]')) b.addEventListener('click', back);
$('mute').addEventListener('click', () => send(Action.SILENCE_LOST, 0, 0, 'Lost alert muted.'));
$('beep').addEventListener('click', () => send(Action.BEEP, 0, 1000, 'The collar beeped.'));
$('vibrate').addEventListener('click', () => send(Action.VIBRATE, 40, 1000, 'The collar vibrated.'));
$('corrections').addEventListener('click', () => {
  if (status && !status.corrections) return send(Action.CORRECTIONS_ON, 0, 0, 'Corrections are back on.');
  if (!confirm(`Pause corrections? ${current.dog || 'The dog'} can then cross the fence line without a beep or vibration, until you resume them.`)) return undefined;
  return send(Action.CORRECTIONS_OFF, 0, 0, 'Corrections are paused.');
});
$('openLog').addEventListener('click', openLog);
$('collarLog').addEventListener('click', openLog);
$('logShare').addEventListener('click', shareLog);
$('logCopy').addEventListener('click', copyLog);
$('logClear').addEventListener('click', () => {
  if (confirm('Clear the debug log?')) { clearLog(); log('app', `log cleared; ${BUILD_TEXT}, ${platform}`); }
});
$('recenter').addEventListener('click', () => { follow = true; placed = false; render(); });
$('remove').addEventListener('click', async () => {
  const c = current;
  if (!c) return;
  const real = !c.demo && !c.sim;
  if (!confirm(`Remove ${titleOf(c)} from this phone?` + (real ? ' To stop the collar accepting this phone at all, also remove the phone under "Connect app" on the base station.' : ''))) return;
  await closeCollar();
  collars = collars.filter(x => x !== c);
  await persist();
  goHome();
});

async function back() {
  if (screen === 'logScreen') { closeLog(); return; }
  if (screen === 'addScreen') closeAdd();
  if (screen === 'collarScreen') await closeCollar();
  goHome();
}

/// A geocollar:// link, from the phone's own camera app or a tapped link.
function openUrl(url) {
  if (!/^geocollar:/i.test(url || '')) return;
  addFromCode(url).catch(e => { goHome(); toast(e.message); });
}

async function main() {
  $('build').textContent = BUILD_TEXT;
  await startLog();
  collars = await loadCollars();
  homes = await loadHomes();
  goHome();
  if (native) {
    App.addListener('appStateChange', st => {
      log('app', st.isActive ? 'came to the front' : 'went to the background');
      if (!st.isActive) flushLog();
    });
    App.addListener('appUrlOpen', e => openUrl(e.url));
    App.addListener('backButton', () => { if (screen === 'home') App.exitApp(); else back(); });
    const launch = await App.getLaunchUrl().catch(() => null);
    if (launch && launch.url) openUrl(launch.url);
  }
}

main();
