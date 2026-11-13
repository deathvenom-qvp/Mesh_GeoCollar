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

    def remove_collar(self, node_id: str) -> bool:
        with self._lock:
            found = self._data["collars"].pop(node_id, None) is not None
            self._save()
            return found

    def set_fences(self, node_id: str, geojson: dict) -> int:
        """Store new fences and return the new fence version number."""
        with self._lock:
            c = self._data["collars"][node_id]
            c["fences"] = geojson
            c["fence_version"] = (c.get("fence_version", 0) + 1) & 0xFFFF or 1
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
        with self._lock:
            c = self._data["collars"].setdefault(
                node_id, {"name": node_id, "fences": None, "fence_version": 0, "status": None, "track": []}
            )
            status = dict(status, received=time.time())
            c["status"] = status
            if status.get("lat") or status.get("lon"):
                c["track"].append([status["received"], status["lat"], status["lon"]])
                del c["track"][:-TRACK_LIMIT]
            self._save()
