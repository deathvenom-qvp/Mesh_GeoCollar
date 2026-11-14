# SPDX-License-Identifier: GPL-3.0-or-later
"""Address search and the home location, for the map.

Addresses are looked up with OpenStreetMap's Nominatim (https://nominatim.org), through the base so
the page works behind Home Assistant ingress. Its usage policy allows at most one request a second
and no search-as-you-type, so the page searches only when asked, and results are cached.
The home location comes from Home Assistant's own configuration (Settings > System > General).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "GeoCollar-base/0.11 (Meshtastic dog collar base station)"
CACHE_S = 24 * 3600


class GeoError(Exception):
    pass


class Geocoder:
    def __init__(self, url: str = NOMINATIM, fetch=None):
        self.url = url
        self._fetch = fetch or self._http
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache: dict[tuple, tuple[float, list]] = {}

    @staticmethod
    def _http(url: str) -> list:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - fixed https host
            return json.load(r)

    def search(self, q: str, viewbox: tuple[float, float, float, float] | None = None, limit: int = 5) -> list[dict]:
        """Places matching q, best first: [{name, lat, lon, bbox: [south, west, north, east]}].
        viewbox (west, south, east, north) prefers results in the area on screen."""
        q = " ".join((q or "").split())
        if len(q) < 3:
            raise GeoError("type at least 3 characters")
        if len(q) > 200:
            raise GeoError("that is too long to search for")
        vb = tuple(round(v, 3) for v in viewbox) if viewbox else None
        key = (q.lower(), vb, limit)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < CACHE_S:
                return hit[1]
            wait = 1.0 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            params = {"q": q, "format": "jsonv2", "limit": str(limit)}
            if vb:
                params["viewbox"] = ",".join(str(v) for v in vb)
            try:
                raw = self._fetch(f"{self.url}?{urllib.parse.urlencode(params)}")
            except OSError as e:
                raise GeoError(f"address search is not reachable: {e}") from e
            except ValueError as e:
                raise GeoError("address search sent back something unreadable") from e
            out = []
            for r in raw or []:
                try:
                    bb = [float(v) for v in r.get("boundingbox", [])]
                    out.append({"name": r.get("display_name", ""), "lat": float(r["lat"]), "lon": float(r["lon"]),
                                "bbox": bb if len(bb) == 4 else None, "kind": r.get("addresstype") or r.get("type") or ""})
                except (KeyError, TypeError, ValueError):
                    continue
            self._cache[key] = (time.time(), out)
            if len(self._cache) > 500:
                self._cache.pop(next(iter(self._cache)))
            return out


def ha_home(fetch=None) -> dict | None:
    """Home Assistant's home location, from the Supervisor's proxy to the Core API (needs
    homeassistant_api in config.yaml). None outside Home Assistant or if it is not set."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token and fetch is None:
        return None

    def http(url):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - fixed internal URL
            return json.load(r)

    try:
        cfg = (fetch or http)("http://supervisor/core/api/config")
        lat, lon = float(cfg["latitude"]), float(cfg["longitude"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return {"lat": lat, "lon": lon, "name": cfg.get("location_name") or "Home", "source": "home_assistant"}


def parse_home(text: str) -> dict | None:
    """A home location typed in the app options as "lat, lon"."""
    try:
        lat, lon = (float(v) for v in (text or "").replace(";", ",").split(","))
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return {"lat": lat, "lon": lon, "name": "Home", "source": "options"}
    return None
