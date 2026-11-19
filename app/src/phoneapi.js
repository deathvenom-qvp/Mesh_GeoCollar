// SPDX-License-Identifier: GPL-3.0-or-later
// The phone's half of the phone API: how the app reaches the base station over the network, at
// home or over the internet, to see the dogs when it is not near them, and to pass on a collar's
// report it got over Bluetooth when the collar can't reach the base itself.
//
// The base's half is base/geocollar_base/phoneapi.py, which describes the scheme, and the
// OpenAPI document is docs/phone-api.openapi.json. Both sides are tested against the same bytes.
//
// In short: the QR code under "Connect app" is one opaque blob (readCode). The app makes a fresh
// P-256 key pair and enrols with the code's one-time token (enroll); both sides work out this
// phone's API key from ECDH, salted with the code's secret. Every request after that is
// AES-256-GCM under that key (BaseApi).

import { concat, fromBase64Url, randomBytes, toBase64Url } from './bytes.js';

const subtle = () => globalThis.crypto.subtle;
const enc = new TextEncoder();
const dec = new TextDecoder();

export const QR_VERSION = 3;
const SECRET_LEN = 16;
const TOKEN_LEN = 8;
const IV_LEN = 12;
export const PREFIX = '/api/phone/v1';

export const STATE_NAMES = ['NO_FENCE', 'INSIDE', 'WARNING', 'OUTSIDE', 'LOST'];

export class ApiError extends Error {
  constructor(message, code, extra = {}) { super(message); this.code = code; Object.assign(this, extra); }
}

export async function hkdf(ikm, salt, info, length) {
  const k = await subtle().importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await subtle().deriveBits({ name: 'HKDF', hash: 'SHA-256', salt, info }, k, length * 8));
}

async function aesKey(raw) {
  return subtle().importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

async function hmacSha256(key, message) {
  const k = await subtle().importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await subtle().sign('HMAC', k, message));
}

/// Whether the text looks like a code of this kind rather than an older geocollar://pair link.
export function isApiCode(text) {
  return /^A[A-Za-z0-9_-]{60,}$/.test(String(text || '').trim());
}

