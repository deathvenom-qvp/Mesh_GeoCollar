# SPDX-License-Identifier: GPL-3.0-or-later
"""Collars, fences and recent positions, kept in one JSON file in the data directory."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

TRACK_LIMIT = 500
BACKUP_EVERY_S = 3600


class Store:
    def __init__(self, data_dir: str | os.PathLike):
        self.path = Path(data_dir) / "geocollar.json"
        self.backup = self.path.with_suffix(".json.bak")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._backup_time = 0.0
        self._data = self._load()

    @staticmethod
    def _read(path: Path) -> dict:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("collars"), dict):
            raise ValueError("not a GeoCollar data file")
        return data

    def _load(self) -> dict:
        """The data file; if it is damaged (a power cut while it was written), the hourly backup;
        if that is unusable too, a fresh start. A damaged file is kept beside the new one."""
        if not self.path.exists() and not self.backup.exists():
            return {"collars": {}}
        try:
            return self._read(self.path)
        except (OSError, ValueError) as e:
            log.error("the data file %s cannot be read (%s)", self.path, e)
        if self.path.exists():
            aside = self.path.with_suffix(f".json.damaged-{int(time.time())}")
            try:
                self.path.replace(aside)
                log.error("kept it as %s", aside)
            except OSError:
                pass
        try:
            data = self._read(self.backup)
            log.error("using the backup %s instead: changes from the last hour may be missing", self.backup)
            return data
        except (OSError, ValueError) as e:
            log.error("no usable backup either (%s): starting with no collars", e)
            return {"collars": {}}

    def _save(self, sync: bool = True) -> None:
        """Write the data file by way of a temporary file, so a crash leaves the old one or the
        new one, never half of each. sync=False skips forcing it to disk, for the frequent,
        replaceable position reports."""
        now = time.time()
        if now - self._backup_time >= BACKUP_EVERY_S and self.path.exists():
            # The file on disk was written whole by the last save: keep a copy of it now and then.
            self._backup_time = now
            try:
                tmp = self.backup.with_suffix(".bak.tmp")
                shutil.copyfile(self.path, tmp)
                tmp.replace(self.backup)
            except OSError as e:
                log.warning("could not back up %s: %s", self.path, e)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(self._data, indent=2))
            if sync:
                f.flush()
                os.fsync(f.fileno())
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
            # A collar that restarted and has had no fix since reports no position at all: keep
            # the last one this base knew, which the page shows as the last known position.
            last = c.get("status") or {}
            if status.get("no_fix") and not (status.get("lat") or status.get("lon")) \
                    and (last.get("lat") or last.get("lon")):
                status["lat"], status["lon"] = last["lat"], last["lon"]
            c["status"] = status
            c.pop("radio_changed", None)  # heard on the base's current settings
            c.pop("hello", None)  # it reports to this base now
            c.pop("send_error", None)  # and the base's messages reach it
            # The trail is where the dog was at that moment: a report without a current fix
            # repeats an old position and adds nothing to it.
            if (status.get("lat") or status.get("lon")) and not status.get("no_fix"):
                track = c.setdefault("track", [])
                track.append([status["received"], status["lat"], status["lon"]])
                del track[:-TRACK_LIMIT]
            self._save(sync=False)

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
