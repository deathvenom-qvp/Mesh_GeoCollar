# SPDX-License-Identifier: GPL-3.0-or-later
"""Simulated collars, for trying fences without hardware.

A simulated collar moves on its own (stays put, wanders, or walks to a point), and checks each
position against its fences with a Python copy of the collar firmware's rules
(firmware/GeoCollar/Fence.cpp): the same clearance maths, warning margin, Outside -> Lost after
3 fixes, re-arming after 3 fixes 5 m back inside, and beep / vibrate / shock choices. It reports
to the base like a real collar would, minus the radio.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from . import protocol

EARTH_RADIUS_M = 6371008.8
MODES = ("still", "wander", "goto")


# ---------------------------------------------------------------------------------------------
# Fence rules, mirroring firmware/GeoCollar/Fence.cpp
# ---------------------------------------------------------------------------------------------
def _project(lat: float, lon: float, ref_lat: float, ref_lon: float, cos_ref: float) -> tuple[float, float]:
    return (math.radians(lon - ref_lon) * cos_ref * EARTH_RADIUS_M, math.radians(lat - ref_lat) * EARTH_RADIUS_M)


def _dist_to_segment(p, a, b) -> float:
    abx, aby = b[0] - a[0], b[1] - a[1]
    apx, apy = p[0] - a[0], p[1] - a[1]
    len2 = abx * abx + aby * aby
    t = 0.0 if len2 <= 0 else max(0.0, min(1.0, (apx * abx + apy * aby) / len2))
    dx, dy = apx - t * abx, apy - t * aby
    return math.hypot(dx, dy)


def signed_distance_m(zone: protocol.Zone, lat: float, lon: float) -> float:
    """Positive on the allowed side of the zone, negative on the wrong side."""
    verts = zone.vertices
    if len(verts) < 3:
        return 1e9
    ref_lat, ref_lon = verts[0]
    cos_ref = math.cos(math.radians(ref_lat))
    p = _project(lat, lon, ref_lat, ref_lon, cos_ref)
    inside, best = False, 1e18
    prev = _project(*verts[-1], ref_lat, ref_lon, cos_ref)
    for v in verts:
        cur = _project(*v, ref_lat, ref_lon, cos_ref)
        if (cur[1] > p[1]) != (prev[1] > p[1]):
            x_cross = prev[0] + (p[1] - prev[1]) * (cur[0] - prev[0]) / (cur[1] - prev[1])
            if p[0] < x_cross:
                inside = not inside
        best = min(best, _dist_to_segment(p, prev, cur))
        prev = cur
    allowed = inside if zone.kind == protocol.ZoneKind.KEEP_IN else not inside
    return best if allowed else -best


def clearance_m(zones: list[protocol.Zone], lat: float, lon: float) -> float:
    return min((signed_distance_m(z, lat, lon) for z in zones), default=1e9)


@dataclass
class EngineSettings:
    corrections_enabled: bool = True
    shock_enabled: bool = False
    shock_level: int = 10
    max_shock_level: int = 30
    vibrate_level: int = 50
    max_shocks_per_breach: int = 2
    max_shocks_per_hour: int = 4
    lost_confirm_fixes: int = 3
    rearm_fixes: int = 3
    rearm_margin_m: float = 5


@dataclass
class Decision:
    correction: str | None = None  # "beep", "vibrate", "shock"
    intensity: int = 0
    send_breach: bool = False
    send_status: bool = False
    clearance_m: float = 0


@dataclass
class FenceEngine:
    settings: EngineSettings = field(default_factory=EngineSettings)
    zones: list[protocol.Zone] = field(default_factory=list)
    state: protocol.CollarState = protocol.CollarState.NO_FENCE
    _outside: int = 0
    _rearm: int = 0
    _shocks_this_breach: int = 0
    _shock_times: deque = field(default_factory=lambda: deque(maxlen=16))

    def set_fences(self, zones: list[protocol.Zone]) -> None:
        self.zones = list(zones)
        self._outside = self._rearm = self._shocks_this_breach = 0
        self.state = protocol.CollarState.INSIDE if self.zones else protocol.CollarState.NO_FENCE

    def force_lost(self) -> None:
        self.state = protocol.CollarState.LOST
        self._rearm = 0

    def clear_lost(self) -> None:
        if self.state == protocol.CollarState.LOST:
            self.state = protocol.CollarState.INSIDE if self.zones else protocol.CollarState.NO_FENCE
        self._outside = self._rearm = self._shocks_this_breach = 0

    def warn_margin_m(self) -> int:
        return min((z.warn_margin_m for z in self.zones), default=0)

    def _set_state(self, s: protocol.CollarState, d: Decision) -> None:
        if s != self.state:
            self.state = s
            d.send_status = True

    def _shock_allowed(self, now: float) -> bool:
        s = self.settings
        if not s.shock_enabled or self._shocks_this_breach >= s.max_shocks_per_breach:
            return False
        return sum(1 for t in self._shock_times if now - t < 3600) < s.max_shocks_per_hour

    def on_fix(self, lat: float, lon: float, now: float) -> Decision:
        S = protocol.CollarState
        d = Decision()
        if self.state == S.NO_FENCE:
            return d
        c = clearance_m(self.zones, lat, lon)
        d.clearance_m = c
        s = self.settings

        if self.state == S.LOST:
            if c >= s.rearm_margin_m:
                self._rearm += 1
                if self._rearm >= s.rearm_fixes:
                    self._rearm = self._outside = self._shocks_this_breach = 0
                    self._set_state(S.INSIDE, d)
            else:
                self._rearm = 0
            return d

        margin = self.warn_margin_m()
        if c >= margin:
            self._outside = self._shocks_this_breach = 0
            self._set_state(S.INSIDE, d)
            return d
        if c >= 0:
            self._outside = 0
            self._set_state(S.WARNING, d)
            if s.corrections_enabled:
                if c < margin / 2:
                    d.correction, d.intensity = "vibrate", s.vibrate_level
                else:
                    d.correction = "beep"
            return d

        self._outside += 1
        if self._outside >= s.lost_confirm_fixes:
            self._set_state(S.LOST, d)
            d.send_breach = True
            return d
        self._set_state(S.OUTSIDE, d)
        if not s.corrections_enabled:
            return d
        if self._shock_allowed(now):
            d.correction, d.intensity = "shock", min(s.shock_level, s.max_shock_level)
            self._shocks_this_breach += 1
            self._shock_times.append(now)
        else:
            d.correction, d.intensity = "vibrate", s.vibrate_level
        return d


# ---------------------------------------------------------------------------------------------
# Simulated collar
# ---------------------------------------------------------------------------------------------
def _move(lat: float, lon: float, heading_deg: float, metres: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    dlat = metres * math.cos(h) / EARTH_RADIUS_M
    dlon = metres * math.sin(h) / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def _bearing_and_distance(lat1, lon1, lat2, lon2) -> tuple[float, float]:
    cos_ref = math.cos(math.radians(lat1))
    x, y = _project(lat2, lon2, lat1, lon1, cos_ref)
    return (math.degrees(math.atan2(x, y)) % 360, math.hypot(x, y))


class SimCollar:
    # Settings the page can change, with their limits.
    LIMITS = {"speed_mps": (0.1, 15.0), "report_s": (1, 600), "noise_m": (0.0, 30.0)}

    def __init__(self, node_id: str, settings: dict, rng: random.Random | None = None):
        self.node_id = node_id
        self.rng = rng or random.Random()
        self.lat = float(settings["lat"])
        self.lon = float(settings["lon"])
        self.mode = settings.get("mode", "still")
        self.speed_mps = float(settings.get("speed_mps", 1.5))
        self.report_s = float(settings.get("report_s", 5))
        self.noise_m = float(settings.get("noise_m", 2))
        self.target = settings.get("target")
        self.heading = self.rng.uniform(0, 360)
        self.battery = 100.0
        self.engine = FenceEngine()
        self.engine.settings.shock_enabled = bool(settings.get("shock_enabled", False))
        self.fence_version = 0
        self.events: deque = deque(maxlen=12)
        self.clearance = None
        self._last_report = -1e9
        self._report_now = True
        self._fix = (self.lat, self.lon)

    # -- settings ---------------------------------------------------------------------------
    def settings(self) -> dict:
        return {
            "lat": round(self.lat, 7), "lon": round(self.lon, 7), "mode": self.mode,
            "speed_mps": self.speed_mps, "report_s": self.report_s, "noise_m": self.noise_m,
            "target": self.target, "shock_enabled": self.engine.settings.shock_enabled,
        }

    def update(self, changes: dict) -> None:
        if "lat" in changes or "lon" in changes:
            lat, lon = float(changes.get("lat", self.lat)), float(changes.get("lon", self.lon))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("position out of range")
            self.lat, self.lon = lat, lon
            self._fix = (lat, lon)
            self._report_now = True
            if self.mode == "goto":  # picked up and put down: stop walking to the old point
                self.mode = "still"
        if "target" in changes:
            t = changes["target"]
            if t is not None:
                t = [float(t[0]), float(t[1])]
                if not (-90 <= t[0] <= 90 and -180 <= t[1] <= 180):
                    raise ValueError("target out of range")
            self.target = t
        if "mode" in changes:
            if changes["mode"] not in MODES:
                raise ValueError(f"mode must be one of {', '.join(MODES)}")
            if changes["mode"] == "goto" and not self.target:
                raise ValueError("pick a point to walk to first")
            self.mode = changes["mode"]
        for key, (lo, hi) in self.LIMITS.items():
            if key in changes:
                v = float(changes[key])
                if not lo <= v <= hi:
                    raise ValueError(f"{key} must be {lo}..{hi}")
                setattr(self, key, v)
        if "shock_enabled" in changes:
            self.engine.settings.shock_enabled = bool(changes["shock_enabled"])
            self._report_now = True

    # -- what the base sends ----------------------------------------------------------------
    def set_fences(self, zones: list[protocol.Zone], version: int) -> None:
        # Round to the collar's integer degrees x 1e7, as the radio would.
        self.engine.set_fences([
            protocol.Zone(z.kind, z.warn_margin_m, [(round(a, 7), round(o, 7)) for a, o in z.vertices]) for z in zones
        ])
        self.fence_version = version
        self._event("fences", f"loaded fence version {version}")
        self._report_now = True

    def command(self, action: protocol.CommandAction, intensity: int = 0) -> None:
        A, s = protocol.CommandAction, self.engine.settings
        if action == A.BEEP:
            self._event("beep", "manual beep")
        elif action == A.VIBRATE:
            self._event("vibrate", f"manual vibrate {intensity}")
        elif action == A.SHOCK:
            if s.shock_enabled:
                self._event("shock", f"manual shock {min(intensity, s.max_shock_level)}")
            else:
                self._event("info", "manual shock ignored: shock is disabled")
        elif action == A.CORRECTIONS_OFF:
            s.corrections_enabled = False
        elif action == A.CORRECTIONS_ON:
            s.corrections_enabled = True
        elif action == A.FORCE_LOST:
            self.engine.force_lost()
        elif action == A.CLEAR_LOST:
            self.engine.clear_lost()
        self._report_now = True

    # -- time -------------------------------------------------------------------------------
    def _event(self, kind: str, text: str) -> None:
        self.events.appendleft({"t": time.time(), "kind": kind, "text": text})

    def _walk(self, dt: float) -> None:
        step = self.speed_mps * dt
        if self.mode == "wander":
            # A gently curving random walk: the heading drifts, sometimes turns more sharply.
            self.heading = (self.heading + self.rng.gauss(0, 25 * math.sqrt(dt))) % 360
            if self.rng.random() < 0.02 * dt:
                self.heading = (self.heading + self.rng.uniform(-120, 120)) % 360
            self.lat, self.lon = _move(self.lat, self.lon, self.heading, step)
        elif self.mode == "goto" and self.target:
            bearing, dist = _bearing_and_distance(self.lat, self.lon, *self.target)
            if dist <= step:
                self.lat, self.lon = self.target
                self.mode = "still"
                self._event("info", "arrived at the target")
            else:
                self.heading = bearing
                self.lat, self.lon = _move(self.lat, self.lon, bearing, step)

    def step(self, dt: float, now: float) -> dict | None:
        """Advance dt seconds. Returns a status record when the collar would report."""
        self._walk(dt)
        self.battery = max(0.0, self.battery - dt / 360)  # about 10 hours from full
        # A GPS fix with some noise, the way the real receiver jitters.
        n = self.noise_m
        lat, lon = _move(self.lat, self.lon, self.rng.uniform(0, 360), abs(self.rng.gauss(0, n / 1.5)) if n else 0)
        self._fix = (lat, lon)
        d = self.engine.on_fix(lat, lon, now)
        if self.engine.zones:
            self.clearance = d.clearance_m
        if d.correction:
            self._event(d.correction, f"{d.correction}{f' {d.intensity}' if d.intensity else ''} at {int(round(d.clearance_m))} m from the line")
        if d.send_breach:
            self._event("lost", "confirmed outside: corrections stop, lost mode")
        if d.send_status or d.send_breach or self._report_now or now - self._last_report >= self.report_s:
            return self.report(now, breach=d.send_breach or self.engine.state == protocol.CollarState.LOST)
        return None

    def report(self, now: float, breach: bool = False) -> dict:
        self._last_report = now
        self._report_now = False
        c = self.clearance
        return {
            "msg_type": "BREACH" if breach else "STATUS",
            "fence_version": self.fence_version,
            "state": self.engine.state.name,
            "battery_pct": int(self.battery),
            "lat": round(self._fix[0], 7),
            "lon": round(self._fix[1], 7),
            "pdop": 1.2,
            "sats": 11,
            "clearance_m": 0 if c is None else int(max(-32768, min(32767, round(c)))),
            "corrections_enabled": self.engine.settings.corrections_enabled,
            "shock_enabled": self.engine.settings.shock_enabled,
        }

    def public(self) -> dict:
        return {**self.settings(), "true_lat": self.lat, "true_lon": self.lon, "events": list(self.events)}


class Simulator:
    """All simulated collars, stepped once a second on a background thread."""

    def __init__(self, on_status: Callable[[str, dict], None], on_settings: Callable[[str, dict], None]):
        self.on_status = on_status
        self.on_settings = on_settings
        self.collars: dict[str, SimCollar] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.collars

    def add(self, node_id: str, settings: dict) -> SimCollar:
        with self.lock:
            sim = self.collars[node_id] = SimCollar(node_id, settings)
        return sim

    def remove(self, node_id: str) -> None:
        with self.lock:
            self.collars.pop(node_id, None)

    def update(self, node_id: str, changes: dict) -> dict:
        with self.lock:
            sim = self.collars[node_id]
            sim.update(changes)
            settings = sim.settings()
        self.on_settings(node_id, settings)
        return settings

    def set_fences(self, node_id: str, zones, version: int) -> None:
        with self.lock:
            self.collars[node_id].set_fences(zones, version)

    def command(self, node_id: str, action, intensity: int = 0) -> None:
        with self.lock:
            self.collars[node_id].command(action, intensity)

    def report_now(self, node_id: str) -> None:
        """Send a status straight away, as the collar does after a command or new fences."""
        with self.lock:
            sim = self.collars.get(node_id)
            r = sim.report(time.time()) if sim else None
        if r:
            self.on_status(node_id, r)

    def public(self, node_id: str) -> dict | None:
        with self.lock:
            sim = self.collars.get(node_id)
            return sim.public() if sim else None

    def tick(self, dt: float = 1.0, now: float | None = None) -> None:
        now = time.time() if now is None else now
        reports, moved = [], []
        with self.lock:
            for node_id, sim in self.collars.items():
                was_moving = sim.mode != "still"
                r = sim.step(dt, now)
                if r:
                    reports.append((node_id, r))
                if was_moving and r:
                    moved.append((node_id, sim.settings()))
        for node_id, r in reports:
            if node_id in self.collars:  # not removed meanwhile
                self.on_status(node_id, r)
        for node_id, s in moved:  # remember where it got to, for a restart
            if node_id in self.collars:
                self.on_settings(node_id, s)

    def start(self) -> None:
        def run():
            last = time.monotonic()
            while not self._stop.wait(1.0):
                t = time.monotonic()
                self.tick(min(t - last, 5.0))
                last = t

        self._thread = threading.Thread(target=run, name="simulator", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