/// What the QR code holds: { secret, token, home, urls }. Throws if it is not one.
export async function readCode(text) {
  let raw;
  try { raw = fromBase64Url(String(text || '').trim()); } catch { raw = new Uint8Array(0); }
  if (raw.length < 1 + SECRET_LEN + 16 || raw[0] !== QR_VERSION) throw new Error('This is not a GeoCollar code.');
  const secret = raw.slice(1, 1 + SECRET_LEN);
  const k = await hkdf(secret, new Uint8Array(0), enc.encode('GeoCollar QR v3'), 16 + IV_LEN);
  let plain;
  try {
    plain = await subtle().decrypt({ name: 'AES-GCM', iv: k.slice(16), additionalData: Uint8Array.of(QR_VERSION) },
      await aesKey(k.slice(0, 16)), raw.slice(1 + SECRET_LEN));
  } catch {
    throw new Error('The code is damaged. Show a new one on the base station.');
  }
  const p = JSON.parse(dec.decode(plain));
  const token = fromBase64Url(p.t);
  if (token.length !== TOKEN_LEN || !/^[0-9a-f]{8}$/.test(p.h) || !Array.isArray(p.u)) throw new Error('The code is damaged.');
  return { secret, token, home: p.h, urls: p.u.filter(u => /^https?:\/\//.test(u)) };
}

export async function seal(key, aad, obj, iv = randomBytes(IV_LEN)) {
  const ct = await subtle().encrypt({ name: 'AES-GCM', iv, additionalData: aad }, await aesKey(key), enc.encode(JSON.stringify(obj)));
  return { iv: toBase64Url(iv), ct: toBase64Url(new Uint8Array(ct)) };
}

export async function unseal(key, aad, env) {
  let plain;
  try {
    plain = await subtle().decrypt({ name: 'AES-GCM', iv: fromBase64Url(env.iv), additionalData: aad }, await aesKey(key), fromBase64Url(env.ct));
  } catch {
    throw new ApiError('The answer does not come from this phone\'s base.', 'not_base');
  }
  return JSON.parse(dec.decode(plain));
}

export const welcomeAad = token => concat(enc.encode('GCW1'), token);
export const requestAad = (op, phone) => enc.encode(`GCP1 ${op} ${phone}`);
export const replyAad = (op, phone, iv) => concat(enc.encode(`GCR1 ${op} ${phone} `), iv);

export const enrollProof = (secret, token, phonePub) => hmacSha256(secret, concat(enc.encode('GCE1'), token, phonePub));

/// The API key, from this phone's private key and the base's public key.
export async function apiKey(privateKey, phonePub, basePub, secret, token) {
  const peer = await subtle().importKey('raw', basePub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = new Uint8Array(await subtle().deriveBits({ name: 'ECDH', public: peer }, privateKey, 256));
  return hkdf(shared, secret, concat(enc.encode('GeoCollar phone API v1'), token, phonePub, basePub), 32);
}

export async function newKeyPair() {
  const pair = await subtle().generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  return { privateKey: pair.privateKey, publicKey: new Uint8Array(await subtle().exportKey('raw', pair.publicKey)) };
}

const TIMEOUT_MS = 10000;

/// HTTP for the API: (url, body) => { status, data }. Throws on a network failure. The phone app
/// passes in Capacitor's native HTTP (no browser CORS or mixed-content rules); tests pass fetch.
export function fetchHttp(fetchFn = globalThis.fetch) {
  return async (url, body) => {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    try {
      const r = await fetchFn(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: ctl.signal });
      let data = null;
      try { data = await r.json(); } catch { /* not JSON: not the base */ }
      return { status: r.status, data };
    } finally {
      clearTimeout(t);
    }
  };
}

function errorFrom(res) {
  const d = res.data && typeof res.data === 'object' ? res.data : {};
  return new ApiError(d.detail || `The base answered ${res.status}.`, d.code || `http_${res.status}`, { status: res.status, serverTime: d.server_time });
}

/// Link this phone with a code. Tries each of the code's addresses. Returns { url, api: { phone,
/// key, urls }, welcome }: welcome is the base's Welcome (docs/phone-api.openapi.json).
export async function enroll(code, http, keyPair = null) {
  const pair = keyPair || await newKeyPair();
  const body = {
    token: toBase64Url(code.token), phone_key: toBase64Url(pair.publicKey),
    proof: toBase64Url(await enrollProof(code.secret, code.token, pair.publicKey)),
  };
  if (!code.urls.length) throw new ApiError('The code has no address for the base. Set the Base URL on the base, then show a new code.', 'no_url');
  let last = null;
  for (const url of code.urls) {
    let res;
    try { res = await http(url + PREFIX + '/enroll', body); } catch (e) { last = e; continue; }
    if (res.status !== 200) {
      const err = errorFrom(res);
      if (['expired', 'used', 'bad_request'].includes(err.code)) throw err; // the base answered: no use trying elsewhere
      last = err;
      continue;
    }
    const basePub = fromBase64Url(res.data.base_key);
    const key = await apiKey(pair.privateKey, pair.publicKey, basePub, code.secret, code.token);
    let welcome;
    try { welcome = await unseal(key, welcomeAad(code.token), res.data); } catch (e) { last = e; continue; }
    return { url, api: { phone: welcome.phone_id, key, urls: welcome.urls && welcome.urls.length ? welcome.urls : code.urls, good: url }, welcome };
  }
  throw new ApiError(`Could not reach the base station (${code.urls.join(', ')}). Is the phone on the same network as the base, `
    + 'or is the Base URL reachable from here?', 'offline', { cause: last });
}

/// Requests to one base. api: { phone, key, urls, good }: good is the address that last worked,
/// tried first. onChange(api) is called when the addresses change, to save them.
export class BaseApi {
  constructor(api, http, opts = {}) {
    this.api = api;
    this.http = http;
    this.now = opts.now || Date.now;
    this.onChange = opts.onChange || (() => {});
    this.offset = 0; // the base's clock minus ours, once it has told us
  }

  _order() {
    const { urls = [], good } = this.api;
    return good && urls.includes(good) ? [good, ...urls.filter(u => u !== good)] : [...urls];
  }

  async call(op, body = {}) {
    let last = null;
    for (const url of this._order()) {
      for (let attempt = 0; attempt < 2; attempt++) {
        const iv = randomBytes(IV_LEN);
        const env = await seal(this.api.key, requestAad(op, this.api.phone), { ts: Math.round(this.now() + this.offset), body }, iv);
        let res;
        try { res = await this.http(url + PREFIX + '/' + op, { phone: this.api.phone, ...env }); } catch (e) { last = e; break; }
        if (res.status === 200 && res.data && res.data.ct) {
          let reply;
          try { reply = await unseal(this.api.key, replyAad(op, this.api.phone, iv), res.data); } catch (e) { last = e; break; }
          if (this.api.good !== url) { this.api.good = url; this.onChange(this.api); }
          if (Array.isArray(reply.urls) && reply.urls.length && JSON.stringify(reply.urls) !== JSON.stringify(this.api.urls)) {
            this.api.urls = reply.urls.filter(u => /^https?:\/\//.test(u));
            if (!this.api.urls.includes(url)) this.api.urls.push(url); // it worked: keep it as a way in
            this.onChange(this.api);
          }
          return reply;
        }
        const err = errorFrom(res);
        if (err.code === 'clock' && err.serverTime && attempt === 0) { this.offset = err.serverTime - this.now(); continue; }
        if (err.code === 'unknown_phone') throw new ApiError('The base no longer knows this phone. It was removed there, or linked again with another code.', 'removed');
        if (res.status >= 400 && res.status < 500 && err.code !== `http_${res.status}`) throw err; // the base answered
        last = err; // not the base (another server, a proxy's error page): try the next address
        break;
      }
    }
    throw new ApiError('Could not reach the base station.', 'offline', { cause: last });
  }
}

/// A report from the base (CollarState) in the shape the app keeps a collar's report in. `at` is
/// on this phone's clock.
export function fromBase(s, serverTime, now = Date.now()) {
  if (!s) return null;
  const age = Math.max(0, serverTime / 1000 - s.received);
  return {
    state: Math.max(0, STATE_NAMES.indexOf(s.state)),
    lat: s.lat, lon: s.lon,
    batteryPct: s.battery_pct == null || s.battery_pct === 255 ? null : s.battery_pct,
    sats: s.sats || 0, pdop: s.pdop || 0, clearanceM: s.clearance_m || 0, fenceVersion: s.fence_version || 0,
    corrections: s.corrections_enabled !== false, shock: !!s.shock_enabled,
    beacon: !!s.beacon_active, beaconSilenced: !!s.beacon_silenced, noFix: !!s.no_fix, home: !!s.home,
    fixAgeS: s.fix_age_s != null ? s.fix_age_s : s.no_fix ? null : 0,
    at: now - age * 1000,
    src: 'base', via: s.via || 'mesh', viaPhone: s.via_phone || '',
  };
}

/// A collar's report heard over Bluetooth, as the relay operation takes it.
export function relayBody(node, st, me, now = Date.now()) {
  return {
    node_id: '!' + node,
    status: {
      state: STATE_NAMES[st.state] || 'NO_FENCE', lat: st.lat, lon: st.lon,
      battery_pct: st.batteryPct, sats: st.sats, pdop: Math.min(655.35, st.pdop || 0), clearance_m: st.clearanceM,
      fence_version: st.fenceVersion, corrections_enabled: !!st.corrections, shock_enabled: !!st.shock,
      beacon_active: !!st.beacon, beacon_silenced: !!st.beaconSilenced, no_fix: !!st.noFix, home: !!st.home,
    },
    fix_age_s: st.fixAgeS == null ? null : Math.min(65535, st.fixAgeS),
    age_ms: Math.max(0, Math.round(now - (st.at || now))),
    phone_position: me ? { lat: me.lat, lon: me.lon, accuracy_m: me.accuracy == null ? null : Math.max(0, me.accuracy) } : null,
  };
}
