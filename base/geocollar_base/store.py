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

from . import protocol

log = logging.getLogger(__name__)

TRACK_LIMIT = 500
BACKUP_EVERY_S = 3600


def fresh_position(status: dict) -> bool:
    """Whether a report says where the dog is now: it has a position, from a current fix with
    enough satellites, and the collar is not home with its GPS off (that repeats the last fix)."""
    return bool((status.get("lat") or status.get("lon")) and not status.get("no_fix") and not status.get("home"))


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

    def record_status(self, node_id: str, status: dict, direct: bool = True) -> dict | None:
        """Record a report from a collar on this base and return it as stored (None for an unknown
        node: a collar is only ever added on purpose, so its reports are dropped). direct=False
        for one a phone passed on: it says nothing about whether the base and collar hear each other."""
        with self._lock:
            c = self._data["collars"].get(node_id)
            if c is None:
                return None
            status = dict(status, received=time.time())
            last = c.get("status") or {}
            # A position from fewer satellites than the collar's fixes need (min_sats) is no
            # position: collar firmware before 0.19.11 still sends one. The report counts as one
            # without a fix.
            sats = status.get("sats")
            if (status.get("lat") or status.get("lon")) and sats is not None and sats < self.min_sats(c) \
                    and not status.get("no_fix"):
                status["no_fix"] = True
                status["few_sats"] = True
                status["lat"], status["lon"] = last.get("lat") or 0, last.get("lon") or 0
            elif status.get("no_fix") and sats and sats < self.min_sats(c):
                status["few_sats"] = True  # 0.19.11 on: no fix because the GPS's had too few satellites
            # A collar that restarted and has had no fix since reports no position at all: keep
            # the last one this base knew, which the page shows as the last known position.
            if status.get("no_fix") and not (status.get("lat") or status.get("lon")) \
                    and (last.get("lat") or last.get("lon")):
                status["lat"], status["lon"] = last["lat"], last["lon"]
            c["status"] = status
            if direct:
                c["heard_direct"] = status["received"]
                c.pop("radio_changed", None)  # heard on the base's current settings
                c.pop("hello", None)  # it reports to this base now
                c.pop("send_error", None)  # and the base's messages reach it
            # The trail is where the dog was at that moment: a report without a current fix, or
            # from a collar at home with its GPS off, repeats an old position and adds nothing.
            if fresh_position(status):
                track = c.setdefault("track", [])
                track.append([status["received"], status["lat"], status["lon"]])
                del track[:-TRACK_LIMIT]
            self._save(sync=False)
            return dict(status)

    @staticmethod
    def min_sats(c: dict) -> int:
        """How many satellites a collar's GPS fix needs: the owner's choice, else MIN_FIX_SATS."""
        v = c.get("min_sats")
        return protocol.MIN_FIX_SATS if v is None else int(v)

    def clear_track(self, node_id: str) -> bool:
        """Forget the path drawn behind a collar on the map. Its log is kept apart (collarlog)."""
        with self._lock:
            c = self._data["collars"].get(node_id)
            if c is None:
                return False
            c["track"] = []
            self._save()
            return True

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

    def refresh_backup(self) -> None:
        """Make the backup a copy of the data as it is now. For when something was removed that
        must not live on in the older copy, such as a password."""
        with self._lock:
            self._save()  # the file on disk is current
            self._backup_time = 0.0
            self._save()  # copies it to the backup

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            s = self._data.setdefault("settings", {})
            if value is None:
                s.pop(key, None)
            else:
                s[key] = value
            self._save()
