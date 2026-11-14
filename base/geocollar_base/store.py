# SPDX-License-Identifier: GPL-3.0-or-later
"""Collars, fences and recent positions, kept in one JSON file in the data directory."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

TRACK_LIMIT = 500


class Store:
    def __init__(self, data_dir: str | os.PathLike):
        self.path = Path(data_dir) / "geocollar.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = {"collars": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def collars(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data["collars"]))

    def collar(self, node_id: str) -> dict | None:
        return self.collars().get(node_id)

    def upsert_collar(self, node_id: str, name: str | None = None) -> dict:
        with self._lock:
            c = self._data["collars"].setdefault(
                node_id, {"name": node_id, "fences": None, "fence_version": 0, "status": None, "track": []}
            )
            if name:
                c["name"] = name
            self._save()
            return dict(c)

    def move_collar(self, old: str, new: str) -> bool:
        """Keep a collar's name, fences and history when its node ID changes (Meshtastic 2.8 derives
        it from the key a fresh collar makes during setup)."""
        with self._lock:
            c = self._data["collars"].pop(old, None)
            if c is None:
                return False
            if c.get("name") == old:
                c["name"] = new
            self._data["collars"][new] = c
            self._save()
            return True

    def remove_collar(self, node_id: str) -> bool:
        with self._lock:
            found = self._data["collars"].pop(node_id, None) is not None
            self._save()
            return found

    def set_fences(self, node_id: str, geojson: dict) -> int:
        """Store new fences and return the new fence version number."""
        from .protocol import next_fence_version

        with self._lock:
            c = self._data["collars"][node_id]
            c["fences"] = geojson
            reported = (c.get("status") or {}).get("fence_version")
            c["fence_version"] = next_fence_version(c.get("fence_version", 0), reported)
            self._save()
            return c["fence_version"]

    def set_sim(self, node_id: str, settings: dict | None) -> None:
        """Simulator settings for a simulated collar (None for a real one)."""
        with self._lock:
            c = self._data["collars"].get(node_id)
            if c is None:
                return
            if settings is None:
                c.pop("sim", None)
            else:
                c["sim"] = settings
            self._save()

    def record_status(self, node_id: str, status: dict) -> None:
        """Record a report from a collar on this base. Reports from unknown nodes are dropped:
        a collar is only ever added on purpose."""
        with self._lock:
            c = self._data["collars"].get(node_id)
            if c is None:
                return
            status = dict(status, received=time.time())
            c["status"] = status
            c.pop("radio_changed", None)  # heard on the base's current settings
            c.pop("hello", None)  # it reports to this base now
            c.pop("send_error", None)  # and the base's messages reach it
            if status.get("lat") or status.get("lon"):
                c["track"].append([status["received"], status["lat"], status["lon"]])
                del c["track"][:-TRACK_LIMIT]
            self._save()

    def set_collar_info(self, node_id: str, key: str, value) -> None:
        """Set or clear (value None) one extra field on a collar, e.g. firmware or update_mode."""
        with self._lock:
            c = self._data["collars"].get(node_id)
            if c is None:
                return
            if value is None:
                c.pop(key, None)
            else:
                c[key] = value
            self._save()

    def next_command_seq(self) -> int:
        """A number that grows with every command: the clock in seconds, or one more than the last
        if the clock is behind. Stored, so it keeps growing across restarts."""
        with self._lock:
            s = self._data.setdefault("settings", {})
            seq = max(int(s.get("cmd_seq", 0)) + 1, int(time.time()))
            s["cmd_seq"] = seq
            self._save()
            return seq

    def setting(self, key: str, default=None):
        with self._lock:
            return json.loads(json.dumps(self._data.get("settings", {}).get(key, default)))

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            s = self._data.setdefault("settings", {})
            if value is None:
                s.pop(key, None)
            else:
                s[key] = value
            self._save()
