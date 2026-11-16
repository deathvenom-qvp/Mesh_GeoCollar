// SPDX-License-Identifier: GPL-3.0-or-later
// Distances and directions between the phone and the collar.

const R = 6371000;
const rad = d => d * Math.PI / 180;
const deg = r => r * 180 / Math.PI;

export function distanceM(a, b) {
  const dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

/// Compass bearing from a to b, degrees clockwise from north.
export function bearingDeg(a, b) {
  const y = Math.sin(rad(b.lon - a.lon)) * Math.cos(rad(b.lat));
  const x = Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) - Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lon - a.lon));
  return (deg(Math.atan2(y, x)) + 360) % 360;
}

const POINTS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
export const compass = bearing => POINTS[Math.round(bearing / 45) % 8];

/// The point `east` and `north` metres from (lat, lon).
export function offset(lat, lon, east, north) {
  return { lat: lat + deg(north / R), lon: lon + deg(east / (R * Math.cos(rad(lat)))) };
}

/// "140 m" or "2.3 km"; feet and miles where people use them.
export function formatDistance(m, imperial = false) {
  if (imperial) {
    const ft = m * 3.28084;
    if (ft < 1000) return `${Math.round(ft / 10) * 10 || Math.round(ft)} ft`;
    const mi = m / 1609.344;
    return `${mi < 10 ? mi.toFixed(1) : Math.round(mi)} mi`;
  }
  if (m < 1000) return `${m < 100 ? Math.round(m) : Math.round(m / 10) * 10} m`;
  const km = m / 1000;
  return `${km < 10 ? km.toFixed(1) : Math.round(km)} km`;
}

/// "just now", "40 s ago", "3 min ago", "2 h ago", "5 days ago".
export function formatAge(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 5) return 'just now';
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
